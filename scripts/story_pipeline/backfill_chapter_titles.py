#!/usr/bin/env python3
"""Backfill chapter titles from source pages.

Finds chapters with bare fallback titles (e.g. "Chapter 527") and fetches
the chapter page to get the real subtitle (e.g. "Chapter 527 - The Ordinary Finn").
Updates both the DB title and the first line of the raw text file on disk.

Currently supports: lightnovelpub (source_url contains lightnovelpub.org)

Usage (from Docker):
  # Dry-run — show what would be updated
  docker compose exec story-crawler-scheduler python /app/scripts/story_pipeline/backfill_chapter_titles.py

  # Apply
  docker compose exec story-crawler-scheduler python /app/scripts/story_pipeline/backfill_chapter_titles.py --apply

  # Limit to specific story
  docker compose exec story-crawler-scheduler python /app/scripts/story_pipeline/backfill_chapter_titles.py \\
    --story-title "Vĩnh Thoái Hiệp Sĩ" --apply
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crawl_lightnovelpub_chapters import extract_chapter_title, fetch_html  # noqa: E402
from story_db.story_pipeline_db.db import connect  # noqa: E402


BARE_TITLE_PATTERN = re.compile(r"^Chapter\s+\d+$", re.IGNORECASE)


def fetch_affected_chapters(conn, story_title: str | None) -> list[dict]:
    conditions = [
        "c.title ~ %s",
        "c.source_url ILIKE %s",
        "c.is_downloaded = true",
    ]
    params: list = [r"^Chapter[[:space:]]+[0-9]+$", "%lightnovelpub%"]
    if story_title:
        conditions.append("s.title ILIKE %s")
        params.append(f"%{story_title}%")

    sql = f"""
        SELECT DISTINCT ON (c.id)
            c.id,
            c.chapter_number,
            c.title AS old_title,
            c.source_url,
            c.raw_text_path,
            s.title AS story_title
        FROM chapters c
        JOIN stories s ON s.id = c.story_id
        WHERE {" AND ".join(conditions)}
        ORDER BY c.id, c.chapter_number
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def update_title_in_db(conn, chapter_id: str, new_title: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE chapters SET title = %s, updated_at = NOW() WHERE id = %s",
            (new_title, chapter_id),
        )


def update_title_in_file(raw_text_path: str, old_title: str, new_title: str) -> bool:
    """Replace the first line of the text file if it matches old_title."""
    path = Path(raw_text_path) if Path(raw_text_path).is_absolute() else ROOT / raw_text_path
    if not path.exists():
        return False
    content = path.read_text(encoding="utf-8")
    # First line should be the title
    lines = content.split("\n", 1)
    if not lines:
        return False
    first_line = lines[0].strip()
    if first_line == old_title:
        new_content = new_title + ("\n" + lines[1] if len(lines) > 1 else "")
        path.write_text(new_content, encoding="utf-8")
        return True
    return False


