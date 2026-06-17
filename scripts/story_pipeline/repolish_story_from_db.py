#!/usr/bin/env python3
"""
Re-polish (hoặc re-translate+polish) toàn bộ chapters của 1 story đã có trong DB.

Dùng khi muốn chạy lại bước polish với model/prompt mới mà không cần crawl lại.

Use cases:
  1. Re-polish chapters đã translated (input = translated_text_path):
       python scripts/story_pipeline/repolish_story_from_db.py \\
         --story-title "Vĩnh Thoái Hiệp Sĩ" --overwrite

  2. Re-polish story tiếng Việt gốc (input = raw_text_path):
       python scripts/story_pipeline/repolish_story_from_db.py \\
         --story-title "..." --source-vi --overwrite

  3. Re-translate + re-polish toàn bộ (dùng translate_chapters_from_db.py):
       python scripts/story_pipeline/translate_chapters_from_db.py \\
         --story-title "Vĩnh Thoái Hiệp Sĩ" --limit 0 \\
         --overwrite-translation --overwrite-polish --post-translate polish
"""
from __future__ import annotations

import argparse
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from story_db.story_pipeline_db import repository as repo
from story_db.story_pipeline_db.db import connect
from genre_prompts import find_char_map_file, resolve_genre_from_context
from polish_chapter_texts_ollama import polish_file
from reader_content_format import format_polished_content


LOG_FILE: Path | None = None


def configure_logging(log_file: str) -> None:
    global LOG_FILE
    LOG_FILE = Path(log_file) if log_file else None
    if LOG_FILE:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def log(message: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} {message}"
    print(line, flush=True)
    if LOG_FILE:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def resolve_path(value: str | None) -> Path | None:
    if not value:
        return None
    p = Path(value)
    return p if p.is_absolute() else ROOT / p


