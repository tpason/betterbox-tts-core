#!/usr/bin/env python3
from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import tempfile
import time
from argparse import Namespace
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
import re

import requests

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPT_DIR = Path(__file__).resolve().parent

from story_db.story_pipeline_db import repository as repo
from genre_prompts import detect_genre, find_char_map_file, resolve_genre_from_context
from polish_chapter_texts_ollama import clean_for_audiobook_tts, polish_file
from reader_content_format import format_polished_content as format_reader_polished_content
from translate_chapter_texts_ollama import single_pass_translate_polish_file, translate_file
from check_translation_quality import check_polished_quality


def story_slug_for_job(job: dict, input_path: Path) -> str:
    payload = job.get("payload") or {}
    return str(payload.get("story_slug") or input_path.parent.name)


def build_args(
    args: argparse.Namespace,
    model: str,
    max_chars: int,
    genre: str = "",
    story_id: str = "",
    story_slug: str = "",
) -> Namespace:
    return Namespace(
        ollama_url=args.ollama_url,
        model=model,
        temperature=args.temperature,
        num_ctx=args.num_ctx,
        timeout=args.timeout,
        retries=args.retries,
        keep_alive=args.keep_alive,
        max_chars_per_chunk=max_chars,
        prompt_profile=args.prompt_profile,
        polish_mode=args.polish_mode,
        min_output_ratio=args.min_output_ratio,
        genre=genre,
        story_id=story_id,
        story_slug=story_slug,
        char_map_file=getattr(args, "char_map_file", ""),
        story_memory_dir=getattr(args, "story_memory_dir", ""),
        fail_on_story_memory_issues=getattr(args, "fail_on_story_memory_issues", False),
    )


def resolve_genre(job: dict, char_map_file: str = "") -> str:
    """Detect genre from job payload or story category in DB (language-aware)."""
    payload = job.get("payload") or {}
    # Allow genre to be pinned explicitly in the job payload.
    if payload.get("genre"):
        return str(payload["genre"])
    story_id = job.get("story_id")
    if not story_id:
        return ""
    try:
        story = repo.get_story_by_id(story_id)
        category = str(story.get("category") or "")
        raw_language = str(payload.get("raw_language") or story.get("language") or "")
        source_code = str(job.get("source_code") or "")
        return resolve_genre_from_context(
            category,
            raw_language=raw_language,
            source_code=source_code,
            char_map_file=char_map_file,
        )
    except Exception:
        return ""


def post_translate_mode(job: dict, args: argparse.Namespace) -> str:
    payload = job.get("payload") or {}
    arg_mode = str(getattr(args, "post_translate", "") or "")
    if arg_mode and arg_mode != "polish":
        return arg_mode
    return str(payload.get("post_translate") or arg_mode or "polish")


def log(message: str) -> None:
    print(message, flush=True)


def choose_char_map_text_source(args: argparse.Namespace, raw_language: str) -> str:
    configured = str(getattr(args, "char_map_text_source", "auto") or "auto").strip().lower()
    if configured in {"raw", "translated", "polished"}:
        return configured
    return "raw" if raw_language.lower() in {"vi", "vn", "vietnamese"} else "translated"