def fix_files_from_db(conn, story_title: str | None, dry_run: bool) -> dict[str, int]:
    """Fix text files whose first line is still the old bare title but DB already has subtitle."""
    params: list = ["%lightnovelpub%"]
    story_filter = ""
    if story_title:
        story_filter = "AND s.title ILIKE %s"
        params.append(f"%{story_title}%")

    sql = f"""
        SELECT DISTINCT ON (c.id)
            c.id, c.chapter_number, c.title AS db_title,
            c.raw_text_path, s.title AS story_title
        FROM chapters c
        JOIN stories s ON s.id = c.story_id
        WHERE c.source_url ILIKE %s
          AND c.raw_text_path IS NOT NULL
          AND c.title ~ %s
          {story_filter}
        ORDER BY c.id, c.chapter_number
    """
    # title has subtitle: "Chapter N - Something"
    params.insert(1, r"^Chapter[[:space:]]+[0-9]+ - .+")

    updated = skipped = 0
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    for row in rows:
        path = Path(row["raw_text_path"]) if Path(row["raw_text_path"]).is_absolute() else ROOT / row["raw_text_path"]
        if not path.exists():
            skipped += 1
            continue
        content = path.read_text(encoding="utf-8")
        first_line = content.split("\n", 1)[0].strip()
        # Check if file still has bare "Chapter N"
        if not BARE_TITLE_PATTERN.match(first_line):
            skipped += 1
            continue
        db_title = row["db_title"]
        label = f"{row['story_title']} ch{row['chapter_number']:04d}"
        print(f"[FILE] {label}: file={first_line!r} → {db_title!r}")
        if not dry_run:
            new_content = db_title + content[len(first_line):]
            path.write_text(new_content, encoding="utf-8")
            updated += 1
        else:
            updated += 1

    return {"updated": updated, "skipped": skipped}


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill chapter titles from source pages.")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default: dry-run).")
    parser.add_argument("--story-title", default="", help="Filter by story title (partial match).")
    parser.add_argument("--fix-files", action="store_true", help="Fix text files whose DB title already has subtitle but file still has bare title.")
    parser.add_argument("--delay", type=float, default=1.0, help="Delay between requests (seconds).")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--stop-on-error", action="store_true")
    args = parser.parse_args()

    dry_run = not args.apply
    story_filter = args.story_title or None

    print(f"[MODE] {'DRY-RUN' if dry_run else 'APPLY'}")
    if story_filter:
        print(f"[FILTER] story_title contains: {story_filter!r}")

    # --fix-files: update text files from DB titles (no HTTP requests needed)
    if args.fix_files:
        with connect() as conn:
            stats = fix_files_from_db(conn, story_filter, dry_run)
            if dry_run:
                conn.rollback()
        print(f"\n[SUMMARY] files_updated={stats['updated']} skipped={stats['skipped']}")
        if dry_run:
            print("[DRY-RUN] No changes made. Re-run with --apply to execute.")
        return

    with connect() as conn:
        chapters = fetch_affected_chapters(conn, story_filter)
        print(f"\n[FOUND] {len(chapters)} chapters with bare titles\n")

        if not chapters:
            print("Nothing to backfill.")
            return

        updated = skipped = failed = file_updated = 0

        for ch in chapters:
            chapter_id = ch["id"]
            chapter_num = ch["chapter_number"]
            old_title = ch["old_title"]
            source_url = ch["source_url"]
            raw_text_path = ch["raw_text_path"]
            story_title = ch["story_title"]

            label = f"{story_title} ch{chapter_num:04d}"
            try:
                html = fetch_html(source_url, timeout=args.timeout, retries=args.retries)
                new_title = extract_chapter_title(html)

                # Reject if page title is also bare, same, or looks garbled
                # (e.g. "Chương 739: Chapter 738 -..." = numbering mismatch artifact)
                title_is_garbled = bool(re.search(r"chương", new_title or "", re.IGNORECASE))
                if not new_title or new_title == old_title or BARE_TITLE_PATTERN.match(new_title) or title_is_garbled:
                    print(f"[SKIP] no better title for {label}: page={new_title!r}")
                    skipped += 1
                else:
                    print(f"[UPDATE] {label}: {old_title!r} → {new_title!r}")
                    if not dry_run:
                        update_title_in_db(conn, chapter_id, new_title)
                        updated += 1
                        if raw_text_path:
                            if update_title_in_file(raw_text_path, old_title, new_title):
                                file_updated += 1
                    else:
                        updated += 1

            except Exception as exc:
                failed += 1
                print(f"[WARN] failed {label} url={source_url}: {type(exc).__name__}: {exc}")
                if args.stop_on_error:
                    if not dry_run:
                        conn.rollback()
                    raise

            time.sleep(args.delay)

        if dry_run:
            conn.rollback()

        print(f"\n[SUMMARY] updated={updated} skipped={skipped} failed={failed} files_updated={file_updated}")
        if dry_run:
            print("[DRY-RUN] No changes made. Re-run with --apply to execute.")


if __name__ == "__main__":
    main()
