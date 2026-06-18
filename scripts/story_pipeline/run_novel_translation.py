#!/usr/bin/env python3
"""CLI runner for the multi-pass novel translation pipeline.

Usage examples:

  # Translate chapter 543 of Vĩnh Thoái Hiệp Sĩ (dry-run, print output only)
  python scripts/story_pipeline/run_novel_translation.py \
    --story-title "Vĩnh Thoái Hiệp Sĩ" --chapter 543

  # Save to DB
  python scripts/story_pipeline/run_novel_translation.py \
    --story-title "Vĩnh Thoái Hiệp Sĩ" --chapter 543 --save

  # Skip polish for faster debugging
  python scripts/story_pipeline/run_novel_translation.py \
    --story-title "Vĩnh Thoái Hiệp Sĩ" --chapter 543 --skip-polish --skip-final-qa

  # Bounded smoke test: run only the first chunk, never save partial output
  python scripts/story_pipeline/run_novel_translation.py \
    --story-title "Vĩnh Thoái Hiệp Sĩ" --chapter 543 --max-chunks 1

  # Use a different model
  python scripts/story_pipeline/run_novel_translation.py \
    --story-id <uuid> --chapter 543 --model qwen3:30b
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from story_db.story_pipeline_db import db, repository as repo
from scripts.story_pipeline.novel_translation.pipeline import PipelineConfig, translate_chapter
from scripts.story_pipeline.genre_prompts import detect_genre, load_char_map

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger(__name__)

STORY_MEMORY_ROOT = ROOT / "story_data" / "story_memory"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen3:14b"


def _find_story(title: str | None, story_id: str | None) -> dict:
    if story_id:
        with db.connect() as conn:
            with conn.cursor(row_factory=__import__("psycopg").rows.dict_row) as cur:
                cur.execute(
                    "SELECT * FROM stories WHERE id = %s OR source_story_id = %s LIMIT 1",
                    (story_id, story_id),
                )
                row = cur.fetchone()
        if not row:
            sys.exit(f"[ERROR] story_id not found: {story_id}")
        return dict(row)
    if title:
        stories = repo.find_stories(title_contains=title, limit=1)
        if not stories:
            sys.exit(f"[ERROR] no story found matching: {title!r}")
        return stories[0]
    sys.exit("[ERROR] --story-title or --story-id required")


def _find_memory_dir(story: dict) -> Path | None:
    sid = str(story.get("source_story_id") or story.get("id") or "")
    title = story.get("title") or story.get("display_title") or ""
    # Normalize slug from title
    import re
    slug_part = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40]
    candidates = [
        STORY_MEMORY_ROOT / f"{sid}-{slug_part}",
        STORY_MEMORY_ROOT / sid,
    ]
    # Also scan for any dir starting with sid
    if STORY_MEMORY_ROOT.exists():
        for d in STORY_MEMORY_ROOT.iterdir():
            if d.is_dir() and (d.name.startswith(sid) or slug_part in d.name):
                candidates.insert(0, d)
    for c in candidates:
        if c.exists():
            return c
    return None


def _fetch_chapter(story_id: str, chapter_number: int) -> dict:
    with db.connect() as conn:
        with conn.cursor(row_factory=__import__("psycopg").rows.dict_row) as cur:
            cur.execute(
                """SELECT id, chapter_number, raw_text_content, polished_text_content,
                          is_translated, is_polished
                   FROM chapters WHERE story_id = %s AND chapter_number = %s LIMIT 1""",
                (story_id, chapter_number),
            )
            row = cur.fetchone()
    if not row:
        sys.exit(f"[ERROR] chapter {chapter_number} not found for story {story_id}")
    return dict(row)


def _detect_story_genre(story: dict, char_map_raw: str) -> str:
    from scripts.story_pipeline.genre_prompts import infer_genre_from_char_map
    category = story.get("category") or story.get("primary_category_id") or ""
    source_code = story.get("source_code") or ""
    language = story.get("language") or ""
    genre = detect_genre(str(category), raw_language=str(language), source_code=str(source_code))
    # Fallback: infer from char_map if detect_genre returns empty
    if not genre and char_map_raw:
        genre = infer_genre_from_char_map(char_map_raw)
    return genre or "western_fantasy"


def main() -> None:
    parser = argparse.ArgumentParser(description="Multi-pass novel translation pipeline")
    parser.add_argument("--story-title", default="")
    parser.add_argument("--story-id", default="")
    parser.add_argument("--chapter", type=int, required=True)
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--save", action="store_true", help="Write polished output to DB")
    parser.add_argument("--skip-polish", action="store_true")
    parser.add_argument("--skip-final-qa", action="store_true")
    parser.add_argument("--genre", default="", help="Override genre detection (e.g. western_fantasy)")
    parser.add_argument("--chunk-size", type=int, default=1800)
    parser.add_argument("--max-chunks", type=int, default=0,
                        help="Debug only: process at most N chunks; partial output cannot be saved")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if args.save and args.max_chunks > 0:
        sys.exit("[ERROR] --save is not allowed with --max-chunks partial runs")

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Resolve story
    story = _find_story(args.story_title or None, args.story_id or None)
    story_id = str(story["id"])
    title = story.get("display_title") or story.get("title") or story_id
    log.info("[STORY] %s (id=%s)", title, story_id)

    # Load char_map from DB metadata (legacy raw text)
    meta = story.get("metadata") or {}
    char_map_raw = meta.get("char_map_content") or ""
    genre = args.genre or _detect_story_genre(story, char_map_raw)
    log.info("[GENRE] %s", genre)

    # Find story_memory directory
    memory_dir = _find_memory_dir(story)
    log.info("[MEMORY] %s", memory_dir or "(not found)")

    # Fetch chapter
    chapter = _fetch_chapter(story_id, args.chapter)
    raw_text = chapter.get("raw_text_content") or ""
    if not raw_text:
        sys.exit(f"[ERROR] chapter {args.chapter} has no raw_text_content")
    log.info("[CHAPTER] %d — raw %d chars", args.chapter, len(raw_text))

    # Build slug (best effort)
    import re
    slug = str(story.get("source_story_id") or "")
    if not slug:
        slug = re.sub(r"[^a-z0-9-]+", "-", title.lower()).strip("-")

    # Pipeline config
    cfg = PipelineConfig(
        ollama_url=args.ollama_url,
        model=args.model,
        skip_polish=args.skip_polish,
        skip_final_qa=args.skip_final_qa,
        chunk_size=args.chunk_size,
        max_chunks=args.max_chunks,
        source_language=str(story.get("language") or ""),
    )

    # Run pipeline
    log.info("[RUN] starting pipeline…")
    result = translate_chapter(
        source_text=raw_text,
        story_id=story_id,
        slug=slug,
        genre=genre,
        chapter_number=args.chapter,
        memory_dir=memory_dir,
        char_map_raw=char_map_raw,
        cfg=cfg,
    )

    # Report
    print("\n" + "=" * 60)
    print(f"PIPELINE RESULT — ch{args.chapter} — {title}")
    print("=" * 60)
    print(f"success:      {result.success}")
    print(f"partial:      {'YES' if result.is_partial else 'no'}")
    print(f"elapsed:      {result.total_elapsed_s:.1f}s")
    print(f"chunks:       {len(result.chunk_results)}")
    total_violations = sum(len(cr.qa_report.violations) for cr in result.chunk_results)
    blocking_chunks = sum(1 for cr in result.chunk_results if cr.qa_report.has_blocking_issues)
    print(f"violations:   {total_violations}")
    print(f"chunk_blocks: {blocking_chunks}")
    if result.final_quality_blocking:
        print(f"final_blocking: {', '.join(result.final_quality_blocking)}")
    if result.final_quality_warnings:
        print(f"final_warnings: {', '.join(result.final_quality_warnings)}")
    if result.error:
        print(f"error:        {result.error}")
    if result.final_qa:
        print(f"final_qa:     {result.final_qa.verdict} ({len(result.final_qa.violations)} issues)")
        if result.final_qa.violations:
            for v in result.final_qa.violations:
                print(f"  [{v.severity}] {v.type} @ {v.location}: {v.description[:80]}")
    if result.needs_review:
        print("needs_review: YES — human check recommended")

    print("\n--- POLISHED OUTPUT ---")
    print(result.polished_text or "(empty)")

    # Per-chunk diagnostics
    if args.verbose:
        print("\n--- CHUNK DETAILS ---")
        for cr in result.chunk_results:
            print(f"\n{cr.chunk_id} (risk={cr.qa_report.chunk_id}):")
            if cr.qa_report.violations:
                for v in cr.qa_report.violations:
                    print(f"  violation [{v.type}] {v.line_id}: {v.reason[:60]}")
            if cr.polish_warnings:
                print(f"  polish warnings: {cr.polish_warnings}")

    # Save to DB if requested
    if args.save and result.success and result.polished_text:
        chapter_id = str(chapter["id"])
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE chapters SET polished_text_content = %s, is_polished = true
                       WHERE id = %s""",
                    (result.polished_text, chapter_id),
                )
            conn.commit()
        log.info("[SAVE] chapter %d polished content written to DB", args.chapter)
    elif args.save and not result.success:
        log.warning("[SAVE] skipped — pipeline did not succeed")

    sys.exit(0 if result.success else 1)


if __name__ == "__main__":
    main()
