#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from story_db.story_pipeline_db.db import connect


CHAPTER_SUMMARY_PROMPT = """Bạn là biên tập viên nội dung truyện chữ tiếng Việt.

Hãy tóm tắt phần nội dung chapter dưới đây để phục vụ viết mô tả bộ truyện.

Yêu cầu bắt buộc:
- Chỉ trả về 3-5 câu tiếng Việt.
- Không tiết lộ twist lớn, ending, danh tính bí mật, phản diện thật, nhân vật chết/phản bội, hoặc năng lực bí mật.
- Không kể chi tiết diễn biến theo kiểu recap từng cảnh.
- Chỉ giữ premise, nhân vật chính, bối cảnh, conflict chính, tone và hook ban đầu.
- Nếu có chi tiết có vẻ là spoiler, hãy khái quát hóa.
- Không markdown, không tiêu đề, không giải thích.

Thông tin truyện:
- Tên: {title}
- Tác giả: {author}
- Thể loại: {category}
- Chương: {chapter_number} - {chapter_title}

Nội dung chapter:
{chapter_text}
"""

DESCRIPTION_PROMPT = """Bạn là biên tập viên mô tả truyện cho web đọc truyện.

Dựa trên các summary chapter bên dưới, hãy viết mô tả ngắn cho bộ truyện.

Yêu cầu bắt buộc:
- Viết bằng tiếng Việt tự nhiên, hấp dẫn, giống synopsis đăng webnovel/manga/light novel.
- Độ dài khoảng {min_words}-{max_words} từ.
- Không spoil twist lớn, ending, danh tính bí mật, phản diện thật, nhân vật chết/phản bội, hoặc năng lực bí mật.
- Viết như phần giới thiệu ở bìa sau sách: tập trung premise, tone, nhân vật chính, mục tiêu/xung đột và hook.
- Không liệt kê theo bullet.
- Không markdown, không tiêu đề, không giải thích.
- Chỉ trả về đúng phần mô tả.

Thông tin truyện:
- Tên: {title}
- Tác giả: {author}
- Thể loại: {category}
- Trạng thái: {status}

Summary chapter:
{summaries}
"""


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
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def clean_text(value: str | None) -> str:
    text = (value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\ufeff\u200b\u200c\u200d\u2060]", "", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def clean_model_text(value: str, *, max_chars: int = 0) -> str:
    text = clean_text(value)
    text = re.sub(r"^```(?:text|markdown)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    text = re.sub(r"^\s*(?:Mô tả|Description|Synopsis|Tóm tắt)\s*:\s*", "", text, flags=re.IGNORECASE)
    text = text.strip().strip("\"'“”‘’")
    text = re.sub(r"\s+", " ", text).strip()
    if max_chars > 0:
        text = text[:max_chars].rstrip()
    return text


def resolve_project_path(value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    return path


def read_chapter_text(row: dict[str, Any], max_chars: int) -> str:
    for key in ("polished_text_content", "translated_text_content", "raw_text_content"):
        text = clean_text(row.get(key))
        if text:
            return text[:max_chars].strip()

    for key in ("polished_text_path", "translated_text_path", "raw_text_path"):
        path = resolve_project_path(row.get(key))
        if path and path.exists():
            return clean_text(path.read_text(encoding="utf-8", errors="ignore")[:max_chars])

    return ""


def call_ollama_generate(
    *,
    base_url: str,
    model: str,
    prompt: str,
    temperature: float,
    num_ctx: int,
    timeout: int,
    retries: int,
    keep_alive: str,
) -> str:
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_ctx": num_ctx,
        },
        "keep_alive": keep_alive,
    }
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.post(f"{base_url.rstrip('/')}/api/generate", json=payload, timeout=timeout)
            response.raise_for_status()
            text = str(response.json().get("response") or "")
            if not text.strip():
                raise ValueError("Ollama returned empty response")
            return text
        except Exception as exc:
            last_error = exc
            log(f"[WARN] Ollama error attempt={attempt}/{retries}: {exc}")
            if attempt < retries:
                time.sleep(2)
    raise RuntimeError(f"Ollama failed after {retries} retries: {last_error}")


