#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from story_db.story_pipeline_db import repository as repo
from story_db.story_pipeline_db.db import connect
from scripts.story_pipeline.crawl_story_alternate_sources import parse_catalog_for_source


DEFAULT_BAD_AUTHORS = {
    "",
    "-",
    "--",
    "n/a",
    "na",
    "none",
    "null",
    "unknown",
    "unknow",
    "không rõ",
    "khong ro",
    "đang cập nhật",
    "dang cap nhat",
    "đang update",
    "dang update",
    "tác giả",
    "tac gia",
    "author",
    "updating",
}


def log(message: str) -> None:
    print(message, flush=True)


def compact_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_for_compare(value: str | None) -> str:
    value = compact_text(value).lower()
    replacements = str.maketrans(
        {
            "á": "a",
            "à": "a",
            "ả": "a",
            "ã": "a",
            "ạ": "a",
            "ă": "a",
            "ắ": "a",
            "ằ": "a",
            "ẳ": "a",
            "ẵ": "a",
            "ặ": "a",
            "â": "a",
            "ấ": "a",
            "ầ": "a",
            "ẩ": "a",
            "ẫ": "a",
            "ậ": "a",
            "é": "e",
            "è": "e",
            "ẻ": "e",
            "ẽ": "e",
            "ẹ": "e",
            "ê": "e",
            "ế": "e",
            "ề": "e",
            "ể": "e",
            "ễ": "e",
            "ệ": "e",
            "í": "i",
            "ì": "i",
            "ỉ": "i",
            "ĩ": "i",
            "ị": "i",
            "ó": "o",
            "ò": "o",
            "ỏ": "o",
            "õ": "o",
            "ọ": "o",
            "ô": "o",
            "ố": "o",
            "ồ": "o",
            "ổ": "o",
            "ỗ": "o",
            "ộ": "o",
            "ơ": "o",
            "ớ": "o",
            "ờ": "o",
            "ở": "o",
            "ỡ": "o",
            "ợ": "o",
            "ú": "u",
            "ù": "u",
            "ủ": "u",
            "ũ": "u",
            "ụ": "u",
            "ư": "u",
            "ứ": "u",
            "ừ": "u",
            "ử": "u",
            "ữ": "u",
            "ự": "u",
            "ý": "y",
            "ỳ": "y",
            "ỷ": "y",
            "ỹ": "y",
            "ỵ": "y",
            "đ": "d",
        }
    )
    return value.translate(replacements)


def clean_author(value: str | None) -> str:
    author = compact_text(value)
    author = re.sub(r"^(?:tác giả|tac gia|author)\s*:?\s*", "", author, flags=re.IGNORECASE).strip()
    author = re.sub(r"\s*(?:thể loại|the loai|status|trạng thái|trang thai)\s*:.*$", "", author, flags=re.IGNORECASE).strip()
    author = author.strip(" :-|,.;")
    return compact_text(author)


def is_bad_author(value: str | None, bad_authors: set[str]) -> bool:
    normalized = normalize_for_compare(clean_author(value))
    if normalized in bad_authors:
        return True
    if len(normalized) <= 1:
        return True
    if re.fullmatch(r"[-_/\\.\s]+", normalized or ""):
        return True
    return False


def list_candidate_stories(args: argparse.Namespace, bad_author_values: set[str]) -> list[dict[str, Any]]:
    query = """
        SELECT s.*, src.code AS source_code, src.base_url AS source_base_url
        FROM stories s
        JOIN sources src ON src.id = s.source_id
        WHERE s.is_active = TRUE
          AND s.source_url IS NOT NULL
          AND s.source_url <> ''
    """
    params: list[Any] = []
    if args.source:
        query += " AND src.code = ANY(%s)"
        params.append(args.source)
    if args.title:
        query += " AND (s.title ILIKE %s OR s.original_title ILIKE %s OR s.display_title ILIKE %s)"
        needle = f"%{args.title}%"
        params.extend([needle, needle, needle])
    if not args.all:
        query += " AND (s.author IS NULL OR btrim(s.author) = ''"
        for bad_author in sorted(bad_author_values):
            query += " OR lower(btrim(s.author)) = %s"
            params.append(bad_author)
        query += ")"
    query += " ORDER BY s.updated_at DESC, s.created_at DESC"
    if args.limit > 0:
        query += " LIMIT %s"
        params.append(args.limit)

    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def build_catalog_args(args: argparse.Namespace) -> SimpleNamespace:
    return SimpleNamespace(
        timeout=args.timeout,
        retries=args.retries,
        retry_sleep=args.retry_sleep,
        max_catalog_pages=args.max_catalog_pages,
        from_chapter=0,
        to_chapter=0,
        latest_chapter=0,
        max_chapters=0,
    )


