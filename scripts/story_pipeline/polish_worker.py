#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
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
from genre_prompts import (
    clean_source_noise,
    detect_genre,
    find_char_map_file,
    load_char_map,
    resolve_genre_from_context,
    validate_char_map,
)
from extract_char_map import update_char_map_incremental
from polish_chapter_texts_ollama import clean_for_audiobook_tts, polish_file
from reader_content_format import format_polished_content as format_reader_polished_content
from translate_chapter_texts_ollama import single_pass_translate_polish_file, translate_file
from check_translation_quality import (
    BLOCKING_QUALITY_ISSUES,
    _WRONG_PRONOUN_GENRES,
    _fix_pronouns_in_text,
    check_polished_quality,
    issue_to_repair_hint,
    run_full_quality_check,
)
from story_memory import (
    find_story_memory_quality_issues,
    load_story_memory,
    save_chapter_recap,
)
from extract_term_glossary import glossary_path_for, update_term_glossary
from llm_quality_judge import judge_chapter_quality


def story_slug_for_job(job: dict, input_path: Path) -> str:
    payload = job.get("payload") or {}
    return str(payload.get("story_slug") or input_path.parent.name)


def build_metadata_context_for_job(job: dict, args: argparse.Namespace) -> str:
    payload = job.get("payload") or {}
    story_id = str(job.get("story_id") or payload.get("story_id") or "")
    story = repo.get_story_by_id(story_id) if story_id else {}
    metadata = story.get("metadata") or {}
    slug = str(payload.get("story_slug") or metadata.get("slug") or "")
    char_map_file = str(payload.get("char_map_file") or getattr(args, "char_map_file", "") or "")

    from scripts.story_pipeline.translate_chapters_from_db import build_metadata_translation_context

    return build_metadata_translation_context(
        story_id=story_id,
        story_slug_value=slug,
        source_code=str(job.get("source_code") or payload.get("source_code") or ""),
        story_title=str(story.get("title") or payload.get("source_story_title") or ""),
        original_title=str(story.get("original_title") or payload.get("source_story_title") or ""),
        display_title=str(story.get("display_title") or ""),
        description=str(
            metadata.get("source_description")
            or metadata.get("original_description_before_vi_translate")
            or story.get("description")
            or payload.get("source_story_description")
            or ""
        ),
        category=str(story.get("category") or metadata.get("genre") or payload.get("genre") or ""),
        raw_language=str(payload.get("raw_language") or story.get("language") or ""),
        char_map_file=char_map_file,
        story_memory_dir=str(getattr(args, "story_memory_dir", "") or ""),
    )


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
        no_chunk_glossary=getattr(args, "no_chunk_glossary", False),
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
        metadata = story.get("metadata") or {}
        return resolve_genre_from_context(
            category,
            raw_language=raw_language,
            source_code=source_code,
            char_map_file=char_map_file,
            title=str(story.get("original_title") or story.get("title") or ""),
            description=str(
                metadata.get("source_description")
                or metadata.get("original_description_before_vi_translate")
                or story.get("description")
                or ""
            ),
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
    """Create/update char map opportunistically. Never fail the story job.

    Hai mode tạo char-map:
    - Batch (build_char_map_from_story.py): scan toàn bộ story, nặng, dùng khi cần rebuild.
      Bị tắt bởi --no-batch-char-map (default ON).
    - Seed (extract_char_map.py --to-chapter 10): scan 10 chapter đầu, nhẹ, dùng khi chưa có map.
      Chạy tự động khi chưa có char-map, ngay cả khi --no-batch-char-map bật.
    - Incremental (_run_incremental_char_map): cập nhật sau mỗi chapter polish xong.
    """
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
    interval = int(getattr(args, "char_map_update_interval", 0) or 0)
    should_update = bool(existing_char_map and interval > 0 and current_chapter >= updated_to + interval)

    no_batch = getattr(args, "no_batch_char_map", False)

    # Nếu --no-batch-char-map: skip batch update, nhưng vẫn tạo seed nếu chưa có map.
    if no_batch and not should_create:
        return existing_char_map
    if not should_create and not should_update:
        return existing_char_map

    # Cooldown sau failed create: tránh hammering Ollama mỗi chapter.
    if should_create:
        failed_at = int(metadata.get("char_map_create_failed_at_chapter") or 0)
        cooldown = int(getattr(args, "char_map_create_cooldown", 30) or 30)
        if failed_at > 0 and current_chapter < failed_at + cooldown:
            log(
                f"[CHAR_MAP] skip create (last attempt failed at ch{failed_at:04d}, "
                f"retry at ch{failed_at + cooldown:04d})"
            )
            return existing_char_map

    model = str(getattr(args, "char_map_model", "") or getattr(args, "vi_model", "qwen3:14b"))
    char_map_timeout = max(30, int(getattr(args, "char_map_timeout", 180) or 180))

    # Seed mode: tạo char-map từ 10 chapter đầu khi --no-batch-char-map bật (hoặc khi tạo lần đầu nhẹ hơn).
    if should_create and no_batch:
        seed_end = max(1, min(10, current_chapter or 10))
        log(f"[CHAR_MAP] seed create ch1-{seed_end:04d} story_id={story_id} slug={slug} text_source={text_source}")
        cmd = [
            sys.executable,
            str(SCRIPT_DIR / "extract_char_map.py"),
            "--story-id", story_id,
            "--text-source", text_source,
            "--from-chapter", "1",
            "--to-chapter", str(seed_end),
            "--sample-chapters", str(seed_end),
            "--model", model,
            "--ollama-url", args.ollama_url,
            "--timeout", str(char_map_timeout),
        ]
        if genre:
            cmd.extend(["--genre", genre])
        try:
            result = subprocess.run(
                cmd, cwd=ROOT, text=True, capture_output=True,
                timeout=char_map_timeout * 3,
            )
            if result.returncode != 0:
                tail = (result.stderr or result.stdout or "").strip()[-800:]
                log(f"[CHAR_MAP WARN] seed extract failed rc={result.returncode}: {tail}")
                _record_char_map_create_failure(story_id, current_chapter)
            else:
                # Mark raw coverage so lookahead knows seed already scanned ch1-seed_end
                try:
                    repo.update_story_metadata(story_id, {"char_map_raw_covered_to": seed_end})
                except Exception:
                    pass
                # Codex finding 1: refresh path — existing_char_map was empty before seed
                refreshed = find_char_map_file(story_id=story_id, slug=slug)
                if refreshed:
                    log(f"[CHAR_MAP] seed ready {refreshed}")
                    return refreshed
                return existing_char_map
        except Exception as exc:
            log(f"[CHAR_MAP WARN] seed extract failed: {type(exc).__name__}: {exc}")
            _record_char_map_create_failure(story_id, current_chapter)
        return existing_char_map

    # Batch mode: build_char_map_from_story.py (full scan hoặc append update).
    cmd = [
        sys.executable,
        str(SCRIPT_DIR / "build_char_map_from_story.py"),
        "--story-id", story_id,
        "--text-source", text_source,
        "--model", model,
        "--ollama-url", args.ollama_url,
        "--timeout", str(char_map_timeout),
        "--min-frequency", str(max(1, int(getattr(args, "char_map_min_frequency", 1) or 1))),
    ]
    if should_create and genre:
        cmd.extend(["--genre", genre])
    if should_update:
        from_chapter = max(1, updated_to + 1)
        cmd.extend(["--from-chapter", str(from_chapter), "--to-chapter", str(current_chapter), "--append-only"])

    reason = "create" if should_create else f"update ch{updated_to + 1:04d}→ch{current_chapter:04d}"
    log(f"[CHAR_MAP] batch {reason} story_id={story_id} slug={slug} text_source={text_source}")
    try:
        result = subprocess.run(
            cmd, cwd=ROOT, text=True, capture_output=True,
            timeout=max(180, char_map_timeout * 4),
        )
        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "").strip()[-1000:]
            log(f"[CHAR_MAP WARN] batch extract failed rc={result.returncode}: {tail}")
            if should_create:
                _record_char_map_create_failure(story_id, current_chapter)
            return existing_char_map
        refreshed = find_char_map_file(story_id=story_id, slug=slug)
        if refreshed:
            log(f"[CHAR_MAP] ready {refreshed}")
            return refreshed
    except Exception as exc:
        log(f"[CHAR_MAP WARN] batch extract failed: {type(exc).__name__}: {exc}")
        if should_create:
            _record_char_map_create_failure(story_id, current_chapter)
    return existing_char_map