def maybe_auto_update_char_map(
    job: dict,
    args: argparse.Namespace,
    *,
    slug: str,
    current_chapter: int,
    existing_char_map: str,
    text_source: str,
    genre: str = "",
) -> str:
    """Create/update char map opportunistically. Never fail the story job."""
    if getattr(args, "no_auto_char_map", False):
        return existing_char_map
    story_id = str(job.get("story_id") or "")
    if not story_id:
        return existing_char_map

    metadata: dict = {}
    try:
        story = repo.get_story_by_id(story_id)
        metadata = story.get("metadata") or {}
        updated_to = int(
            metadata.get("char_map_updated_to_chapter")
            or metadata.get("char_map_scanned_to_chapter")
            or metadata.get("char_map_sampled_to_chapter")
            or 0
        )
    except Exception:
        updated_to = 0

    should_create = not existing_char_map
    interval = int(getattr(args, "char_map_update_interval", 150) or 0)
    should_update = bool(existing_char_map and interval > 0 and current_chapter >= updated_to + interval)
    if not should_create and not should_update:
        return existing_char_map

    # Cooldown after a failed create attempt: skip retry until enough chapters later.
    # Prevents hammering Ollama every chapter when the model is busy with other workers.
    if should_create:
        failed_at = int(metadata.get("char_map_create_failed_at_chapter") or 0)
        cooldown = int(getattr(args, "char_map_create_cooldown", 30) or 30)
        if failed_at > 0 and current_chapter < failed_at + cooldown:
            log(
                f"[CHAR_MAP] skip create (last attempt failed at ch{failed_at:04d}, "
                f"retry at ch{failed_at + cooldown:04d})"
            )
            return existing_char_map

    cmd = [
        sys.executable,
        str(SCRIPT_DIR / "build_char_map_from_story.py"),
        "--story-id",
        story_id,
        "--text-source",
        text_source,
        "--model",
        str(getattr(args, "char_map_model", "") or getattr(args, "vi_model", "qwen3:14b")),
        "--ollama-url",
        args.ollama_url,
        "--timeout",
        str(max(30, int(getattr(args, "char_map_timeout", 180) or 180))),
        "--min-frequency",
        str(max(1, int(getattr(args, "char_map_min_frequency", 1) or 1))),
    ]
    # Inject genre header khi auto-create map mới cho truyện chưa có map
    if should_create and genre:
        cmd.extend(["--genre", genre])
    if should_update:
        from_chapter = max(1, updated_to + 1)
        cmd.extend(["--from-chapter", str(from_chapter), "--to-chapter", str(current_chapter), "--append-only"])

    reason = "create" if should_create else f"update from ch{updated_to + 1:04d} to ch{current_chapter:04d}"
    log(f"[CHAR_MAP] auto {reason} story_id={story_id} slug={slug} text_source={text_source}")
    try:
        result = subprocess.run(
            cmd,
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=max(180, int(getattr(args, "char_map_timeout", 180) or 180) * 4),
        )
        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "").strip()[-1000:]
            log(f"[CHAR_MAP WARN] auto extract failed rc={result.returncode}: {tail}")
            if should_create:
                _record_char_map_create_failure(story_id, current_chapter)
            return existing_char_map
        refreshed = find_char_map_file(story_id=story_id, slug=slug)
        if refreshed:
            log(f"[CHAR_MAP] ready {refreshed}")
            return refreshed
    except Exception as exc:
        log(f"[CHAR_MAP WARN] auto extract failed: {type(exc).__name__}: {exc}")
        if should_create:
            _record_char_map_create_failure(story_id, current_chapter)
    return existing_char_map


def _record_char_map_create_failure(story_id: str, chapter: int) -> None:
    try:
        repo.update_story_metadata(story_id, {"char_map_create_failed_at_chapter": chapter})
        log(f"[CHAR_MAP] recorded create failure at ch{chapter:04d} — cooldown active")
    except Exception as exc:
        log(f"[CHAR_MAP WARN] could not record failure: {exc}")


# ─── Resource guard ────────────────────────────────────────────────────────────

