#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from argparse import Namespace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from story_db.story_pipeline_db import repository as repo
from story_db.story_pipeline_db.db import connect
from genre_prompts import find_char_map_file
from scripts.story_pipeline.translate_chapters_from_db import (
    build_metadata_translation_context,
    chapter_title_from_content,
    translate_chapter_title,
    translate_story_description,
    translate_story_title,
    update_story_translation,
)


def _model_args(args: argparse.Namespace) -> Namespace:
    return Namespace(
        ollama_url=args.ollama_url,
        story_model=args.story_model,
        translate_model=args.translate_model,
        temperature=args.temperature,
        num_ctx=args.num_ctx,
        ollama_timeout=args.timeout,
        ollama_retries=args.retries,
        keep_alive=args.keep_alive,
        char_map_file=args.char_map_file,
    )


def _find_story(args: argparse.Namespace) -> dict[str, Any]:
    if args.story_id:
        return repo.get_story_by_id(args.story_id)
    stories = repo.find_stories(
        title_contains=args.story_title or None,
        source_codes=[args.source_code] if args.source_code else None,
        limit=20,
    )
    if not stories:
        raise SystemExit("No matching story found.")
    if len(stories) > 1:
        lines = [f"- {story['title']} (id={story['id']}, source={story.get('source_code')})" for story in stories]
        raise SystemExit("Multiple matching stories:\n" + "\n".join(lines))
    return stories[0]


def _story_slug(story: dict[str, Any]) -> str:
    metadata = story.get("metadata") or {}
    return str(metadata.get("slug") or story.get("source_story_id") or story.get("id") or "")


def _context_for_story(story: dict[str, Any], args: argparse.Namespace) -> str:
    metadata = story.get("metadata") or {}
    slug = _story_slug(story)
    char_map = args.char_map_file or find_char_map_file(story_id=str(story["id"]), slug=slug)
    if args.require_char_map and not char_map and not metadata.get("char_map_content"):
        raise SystemExit("[ERROR] No char-map — run preflight first or drop --require-char-map")
    source_description = str(
        metadata.get("source_description")
        or metadata.get("original_description_before_vi_translate")
        or story.get("description")
        or ""
    )
    return build_metadata_translation_context(
        story_id=str(story["id"]),
        story_slug_value=slug,
        source_code=str(story.get("source_code") or args.source_code or ""),
        story_title=str(story.get("title") or ""),
        original_title=str(story.get("original_title") or story.get("title") or ""),
        display_title=str(story.get("display_title") or ""),
        description=source_description,
        category=str(story.get("category") or metadata.get("genre") or ""),
        raw_language=str(story.get("language") or ""),
        char_map_file=char_map,
        story_memory_dir=args.story_memory_dir,
    )


def _list_chapters_for_titles(story_id: str, args: argparse.Namespace) -> list[dict[str, Any]]:
    query = """
        SELECT
            c.id,
            c.chapter_number,
            c.title AS current_title,
            c.polished_text_content,
            COALESCE(j.payload->>'source_chapter_title', j.payload->>'chapter_title') AS source_title
        FROM chapters c
        LEFT JOIN story_jobs j
          ON j.chapter_id = c.id
         AND j.job_type = 'polish_chapter'
        WHERE c.story_id = %s
    """
    params: list[Any] = [story_id]
    if args.from_chapter:
        query += " AND c.chapter_number >= %s"
        params.append(args.from_chapter)
    if args.to_chapter:
        query += " AND c.chapter_number <= %s"
        params.append(args.to_chapter)
    query += " ORDER BY c.chapter_number"
    if args.limit:
        query += " LIMIT %s"
        params.append(args.limit)
    with connect() as conn:
        return [dict(row) for row in conn.execute(query, params).fetchall()]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill story metadata + chapter titles (unified context with chapter body)."
    )
    parser.add_argument("--story-id", default="")
    parser.add_argument("--story-title", default="")
    parser.add_argument("--source-code", default="wetriedtls")
    parser.add_argument("--from-chapter", type=int, default=0)
    parser.add_argument("--to-chapter", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--translate-model", default="translategemma:12b")
    parser.add_argument("--story-model", default="")
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--num-ctx", type=int, default=8192)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--keep-alive", default="30m")
    parser.add_argument("--char-map-file", default="")
    parser.add_argument("--story-memory-dir", default="")
    parser.add_argument("--require-char-map", action="store_true", help="Fail if char-map missing.")
    parser.add_argument(
        "--llm-chapter-titles",
        action="store_true",
        help="Legacy: LLM-translate chapter titles from EN source. Default: first polished line.",
    )
    parser.add_argument("--skip-story", action="store_true")
    parser.add_argument("--skip-chapters", action="store_true")
    parser.add_argument("--apply", action="store_true", help="Write changes. Default is dry-run.")
    args = parser.parse_args()

    if not args.story_id and not args.story_title:
        parser.error("Pass --story-id or --story-title")

    story = _find_story(args)
    model_args = _model_args(args)
    context = _context_for_story(story, args)
    story_id = str(story["id"])
    print(f"[STORY] {story.get('title')} id={story_id} source={story.get('source_code') or args.source_code}")
    print(f"[MODE] {'apply' if args.apply else 'dry-run'}")
    if "story_memory_excerpt" in context:
        print("[CONTEXT] story_memory + glossary injected")
    if "char_map_excerpt" in context:
        print("[CONTEXT] char_map injected")

    if not args.skip_story:
        metadata = story.get("metadata") or {}
        source_title = str(story.get("original_title") or story.get("title") or "").strip()
        source_description = str(
            metadata.get("source_description")
            or metadata.get("original_description_before_vi_translate")
            or story.get("description")
            or ""
        ).strip()
        next_title = translate_story_title(source_title, model_args, context=context) if source_title else None
        next_description = (
            translate_story_description(source_description, model_args, context=context)
            if source_description
            else None
        )
        print(f"[STORY TITLE] {story.get('display_title') or story.get('title')} -> {next_title}")
        if next_description:
            print(f"[STORY DESC] chars={len(source_description)} -> chars={len(next_description)}")
        if args.apply:
            update_story_translation(
                story_id,
                display_title=next_title,
                author=None,
                description=next_description,
                original_description=source_description or None,
                model=args.story_model or args.translate_model,
            )

    if args.skip_chapters:
        return

    rows = _list_chapters_for_titles(story_id, args)
    changed = 0
    skipped = 0
    for row in rows:
        polished = str(row.get("polished_text_content") or "").strip()
        if args.llm_chapter_titles:
            source_title = str(row.get("source_title") or "").strip()
            if not source_title:
                skipped += 1
                continue
            next_title = translate_chapter_title(source_title, model_args, context=context)
        else:
            if not polished:
                skipped += 1
                continue
            next_title = chapter_title_from_content(polished)
            if not next_title:
                skipped += 1
                continue
        current_title = str(row.get("current_title") or "")
        print(f"[CH {row['chapter_number']:04d}] {current_title} -> {next_title}")
        if args.apply and next_title and next_title != current_title:
            repo.update_chapter_title(row["id"], next_title)
            changed += 1
    print(f"[DONE] chapters={len(rows)} changed={changed} skipped={skipped}")


if __name__ == "__main__":
    main()