def list_candidate_stories(args: argparse.Namespace) -> list[dict[str, Any]]:
    where = ["s.is_active = TRUE"]
    params: dict[str, Any] = {}

    if not args.overwrite:
        where.append("(s.description IS NULL OR btrim(s.description) = '')")
    if args.story_id:
        where.append("s.id = %(story_id)s")
        params["story_id"] = args.story_id
    if args.story_url:
        where.append("rtrim(s.source_url, '/') = %(story_url)s")
        params["story_url"] = args.story_url.rstrip("/")
    if args.story_title:
        where.append("(s.title ILIKE %(story_title)s OR s.original_title ILIKE %(story_title)s OR s.display_title ILIKE %(story_title)s)")
        params["story_title"] = f"%{args.story_title}%"
    if args.story_slug:
        where.append("(s.metadata->>'slug' = %(story_slug)s OR s.source_url ILIKE %(story_slug_like)s)")
        params["story_slug"] = args.story_slug
        params["story_slug_like"] = f"%{args.story_slug}%"
    if args.source_code:
        where.append("src.code = ANY(%(source_codes)s::text[])")
        params["source_codes"] = args.source_code

    limit_sql = ""
    if args.limit > 0:
        limit_sql = "LIMIT %(limit)s"
        params["limit"] = args.limit

    query = f"""
        SELECT
            s.id,
            COALESCE(NULLIF(s.display_title, ''), s.title) AS title,
            s.original_title,
            s.display_title,
            s.author,
            s.category,
            s.status,
            s.description,
            s.source_url,
            s.metadata,
            src.code AS source_code,
            COUNT(c.id)::int AS chapter_count,
            COUNT(c.id) FILTER (
                WHERE c.polished_text_content IS NOT NULL
                   OR c.translated_text_content IS NOT NULL
                   OR c.raw_text_content IS NOT NULL
                   OR c.polished_text_path IS NOT NULL
                   OR c.translated_text_path IS NOT NULL
                   OR c.raw_text_path IS NOT NULL
            )::int AS readable_chapter_count
        FROM stories s
        JOIN sources src ON src.id = s.source_id
        LEFT JOIN chapters c ON c.story_id = s.id
        WHERE {' AND '.join(where)}
        GROUP BY s.id, src.code
        HAVING COUNT(c.id) FILTER (
            WHERE c.polished_text_content IS NOT NULL
               OR c.translated_text_content IS NOT NULL
               OR c.raw_text_content IS NOT NULL
               OR c.polished_text_path IS NOT NULL
               OR c.translated_text_path IS NOT NULL
               OR c.raw_text_path IS NOT NULL
        ) > 0
        ORDER BY s.updated_at DESC, s.created_at DESC
        {limit_sql}
    """
    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]


def list_story_chapters(story_id: str, args: argparse.Namespace) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "story_id": story_id,
        "sample_chapters": args.sample_chapters,
        "max_chapters": args.max_chapters,
    }
    where = [
        "story_id = %(story_id)s",
        """(
            polished_text_content IS NOT NULL
            OR translated_text_content IS NOT NULL
            OR raw_text_content IS NOT NULL
            OR polished_text_path IS NOT NULL
            OR translated_text_path IS NOT NULL
            OR raw_text_path IS NOT NULL
        )""",
    ]
    if args.from_chapter:
        where.append("chapter_number >= %(from_chapter)s")
        params["from_chapter"] = args.from_chapter
    if args.to_chapter:
        where.append("chapter_number <= %(to_chapter)s")
        params["to_chapter"] = args.to_chapter

    query = f"""
        WITH base AS (
            SELECT
                *,
                row_number() OVER (ORDER BY chapter_number) AS row_number,
                count(*) OVER () AS total_count
            FROM chapters
            WHERE {' AND '.join(where)}
            ORDER BY chapter_number
            LIMIT NULLIF(%(max_chapters)s, 0)
        ),
        bucketed AS (
            SELECT
                *,
                CASE
                    WHEN %(sample_strategy)s = 'spread' AND total_count > %(sample_chapters)s
                    THEN floor((row_number - 1) * %(sample_chapters)s::numeric / total_count)
                    ELSE row_number
                END AS sample_bucket
            FROM base
        ),
        sampled AS (
            SELECT DISTINCT ON (sample_bucket)
                id,
                chapter_number,
                title,
                raw_text_path,
                translated_text_path,
                polished_text_path,
                raw_text_content,
                translated_text_content,
                polished_text_content
            FROM bucketed
            ORDER BY sample_bucket, chapter_number
        )
        SELECT *
        FROM sampled
        ORDER BY chapter_number
        LIMIT %(sample_chapters)s
    """
    params["sample_strategy"] = args.sample_strategy
    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]