def _free_vram_mb() -> int:
    """Free VRAM in MB from nvidia-smi. Returns -1 if unavailable."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheaders,nounits"],
            timeout=10, text=True,
        )
        values = [int(v.strip()) for v in out.strip().splitlines() if v.strip().isdigit()]
        return min(values) if values else -1
    except Exception:
        return -1


def _free_ram_mb() -> int:
    """Free system RAM in MB via psutil. Returns -1 if unavailable."""
    try:
        import psutil
        return psutil.virtual_memory().available // (1024 * 1024)
    except Exception:
        return -1


def _cpu_pct() -> float:
    """1-second CPU usage percent via psutil. Returns -1.0 if unavailable."""
    try:
        import psutil
        return psutil.cpu_percent(interval=1)
    except Exception:
        return -1.0


def _ollama_loaded_models(base_url: str) -> list[str]:
    """Return names of models currently loaded in Ollama via /api/ps."""
    try:
        resp = requests.get(base_url.rstrip("/") + "/api/ps", timeout=10)
        if resp.ok:
            return [m.get("name", "") for m in resp.json().get("models", [])]
    except Exception:
        pass
    return []


def unload_ollama_model(base_url: str, model: str) -> None:
    """Unload a model from GPU memory by sending keep_alive=0 to Ollama."""
    try:
        requests.post(
            base_url.rstrip("/") + "/api/generate",
            json={"model": model, "keep_alive": 0},
            timeout=30,
        )
        log(f"[GPU] unload requested: {model}")
    except Exception as exc:
        log(f"[GPU WARN] could not unload {model}: {exc}")


def wait_for_resources(
    base_url: str,
    *,
    label: str = "",
    min_vram_mb: int = 0,
    max_cpu_pct: float = 90.0,
    min_ram_mb: int = 1500,
    unloaded_model: str = "",
    max_wait: int = 600,
    poll: int = 20,
) -> None:
    """Block until GPU VRAM, CPU and RAM are within acceptable limits.

    If unloaded_model is set, also waits until that model is gone from Ollama.
    Raises RuntimeError if max_wait seconds are exceeded.
    """
    prefix = f"[RESOURCE:{label}] " if label else "[RESOURCE] "
    deadline = time.monotonic() + max_wait

    while True:
        reasons: list[str] = []

        if unloaded_model:
            loaded = _ollama_loaded_models(base_url)
            if any(unloaded_model in m for m in loaded):
                reasons.append(f"{unloaded_model} still in VRAM")

        vram = _free_vram_mb()
        if min_vram_mb > 0 and vram != -1 and vram < min_vram_mb:
            reasons.append(f"VRAM free {vram}MB < {min_vram_mb}MB")

        ram = _free_ram_mb()
        if ram != -1 and ram < min_ram_mb:
            reasons.append(f"RAM free {ram}MB < {min_ram_mb}MB")

        cpu = _cpu_pct()
        if cpu != -1.0 and cpu > max_cpu_pct:
            reasons.append(f"CPU {cpu:.0f}% > {max_cpu_pct:.0f}%")

        if not reasons:
            parts = []
            if vram != -1:
                parts.append(f"VRAM {vram}MB free")
            if ram != -1:
                parts.append(f"RAM {ram}MB free")
            if cpu != -1.0:
                parts.append(f"CPU {cpu:.0f}%")
            if parts:
                log(f"{prefix}OK — {', '.join(parts)}")
            return

        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"{prefix}timeout {max_wait}s waiting for: {'; '.join(reasons)}"
            )

        elapsed = time.monotonic() - (deadline - max_wait)
        log(f"{prefix}waiting {poll}s — {'; '.join(reasons)} (elapsed {elapsed:.0f}s)")
        time.sleep(poll)


def _check_resources(args: argparse.Namespace, *, label: str = "", unloaded_model: str = "") -> None:
    """Call wait_for_resources unless --no-resource-check is set."""
    if getattr(args, "no_resource_check", False):
        return
    wait_for_resources(
        args.ollama_url,
        label=label,
        min_vram_mb=getattr(args, "min_vram_mb", 0),
        max_cpu_pct=getattr(args, "max_cpu_pct", 90.0),
        min_ram_mb=getattr(args, "min_ram_mb", 1500),
        unloaded_model=unloaded_model,
        max_wait=getattr(args, "resource_wait", 600),
        poll=getattr(args, "resource_poll", 20),
    )


# ─── End resource guard ────────────────────────────────────────────────────────


def read_formatted_output(output_path: Path, job: dict, *, write_back: bool, label: str) -> str:
    content = output_path.read_text(encoding="utf-8")
    formatted = format_reader_polished_content(content, job)
    if formatted and write_back and formatted.strip() != content.strip():
        output_path.write_text(formatted.strip() + "\n", encoding="utf-8")
        log(f"[FORMAT] cleaned {label} output: {output_path}")
    return formatted or content.strip()


def read_formatted_polished_output(output_path: Path, job: dict, *, write_back: bool) -> str:
    return read_formatted_output(output_path, job, write_back=write_back, label="polished")


def _quality_warn(text: str, genre: str, char_map: str, label: str) -> None:
    issues = check_polished_quality(text, genre=genre, char_map_path=char_map)
    if issues:
        log(f"[QUALITY_WARN] {label}: {', '.join(issues)}")


def story_metadata_args(args: argparse.Namespace) -> Namespace:
    return Namespace(
        ollama_url=args.ollama_url,
        story_model=args.story_model,
        translate_model=args.translate_model,
        temperature=args.temperature,
        num_ctx=args.num_ctx,
        ollama_timeout=args.timeout,
        ollama_retries=args.retries,
        keep_alive=args.keep_alive,
    )


def first_content_line(text: str) -> str:
    for line in (text or "").splitlines():
        cleaned = re.sub(r"\s+", " ", line).strip()
        if cleaned:
            return cleaned
    return ""


def maybe_update_translated_chapter_title(job: dict, translated_text_content: str) -> str:
    title = first_content_line(translated_text_content)
    if not title or len(title) > 220:
        return ""
    repo.update_chapter_title(job["chapter_id"], title)
    return title


def maybe_translate_story_metadata(job: dict, args: argparse.Namespace) -> None:
    payload = job.get("payload") or {}
    if not payload.get("translate_story_metadata") or not job.get("story_id"):
        return

    from scripts.story_pipeline.translate_chapters_from_db import (
        translate_story_author,
        translate_story_description,
        translate_story_title,
        update_story_translation,
    )

    story = repo.get_story_by_id(job["story_id"])
    metadata = story.get("metadata") or {}
    if metadata.get("story_metadata_translated_to") == "vi" and story.get("display_title"):
        return

    source_title = str(payload.get("source_story_title") or story.get("original_title") or story.get("title") or "").strip()
    source_author = str(payload.get("source_story_author") or metadata.get("source_author") or story.get("author") or "").strip()
    source_description = str(
        payload.get("source_story_description")
        or metadata.get("source_description")
        or story.get("description")
        or ""
    ).strip()
    if not source_title and not source_author and not source_description:
        return

    meta_args = story_metadata_args(args)
    display_title = translate_story_title(source_title, meta_args) if source_title and not story.get("display_title") else None
    author = translate_story_author(source_author, meta_args) if source_author else None
    description = translate_story_description(source_description, meta_args) if source_description else None

    update_story_translation(
        job["story_id"],
        display_title=display_title,
        author=author,
        description=description,
        original_description=source_description or None,
        model=args.story_model or args.translate_model,
    )
    repo.update_story_metadata(
        job["story_id"],
        {
            "source_author": source_author or metadata.get("source_author"),
            "story_author_translated_to": "vi" if author else metadata.get("story_author_translated_to"),
        },
    )
    log(f"[STORY] metadata translated story_id={job['story_id']}")


def _resolve_input_for_polish(job: dict) -> tuple[Path, bool]:
    """Return (input_path, is_temp). Fetch from DB if file is missing.

    For raw_language=vi jobs (post-translate queue flow), the job input is the
    translated file — fall back to translated_text_content, not raw_text_content,
    to avoid polishing untranslated source text.
    """
    raw_input = job.get("input_path") or ""
    input_path = Path(raw_input) if raw_input else None
    if input_path and input_path.exists():
        return input_path, False

    payload = job.get("payload") or {}

    # Post-translate queue jobs: raw_language=vi AND translated_text_path present in payload.
    # Native VI jobs: raw_language=vi but no translated_text_path (or raw_language=en/zh).
    # Only use translated_text_content for the post-translate case to avoid polishing
    # untranslated source text.
    raw_language = (payload.get("raw_language") or "vi").lower()
    is_post_translate = raw_language == "vi" and bool(payload.get("translated_text_path"))
    if is_post_translate:
        content = repo.get_chapter_translated_content(job["chapter_id"])
        content_label = "translated_text_content"
    else:
        content = repo.get_chapter_raw_content(job["chapter_id"])
        content_label = "raw_text_content"

    if not content:
        raise FileNotFoundError(
            f"Input missing for chapter_id={job['chapter_id']}: "
            f"file not found at {input_path!r} and no {content_label} in DB"
        )
    suffix = Path(raw_input).suffix if raw_input else ".txt"
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=suffix, encoding="utf-8", delete=False,
        prefix=f"polish_in_{payload.get('chapter_number', 0):04d}_",
    )
    tmp.write(content)
    tmp.close()
    log(f"[TMP] input from DB ({content_label}) -> {tmp.name}")
    return Path(tmp.name), True


def process_job(job: dict, args: argparse.Namespace) -> None:
    payload = job.get("payload") or {}
    raw_language = (payload.get("raw_language") or "vi").lower()
    input_path, input_is_temp = _resolve_input_for_polish(job)
    raw_output_path = job.get("output_path") or ""
    _tmp_out_dir: tempfile.TemporaryDirectory | None = None
    if raw_output_path:
        output_path = Path(raw_output_path)
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Fallback to temp only when directory cannot be created (e.g. read-only mount)
            _tmp_out_dir = tempfile.TemporaryDirectory(prefix="polish_out_")
            output_path = Path(_tmp_out_dir.name) / output_path.name
            log(f"[WARN] cannot create output dir, using temp: {output_path}")
    else:
        _tmp_out_dir = tempfile.TemporaryDirectory(prefix="polish_out_")
        output_path = Path(_tmp_out_dir.name) / "chapter.txt"

    maybe_translate_story_metadata(job, args)

    # Auto-resolve char map: payload > --char-map-file arg > convention-based lookup
    story_id = str(job.get("story_id") or "")
    job_slug = story_slug_for_job(job, input_path)
    effective_char_map = (
        payload.get("char_map_file")
        or getattr(args, "char_map_file", "")
        or find_char_map_file(story_id=story_id, slug=job_slug)
    )
    char_map_text_source = choose_char_map_text_source(args, raw_language)
    current_chapter = int(payload.get("chapter_number") or 0)
    # Pre-resolve genre from DB only (no char_map yet) so auto-create can inject genre header
    pre_genre = resolve_genre(job, char_map_file="")
    if raw_language == "vi" or effective_char_map:
        effective_char_map = maybe_auto_update_char_map(
            job,
            args,
            slug=job_slug,
            current_chapter=current_chapter,
            existing_char_map=effective_char_map,
            text_source=char_map_text_source,
            genre=pre_genre,
        )
    if effective_char_map:
        log(f"[CHAR_MAP] {effective_char_map} (story_id={story_id})")

    genre = resolve_genre(job, effective_char_map)
    if genre:
        log(f"[GENRE] {genre} (story_id={job.get('story_id')})")

    if output_path.exists() and not args.overwrite:
        log(f"[SKIP] output exists: {output_path}")
        translated_path = None
        if raw_language in {"zh", "cn", "ko", "kr", "en"}:
            translated_path = (
                payload.get("translated_text_path")
                or (Path(args.translated_output_root) / story_slug_for_job(job, input_path) / output_path.name).as_posix()
            )
        repo.update_chapter_text_outputs(
            job["chapter_id"],
            translated_text_path=translated_path,
            polished_text_path=output_path.as_posix() if _tmp_out_dir is None else None,
            translated_text_content=Path(translated_path).read_text(encoding="utf-8") if translated_path and Path(translated_path).exists() else None,
            polished_text_content=read_formatted_polished_output(output_path, job, write_back=False),
        )
        if translated_path and Path(translated_path).exists():
            maybe_update_translated_chapter_title(
                job,
                read_formatted_output(Path(translated_path), job, write_back=False, label="translated"),
            )
        repo.complete_story_job(job["id"], result_payload={"skipped": "output_exists"})
        return

    def _build_args_with_char_map(model: str, max_chars: int, genre: str = "") -> Namespace:
        ns = build_args(args, model, max_chars, genre, story_id=story_id, story_slug=job_slug)
        ns.char_map_file = effective_char_map
        return ns

    if raw_language == "vi":
        model = job.get("model") or args.vi_model
        max_chars = int(payload.get("polish_max_chars_per_chunk") or args.polish_max_chars_per_chunk)
        _check_resources(args, label="polish")
        polish_file(
            input_path,
            output_path,
            _build_args_with_char_map(model, max_chars, genre),
        )
        polished_text_content = read_formatted_polished_output(output_path, job, write_back=True)
        _quality_warn(polished_text_content, genre, effective_char_map or "", f"vi ch{current_chapter}")
        repo.update_chapter_text_outputs(
            job["chapter_id"],
            polished_text_path=output_path.as_posix() if _tmp_out_dir is None else None,
            polished_text_content=polished_text_content,
        )
    elif args.single_pass and raw_language == "en":
        # Single-pass EN→VI: translate + polish in one Ollama call.
        single_pass_max_chars = int(payload.get("translate_max_chars_per_chunk") or args.single_pass_max_chars_per_chunk)
        translate_model = job.get("model") or args.translate_model
        _check_resources(args, label="translate")
        sp_args = _build_args_with_char_map(translate_model, single_pass_max_chars, genre)
        sp_args.num_ctx = args.single_pass_num_ctx
        single_pass_translate_polish_file(input_path, output_path, sp_args)
        polished_text_content = read_formatted_polished_output(output_path, job, write_back=True)
        _quality_warn(polished_text_content, genre, effective_char_map or "", f"single-pass ch{current_chapter}")
        repo.update_chapter_text_outputs(
            job["chapter_id"],
            polished_text_path=output_path.as_posix() if _tmp_out_dir is None else None,
            polished_text_content=polished_text_content,
        )
        # For single-pass, polished output IS the translated output — use it for chapter title and char-map.
        translated_chapter_title = maybe_update_translated_chapter_title(job, polished_text_content)
        effective_char_map = maybe_auto_update_char_map(
            job,
            args,
            slug=job_slug,
            current_chapter=current_chapter,
            existing_char_map=effective_char_map,
            text_source="polished",
            genre=genre,
        )
        if effective_char_map:
            log(f"[CHAR_MAP] {effective_char_map} (story_id={story_id})")
    else:
        if args.single_pass and raw_language != "en":
            log(f"[SINGLE_PASS] WARNING: --single-pass only supports EN; raw_language={raw_language!r} → falling back to two-pass")
        translate_model = job.get("model") or args.translate_model
        translate_max_chars = int(payload.get("translate_max_chars_per_chunk") or args.translate_max_chars_per_chunk)
        polish_max_chars = int(payload.get("polish_max_chars_per_chunk") or args.polish_max_chars_per_chunk)
        translated_path = Path(args.translated_output_root) / story_slug_for_job(job, input_path) / output_path.name
        _check_resources(args, label="translate")
        translate_file(
            input_path,
            translated_path,
            _build_args_with_char_map(translate_model, translate_max_chars, genre),
        )
        translated_text_content = read_formatted_output(translated_path, job, write_back=True, label="translated")
        translated_chapter_title = maybe_update_translated_chapter_title(job, translated_text_content)
        repo.update_chapter_text_outputs(
            job["chapter_id"],
            translated_text_path=translated_path.as_posix(),
            translated_text_content=translated_text_content,
        )
        effective_char_map = maybe_auto_update_char_map(
            job,
            args,
            slug=job_slug,
            current_chapter=current_chapter,
            existing_char_map=effective_char_map,
            text_source=char_map_text_source,
            genre=genre,
        )
        if effective_char_map:
            log(f"[CHAR_MAP] {effective_char_map} (story_id={story_id})")
        genre = resolve_genre(job, effective_char_map)
        mode = post_translate_mode(job, args)
        if mode == "copy":
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(clean_for_audiobook_tts(translated_text_content).strip() + "\n", encoding="utf-8")
            log(f"[COPY] translated output reused as polished output: {output_path}")
        else:
            # Unload translate model trước khi load polish model để tránh chiếm VRAM cùng lúc.
            if translate_model != args.vi_model:
                unload_ollama_model(args.ollama_url, translate_model)
            _check_resources(args, label="polish", unloaded_model=translate_model if translate_model != args.vi_model else "")
            polish_file(
                translated_path,
                output_path,
                _build_args_with_char_map(args.vi_model, polish_max_chars, genre),
            )
        polished_text_content = read_formatted_polished_output(output_path, job, write_back=True)
        _quality_warn(polished_text_content, genre, effective_char_map or "", f"two-pass ch{current_chapter}")
        repo.update_chapter_text_outputs(
            job["chapter_id"],
            translated_text_path=translated_path.as_posix(),
            polished_text_path=output_path.as_posix() if _tmp_out_dir is None else None,
            translated_text_content=translated_text_content,
            polished_text_content=polished_text_content,
        )

    repo.complete_story_job(
        job["id"],
        result_payload={
            "output_path": output_path.as_posix(),
            "raw_language": raw_language,
            "translated_chapter_title": locals().get("translated_chapter_title") or None,
            **({"single_pass": True} if args.single_pass and raw_language == "en" else {}),
        },
    )
    log(f"[DONE] {job['id']} -> {output_path}")

    if input_is_temp:
        input_path.unlink(missing_ok=True)
    if _tmp_out_dir is not None:
        _tmp_out_dir.cleanup()


def run_one(job: dict, args: argparse.Namespace) -> None:
    try:
        log(
            "[START] "
            f"job={job.get('id')} source={job.get('source_code')} chapter={job.get('chapter_id')} "
            f"input={job.get('input_path')} output={job.get('output_path')}"
        )
        process_job(job, args)
    except Exception as exc:
        log(f"[ERROR] job={job.get('id')}: {exc}")
        repo.fail_story_job(job["id"], str(exc), retry_delay_seconds=args.retry_delay)


def main() -> None:
    # Tip tốc độ: đặt OLLAMA_FLASH_ATTENTION=1 trong môi trường chạy Ollama để giảm
    # 20-40% inference time nhờ Flash Attention 2. Không cần thay đổi code.
    # Ví dụ: OLLAMA_FLASH_ATTENTION=1 ollama serve
    parser = argparse.ArgumentParser(description="Worker xử lý polish/translate chapter jobs từ story_jobs.")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--batch-size", type=int, default=0, help="0 means use --workers.")
    parser.add_argument(
        "--source-code",
        action="append",
        default=[],
        help="Only claim jobs for this source_code. Repeat for multiple sources.",
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--idle-sleep", type=float, default=3.0)
    parser.add_argument("--idle-log-interval", type=float, default=30.0, help="Seconds between idle queue logs. Use 0 to disable.")
    parser.add_argument("--retry-delay", type=int, default=120)
    parser.add_argument("--worker-id", default=f"polish-{socket.gethostname()}")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--vi-model", default="qwen3:14b")
    parser.add_argument("--translate-model", default="qwen3:14b")
    parser.add_argument("--story-model", default="", help="Model riêng cho story title/author/description; mặc định dùng --translate-model.")
    parser.add_argument("--translated-output-root", default="story_data/translated")
    parser.add_argument("--temperature", type=float, default=0.25)
    parser.add_argument("--num-ctx", type=int, default=8192)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--keep-alive", default="30m")
    parser.add_argument("--prompt-profile", choices=("fast", "full"), default="full")
    parser.add_argument("--polish-mode", choices=("llm", "clean"), default="llm")
    parser.add_argument(
        "--post-translate",
        choices=("polish", "copy"),
        default="polish",
        help="Sau khi dịch raw khác tiếng Việt: polish bằng LLM, hoặc copy bản dịch sang polished output.",
    )
    parser.add_argument("--polish-max-chars-per-chunk", type=int, default=4000)
    parser.add_argument("--translate-max-chars-per-chunk", type=int, default=2500)
    parser.add_argument(
        "--single-pass",
        action="store_true",
        default=False,
        help=(
            "Gộp translate+polish thành 1 Ollama call (EN source only). "
            "KO/ZH tự động fall through sang two-pass. "
            "Dùng --single-pass-num-ctx để override num_ctx cho single-pass."
        ),
    )
    parser.add_argument(
        "--single-pass-num-ctx",
        type=int,
        default=6144,
        help="num_ctx cho single-pass mode (default 6144; monitor [TOKENS] log để điều chỉnh).",
    )
    parser.add_argument(
        "--single-pass-max-chars-per-chunk",
        type=int,
        default=2000,
        help="Chunk size cho single-pass (default 2000 chars EN; nhỏ hơn two-pass để fit num_ctx).",
    )
    parser.add_argument(
        "--min-output-ratio",
        type=float,
        default=0.70,
        help=(
            "Ngưỡng fallback: nếu output ngắn hơn X%% input (ký tự, bỏ whitespace), dùng lại chunk raw. "
            "0.70 = an toàn; 0 = tắt kiểm tra."
        ),
    )
    parser.add_argument(
        "--char-map-file",
        default="",
        help=(
            "File nhân vật (character map) inject vào system prompt. "
            "VD: story_data/char_maps/21180-vinh-thoai-hiep-si.txt"
        ),
    )
    parser.add_argument(
        "--story-memory-dir",
        default="",
        help=(
            "Root story memory hoặc thư mục memory cụ thể dùng chung cho worker. "
            "Nếu bỏ trống, mỗi job tự tìm theo story_data/story_memory/{story_id}-{slug}."
        ),
    )
    parser.add_argument(
        "--fail-on-story-memory-issues",
        action="store_true",
        help="Nếu story memory QA phát hiện lỗi tên/thuật ngữ/register, fail job thay vì chỉ cảnh báo.",
    )
    parser.add_argument(
        "--no-auto-char-map",
        action="store_true",
        help="Tắt tự động tạo/cập nhật character map trong worker.",
    )
    parser.add_argument(
        "--char-map-update-interval",
        type=int,
        default=150,
        help="Tự cập nhật char map sau mỗi N chapter mới. 0 = chỉ auto-create khi thiếu map.",
    )
    parser.add_argument(
        "--char-map-text-source",
        choices=("auto", "raw", "translated", "polished"),
        default="auto",
        help="Nguồn text để auto build char-map. auto=raw cho VI, translated cho raw khác tiếng Việt.",
    )
    parser.add_argument(
        "--char-map-min-frequency",
        type=int,
        default=1,
        help="Tần suất tối thiểu để candidate name được gửi vào LLM khi build char-map.",
    )
    parser.add_argument("--char-map-sample-chapters", type=int, default=30, help="Legacy no-op: giữ để tương thích CLI cũ.")
    parser.add_argument("--char-map-model", default="", help="Model dùng riêng để build char map; mặc định dùng --vi-model.")
    parser.add_argument("--char-map-timeout", type=int, default=180)
    parser.add_argument(
        "--char-map-create-cooldown",
        type=int,
        default=30,
        help=(
            "Sau khi char map build lần đầu thất bại, skip retry trong N chapter. "
            "Ngăn worker spam Ollama khi model bận. Mặc định 30."
        ),
    )
    parser.add_argument(
        "--min-vram-mb",
        type=int,
        default=0,
        help="VRAM tối thiểu (MB) trước khi gọi model. 0 = không check VRAM (dùng khi không có nvidia-smi hoặc GPU không đủ lớn để đặt ngưỡng cố định).",
    )
    parser.add_argument(
        "--max-cpu-pct",
        type=float,
        default=90.0,
        help="CPU%% tối đa cho phép trước khi gọi model. Nếu vượt ngưỡng, worker sẽ chờ.",
    )
    parser.add_argument(
        "--min-ram-mb",
        type=int,
        default=1500,
        help="RAM tự do tối thiểu (MB) trước khi gọi model.",
    )
    parser.add_argument(
        "--resource-wait",
        type=int,
        default=600,
        help="Số giây tối đa chờ tài nguyên trước khi báo lỗi (default: 600).",
    )
    parser.add_argument(
        "--resource-poll",
        type=int,
        default=20,
        help="Khoảng cách giữa các lần kiểm tra tài nguyên (giây, default: 20).",
    )
    parser.add_argument(
        "--no-resource-check",
        action="store_true",
        help="Bỏ qua toàn bộ kiểm tra tài nguyên GPU/CPU/RAM.",
    )
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    if args.workers < 1:
        raise SystemExit("--workers must be >= 1")

    stale_reset = repo.reset_stale_running_jobs("polish_chapter", stale_after_minutes=120)
    if stale_reset:
        log(f"[STARTUP] reset {stale_reset} stale running polish jobs back to pending")

    batch_size = args.batch_size or args.workers
    source_label = ",".join(args.source_code) if args.source_code else "all"
    log(f"worker={args.worker_id}, workers={args.workers}, batch_size={batch_size}, source={source_label}")

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        in_flight: dict[Future[None], str] = {}
        claimed_once = False
        last_idle_log = 0.0

        while True:
            can_claim = not (args.once and claimed_once)
            available_slots = args.workers - len(in_flight)

            if can_claim and available_slots > 0:
                claim_limit = min(batch_size, available_slots)
                jobs = repo.claim_story_jobs(
                    "polish_chapter",
                    args.worker_id,
                    limit=claim_limit,
                    source_codes=args.source_code,
                )
                if args.once:
                    claimed_once = True
                if jobs:
                    log(f"[CLAIM] count={len(jobs)} worker={args.worker_id}")
                for job in jobs:
                    future = executor.submit(run_one, job, args)
                    in_flight[future] = str(job.get("id"))

            if not in_flight:
                if args.once:
                    log("No pending jobs.")
                    return
                now = time.monotonic()
                if args.idle_log_interval > 0 and now - last_idle_log >= args.idle_log_interval:
                    log(
                        "[IDLE] no eligible jobs "
                        f"type=polish_chapter source={source_label} "
                        "waiting for pending jobs with run_after <= now and attempts < max_attempts"
                    )
                    last_idle_log = now
                time.sleep(args.idle_sleep)
                continue

            done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
            for future in done:
                in_flight.pop(future, None)
                future.result()


if __name__ == "__main__":
    main()