def _record_char_map_create_failure(story_id: str, chapter: int) -> None:
    try:
        repo.update_story_metadata(story_id, {"char_map_create_failed_at_chapter": chapter})
        log(f"[CHAR_MAP] recorded create failure at ch{chapter:04d} — cooldown active")
    except Exception as exc:
        log(f"[CHAR_MAP WARN] could not record failure: {exc}")


def _run_incremental_char_map(
    polished_text: str,
    chapter_num: int,
    char_map_path: str,
    story_id: str,
    slug: str,
    genre: str,
    args: argparse.Namespace,
) -> None:
    """
    Gọi update_char_map_incremental sau khi chapter được polish xong.
    Không bao giờ raise exception — lỗi chỉ được log.
    """
    if getattr(args, "no_auto_char_map", False):
        return
    if getattr(args, "no_incremental_char_map", False):
        return
    if not polished_text or not story_id:
        return
    try:
        # story title chỉ cần cho lần đầu tạo file
        story_title = ""
        try:
            story = repo.get_story_by_id(story_id)
            story_title = str(story.get("display_title") or story.get("title") or "")
        except Exception:
            pass

        model = str(getattr(args, "char_map_model", "") or getattr(args, "vi_model", "qwen3:14b"))
        update_char_map_incremental(
            chapter_text=polished_text,
            chapter_num=chapter_num,
            char_map_path=char_map_path,
            story_id=story_id,
            story_title=story_title,
            slug=slug,
            genre=genre,
            ollama_url=args.ollama_url,
            model=model,
            timeout=int(getattr(args, "char_map_timeout", 90) or 90),
        )
    except Exception as exc:
        log(f"[CHAR_MAP_INC WARN] ch{chapter_num:04d}: {exc}")


def _lookahead_coverage_key(text_source: str) -> str:
    """Return the metadata key tracking lookahead coverage for a given text_source."""
    return "char_map_raw_covered_to" if text_source == "raw" else f"char_map_{text_source}_covered_to"


def _run_lookahead_char_map(
    job: dict,
    args: argparse.Namespace,
    *,
    current_chapter: int,
    char_map_path: str,
    slug: str,
    genre: str,
    text_source: str = "raw",
) -> None:
    """Pre-extract characters N chapters ahead of current_chapter.

    Uses source-aware coverage metadata so raw and translated lookahead
    track independently (char_map_raw_covered_to vs char_map_translated_covered_to).

    Each call extends coverage by exactly 1 chapter (incremental sliding window),
    so the overhead per chapter is a single LLM call (~30-60s). No block pipeline.
    """
    if getattr(args, "no_auto_char_map", False):
        return
    if getattr(args, "no_incremental_char_map", False):
        return
    lookahead = int(getattr(args, "char_map_lookahead", 10) or 10)
    if lookahead <= 0 or not char_map_path or not current_chapter:
        return
    story_id = str(job.get("story_id") or "")
    if not story_id:
        return

    target = current_chapter + lookahead
    coverage_key = _lookahead_coverage_key(text_source)

    try:
        story = repo.get_story_by_id(story_id)
        metadata = story.get("metadata") or {}
        covered = int(metadata.get(coverage_key) or 0)
    except Exception:
        covered = 0

    if covered >= target:
        return  # already ahead enough

    from_ch = covered + 1
    model = str(getattr(args, "char_map_model", "") or getattr(args, "vi_model", "qwen3:14b"))
    char_map_timeout = max(30, int(getattr(args, "char_map_timeout", 90) or 90))
    n_new = target - from_ch + 1

    log(
        f"[CHAR_MAP LOOKAHEAD] ch{from_ch:04d}→ch{target:04d} "
        f"(text_source={text_source}, {n_new} chaps) story_id={story_id}"
    )
    cmd = [
        sys.executable,
        str(SCRIPT_DIR / "extract_char_map.py"),
        "--story-id", story_id,
        "--text-source", text_source,
        "--from-chapter", str(from_ch),
        "--to-chapter", str(target),
        "--sample-chapters", str(n_new),
        "--append-only",
        "--model", model,
        "--ollama-url", args.ollama_url,
        "--timeout", str(char_map_timeout),
    ]
    if genre:
        cmd.extend(["--genre", genre])
    try:
        result = subprocess.run(
            cmd, cwd=ROOT, text=True, capture_output=True,
            timeout=char_map_timeout * (n_new + 2),
        )
        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "").strip()[-500:]
            log(f"[CHAR_MAP LOOKAHEAD WARN] failed rc={result.returncode}: {tail}")
            return
        repo.update_story_metadata(story_id, {coverage_key: target})
        log(f"[CHAR_MAP LOOKAHEAD] done — {coverage_key}={target}")
    except Exception as exc:
        log(f"[CHAR_MAP LOOKAHEAD WARN] {type(exc).__name__}: {exc}")


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


