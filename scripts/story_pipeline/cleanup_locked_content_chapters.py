#!/usr/bin/env python3
"""Detect and clean up chapters that were saved with paywall/locked content.

Identifies chapters whose raw_text_content matches known lock patterns,
removes the associated txt files from disk, and resets the DB rows to
is_locked=True so they can be re-crawled once unlocked.

Usage (từ Docker — recommended):
  # Dry-run (default): show what would be removed
  docker compose exec story-crawler-scheduler python /app/scripts/story_pipeline/cleanup_locked_content_chapters.py

  # Apply changes
  docker compose exec story-crawler-scheduler python /app/scripts/story_pipeline/cleanup_locked_content_chapters.py --apply

  # Limit to specific source
  docker compose exec story-crawler-scheduler python /app/scripts/story_pipeline/cleanup_locked_content_chapters.py --source skydemonorder --apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from story_db.story_pipeline_db.db import connect  # noqa: E402

# Only patterns that are highly specific to paywall/lock pages and won't appear in normal novel text.
# Patterns like "members only", "premium chapter", "subscribe to read" are too generic
# and cause false positives in royalroad/lightnovelpub content.
LOCK_PATTERNS = [
    # SkyDemonOrder / generic points gate — these phrases only appear on paywall pages
    "unlock this episode",
    "you have 0 points",
    "log in to purchase",
    "log in to subscribe",
    "purchase points to unlock",
    "buy points to unlock",
    # Hard lock pages
    "this chapter is locked",
    "chapter is locked",
    # Vietnamese lock gates (full lock page only — not partial chapter footers)
    "chương này bị khóa",
    "chương đã bị khóa",
    # Chinese
    "章节锁定",
]


def build_where_clause(source_filter: str | None) -> tuple[str, list]:
    conditions = ["c.is_locked = false"]
    params: list = []

    pattern_conditions = []
    for pattern in LOCK_PATTERNS:
        pattern_conditions.append("c.raw_text_content ILIKE %s")
        params.append(f"%{pattern}%")
    conditions.append(f"({' OR '.join(pattern_conditions)})")

    if source_filter:
        conditions.append("src.code = %s")
        params.append(source_filter)

    return " AND ".join(conditions), params


def fetch_affected_chapters(conn, source_filter: str | None) -> list[dict]:
    where, params = build_where_clause(source_filter)
    sql = f"""
        SELECT DISTINCT ON (c.id)
            c.id,
            c.chapter_number,
            c.raw_text_path,
            c.translated_text_path,
            c.polished_text_path,
            c.audio_path,
            s.title AS story_title,
            src.code AS source_code,
            LEFT(c.raw_text_content, 200) AS content_preview
        FROM chapters c
        JOIN stories s ON s.id = c.story_id
        JOIN sources src ON src.id = s.source_id
        WHERE {where}
        ORDER BY c.id, s.title, c.chapter_number
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def delete_file(path: str | None, dry_run: bool) -> bool:
    if not path:
        return False
    p = Path(path)
    if not p.exists():
        return False
    if not dry_run:
        p.unlink()
    return True


def reset_chapter_in_db(conn, chapter_id: str, dry_run: bool) -> None:
    if dry_run:
        return
    sql = """
        UPDATE chapters SET
            is_locked = true,
            lock_reason = 'paywall_content_detected',
            is_downloaded = false,
            is_translated = false,
            is_polished = false,
            is_audio_generated = false,
            raw_text_path = NULL,
            raw_text_content = NULL,
            translated_text_path = NULL,
            translated_text_content = NULL,
            polished_text_path = NULL,
            polished_text_content = NULL,
            reader_formatted_text_content = NULL,
            reader_formatted_content_version = NULL,
            reader_formatted_source = NULL,
            reader_formatted_source_hash = NULL,
            audio_path = NULL,
            downloaded_at = NULL,
            translated_at = NULL,
            polished_at = NULL,
            audio_generated_at = NULL,
            updated_at = NOW()
        WHERE id = %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (chapter_id,))


def main() -> None:
    parser = argparse.ArgumentParser(description="Cleanup chapters with paywall/locked content.")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default: dry-run).")
    parser.add_argument("--source", default="", help="Limit to a specific source code (e.g. skydemonorder).")
    args = parser.parse_args()

    dry_run = not args.apply
    source_filter = args.source or None

    print(f"[MODE] {'DRY-RUN — no changes will be made' if dry_run else 'APPLY — files will be deleted and DB updated'}")
    if source_filter:
        print(f"[FILTER] source={source_filter}")

    with connect() as conn:
        chapters = fetch_affected_chapters(conn, source_filter)
        print(f"\n[FOUND] {len(chapters)} chapters with locked/paywall content\n")

        if not chapters:
            print("Nothing to clean up.")
            return

        by_source: dict[str, int] = {}
        for ch in chapters:
            by_source[ch["source_code"]] = by_source.get(ch["source_code"], 0) + 1
        for src, cnt in sorted(by_source.items(), key=lambda x: -x[1]):
            print(f"  {src}: {cnt} chapters")
        print()

        raw_deleted = 0
        translated_deleted = 0
        polished_deleted = 0
        audio_deleted = 0
        db_updated = 0

        for ch in chapters:
            label = f"{ch['story_title']} ch{ch['chapter_number']:04d} [{ch['source_code']}]"
            preview = (ch["content_preview"] or "").replace("\n", " ").strip()[:100]

            r = delete_file(ch["raw_text_path"], dry_run)
            t = delete_file(ch["translated_text_path"], dry_run)
            p = delete_file(ch["polished_text_path"], dry_run)
            a = delete_file(ch["audio_path"], dry_run)

            raw_deleted += int(r)
            translated_deleted += int(t)
            polished_deleted += int(p)
            audio_deleted += int(a)

            reset_chapter_in_db(conn, ch["id"], dry_run)
            db_updated += 1

            action = "WOULD DELETE" if dry_run else "DELETED"
            files_note = " ".join(filter(None, [
                "raw" if r else None,
                "translated" if t else None,
                "polished" if p else None,
                "audio" if a else None,
            ])) or "no files on disk"
            print(f"[{action}] {label} | files: {files_note}")
            print(f"          preview: {preview!r}")

        if dry_run:
            conn.rollback()

        print(f"\n[SUMMARY] chapters={db_updated} raw_files={raw_deleted} translated={translated_deleted} polished={polished_deleted} audio={audio_deleted}")
        if dry_run:
            print("[DRY-RUN] No changes made. Re-run with --apply to execute.")


if __name__ == "__main__":
    main()
