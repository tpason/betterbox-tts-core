#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from story_db.story_pipeline_db.db import connect  # noqa: E402


def project_path(value: str) -> Path:
    path = (ROOT / value).resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError(f"Path is outside project root: {value}")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Update DB chapter text paths/content from local files.")
    parser.add_argument("--story-id", required=True)
    parser.add_argument("--from-chapter", type=int, required=True)
    parser.add_argument("--to-chapter", type=int, required=True)
    parser.add_argument("--translated-dir", default="")
    parser.add_argument("--polished-dir", default="")
    parser.add_argument("--raw-dir", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    changed = 0
    missing = 0
    with connect() as conn:
        for chapter_number in range(args.from_chapter, args.to_chapter + 1):
            values: dict[str, str | None] = {
                "raw_text_path": None,
                "raw_text_content": None,
                "translated_text_path": None,
                "translated_text_content": None,
                "polished_text_path": None,
                "polished_text_content": None,
            }

            for prefix, directory in (
                ("raw", args.raw_dir),
                ("translated", args.translated_dir),
                ("polished", args.polished_dir),
            ):
                if not directory:
                    continue
                relative_path = f"{directory.rstrip('/')}/chapter{chapter_number:04d}.txt"
                path = project_path(relative_path)
                if not path.exists() or path.stat().st_size == 0:
                    missing += 1
                    print(f"[MISS] chapter={chapter_number} {prefix}={relative_path}", flush=True)
                    continue
                values[f"{prefix}_text_path"] = relative_path
                values[f"{prefix}_text_content"] = path.read_text(encoding="utf-8")

            if not any(values.values()):
                continue

            changed += 1
            print(f"[UPDATE] chapter={chapter_number}", flush=True)
            if args.dry_run:
                continue
            conn.execute(
                """
                UPDATE chapters
                SET raw_text_path = COALESCE(%(raw_text_path)s::text, raw_text_path),
                    raw_text_content = COALESCE(%(raw_text_content)s::text, raw_text_content),
                    translated_text_path = COALESCE(%(translated_text_path)s::text, translated_text_path),
                    translated_text_content = COALESCE(%(translated_text_content)s::text, translated_text_content),
                    polished_text_path = COALESCE(%(polished_text_path)s::text, polished_text_path),
                    polished_text_content = COALESCE(%(polished_text_content)s::text, polished_text_content),
                    is_downloaded = is_downloaded OR %(raw_text_content)s::text IS NOT NULL,
                    is_translated = is_translated OR %(translated_text_content)s::text IS NOT NULL,
                    is_polished = is_polished OR %(polished_text_content)s::text IS NOT NULL,
                    translated_at = CASE WHEN %(translated_text_content)s::text IS NOT NULL THEN now() ELSE translated_at END,
                    polished_at = CASE WHEN %(polished_text_content)s::text IS NOT NULL THEN now() ELSE polished_at END,
                    text_content_backfilled_at = now(),
                    updated_at = now()
                WHERE story_id = %(story_id)s
                  AND chapter_number = %(chapter_number)s
                """,
                {"story_id": args.story_id, "chapter_number": chapter_number, **values},
            )

    print(f"chapters_changed={changed} missing_files={missing} dry_run={args.dry_run}", flush=True)


if __name__ == "__main__":
    main()