def _dedupe_repeated_paragraphs(text: str, *, min_block: int = 120) -> tuple[str, int]:
    """Remove exact duplicate long paragraphs produced by model loops.

    This is intentionally narrow: only exact repeated paragraphs of substantial
    length are removed, matching the `repeated_content` quality gate.
    """
    if not text:
        return text, 0
    parts = re.split(r"\n\s*\n", text.strip())
    seen: set[str] = set()
    kept: list[str] = []
    removed = 0
    for part in parts:
        paragraph = part.strip()
        if not paragraph:
            continue
        if len(paragraph) >= min_block and paragraph in seen:
            removed += 1
            continue
        if len(paragraph) >= min_block:
            seen.add(paragraph)
        kept.append(paragraph)
    if not removed:
        return text, 0
    return "\n\n".join(kept).strip(), removed


_CJK_PAREN_RE = re.compile(r"\s*[\(（][^\n()（）]{0,80}[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af][^\n()（）]{0,80}[\)）]")


def _strip_cjk_parentheticals(text: str) -> tuple[str, int]:
    """Remove source-script parentheticals like `Đoạn Sơn Kiếm Pháp (斷岳劍法)`.

    Korean cultivation chapters often include Hanja/Hangul in parentheses after
    an already translated technique/title. Keeping those brackets trips the CJK
    contamination gate and hurts TTS, but CJK outside parentheses must still fail.
    """
    if not text:
        return text, 0
    return _CJK_PAREN_RE.subn("", text)


def read_formatted_output(output_path: Path, job: dict, *, write_back: bool, label: str) -> str:
    content = output_path.read_text(encoding="utf-8")
    formatted = format_reader_polished_content(content, job)
    result = formatted or content.strip()
    result, removed_cjk_parentheticals = _strip_cjk_parentheticals(result)
    if removed_cjk_parentheticals:
        log(f"[CJK_CLEAN] removed {removed_cjk_parentheticals} source-script parenthetical(s) from {label} output")
    result, removed_repeats = _dedupe_repeated_paragraphs(result)
    if removed_repeats:
        log(f"[DEDUP] removed {removed_repeats} repeated paragraph(s) from {label} output")
    if write_back and result.strip() != content.strip():
        output_path.write_text(result.strip() + "\n", encoding="utf-8")
        log(f"[FORMAT] cleaned {label} output: {output_path}")
    return result


def read_formatted_polished_output(output_path: Path, job: dict, *, write_back: bool) -> str:
    return read_formatted_output(output_path, job, write_back=write_back, label="polished")


def maybe_auto_update_term_glossary(
    job: dict,
    args: argparse.Namespace,
    *,
    slug: str,
    current_chapter: int,
    genre: str,
) -> None:
    """Auto seed/update terminology glossary cho story memory. Never fail the job.

    - Seed: chưa có glossary.json → mine từ raw chapters 1..max(10, current).
    - Incremental: mỗi --glossary-update-interval chapters, mine terms mới.
    Glossary được story_memory inject tự động vào translate + polish, và
    quality gate enforce wrong_translations/forbidden.
    """
    if getattr(args, "no_auto_glossary", False):
        return
    story_id = str(job.get("story_id") or "")
    if not story_id or current_chapter <= 0:
        return
    try:
        interval = max(5, int(getattr(args, "glossary_update_interval", 20)))
        story = repo.get_story_by_id(story_id)
        metadata = story.get("metadata") or {}
        failed_at = int(metadata.get("glossary_failed_at_chapter") or 0)
        if failed_at and current_chapter < failed_at + interval:
            return
        g_path = glossary_path_for(story_id, slug)
        updated_to = int(metadata.get("glossary_updated_to_chapter") or 0)

        if not g_path.exists():
            from_chapter, to_chapter = 1, max(20, current_chapter)
            label = "seed"
        elif current_chapter >= updated_to + interval:
            from_chapter, to_chapter = max(1, updated_to + 1), current_chapter
            label = "incremental"
        else:
            return

        log(f"[GLOSSARY] auto-{label} story={story_id} ch{from_chapter}-{to_chapter}")
        result = update_term_glossary(
            story_id=story_id,
            from_chapter=from_chapter,
            to_chapter=to_chapter,
            text_source="raw",
            ollama_url=args.ollama_url,
            model=args.translate_model or "qwen3:14b",
            genre=genre,
        )
        if result.get("status") == "ok":
            log(f"[GLOSSARY] auto-{label}: +{result.get('added', 0)} terms (total {result.get('total', 0)})")
        # update_term_glossary tự ghi glossary_updated_to_chapter; với no_new_terms
        # vẫn bump để không re-scan cùng khoảng chương mỗi job.
        if result.get("status") in {"ok", "no_new_terms"}:
            repo.update_story_metadata(story_id, {"glossary_updated_to_chapter": int(to_chapter)})
    except Exception as exc:  # noqa: BLE001 — glossary là enhancement, không chặn pipeline
        log(f"[GLOSSARY] auto-update failed (cooldown {getattr(args, 'glossary_update_interval', 20)} chapters): {exc}")
        try:
            repo.update_story_metadata(story_id, {"glossary_failed_at_chapter": int(current_chapter)})
        except Exception:
            pass


