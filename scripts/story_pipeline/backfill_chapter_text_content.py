#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from story_db.story_pipeline_db.db import connect


TEXT_FIELDS = (
    ("raw_text_path", "raw_text_content"),
    ("translated_text_path", "translated_text_content"),
    ("polished_text_path", "polished_text_content"),
)


def resolve_project_path(value: str | None) -> Path | None:
    if not value:
        return None
    path = (ROOT / value).resolve()
    if not path.is_relative_to(ROOT):
        return None
    return path


def read_text(path_value: str | None) -> str | None:
    path = resolve_project_path(path_value)
    if path is None or not path.exists() or not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def fetch_candidates(limit: int, overwrite: bool, story_id: str | None) -> list[dict[str, Any]]:
    where = [
        "("
        + " OR ".join(f"({path_col} IS NOT NULL AND ({content_col} IS NULL OR %s))" for path_col, content_col in TEXT_FIELDS)
        + ")"
    ]
    params: list[Any] = [overwrite for _ in TEXT_FIELDS]
    if story_id:
        where.append("story_id = %s")
        params.append(story_id)

    params.append(limit)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT id, story_id, chapter_number,
                   raw_text_path, translated_text_path, polished_text_path,
                   raw_text_content, translated_text_content, polished_text_content
            FROM chapters
            WHERE {' AND '.join(where)}
            ORDER BY story_id, chapter_number
            LIMIT %s
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]


def update_chapter(chapter_id: str, values: dict[str, str | None]) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE chapters
            SET raw_text_content = COALESCE(%(raw_text_content)s::text, raw_text_content),
                translated_text_content = COALESCE(%(translated_text_content)s::text, translated_text_content),
                polished_text_content = COALESCE(%(polished_text_content)s::text, polished_text_content),
                text_content_backfilled_at = now(),
                updated_at = now()
            WHERE id = %(chapter_id)s
            """,
            {"chapter_id": chapter_id, **values},
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill chapter plain text content from DB text paths.")
    parser.add_argument("--limit", type=int, default=100, help="Number of chapters to inspect in this run.")
    parser.add_argument("--story-id", help="Only backfill one story UUID.")
    parser.add_argument("--overwrite", action="store_true", help="Replace existing *_text_content values.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    rows = fetch_candidates(args.limit, args.overwrite, args.story_id)
    changed = 0
    missing = 0
    for row in rows:
        values: dict[str, str | None] = {
            "raw_text_content": None,
            "translated_text_content": None,
            "polished_text_content": None,
        }
        for path_col, content_col in TEXT_FIELDS:
            if row.get(content_col) and not args.overwrite:
                continue
            text = read_text(row.get(path_col))
            if text is None:
                if row.get(path_col):
                    missing += 1
                continue
            values[content_col] = text

        if any(value is not None for value in values.values()):
            changed += 1
            print(f"[BACKFILL] chapter={row['id']} number={row['chapter_number']}")
            if not args.dry_run:
                update_chapter(row["id"], values)

    print(f"chapters_checked={len(rows)} chapters_changed={changed} missing_files={missing} dry_run={args.dry_run}")


if __name__ == "__main__":
    main()