def summarize_chapter(story: dict[str, Any], chapter: dict[str, Any], args: argparse.Namespace) -> str:
    chapter_text = read_chapter_text(chapter, args.max_chars_per_chapter)
    if len(chapter_text) < args.min_chapter_chars:
        raise ValueError(
            f"chapter text too short chapter={chapter['chapter_number']} chars={len(chapter_text)}"
        )
    prompt = CHAPTER_SUMMARY_PROMPT.format(
        title=story.get("title") or "",
        author=story.get("author") or "không rõ",
        category=story.get("category") or "không rõ",
        chapter_number=chapter.get("chapter_number"),
        chapter_title=chapter.get("title") or "",
        chapter_text=chapter_text,
    )
    raw = call_ollama_generate(
        base_url=args.ollama_url,
        model=args.model,
        prompt=prompt,
        temperature=args.temperature,
        num_ctx=args.num_ctx,
        timeout=args.ollama_timeout,
        retries=args.ollama_retries,
        keep_alive=args.keep_alive,
    )
    return clean_model_text(raw, max_chars=args.max_summary_chars)


def generate_description(story: dict[str, Any], summaries: list[dict[str, Any]], args: argparse.Namespace) -> str:
    summary_text = "\n".join(
        f"- Chương {item['chapter_number']}: {item['summary']}"
        for item in summaries
        if item.get("summary")
    )
    prompt = DESCRIPTION_PROMPT.format(
        title=story.get("title") or "",
        author=story.get("author") or "không rõ",
        category=story.get("category") or "không rõ",
        status=story.get("status") or "không rõ",
        min_words=args.min_description_words,
        max_words=args.max_description_words,
        summaries=summary_text,
    )
    raw = call_ollama_generate(
        base_url=args.ollama_url,
        model=args.model,
        prompt=prompt,
        temperature=args.temperature,
        num_ctx=args.num_ctx,
        timeout=args.ollama_timeout,
        retries=args.ollama_retries,
        keep_alive=args.keep_alive,
    )
    description = clean_model_text(raw, max_chars=args.max_description_chars)
    if len(description) < args.min_description_chars:
        raise ValueError(f"description too short chars={len(description)}")
    return description


def update_story_description(
    story_id: str,
    description: str,
    *,
    model: str,
    summaries: list[dict[str, Any]],
    overwrite: bool,
) -> None:
    metadata = {
        "description_generated_by": "ollama",
        "description_generation_model": model,
        "description_generated_at": datetime.now().isoformat(timespec="seconds"),
        "description_generation_source": "chapter_summaries",
        "description_generation_chapters": [
            {
                "chapter_number": item["chapter_number"],
                "summary": item["summary"],
            }
            for item in summaries
        ],
    }
    where = "id = %(story_id)s"
    if not overwrite:
        where += " AND (description IS NULL OR btrim(description) = '')"
    with connect() as conn:
        row = conn.execute(
            f"""
            UPDATE stories
            SET description = %(description)s,
                metadata = metadata || %(metadata)s,
                updated_at = now()
            WHERE {where}
            RETURNING id
            """,
            {
                "story_id": story_id,
                "description": description,
                "metadata": Jsonb(metadata),
            },
        ).fetchone()
        if row is None:
            raise ValueError(f"Story skipped or missing during update: {story_id}")