_RECAP_PROMPT = """/no_think
Tóm tắt chương truyện dưới đây trong TỐI ĐA 3 câu tiếng Việt. Tập trung vào:
nhân vật xuất hiện (và cách họ xưng hô với nhau), sự kiện chính, thuật ngữ/danh xưng mới xuất hiện.
Chỉ trả về phần tóm tắt, không tiêu đề, không giải thích.

{chapter_text}"""


def maybe_update_chapter_recap(
    job: dict,
    args: argparse.Namespace,
    *,
    slug: str,
    chapter_number: int,
    polished_text: str,
    genre: str,
) -> None:
    """Sinh recap <= 3 câu cho chapter vừa polish và lưu vào story memory
    (recaps.json — atomic, per-story lock). Recap các chương trước được inject
    vào translate/polish prompt của chương sau để giữ mạch truyện qua chương.

    Never-fail: lỗi LLM/IO chỉ log warning, không fail job. --no-chapter-recap để tắt.
    """
    if getattr(args, "no_chapter_recap", False):
        return
    story_id = str(job.get("story_id") or "")
    if not story_id or chapter_number <= 0 or not polished_text.strip():
        return
    try:
        # Cùng convention dir với glossary: story_data/story_memory/{story_id}-{slug}/
        memory_dir = glossary_path_for(story_id, slug).parent
        # Bound prompt: đầu + cuối chương đủ cho recap 3 câu.
        text = polished_text.strip()
        if len(text) > 4000:
            text = text[:2800] + "\n[...]\n" + text[-1200:]
        payload = {
            "model": str(getattr(args, "judge_model", "") or args.translate_model or "qwen3:14b"),
            "messages": [{"role": "user", "content": _RECAP_PROMPT.format(chapter_text=text)}],
            "stream": False,
            "options": {"temperature": 0, "num_ctx": 4096},
            "keep_alive": args.keep_alive,
        }
        response = requests.post(
            f"{args.ollama_url.rstrip('/')}/api/chat", json=payload, timeout=args.timeout,
        )
        response.raise_for_status()
        recap = response.json().get("message", {}).get("content", "")
        recap = re.sub(r"<think>.*?</think>", "", recap, flags=re.DOTALL).strip()
        # Bound recap: model lan man → cắt cứng, recap chỉ là context block.
        if len(recap) > 500:
            recap = recap[:500].rsplit(" ", 1)[0] + "…"
        if not recap:
            log(f"[RECAP] ch{chapter_number}: empty recap, skipped")
            return
        if save_chapter_recap(memory_dir, chapter_number, recap):
            log(f"[RECAP] ch{chapter_number}: saved ({len(recap)} chars)")
        else:
            log(f"[RECAP WARN] ch{chapter_number}: cannot write recaps.json (lock/fs) — skipped")
    except Exception as exc:  # noqa: BLE001 — recap là enhancement, không chặn pipeline
        log(f"[RECAP WARN] ch{chapter_number}: {exc}")


class QualityGateError(RuntimeError):
    """Raised when output still has blocking quality issues after all retries.

    run_one() catches this and fails the job — content is NOT written to DB.
    """


def _fix_pronouns_for_genre(text: str, genre: str, output_path: Path | None) -> str:
    """Deterministic post-fix: hắn→anh ta, nàng→cô ấy... cho genre cấm đại từ cổ phong.

    Chạy TRƯỚC quality gate (giảm false gate-fail vì wrong_pronoun) và ghi lại file
    output để file trên disk và DB content luôn cùng một bản — audio pipeline đọc
    polished_text_path khi file tồn tại.
    """
    if not text or genre not in _WRONG_PRONOUN_GENRES:
        return text
    try:
        fixed, n_fixed = _fix_pronouns_in_text(text)
    except Exception as exc:  # noqa: BLE001 — fix là enhancement, không chặn pipeline
        log(f"[PRONOUN_FIX WARN] {exc}")
        return text
    if n_fixed <= 0:
        return text
    log(f"[PRONOUN_FIX] fixed {n_fixed} pronouns ({genre})")
    if output_path is not None:
        try:
            output_path.write_text(fixed.strip() + "\n", encoding="utf-8")
        except OSError as exc:
            log(f"[PRONOUN_FIX WARN] cannot write back {output_path}: {exc}")
    return fixed


def _quality_check(
    text: str,
    genre: str,
    char_map: str,
    label: str,
    *,
    story_id: str = "",
    slug: str = "",
    story_memory_dir: str = "",
    source_text: str = "",
    source_language: str = "",
) -> tuple[list[str], list[str]]:
    """Check quality and log. Returns (blocking, warnings).

    Thin wrapper quanh run_full_quality_check (shared với CLI scanner) — chỉ thêm
    worker-style logging.
    """
    blocking, warnings = run_full_quality_check(
        text,
        genre=genre,
        char_map=char_map,
        story_id=story_id,
        slug=slug,
        story_memory_dir=story_memory_dir,
        source_text=source_text,
        source_language=source_language,
        log=lambda msg: log(f"{msg} ({label})"),
    )
    if blocking:
        log(f"[QUALITY_FAIL] {label}: {', '.join(blocking)}")
    if warnings:
        log(f"[QUALITY_WARN] {label}: {', '.join(warnings)}")
    return blocking, warnings