def should_update_author(existing: str | None, discovered: str, args: argparse.Namespace, bad_authors: set[str]) -> bool:
    if not discovered:
        return False
    if args.overwrite:
        return normalize_for_compare(existing) != normalize_for_compare(discovered)
    return is_bad_author(existing, bad_authors)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch story catalog pages and update missing/bad story authors in DB."
    )
    parser.add_argument("--source", nargs="*", default=[], help="Filter source code, ví dụ truyenfull_today lightnovelpub.")
    parser.add_argument("--title", default="", help="Filter title contains.")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--all", action="store_true", help="Scan cả story đã có author. Mặc định chỉ scan author trống/rác.")
    parser.add_argument("--overwrite", action="store_true", help="Cho phép thay author đang có nếu catalog trả author khác.")
    parser.add_argument("--dry-run", action="store_true", help="Chỉ log thay đổi, không update DB.")
    parser.add_argument("--bad-author", nargs="*", default=[], help="Thêm giá trị author xem là rác.")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-sleep", type=float, default=2.0)
    parser.add_argument("--request-delay", type=float, default=1.0)
    parser.add_argument("--max-catalog-pages", type=int, default=1, help="Giới hạn page catalog để lấy metadata, tránh crawl quá sâu.")
    parser.add_argument("--stop-on-error", action="store_true")
    args = parser.parse_args()

    raw_bad_authors = {compact_text(item).lower() for item in DEFAULT_BAD_AUTHORS if compact_text(item)}
    raw_bad_authors.update(compact_text(item).lower() for item in args.bad_author if compact_text(item))
    bad_authors = {normalize_for_compare(item) for item in raw_bad_authors}
    bad_author_values = raw_bad_authors | bad_authors
    stories = list_candidate_stories(args, bad_author_values)
    log(f"[START] candidates={len(stories)} dry_run={args.dry_run} overwrite={args.overwrite}")
    catalog_args = build_catalog_args(args)
    checked = 0
    updated = 0
    skipped = 0
    failed = 0

    for story in stories:
        checked += 1
        story_id = str(story["id"])
        source_code = str(story["source_code"])
        source_url = str(story["source_url"])
        old_author = clean_author(story.get("author"))
        try:
            catalog = parse_catalog_for_source(source_code, source_url, catalog_args)
            new_author = clean_author(catalog.get("author"))
            if should_update_author(old_author, new_author, args, bad_authors):
                log(f"[AUTHOR] {story['title']} | {old_author or '<empty>'} -> {new_author} | {source_code} | {source_url}")
                if not args.dry_run:
                    repo.update_story_author(
                        story_id,
                        new_author,
                        {
                            "author_updated_from_catalog": True,
                            "author_updated_at": datetime.now(timezone.utc).isoformat(),
                            "author_update_source_url": source_url,
                            "previous_author": old_author or None,
                        },
                    )
                updated += 1
            else:
                skipped += 1
                log(f"[SKIP] {story['title']} | old={old_author or '<empty>'} discovered={new_author or '<empty>'}")
        except Exception as exc:
            failed += 1
            log(f"[WARN] failed {story['title']} | {source_code} | {source_url} | {type(exc).__name__}: {exc}")
            if args.stop_on_error:
                raise
        time.sleep(args.request_delay)

    log(f"[DONE] checked={checked} updated={updated} skipped={skipped} failed={failed} dry_run={args.dry_run}")


if __name__ == "__main__":
    main()