def story_slug_from_row(row: dict[str, Any]) -> str:
    metadata = row.get("story_metadata") or {}
    if isinstance(metadata, dict) and metadata.get("slug"):
        slug = str(metadata["slug"])
    else:
        from urllib.parse import urlparse
        parsed = urlparse(str(row.get("story_url") or ""))
        slug = parsed.path.rstrip("/").rsplit("/", 1)[-1] or str(row.get("story_title") or "story")
    slug = re.sub(r"\s+", "-", slug.strip().lower())
    slug = re.sub(r"[^a-z0-9À-ỹ-]+", "", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug or "story"


def build_model_args(
    args: argparse.Namespace,
    genre: str = "",
    story_id: str = "",
    story_slug_value: str = "",
) -> argparse.Namespace:
    ns = argparse.Namespace(
        ollama_url=args.ollama_url,
        model=args.vi_model,
        temperature=args.temperature,
        num_ctx=args.num_ctx,
        timeout=args.ollama_timeout,
        retries=args.ollama_retries,
        keep_alive=args.keep_alive,
        max_chars_per_chunk=args.max_chars_per_chunk,
        prompt_profile=args.prompt_profile,
        polish_mode=args.polish_mode,
        min_output_ratio=args.min_output_ratio,
        genre=genre,
        story_id=story_id or str(getattr(args, "story_id", "") or ""),
        story_slug=story_slug_value or str(getattr(args, "story_slug", "") or ""),
        char_map_file=getattr(args, "char_map_file", ""),
        story_memory_dir=getattr(args, "story_memory_dir", ""),
        fail_on_story_memory_issues=getattr(args, "fail_on_story_memory_issues", False),
    )
    return ns


def _metadata_int(metadata: Any, key: str) -> int:
    if not isinstance(metadata, dict):
        return 0
    try:
        return int(metadata.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _run_char_map_extract(cmd: list[str]) -> bool:
    log("[CHAR_MAP] command: " + " ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            log(f"[CHAR_MAP EXTRACT] {line}")
    rc = proc.wait()
    if rc != 0:
        log(f"[CHAR_MAP WARN] extract_char_map failed rc={rc}")
        return False
    return True


def ensure_char_map_for_story(
    row: dict[str, Any],
    args: argparse.Namespace,
    *,
    max_chapter: int,
) -> str:
    if getattr(args, "char_map_file", ""):
        explicit = args.char_map_file
        log(f"[CHAR_MAP] using explicit file: {explicit}")
        return explicit

    story_id = str(row.get("story_id") or "")
    slug = story_slug_from_row(row)
    existing = find_char_map_file(story_id=story_id, slug=slug)
    if existing and getattr(args, "char_map_update_interval", 150) <= 0:
        log(f"[CHAR_MAP] existing: {existing} (auto-update disabled)")
        return existing

    if getattr(args, "no_auto_char_map", False):
        if existing:
            log(f"[CHAR_MAP] existing: {existing}")
        else:
            log(f"[CHAR_MAP] missing for story_id={story_id} slug={slug}; auto-create disabled")
        return existing

    model = getattr(args, "char_map_model", "") or args.vi_model
    configured_source = str(getattr(args, "char_map_text_source", "auto") or "auto").strip().lower()
    if configured_source in {"raw", "translated", "polished"}:
        text_source = configured_source
    else:
        text_source = "raw" if args.source_vi else "translated"
    base_cmd = [
        sys.executable,
        str(SCRIPT_DIR / "build_char_map_from_story.py"),
        "--story-id",
        story_id,
        "--text-source",
        text_source,
        "--ollama-url",
        args.ollama_url,
        "--model",
        model,
        "--timeout",
        str(getattr(args, "char_map_timeout", 180)),
        "--min-frequency",
        str(max(1, int(getattr(args, "char_map_min_frequency", 1) or 1))),
    ]

    if not existing:
        log(
            f"[CHAR_MAP] missing story_id={story_id} slug={slug}; "
            f"auto-create before polish (text_source={text_source}, model={model})"
        )
        if _run_char_map_extract(base_cmd):
            refreshed = find_char_map_file(story_id=story_id, slug=slug)
            if refreshed:
                log(f"[CHAR_MAP] ready: {refreshed}")
                return refreshed
        log("[CHAR_MAP WARN] continue polishing without char map")
        return ""

    updated_to = (
        _metadata_int(row.get("story_metadata"), "char_map_updated_to_chapter")
        or _metadata_int(row.get("story_metadata"), "char_map_scanned_to_chapter")
        or _metadata_int(row.get("story_metadata"), "char_map_sampled_to_chapter")
    )
    interval = int(getattr(args, "char_map_update_interval", 150) or 0)
    if updated_to and interval > 0 and max_chapter >= updated_to + interval:
        update_cmd = base_cmd + [
            "--from-chapter",
            str(updated_to + 1),
            "--to-chapter",
            str(max_chapter),
            "--append-only",
        ]
        log(
            f"[CHAR_MAP] existing but stale: {existing}; "
            f"updated_to={updated_to}, target={max_chapter}, interval={interval}. Auto-update..."
        )
        if _run_char_map_extract(update_cmd):
            refreshed = find_char_map_file(story_id=story_id, slug=slug) or existing
            log(f"[CHAR_MAP] updated: {refreshed}")
            return refreshed
        log(f"[CHAR_MAP WARN] auto-update failed; using existing: {existing}")
        return existing

    if existing:
        if updated_to:
            log(f"[CHAR_MAP] existing: {existing} (updated_to={updated_to})")
        else:
            log(f"[CHAR_MAP] existing: {existing} (metadata updated_to missing; skip auto-update)")
    return existing


def prepare_char_maps(candidates: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, str]:
    by_story: dict[str, list[dict[str, Any]]] = {}
    for row in candidates:
        by_story.setdefault(str(row["story_id"]), []).append(row)

    resolved: dict[str, str] = {}
    for story_id, rows in by_story.items():
        max_chapter = max(int(r["chapter_number"]) for r in rows)
        resolved[story_id] = ensure_char_map_for_story(rows[0], args, max_chapter=max_chapter)
    return resolved


def list_candidates(args: argparse.Namespace) -> list[dict[str, Any]]:
    query = """
        SELECT
            c.id AS chapter_id,
            c.story_id,
            c.chapter_number,
            c.title AS chapter_title,
            c.raw_language,
            c.raw_text_path,
            c.translated_text_path,
            c.polished_text_path,
            c.raw_text_content,
            c.translated_text_content,
            c.polished_text_content,
            c.is_downloaded,
            c.is_translated,
            c.is_polished,
            s.title AS story_title,
            s.display_title AS story_display_title,
            s.source_url AS story_url,
            s.language AS story_language,
            s.category AS story_category,
            s.metadata AS story_metadata,
            src.code AS source_code
        FROM chapters c
        JOIN stories s ON s.id = c.story_id
        JOIN sources src ON src.id = s.source_id
        WHERE s.is_active = TRUE
          AND c.is_downloaded = TRUE
    """
    params: dict[str, Any] = {}

    if args.source_vi:
        # Polish từ raw text (story tiếng Việt gốc)
        query += " AND c.raw_text_content IS NOT NULL"
    else:
        # Polish từ translated text
        query += " AND c.translated_text_content IS NOT NULL"

    if not args.overwrite:
        query += " AND c.is_polished = FALSE"

    if args.story_id:
        query += " AND s.id = %(story_id)s"
        params["story_id"] = args.story_id
    if args.story_url:
        query += " AND rtrim(s.source_url, '/') = %(story_url)s"
        params["story_url"] = args.story_url.rstrip("/")
    if args.story_title:
        query += " AND (s.title ILIKE %(story_title)s OR s.original_title ILIKE %(story_title)s OR s.display_title ILIKE %(story_title)s)"
        params["story_title"] = f"%{args.story_title}%"
    if args.story_slug:
        query += " AND (s.metadata->>'slug' = %(story_slug)s OR s.source_url ILIKE %(story_slug_like)s)"
        params["story_slug"] = args.story_slug
        params["story_slug_like"] = f"%{args.story_slug}%"
    if args.source_code:
        query += " AND src.code = ANY(%(source_codes)s)"
        params["source_codes"] = args.source_code
    if args.from_chapter:
        query += " AND c.chapter_number >= %(from_chapter)s"
        params["from_chapter"] = args.from_chapter
    if args.to_chapter:
        query += " AND c.chapter_number <= %(to_chapter)s"
        params["to_chapter"] = args.to_chapter

    query += " ORDER BY s.title, c.chapter_number"
    if args.limit > 0:
        query += " LIMIT %(limit)s"
        params["limit"] = args.limit

    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]


def process_row(row: dict[str, Any], args: argparse.Namespace, *, index: int, total: int) -> bool:
    started = time.monotonic()
    slug = story_slug_from_row(row)
    label = f"[{index}/{total}] {row['source_code']} {slug} chapter{int(row['chapter_number']):04d}"

    # Resolve input content from DB
    if args.source_vi:
        input_content = row.get("raw_text_content") or ""
        if not input_content:
            legacy = resolve_path(row.get("raw_text_path"))
            if legacy and legacy.exists():
                input_content = legacy.read_text(encoding="utf-8")
    else:
        input_content = row.get("translated_text_content") or ""
        if not input_content:
            legacy = resolve_path(row.get("translated_text_path"))
            if legacy and legacy.exists():
                input_content = legacy.read_text(encoding="utf-8")

    if not input_content:
        log(f"[SKIP] {label} — không có input content")
        return False

    if args.dry_run:
        log(f"[DRY] {label} chars={len(input_content)} model={args.vi_model}")
        return True

    story_id = str(row.get("story_id") or "")
    char_map_by_story = getattr(args, "_char_map_by_story", {}) or {}
    effective_char_map = (
        getattr(args, "char_map_file", "")
        or char_map_by_story.get(story_id, "")
        or find_char_map_file(story_id=story_id, slug=slug)
    )

    log(f"[START] {label} chars={len(input_content)} model={args.vi_model} prompt={args.prompt_profile}"
        + (f" char_map={effective_char_map}" if effective_char_map else ""))

    _tmp_input: Path | None = None
    _tmp_output: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w", encoding="utf-8") as f:
            f.write(input_content)
            _tmp_input = Path(f.name)
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            _tmp_output = Path(f.name)

        category = str((row.get("story_metadata") or {}).get("category") or row.get("story_category") or "")
        raw_language = str(row.get("raw_language") or row.get("story_language") or "")
        source_code = str(row.get("source_code") or "")
        genre = args.genre or resolve_genre_from_context(
            category,
            raw_language=raw_language,
            source_code=source_code,
            char_map_file=effective_char_map,
        )
        if genre:
            log(f"[GENRE] {genre}")

        model_args = build_model_args(args, genre, story_id=story_id, story_slug_value=slug)
        model_args.char_map_file = effective_char_map
        polish_file(_tmp_input, _tmp_output, model_args)

        polished_content = _tmp_output.read_text(encoding="utf-8") if _tmp_output.exists() else None
        if polished_content:
            polished_content = format_polished_content(polished_content, {
                "chapter_title": row.get("chapter_title") or "",
            })

        repo.update_chapter_text_outputs(
            row["chapter_id"],
            polished_text_path=None,
            polished_text_content=polished_content,
        )
        elapsed = time.monotonic() - started
        log(f"[DONE] {label} elapsed={elapsed:.1f}s chars={len(polished_content or '')}")
        return True
    except Exception as exc:
        elapsed = time.monotonic() - started
        log(f"[ERROR] {label} elapsed={elapsed:.1f}s {type(exc).__name__}: {exc}")
        if args.stop_on_error:
            raise
        return False
    finally:
        for _tmp in (_tmp_input, _tmp_output):
            if _tmp:
                _tmp.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-polish chapters của 1 story từ DB (dùng translated_text hoặc raw_text làm input).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Bộ lọc story
    filter_group = parser.add_argument_group("bộ lọc story / chapter")
    filter_group.add_argument("--story-title", default="", help="Lọc theo tên truyện (ILIKE, không cần chính xác).")
    filter_group.add_argument("--story-id", default="", help="Lọc theo UUID story.")
    filter_group.add_argument("--story-url", default="", help="Lọc theo source_url chính xác.")
    filter_group.add_argument("--story-slug", default="", help="Lọc theo slug trong metadata hoặc URL.")
    filter_group.add_argument("--source-code", action="append", default=[], help="Lọc theo source_code. Có thể truyền nhiều lần.")
    filter_group.add_argument("--from-chapter", type=int, default=0, help="Chapter bắt đầu (bao gồm).")
    filter_group.add_argument("--to-chapter", type=int, default=0, help="Chapter kết thúc (bao gồm). 0 = không giới hạn.")
    filter_group.add_argument("--limit", type=int, default=0, help="Số chapters tối đa. 0 = không giới hạn.")

    # Chế độ
    mode_group = parser.add_argument_group("chế độ xử lý")
    mode_group.add_argument(
        "--source-vi",
        action="store_true",
        help="Input là raw_text_path (story tiếng Việt gốc). Mặc định: dùng translated_text_path.",
    )
    mode_group.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-polish kể cả chapter đã có polished output.",
    )
    mode_group.add_argument("--dry-run", action="store_true", help="In danh sách sẽ xử lý, không thực thi.")
    mode_group.add_argument("--stop-on-error", action="store_true")

    # Output
    parser.add_argument("--polished-output-root", default="story_data/polished")

    # Model
    model_group = parser.add_argument_group("model / ollama")
    model_group.add_argument("--vi-model", default=os.environ.get("POLISH_VI_MODEL", "qwen3:14b"))
    model_group.add_argument("--ollama-url", default=os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434"))
    model_group.add_argument("--temperature", type=float, default=0.25)
    model_group.add_argument("--num-ctx", type=int, default=8192)
    model_group.add_argument("--ollama-timeout", type=int, default=300)
    model_group.add_argument("--ollama-retries", type=int, default=3)
    model_group.add_argument("--keep-alive", default="30m")
    model_group.add_argument("--max-chars-per-chunk", type=int, default=4000)
    model_group.add_argument(
        "--min-output-ratio",
        type=float,
        default=0.70,
        help=(
            "Ngưỡng fallback: nếu output ngắn hơn X%% input (ký tự, bỏ whitespace), dùng lại chunk raw. "
            "0.70 = an toàn (polish tốt được rút ngắn); 0 = tắt kiểm tra."
        ),
    )
    model_group.add_argument(
        "--char-map-file",
        default="",
        help=(
            "File nhân vật (character map) chứa thông tin giọng nói, giới tính, xưng hô. "
            "Inject vào system prompt. VD: story_data/char_maps/21180-vinh-thoai-hiep-si.txt"
        ),
    )
    model_group.add_argument(
        "--story-memory-dir",
        default="",
        help=(
            "Root story memory hoặc thư mục memory cụ thể. Nếu bỏ trống, script tự tìm theo "
            "story_data/story_memory/{story_id}-{slug}."
        ),
    )
    model_group.add_argument(
        "--fail-on-story-memory-issues",
        action="store_true",
        help="Nếu story memory QA phát hiện lỗi tên/thuật ngữ/register, fail chapter thay vì chỉ cảnh báo.",
    )
    model_group.add_argument(
        "--no-auto-char-map",
        action="store_true",
        help="Không tự tạo/cập nhật character map khi thiếu hoặc stale.",
    )
    model_group.add_argument(
        "--char-map-update-interval",
        type=int,
        default=150,
        help="Tự append-update char map khi range hiện tại vượt metadata updated_to ít nhất N chapter. 0 = tắt update, vẫn auto-create khi thiếu.",
    )
    model_group.add_argument(
        "--char-map-sample-chapters",
        type=int,
        default=30,
        help="Legacy no-op: giữ để tương thích CLI cũ.",
    )
    model_group.add_argument(
        "--char-map-text-source",
        choices=("auto", "raw", "translated", "polished"),
        default="auto",
        help="Nguồn text để auto build char-map. auto=raw khi --source-vi, translated cho truyện dịch.",
    )
    model_group.add_argument(
        "--char-map-min-frequency",
        type=int,
        default=1,
        help="Tần suất tối thiểu để candidate name được gửi vào LLM khi build char-map.",
    )
    model_group.add_argument(
        "--char-map-model",
        default="",
        help="Model dùng riêng để build char map. Mặc định dùng --vi-model để tránh load thêm model khác.",
    )
    model_group.add_argument(
        "--char-map-timeout",
        type=int,
        default=180,
        help="Timeout mỗi request Ollama khi extract char map.",
    )
    model_group.add_argument(
        "--prompt-profile",
        choices=("fast", "full"),
        default="full",
        help="fast = prompt ngắn, nhanh hơn ~30%%; full = prompt chi tiết, chất lượng cao hơn.",
    )
    model_group.add_argument(
        "--polish-mode",
        choices=("llm", "clean"),
        default="llm",
        help="llm = gọi Ollama; clean = chỉ chuẩn hóa TTS, không rewrite.",
    )
    model_group.add_argument(
        "--genre",
        default="",
        help="Thể loại: tien_hiep, huyen_huyen, he_thong, kiem_hiep, do_thi, xuyen_khong, mat_the, vong_du, lang_man, western_fantasy. Mặc định auto-detect từ DB.",
    )

    parser.add_argument(
        "--log-file",
        default="story_data/logs/repolish_story.log",
        help="Ghi mirror log vào file. Truyền chuỗi rỗng để tắt.",
    )
    parser.add_argument("--worker-id", default=f"repolish-{socket.gethostname()}")

    args = parser.parse_args()
    configure_logging(args.log_file)

    if not any([args.story_title, args.story_id, args.story_url, args.story_slug]):
        parser.error("Cần ít nhất một trong: --story-title, --story-id, --story-url, --story-slug")

    started = time.monotonic()
    log(
        f"[RUN] start worker={args.worker_id} story_title={args.story_title or '-'} "
        f"story_id={args.story_id or '-'} source_vi={args.source_vi} "
        f"overwrite={args.overwrite} dry_run={args.dry_run} "
        f"model={args.vi_model} prompt={args.prompt_profile} genre={args.genre or 'auto'} "
        f"limit={args.limit or 'unlimited'}"
    )

    candidates = list_candidates(args)
    log(f"[QUERY] candidates={len(candidates)}")

    if not candidates:
        log("[WARN] Không tìm thấy chapter nào. Kiểm tra --story-title / --overwrite / --source-vi.")
        return

    # In summary story
    seen_stories: set[str] = set()
    for row in candidates:
        sid = str(row["story_id"])
        if sid not in seen_stories:
            seen_stories.add(sid)
            log(
                f"[STORY] {row.get('story_display_title') or row.get('story_title')} "
                f"({row['source_code']}) — {len([r for r in candidates if str(r['story_id']) == sid])} chapters"
            )

    if args.dry_run:
        for i, row in enumerate(candidates, 1):
            slug = story_slug_from_row(row)
            story_id = str(row.get("story_id") or "")
            char_map_file = (
                getattr(args, "char_map_file", "")
                or find_char_map_file(story_id=story_id, slug=slug)
            )
            category = str((row.get("story_metadata") or {}).get("category") or row.get("story_category") or "")
            raw_language = str(row.get("raw_language") or row.get("story_language") or "")
            source_code = str(row.get("source_code") or "")
            genre = args.genre or resolve_genre_from_context(
                category,
                raw_language=raw_language,
                source_code=source_code,
                char_map_file=char_map_file,
            )
            input_chars = len(
                (row.get("raw_text_content") if args.source_vi else row.get("translated_text_content")) or ""
            )
            log(
                f"[DRY] [{i}/{len(candidates)}] chapter{int(row['chapter_number']):04d} "
                f"polished={row.get('is_polished')} genre={genre or '(default)'} "
                f"char_map={char_map_file or '-'} input_chars={input_chars}"
            )
        return

    args._char_map_by_story = prepare_char_maps(candidates, args)

    ok = 0
    failed = 0
    for index, row in enumerate(candidates, start=1):
        if process_row(row, args, index=index, total=len(candidates)):
            ok += 1
        else:
            failed += 1

    elapsed = time.monotonic() - started
    log(f"[RUN] done ok={ok} failed={failed} elapsed={elapsed:.1f}s")


if __name__ == "__main__":
    main()