def _gated_pass(
    run_pass,
    read_output,
    *,
    args: argparse.Namespace,
    genre: str,
    char_map: str,
    label: str,
    story_id: str = "",
    slug: str = "",
    source_text: str = "",
    source_language: str = "",
    cleanup_on_fail: tuple[Path, ...] = (),
) -> str:
    """Run an LLM pass with a chapter-level quality gate.

    - mode=block (default): re-run toàn bộ pass khi còn blocking issues
      (tối đa --chapter-quality-retries lần); hết retry → QualityGateError,
      caller KHÔNG ghi DB. Mỗi lần re-run nhận repair hints cụ thể từ các
      blocking issues của lần trước (run_pass(repair_hints)).
    - mode=warn: check + log nhưng vẫn trả text (hành vi cũ).
    - mode=off: không check deterministic (judge vẫn chạy nếu bật).

    Stage 2 — LLM judge (--llm-judge off|warn|block, default warn): sampled
    semantic QA, chỉ chạy khi deterministic checks pass. judge=block chỉ có
    tác dụng re-run khi quality_gate=block (dùng chung retry budget).

    Chunk-level retry với repair hints vẫn chạy bên trong run_pass như cũ;
    gate này bắt các lỗi sống sót qua chunk retry (cross-chunk, glossary drift).
    """
    mode = str(getattr(args, "quality_gate", "block") or "block").lower()
    judge_mode = str(getattr(args, "llm_judge", "warn") or "warn").lower()
    retries = max(0, int(getattr(args, "chapter_quality_retries", 1)))
    attempts = (retries + 1) if mode == "block" else 1
    text = ""
    blocking: list[str] = []
    repair_hints = ""
    for attempt in range(1, attempts + 1):
        run_pass(repair_hints)
        text = read_output()
        if mode == "off" and judge_mode == "off":
            return text
        if mode == "off":
            blocking = []
        else:
            blocking, _ = _quality_check(
                text,
                genre,
                char_map,
                label,
                story_id=story_id,
                slug=slug,
                story_memory_dir=str(getattr(args, "story_memory_dir", "") or ""),
                source_text=source_text,
                source_language=source_language,
            )

        # Stage 2: LLM judge — chỉ khi deterministic pass, tránh tốn call vô ích.
        if not blocking and judge_mode != "off" and source_text:
            judge = judge_chapter_quality(
                source_text,
                text,
                genre=genre,
                ollama_url=args.ollama_url,
                model=str(getattr(args, "judge_model", "") or args.translate_model or "qwen3:14b"),
                num_ctx=int(getattr(args, "num_ctx", 8192) or 8192),
                timeout=int(getattr(args, "timeout", 600) or 600),
                seed=label,
                attempt=attempt - 1,
            )
            if judge.warnings:
                log(f"[JUDGE_WARN] {label}: {', '.join(judge.warnings)}")
            if judge.issues:
                if judge_mode == "block":
                    blocking = list(judge.issues)
                    log(f"[JUDGE_FAIL] {label}: {', '.join(judge.issues)}")
                else:
                    log(f"[JUDGE_WARN] {label} (major, warn-only): {', '.join(judge.issues)}")

        if not blocking or mode != "block":
            return text
        repair_hints = "\n".join(f"- {issue_to_repair_hint(i)}" for i in blocking)
        if attempt < attempts:
            log(f"[QUALITY_GATE] {label}: re-run {attempt}/{retries} — {', '.join(blocking)}")
    # Xóa output hỏng trên disk — nếu để lại, lần retry job sau sẽ dính nhánh
    # skip-exists và ghi thẳng output hỏng vào DB, bypass toàn bộ gate.
    for p in cleanup_on_fail:
        try:
            p.unlink(missing_ok=True)
            log(f"[QUALITY_GATE] {label}: removed failed output {p}")
        except OSError as exc:
            log(f"[QUALITY_GATE] {label}: cannot remove {p}: {exc}")
    raise QualityGateError(f"quality_gate {label}: {', '.join(blocking)}")


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


def maybe_update_translated_chapter_title(
    job: dict,
    polished_or_translated_text: str,
    args: argparse.Namespace | None = None,
) -> str:
    """Set chapter title from first polished line (TTS-clean). Matches body terminology."""
    from scripts.story_pipeline.translate_chapters_from_db import chapter_title_from_content

    overwrite = getattr(args, "overwrite", False)
    current_title = str(job.get("chapter_title") or "").strip()
    if current_title and not overwrite:
        return current_title

    if getattr(args, "legacy_chapter_title_llm", False):
        payload = job.get("payload") or {}
        source_chapter_title = str(payload.get("source_chapter_title") or "").strip()
        if source_chapter_title and args is not None:
            try:
                from scripts.story_pipeline.translate_chapters_from_db import translate_chapter_title

                translated_title = translate_chapter_title(
                    source_chapter_title,
                    story_metadata_args(args),
                    context=build_metadata_context_for_job(job, args),
                )
                if translated_title:
                    repo.update_chapter_title(job["chapter_id"], translated_title)
                    return translated_title
            except Exception as exc:  # noqa: BLE001
                log(f"[TITLE] legacy LLM chapter title failed ({exc}), using first-line")

    title = chapter_title_from_content(polished_or_translated_text)
    if not title:
        return ""

    repo.update_chapter_title(job["chapter_id"], title)
    return title


