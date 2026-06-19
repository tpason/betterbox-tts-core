#!/usr/bin/env python3
"""Reset chapter text + jobs for wetriedtls E2E quality re-test."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
for p in (ROOT, SCRIPT_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from story_db.story_pipeline_db import repository as repo
from story_db.story_pipeline_db.db import connect
from extract_term_glossary import glossary_path_for
from genre_prompts import find_char_map_file


def reset_story_test_range(
    story_id: str,
    *,
    from_chapter: int,
    to_chapter: int,
    clear_char_map: bool,
    clear_glossary: bool,
    clear_genre: bool,
    dry_run: bool,
) -> None:
    story = repo.get_story_by_id(story_id)
    if not story:
        raise SystemExit(f"story_id={story_id} not found")
    meta = story.get("metadata") or {}
    slug = str(meta.get("slug") or story.get("source_story_id") or "")

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, chapter_number FROM chapters
            WHERE story_id = %s AND chapter_number BETWEEN %s AND %s
            ORDER BY chapter_number
            """,
            (story_id, from_chapter, to_chapter),
        ).fetchall()
        chapter_ids = [str(r["id"]) for r in rows]
        print(f"[RESET] story={story.get('title')} ch{from_chapter}-{to_chapter} ({len(chapter_ids)} chapters)")

        if not chapter_ids:
            raise SystemExit("No chapters in range")

        if dry_run:
            print("[DRY] would reset polished/translated + delete polish jobs")
            return

        conn.execute(
            """
            UPDATE chapters SET
                translated_text_content = NULL,
                polished_text_content = NULL,
                reader_formatted_text_content = NULL,
                reader_formatted_content_version = NULL,
                is_translated = FALSE,
                is_polished = FALSE,
                is_audio_generated = FALSE,
                audio_path = NULL
            WHERE id = ANY(%s::uuid[])
            """,
            (chapter_ids,),
        )
        conn.execute(
            """
            DELETE FROM story_jobs
            WHERE job_type IN ('polish_chapter', 'translate_chapter', 'audio_chapter', 'audio_chapter_segments')
              AND chapter_id = ANY(%s::uuid[])
            """,
            (chapter_ids,),
        )

    if clear_char_map:
        repo.delete_story_metadata_keys(
            story_id,
            [
                "char_map_content",
                "char_map_path",
                "char_map_updated_at",
                "char_map_updated_to_chapter",
                "char_map_raw_covered_to",
                "char_map_create_failed_at_chapter",
            ],
        )
        cm = find_char_map_file(story_id=story_id, slug=slug)
        if cm and Path(cm).exists():
            Path(cm).unlink(missing_ok=True)
            print(f"[RESET] deleted char_map file {cm}")

    if clear_glossary:
        g = glossary_path_for(story_id, slug)
        if g.exists():
            g.unlink()
            print(f"[RESET] deleted glossary {g}")

    if clear_genre:
        repo.delete_story_metadata_keys(story_id, ["genre", "story_metadata_translated_to"])
        print("[RESET] cleared genre + metadata translation flag")

    print("[RESET] ✓ done")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--story-id", required=True)
    p.add_argument("--from-chapter", type=int, default=1)
    p.add_argument("--to-chapter", type=int, default=5)
    p.add_argument("--keep-char-map", action="store_true")
    p.add_argument("--keep-glossary", action="store_true")
    p.add_argument("--keep-genre", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    reset_story_test_range(
        args.story_id,
        from_chapter=args.from_chapter,
        to_chapter=args.to_chapter,
        clear_char_map=not args.keep_char_map,
        clear_glossary=not args.keep_glossary,
        clear_genre=not args.keep_genre,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