def process_story(story: dict[str, Any], args: argparse.Namespace, *, index: int, total: int) -> bool:
    started = time.monotonic()
    label = f"[{index}/{total}] {story['source_code']} {story['title']} id={story['id']}"
    log(f"[START] {label} chapters={story['readable_chapter_count']}/{story['chapter_count']}")
    try:
        chapters = list_story_chapters(str(story["id"]), args)
        if not chapters:
            raise ValueError("No readable chapters selected")
        summaries: list[dict[str, Any]] = []
        for chapter_index, chapter in enumerate(chapters, start=1):
            log(
                f"[SUMMARY] {label} chapter={chapter['chapter_number']} "
                f"({chapter_index}/{len(chapters)})"
            )
            summary = summarize_chapter(story, chapter, args)
            summaries.append(
                {
                    "chapter_number": int(chapter["chapter_number"]),
                    "chapter_title": chapter.get("title") or "",
                    "summary": summary,
                }
            )
        description = generate_description(story, summaries, args)
        if args.dry_run:
            log(f"[DRY] {label} description={description}")
        else:
            update_story_description(
                str(story["id"]),
                description,
                model=args.model,
                summaries=summaries,
                overwrite=args.overwrite,
            )
            log(f"[DB] updated description chars={len(description)} {label}")
        elapsed = time.monotonic() - started
        log(f"[DONE] {label} elapsed={elapsed:.1f}s")
        return True
    except Exception as exc:
        elapsed = time.monotonic() - started
        log(f"[ERROR] {label} elapsed={elapsed:.1f}s {type(exc).__name__}: {exc}")
        if args.stop_on_error:
            raise
        return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Tạo mô tả story từ chapter summaries bằng Ollama và update stories.description."
    )
    parser.add_argument("--source-code", action="append", default=[], help="Lọc source_code. Có thể truyền nhiều lần.")
    parser.add_argument("--story-id")
    parser.add_argument("--story-url")
    parser.add_argument("--story-title")
    parser.add_argument("--story-slug")
    parser.add_argument("--from-chapter", type=int, default=0)
    parser.add_argument("--to-chapter", type=int, default=0)
    parser.add_argument("--limit", type=int, default=20, help="0 = không giới hạn.")
    parser.add_argument("--overwrite", action="store_true", help="Ghi đè stories.description đã có.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--stop-on-error", action="store_true")
    parser.add_argument("--sample-strategy", choices=("first", "spread"), default="first")
    parser.add_argument("--sample-chapters", type=int, default=8)
    parser.add_argument("--max-chapters", type=int, default=80, help="0 = xét toàn bộ chapter trước khi sample.")
    parser.add_argument("--min-chapter-chars", type=int, default=300)
    parser.add_argument("--max-chars-per-chapter", type=int, default=4500)
    parser.add_argument("--max-summary-chars", type=int, default=900)
    parser.add_argument("--min-description-chars", type=int, default=120)
    parser.add_argument("--max-description-chars", type=int, default=1200)
    parser.add_argument("--min-description-words", type=int, default=90)
    parser.add_argument("--max-description-words", type=int, default=140)
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default="qwen3:14b")
    parser.add_argument("--temperature", type=float, default=0.35)
    parser.add_argument("--num-ctx", type=int, default=8192)
    parser.add_argument("--ollama-timeout", type=int, default=300)
    parser.add_argument("--ollama-retries", type=int, default=3)
    parser.add_argument("--keep-alive", default="24h")
    parser.add_argument(
        "--log-file",
        default="story_data/logs/backfill_story_descriptions_ollama.log",
        help="Ghi mirror console log vào file. Truyền chuỗi rỗng để tắt.",
    )
    args = parser.parse_args()
    configure_logging(args.log_file)

    if args.sample_chapters <= 0:
        raise SystemExit("--sample-chapters phải lớn hơn 0")

    started = time.monotonic()
    log(
        "[RUN] start "
        f"dry_run={args.dry_run} overwrite={args.overwrite} limit={args.limit} "
        f"model={args.model} sample={args.sample_strategy}:{args.sample_chapters} "
        f"log_file={args.log_file or '-'}"
    )
    stories = list_candidate_stories(args)
    log(f"[QUERY] stories={len(stories)}")
    ok = 0
    failed = 0
    for index, story in enumerate(stories, start=1):
        if process_story(story, args, index=index, total=len(stories)):
            ok += 1
        else:
            failed += 1
    elapsed = time.monotonic() - started
    log(f"[RUN] done ok={ok} failed={failed} elapsed={elapsed:.1f}s")


if __name__ == "__main__":
    main()