def maybe_translate_story_metadata(job: dict, args: argparse.Namespace, overwrite: bool = False) -> None:
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
    if not overwrite and metadata.get("story_metadata_translated_to") == "vi" and story.get("display_title"):
        return

    source_title = str(payload.get("source_story_title") or story.get("original_title") or story.get("title") or "").strip()
    source_author = str(payload.get("source_story_author") or metadata.get("source_author") or story.get("author") or "").strip()
    source_description = str(
        payload.get("source_story_description")
        or metadata.get("source_description")
        or metadata.get("original_description_before_vi_translate")
        or story.get("description")
        or ""
    ).strip()
    if not source_title and not source_author and not source_description:
        return

    meta_args = story_metadata_args(args)
    metadata_context = build_metadata_context_for_job(job, args)
    display_title = translate_story_title(source_title, meta_args, context=metadata_context) if source_title and (overwrite or not story.get("display_title")) else None
    author = translate_story_author(source_author, meta_args) if source_author else None
    description = translate_story_description(source_description, meta_args, context=metadata_context) if source_description else None

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
    is_post_translate = raw_language == "vi" and bool(
        payload.get("is_post_translate") or payload.get("translated_text_path")
    )
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
    if getattr(args, "no_save_files", False) or not raw_output_path:
        # DB-only mode: write to temp, save content to DB, discard files after.
        _tmp_out_dir = tempfile.TemporaryDirectory(prefix="polish_out_")
        _stem = Path(raw_output_path).name if raw_output_path else "chapter.txt"
        output_path = Path(_tmp_out_dir.name) / _stem
    else:
        output_path = Path(raw_output_path)
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Fallback to temp when directory cannot be created (e.g. read-only mount)
            _tmp_out_dir = tempfile.TemporaryDirectory(prefix="polish_out_")
            output_path = Path(_tmp_out_dir.name) / output_path.name
            log(f"[WARN] cannot create output dir, using temp: {output_path}")

    # Source text cho completeness check trong quality gate (length ratio /
    # structure drift — warning-only). Strip noise giống translate pass để ratio
    # so trên cùng nội dung thực.
    try:
        gate_source_text = clean_source_noise(input_path.read_text(encoding="utf-8")).strip()
    except Exception as exc:  # noqa: BLE001 — completeness là enhancement
        log(f"[QUALITY] cannot read source for completeness check: {exc}")
        gate_source_text = ""

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
    # Seed char-map from raw when missing — EN raw_text_content exists right after crawl.
    char_map_seed_source = "raw" if not effective_char_map else char_map_text_source
    effective_char_map = maybe_auto_update_char_map(
        job,
        args,
        slug=job_slug,
        current_chapter=current_chapter,
        existing_char_map=effective_char_map,
        text_source=char_map_seed_source,
        genre=pre_genre,
    )
    if effective_char_map:
        log(f"[CHAR_MAP] {effective_char_map} (story_id={story_id})")
        try:
            cm_issues = validate_char_map(load_char_map(effective_char_map, story_id))
            if cm_issues:
                shown = ", ".join(cm_issues[:8])
                more = f" (+{len(cm_issues) - 8} more)" if len(cm_issues) > 8 else ""
                log(f"[CHARMAP_WARN] {len(cm_issues)} issue(s): {shown}{more}")
        except Exception as exc:  # noqa: BLE001 — validation chỉ là cảnh báo
            log(f"[CHARMAP_WARN] validate error: {exc}")

    # Lookahead: pre-extract characters N chapters ahead so the char-map is
    # already populated when those chapters are translated (sliding window, 1 new
    # chapter per call → minimal overhead). Runs only if char_map already exists.
    if effective_char_map and current_chapter > 0:
        _run_lookahead_char_map(
            job,
            args,
            current_chapter=current_chapter,
            char_map_path=effective_char_map,
            slug=job_slug,
            genre=pre_genre,
            text_source=char_map_text_source,
        )

    genre = resolve_genre(job, effective_char_map)
    if genre:
        log(f"[GENRE] {genre} (story_id={job.get('story_id')})")

    # Auto-glossary: chạy TRƯỚC translate để glossary kịp inject vào chapter này.
    maybe_auto_update_term_glossary(
        job, args, slug=job_slug, current_chapter=current_chapter, genre=genre,
    )

    # Story title/description: sau char-map + glossary seed để metadata dùng cùng context.
    maybe_translate_story_metadata(job, args, overwrite=args.overwrite)

    if output_path.exists() and not args.overwrite:
        # Output cũ cũng phải qua quality gate trước khi ghi DB — output hỏng
        # (từ run cũ hoặc trước khi có gate) sẽ rơi xuống re-run thay vì skip.
        existing_polished = read_formatted_polished_output(output_path, job, write_back=True)
        existing_blocking: list[str] = []
        if str(getattr(args, "quality_gate", "block") or "block").lower() == "block":
            existing_blocking, _ = _quality_check(
                existing_polished,
                genre,
                effective_char_map or "",
                f"existing ch{current_chapter}",
                story_id=story_id,
                slug=job_slug,
                story_memory_dir=str(getattr(args, "story_memory_dir", "") or ""),
                source_text=gate_source_text,
                source_language=raw_language,
            )
        if existing_blocking:
            log(f"[QUALITY_GATE] existing output failed check → re-running pass: {output_path}")
            output_path.unlink(missing_ok=True)
        else:
            log(f"[SKIP] output exists: {output_path}")
            translated_path = None
            if raw_language in {"zh", "cn", "ko", "kr", "en"}:
                translated_path = (
                    payload.get("translated_text_path")
                    or (Path(args.translated_output_root) / story_slug_for_job(job, input_path) / output_path.name).as_posix()
                )
            existing_translated = (
                read_formatted_output(Path(translated_path), job, write_back=True, label="translated")
                if translated_path and Path(translated_path).exists()
                else None
            )
            repo.update_chapter_text_outputs(
                job["chapter_id"],
                translated_text_path=translated_path,
                polished_text_path=output_path.as_posix() if _tmp_out_dir is None else None,
                translated_text_content=existing_translated,
                polished_text_content=existing_polished,
            )
            if existing_polished:
                maybe_update_translated_chapter_title(job, existing_polished, args=args)
            # Codex finding 3: nếu recap chưa có (worker crash sau write text nhưng trước recap),
            # backfill từ existing_polished trước khi complete job.
            maybe_update_chapter_recap(
                job,
                args,
                slug=job_slug,
                chapter_number=current_chapter,
                polished_text=existing_polished,
                genre=genre,
            )
            repo.complete_story_job(job["id"], result_payload={"skipped": "output_exists"})
            return

    def _build_args_with_char_map(
        model: str, max_chars: int, genre: str = "", repair_hints: str = ""
    ) -> Namespace:
        ns = build_args(args, model, max_chars, genre, story_id=story_id, story_slug=job_slug)
        ns.char_map_file = effective_char_map
        ns.chapter_repair_hints = repair_hints
        # Cho story memory chọn recap các chương < current (continuity qua chương).
        # --no-chapter-recap tắt cả inject (current_chapter=0 → bỏ recap block).
        ns.current_chapter = 0 if getattr(args, "no_chapter_recap", False) else current_chapter
        return ns

    if raw_language == "vi":
        model = job.get("model") or args.vi_model
        max_chars = int(payload.get("polish_max_chars_per_chunk") or args.polish_max_chars_per_chunk)
        _check_resources(args, label="polish")
        polished_text_content = _gated_pass(
            lambda hints: polish_file(
                input_path,
                output_path,
                _build_args_with_char_map(model, max_chars, genre, hints),
            ),
            lambda: _fix_pronouns_for_genre(
                read_formatted_polished_output(output_path, job, write_back=True), genre, output_path,
            ),
            args=args,
            genre=genre,
            char_map=effective_char_map or "",
            label=f"vi ch{current_chapter}",
            story_id=story_id,
            slug=job_slug,
            source_text=gate_source_text,
            source_language="vi",
            cleanup_on_fail=(output_path,),
        )
        repo.update_chapter_text_outputs(
            job["chapter_id"],
            polished_text_path=output_path.as_posix() if _tmp_out_dir is None else None,
            polished_text_content=polished_text_content,
            clear_audio=args.overwrite,
        )
    elif args.single_pass and raw_language == "en":
        # Single-pass EN→VI: translate + polish in one Ollama call.
        single_pass_max_chars = int(payload.get("translate_max_chars_per_chunk") or args.single_pass_max_chars_per_chunk)
        translate_model = job.get("model") or args.translate_model
        _check_resources(args, label="translate")

        def _single_pass_run(hints: str) -> None:
            sp_args = _build_args_with_char_map(translate_model, single_pass_max_chars, genre, hints)
            sp_args.num_ctx = args.single_pass_num_ctx
            single_pass_translate_polish_file(input_path, output_path, sp_args)

        polished_text_content = _gated_pass(
            _single_pass_run,
            lambda: _fix_pronouns_for_genre(
                read_formatted_polished_output(output_path, job, write_back=True), genre, output_path,
            ),
            args=args,
            genre=genre,
            char_map=effective_char_map or "",
            label=f"single-pass ch{current_chapter}",
            story_id=story_id,
            slug=job_slug,
            source_text=gate_source_text,
            source_language=raw_language,
            cleanup_on_fail=(output_path,),
        )
        repo.update_chapter_text_outputs(
            job["chapter_id"],
            polished_text_path=output_path.as_posix() if _tmp_out_dir is None else None,
            polished_text_content=polished_text_content,
            clear_audio=args.overwrite,
        )
        # For single-pass, polished output IS the translated output — use it for chapter title and char-map.
        translated_chapter_title = maybe_update_translated_chapter_title(job, polished_text_content, args=args)
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
        no_save = getattr(args, "no_save_files", False)
        if no_save:
            _trans_tmp = tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w", encoding="utf-8")
            translated_path = Path(_trans_tmp.name)
            _trans_tmp.close()
        else:
            translated_path = Path(args.translated_output_root) / story_slug_for_job(job, input_path) / output_path.name
            translated_path.parent.mkdir(parents=True, exist_ok=True)
        _check_resources(args, label="translate")
        # Gate translate trước: nếu bản dịch còn blocking issues sau retries thì
        # fail sớm, không tốn polish pass. DB write của translated DỜI xuống sau
        # khi polished cũng pass gate — chapter fail không ghi gì vào DB.
        translated_text_content = _gated_pass(
            lambda hints: translate_file(
                input_path,
                translated_path,
                _build_args_with_char_map(translate_model, translate_max_chars, genre, hints),
            ),
            lambda: _fix_pronouns_for_genre(
                read_formatted_output(translated_path, job, write_back=True, label="translated"),
                genre,
                translated_path,
            ),
            args=args,
            genre=genre,
            char_map=effective_char_map or "",
            label=f"translate ch{current_chapter}",
            story_id=story_id,
            slug=job_slug,
            source_text=gate_source_text,
            source_language=raw_language,
            cleanup_on_fail=(translated_path,),
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
            def _copy_pass(_hints: str = "") -> None:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(clean_for_audiobook_tts(translated_text_content).strip() + "\n", encoding="utf-8")
                log(f"[COPY] translated output reused as polished output: {output_path}")

            # Copy là deterministic — không retry; translated đã pass gate nên chỉ check 1 lần.
            _copy_args = Namespace(**vars(args))
            _copy_args.chapter_quality_retries = 0
            polished_text_content = _gated_pass(
                _copy_pass,
                lambda: _fix_pronouns_for_genre(
                    read_formatted_polished_output(output_path, job, write_back=True), genre, output_path,
                ),
                args=_copy_args,
                genre=genre,
                char_map=effective_char_map or "",
                label=f"copy ch{current_chapter}",
                story_id=story_id,
                slug=job_slug,
                source_text=gate_source_text,
                source_language=raw_language,
                cleanup_on_fail=(output_path, translated_path) if no_save else (output_path,),
            )
        else:
            # Unload translate model trước khi load polish model để tránh chiếm VRAM cùng lúc.
            if translate_model != args.vi_model:
                unload_ollama_model(args.ollama_url, translate_model)
            _check_resources(args, label="polish", unloaded_model=translate_model if translate_model != args.vi_model else "")
            polished_text_content = _gated_pass(
                lambda hints: polish_file(
                    translated_path,
                    output_path,
                    _build_args_with_char_map(args.vi_model, polish_max_chars, genre, hints),
                ),
                lambda: _fix_pronouns_for_genre(
                    read_formatted_polished_output(output_path, job, write_back=True), genre, output_path,
                ),
                args=args,
                genre=genre,
                char_map=effective_char_map or "",
                label=f"two-pass ch{current_chapter}",
                story_id=story_id,
                slug=job_slug,
                source_text=gate_source_text,
                source_language=raw_language,
                cleanup_on_fail=(output_path, translated_path) if no_save else (output_path,),
            )
        # Discard temp translated file only after polish finishes reading it.
        if no_save:
            translated_path.unlink(missing_ok=True)
        repo.update_chapter_text_outputs(
            job["chapter_id"],
            translated_text_path=None if no_save else translated_path.as_posix(),
            polished_text_path=output_path.as_posix() if _tmp_out_dir is None else None,
            translated_text_content=translated_text_content,
            polished_text_content=polished_text_content,
            clear_audio=args.overwrite,
        )
        translated_chapter_title = maybe_update_translated_chapter_title(job, polished_text_content, args=args)

    # Incremental char-map update: extract nhân vật mới từ chapter vừa polished.
    # Chạy sau mỗi chapter để char-map dần dày context cho các chapter tiếp theo.
    polished_text_content = locals().get("polished_text_content") or ""
    if polished_text_content and current_chapter > 0:
        _run_incremental_char_map(
            polished_text=polished_text_content,
            chapter_num=current_chapter,
            char_map_path=effective_char_map or "",
            story_id=story_id,
            slug=job_slug,
            genre=genre,
            args=args,
        )
        # Rolling recap: chương sau sẽ thấy tóm tắt chương này trong prompt.
        maybe_update_chapter_recap(
            job,
            args,
            slug=job_slug,
            chapter_number=current_chapter,
            polished_text=polished_text_content,
            genre=genre,
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
    parser.add_argument(
        "--story-id",
        action="append",
        default=[],
        help="Only claim jobs for this story id. Repeat for multiple stories.",
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
    parser.add_argument("--max-quality-retries", type=int, default=2,
                        help="Số lần retry khi chunk polish fail quality check (CJK/EN còn sót, sai đại từ...). Default: 2.")
    parser.add_argument("--quality-gate", choices=("block", "warn", "off"), default="block",
                        help="Chapter-level quality gate trước khi ghi DB. block: còn blocking issues sau retries → fail job, KHÔNG ghi DB (default). warn: chỉ log, vẫn ghi (hành vi cũ). off: không check.")
    parser.add_argument("--chapter-quality-retries", type=int, default=1,
                        help="Số lần re-run TOÀN BỘ pass (translate/polish) khi chapter fail quality gate. Default: 1.")
    parser.add_argument("--llm-judge", choices=("off", "warn", "block"), default="warn",
                        help="LLM judge (sampled semantic QA) sau khi deterministic checks pass. "
                             "warn: log major issues, vẫn ghi DB (default — đang calibrate). "
                             "block: re-run pass với repair hints, chỉ có tác dụng khi --quality-gate block. off: tắt.")
    parser.add_argument("--judge-model", default="",
                        help="Model cho LLM judge. Mặc định dùng --translate-model.")
    parser.add_argument("--no-auto-glossary", action="store_true",
                        help="Tắt auto seed/update terminology glossary (story memory).")
    parser.add_argument(
        "--no-chunk-glossary",
        action="store_true",
        help="Tắt per-chunk glossary supplement trong translate (Phase 2 resolver).",
    )
    parser.add_argument(
        "--legacy-chapter-title-llm",
        action="store_true",
        help="Dùng LLM dịch chapter title từ source EN (legacy). Mặc định: lấy dòng đầu polished.",
    )
    parser.add_argument("--no-chapter-recap", action="store_true",
                        help="Tắt rolling recap (tóm tắt chương trước inject vào prompt chương sau).")
    parser.add_argument("--glossary-update-interval", type=int, default=20,
                        help="Incremental glossary update mỗi N chapters. Default: 20.")
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
        default=8192,
        help="num_ctx cho single-pass mode (default 8192; single-pass có prompt lớn nhất — translate+polish+story memory).",
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
        help="Tắt tất cả auto char-map (cả batch lẫn incremental).",
    )
    parser.add_argument(
        "--no-batch-char-map",
        action="store_true",
        default=bool(int(os.environ.get("POLISH_NO_BATCH_CHAR_MAP", "1"))),
        help=(
            "Tắt batch char-map builder (build_char_map_from_story.py). "
            "Incremental per-chapter vẫn chạy sau mỗi chapter polish xong. Default: ON. "
            "Tắt bằng POLISH_NO_BATCH_CHAR_MAP=0 hoặc bỏ flag khi cần full rescan."
        ),
    )
    parser.add_argument(
        "--char-map-update-interval",
        type=int,
        default=0,
        help=(
            "Batch deep-scan char map sau mỗi N chapter (via build_char_map_from_story). "
            "0 (default) = tắt batch — chỉ dùng incremental per-chapter update. "
            "Dùng khi cần re-scan nhiều chapter cùng lúc."
        ),
    )
    parser.add_argument(
        "--no-incremental-char-map",
        action="store_true",
        help=(
            "Tắt incremental char-map update per-chapter. "
            "Char-map chỉ được tạo/cập nhật qua --char-map-update-interval (batch) "
            "hoặc manual CLI extract_char_map.py."
        ),
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
    parser.add_argument(
        "--char-map-lookahead",
        type=int,
        default=10,
        help=(
            "Số chapter raw pre-extract trước chapter hiện tại (sliding window). "
            "VD: 10 → khi dịch ch11, char-map đã có raw data đến ch21. "
            "0 = tắt lookahead. Default: 10."
        ),
    )
    parser.add_argument(
        "--char-map-timeout",
        type=int,
        default=90,
        help="Timeout (giây) cho mỗi LLM call trong char-map update (incremental hoặc batch). Default: 90.",
    )
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
    parser.add_argument(
        "--no-save-files",
        action="store_true",
        default=bool(int(os.environ.get("POLISH_NO_SAVE_FILES", "1"))),
        help="DB-only mode: write translated/polished output to temp, save content to DB, discard files. Default: ON.",
    )
    args = parser.parse_args()

    if args.workers < 1:
        raise SystemExit("--workers must be >= 1")

    stale_reset = repo.reset_stale_running_jobs("polish_chapter", stale_after_minutes=120)
    if stale_reset:
        log(f"[STARTUP] reset {stale_reset} stale running polish jobs back to pending")

    batch_size = args.batch_size or args.workers
    source_label = ",".join(args.source_code) if args.source_code else "all"
    story_label = ",".join(args.story_id) if args.story_id else "all"
    log(
        f"worker={args.worker_id}, workers={args.workers}, batch_size={batch_size}, "
        f"source={source_label}, story={story_label}"
    )

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
                    story_ids=args.story_id,
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
                        f"type=polish_chapter source={source_label} story={story_label} "
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
