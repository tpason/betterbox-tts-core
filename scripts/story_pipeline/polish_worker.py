#!/usr/bin/env python3
from __future__ import annotations

import argparse
import socket
import subprocess
import sys
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
from translate_chapter_texts_ollama import translate_file


def story_slug_for_job(job: dict, input_path: Path) -> str:
    payload = job.get("payload") or {}
    return str(payload.get("story_slug") or input_path.parent.name)


def build_args(args: argparse.Namespace, model: str, max_chars: int, genre: str = "") -> Namespace:
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
        char_map_file=getattr(args, "char_map_file", ""),
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


def maybe_auto_update_char_map(
    job: dict,
    args: argparse.Namespace,
    *,
    slug: str,
    current_chapter: int,
    existing_char_map: str,
) -> str:
    """Create/update char map opportunistically. Never fail the story job."""
    if getattr(args, "no_auto_char_map", False):
        return existing_char_map
    story_id = str(job.get("story_id") or "")
    if not story_id:
        return existing_char_map

    try:
        story = repo.get_story_by_id(story_id)
        metadata = story.get("metadata") or {}
        updated_to = int(metadata.get("char_map_updated_to_chapter") or 0)
    except Exception:
        updated_to = 0

    should_create = not existing_char_map
    interval = int(getattr(args, "char_map_update_interval", 150) or 0)
    should_update = bool(existing_char_map and interval > 0 and current_chapter >= updated_to + interval)
    if not should_create and not should_update:
        return existing_char_map

    cmd = [
        sys.executable,
        str(SCRIPT_DIR / "extract_char_map.py"),
        "--story-id",
        story_id,
        "--sample-chapters",
        str(max(1, int(getattr(args, "char_map_sample_chapters", 30) or 30))),
        "--model",
        str(getattr(args, "char_map_model", "") or getattr(args, "vi_model", "qwen3:14b")),
        "--ollama-url",
        args.ollama_url,
        "--timeout",
        str(max(30, int(getattr(args, "char_map_timeout", 180) or 180))),
    ]
    if should_update:
        from_chapter = max(1, updated_to + 1)
        cmd.extend(["--from-chapter", str(from_chapter), "--to-chapter", str(current_chapter), "--append-only"])

    reason = "create" if should_create else f"update from ch{updated_to + 1:04d} to ch{current_chapter:04d}"
    log(f"[CHAR_MAP] auto {reason} story_id={story_id} slug={slug}")
    try:
        result = subprocess.run(
            cmd,
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=max(60, int(getattr(args, "char_map_timeout", 180) or 180) + 60),
        )
        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "").strip()[-1000:]
            log(f"[CHAR_MAP WARN] auto extract failed rc={result.returncode}: {tail}")
            return existing_char_map
        refreshed = find_char_map_file(story_id=story_id, slug=slug)
        if refreshed:
            log(f"[CHAR_MAP] ready {refreshed}")
            return refreshed
    except Exception as exc:
        log(f"[CHAR_MAP WARN] auto extract failed: {type(exc).__name__}: {exc}")
    return existing_char_map


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


def process_job(job: dict, args: argparse.Namespace) -> None:
    payload = job.get("payload") or {}
    raw_language = (payload.get("raw_language") or "vi").lower()
    input_path = Path(job["input_path"])
    output_path = Path(job["output_path"])

    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    maybe_translate_story_metadata(job, args)

    # Auto-resolve char map: payload > --char-map-file arg > convention-based lookup
    story_id = str(job.get("story_id") or "")
    job_slug = story_slug_for_job(job, input_path)
    effective_char_map = (
        payload.get("char_map_file")
        or getattr(args, "char_map_file", "")
        or find_char_map_file(story_id=story_id, slug=job_slug)
    )
    effective_char_map = maybe_auto_update_char_map(
        job,
        args,
        slug=job_slug,
        current_chapter=int(payload.get("chapter_number") or 0),
        existing_char_map=effective_char_map,
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
            polished_text_path=output_path.as_posix(),
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
        ns = build_args(args, model, max_chars, genre)
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
        repo.update_chapter_text_outputs(
            job["chapter_id"],
            polished_text_path=output_path.as_posix(),
            polished_text_content=polished_text_content,
        )
    else:
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
        repo.update_chapter_text_outputs(
            job["chapter_id"],
            translated_text_path=translated_path.as_posix(),
            polished_text_path=output_path.as_posix(),
            translated_text_content=translated_text_content,
            polished_text_content=polished_text_content,
        )

    repo.complete_story_job(
        job["id"],
        result_payload={
            "output_path": output_path.as_posix(),
            "raw_language": raw_language,
            "translated_chapter_title": locals().get("translated_chapter_title") or None,
        },
    )
    log(f"[DONE] {job['id']} -> {output_path}")


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
    parser.add_argument("--translate-model", default="translategemma:12b")
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
    parser.add_argument("--char-map-sample-chapters", type=int, default=30)
    parser.add_argument("--char-map-model", default="", help="Model dùng riêng để extract char map; mặc định dùng --vi-model.")
    parser.add_argument("--char-map-timeout", type=int, default=180)
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
        help="CPU% tối đa cho phép trước khi gọi model. Nếu vượt ngưỡng, worker sẽ chờ.",
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
