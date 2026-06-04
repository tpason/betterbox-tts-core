#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
for path in (ROOT, SCRIPT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def resolve_project_path(value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    absolute_path = path.resolve() if path.is_absolute() else (ROOT / path).resolve()
    if not absolute_path.is_relative_to(ROOT):
        return None
    return absolute_path


def read_project_text(path_value: str | None) -> str | None:
    path = resolve_project_path(path_value)
    if path is None or not path.exists() or not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def reader_formatted_columns_exist(conn: Any) -> bool:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM information_schema.columns
        WHERE table_name = 'chapters'
          AND column_name IN (
            'reader_formatted_text_content',
            'reader_formatted_content_version',
            'reader_formatted_source',
            'reader_formatted_source_hash',
            'reader_formatted_at'
          )
        """
    ).fetchone()
    return int(row["count"]) == 5


def fetch_candidates(conn: Any, *, story_id: str | None, limit: int, offset: int) -> list[dict[str, Any]]:
    params: list[Any] = []
    where = ["(c.polished_text_content IS NOT NULL OR c.polished_text_path IS NOT NULL)"]
    if story_id:
        params.append(story_id)
        where.append(f"c.story_id = %s")

    limit_clause = ""
    if limit > 0:
        params.append(limit)
        limit_clause = "LIMIT %s"

    offset_clause = ""
    if offset > 0:
        params.append(offset)
        offset_clause = "OFFSET %s"

    rows = conn.execute(
        f"""
        SELECT
            c.id,
            c.story_id,
            c.chapter_number,
            c.title AS chapter_title,
            c.translated_text_path,
            c.translated_text_content,
            c.polished_text_path,
            c.polished_text_content,
            s.title AS story_title
        FROM chapters c
        LEFT JOIN stories s ON s.id = c.story_id
        WHERE {' AND '.join(where)}
        ORDER BY s.title, c.chapter_number
        {limit_clause}
        {offset_clause}
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def update_chapter_content(conn: Any, row: dict[str, Any], formatted: str, *, clear_reader_cache: bool) -> None:
    if clear_reader_cache:
        conn.execute(
            """
            UPDATE chapters
            SET polished_text_content = %s,
                is_polished = TRUE,
                text_content_backfilled_at = now(),
                reader_formatted_text_content = NULL,
                reader_formatted_content_version = NULL,
                reader_formatted_source = NULL,
                reader_formatted_source_hash = NULL,
                reader_formatted_at = NULL,
                updated_at = now()
            WHERE id = %s
            """,
            (formatted, row["id"]),
        )
        return

    conn.execute(
        """
        UPDATE chapters
        SET polished_text_content = %s,
            is_polished = TRUE,
            text_content_backfilled_at = now(),
            updated_at = now()
        WHERE id = %s
        """,
        (formatted, row["id"]),
    )


def write_polished_file(row: dict[str, Any], formatted: str) -> bool:
    path = resolve_project_path(row.get("polished_text_path"))
    if path is None:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(formatted.rstrip() + "\n", encoding="utf-8")
    return True


def source_text(row: dict[str, Any]) -> tuple[str | None, str | None]:
    if row.get("polished_text_content"):
        return "db", row["polished_text_content"]
    file_text = read_project_text(row.get("polished_text_path"))
    if file_text is not None:
        return "file", file_text
    return None, None


def translated_source_text(row: dict[str, Any]) -> tuple[str | None, str | None]:
    if row.get("translated_text_content"):
        return "translated_db", row["translated_text_content"]
    file_text = read_project_text(row.get("translated_text_path"))
    if file_text is not None:
        return "translated_file", file_text
    return None, None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reformat existing polished chapter content in DB using polish_worker formatter."
    )
    parser.add_argument("--story-id", help="Only update one story UUID.")
    parser.add_argument("--limit", type=int, default=100, help="Max chapters to inspect. Use 0 for all matching chapters.")
    parser.add_argument("--offset", type=int, default=0, help="Skip this many matching chapters before applying --limit.")
    parser.add_argument("--apply", action="store_true", help="Actually update DB/files. Without this flag the script only previews changes.")
    parser.add_argument("--dry-run", action="store_true", help="Print changes without updating DB/files. This is the default unless --apply is set.")
    parser.add_argument("--write-files", action="store_true", help="Also rewrite polished_text_path files with formatted content.")
    parser.add_argument(
        "--prefer-translated",
        action="store_true",
        help="Use translated_text_content/path as repair source when available, then write cleaned result to polished content/path.",
    )
    parser.add_argument("--backup-jsonl", help="Write one JSON object per changed chapter with old/new content before applying.")
    parser.add_argument(
        "--show-sample",
        type=int,
        default=0,
        help="Print first N formatted samples for inspection.",
    )
    args = parser.parse_args()

    from reader_content_format import format_polished_content
    from story_db.story_pipeline_db.db import connect

    checked = 0
    changed = 0
    missing_source = 0
    files_written = 0
    dry_run = not args.apply or args.dry_run
    backup_file = Path(args.backup_jsonl) if args.backup_jsonl else None
    if backup_file and not backup_file.is_absolute():
        backup_file = (ROOT / backup_file).resolve()

    with connect() as conn:
        clear_reader_cache = reader_formatted_columns_exist(conn)
        rows = fetch_candidates(conn, story_id=args.story_id, limit=args.limit, offset=max(0, args.offset))

        for row in rows:
            checked += 1
            if args.prefer_translated:
                source, text = translated_source_text(row)
                if not text:
                    source, text = source_text(row)
            else:
                source, text = source_text(row)
            if not text:
                missing_source += 1
                continue

            job_context = {
                "chapter_title": row.get("chapter_title"),
                "payload": {
                    "chapter_title": row.get("chapter_title"),
                    "story_title": row.get("story_title"),
                    "chapter_number": row.get("chapter_number"),
                },
            }
            formatted = format_polished_content(text, job_context)
            if not formatted:
                missing_source += 1
                continue

            needs_db_update = formatted.strip() != (row.get("polished_text_content") or "").strip()
            needs_file_update = args.write_files and formatted.strip() != (read_project_text(row.get("polished_text_path")) or "").strip()
            if not needs_db_update and not needs_file_update:
                continue

            changed += 1
            print(
                "[REFORMAT] "
                f"story={row.get('story_title') or row['story_id']} "
                f"chapter={row.get('chapter_number')} source={source} "
                f"chars={len(text)}->{len(formatted)}"
            )

            if args.show_sample and changed <= args.show_sample:
                print("--- sample start ---")
                print(formatted[:1200])
                print("--- sample end ---")

            if backup_file:
                backup_file.parent.mkdir(parents=True, exist_ok=True)
                with backup_file.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            {
                                "chapter_id": row["id"],
                                "story_id": row["story_id"],
                                "story_title": row.get("story_title"),
                                "chapter_number": row.get("chapter_number"),
                                "chapter_title": row.get("chapter_title"),
                                "polished_text_path": row.get("polished_text_path"),
                                "old_polished_text_content": row.get("polished_text_content"),
                                "formatted_polished_text_content": formatted.rstrip() + "\n",
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )

            if dry_run:
                continue

            update_chapter_content(conn, row, formatted.rstrip() + "\n", clear_reader_cache=clear_reader_cache)
            if needs_file_update and write_polished_file(row, formatted):
                files_written += 1

    print(
        "chapters_checked="
        f"{checked} chapters_changed={changed} missing_source={missing_source} "
        f"files_written={files_written} dry_run={dry_run}"
    )


if __name__ == "__main__":
    main()
