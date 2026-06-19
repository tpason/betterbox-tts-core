#!/usr/bin/env python3
"""Preflight translate/polish context for wetriedtls (or any EN story source).

Ensures genre, char-map, and glossary exist BEFORE polish queue drains — and
optionally repolishes when gap scan finds quality issues.

Steps:
  1. Resolve story + auto-detect genre → backfill metadata.genre
  2. Seed char-map from raw chapters (extract_char_map.py)
  3. Seed glossary from raw chapters (extract_term_glossary.py)
  4. Gap scan on polished/translated DB content
  5. Optional repolish (repolish_story_from_db.py)

Usage:
  viterbox/venv/bin/python scripts/story_pipeline/wetriedtls_preflight.py \\
      --story-title "A Regressor's Tale of Cultivation"

  viterbox/venv/bin/python scripts/story_pipeline/wetriedtls_preflight.py \\
      --story-title "A Regressor" --repolish --from-chapter 1 --to-chapter 30
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from story_db.story_pipeline_db import repository as repo
from genre_prompts import find_char_map_file, infer_genre_from_story_signals, resolve_genre_from_context
from extract_term_glossary import glossary_path_for, update_term_glossary
from scan_translation_gaps import scan_story_gaps_from_db


def _log(msg: str) -> None:
    print(msg, flush=True)


def find_story(title: str, source_code: str) -> dict[str, Any]:
    stories = repo.find_stories(title_contains=title, source_codes=[source_code])
    if not stories:
        raise SystemExit(f"[ERROR] No story matching {title!r} in source {source_code!r}")
    if len(stories) > 1:
        lines = [f"  - {s['title']} (id={s['id']})" for s in stories]
        raise SystemExit(f"[ERROR] Multiple matches:\n" + "\n".join(lines))
    return stories[0]


def story_slug(story: dict[str, Any]) -> str:
    meta = story.get("metadata") or {}
    return str(meta.get("slug") or story.get("source_story_id") or "")


def resolve_genre(story: dict[str, Any], source_code: str) -> str:
    meta = story.get("metadata") or {}
    char_map_file = find_char_map_file(story_id=str(story["id"]), slug=story_slug(story))
    return resolve_genre_from_context(
        str(story.get("category") or meta.get("genre") or ""),
        raw_language=str(story.get("language") or "en"),
        source_code=source_code,
        char_map_file=char_map_file,
        title=str(story.get("original_title") or story.get("title") or ""),
        description=str(meta.get("source_description") or story.get("description") or ""),
    )


def backfill_genre_metadata(story: dict[str, Any], source_code: str, *, apply: bool) -> str:
    meta = story.get("metadata") or {}
    detected = infer_genre_from_story_signals(
        category=str(story.get("category") or ""),
        title=str(story.get("original_title") or story.get("title") or ""),
        description=str(meta.get("source_description") or story.get("description") or ""),
        raw_language=str(story.get("language") or "en"),
        source_code=source_code,
    )
    current = str(meta.get("genre") or "")
    if detected and detected != current:
        _log(f"[GENRE] {current or '(empty)'} → {detected}")
        if apply:
            repo.update_story_metadata(str(story["id"]), {"genre": detected})
        return detected
    _log(f"[GENRE] keep {current or detected or '(none)'}")
    return current or detected


def run_subprocess(cmd: list[str], *, label: str, timeout: int) -> int:
    _log(f"[RUN] {label}: {' '.join(cmd[-6:])}…")
    try:
        result = subprocess.run(cmd, cwd=ROOT, timeout=timeout)
        if result.returncode != 0:
            _log(f"[WARN] {label} exited rc={result.returncode}")
        return int(result.returncode or 0)
    except subprocess.TimeoutExpired:
        _log(f"[WARN] {label} timed out after {timeout}s")
        return 1


def seed_char_map(args: argparse.Namespace, story: dict[str, Any], genre: str) -> None:
    story_id = str(story["id"])
    meta = story.get("metadata") or {}
    if meta.get("char_map_content") and not args.force_char_map:
        _log("[CHAR_MAP] already in DB metadata — skip (use --force-char-map to rebuild)")
        return
    cmd = [
        sys.executable,
        str(SCRIPT_DIR / "extract_char_map.py"),
        "--story-id", story_id,
        "--text-source", "raw",
        "--from-chapter", "1",
        "--to-chapter", str(args.preflight_chapters),
        "--sample-chapters", str(args.preflight_chapters),
        "--model", args.model,
        "--ollama-url", args.ollama_url,
    ]
    if genre:
        primary = genre.split(",")[0].strip()
        cmd.extend(["--genre", primary])
    run_subprocess(cmd, label="char-map seed", timeout=args.timeout * 3)


def seed_glossary(args: argparse.Namespace, story: dict[str, Any], genre: str) -> None:
    story_id = str(story["id"])
    slug = story_slug(story)
    g_path = glossary_path_for(story_id, slug)
    if g_path.exists() and not args.force_glossary:
        _log(f"[GLOSSARY] exists ({g_path.name}) — skip seed (use --force-glossary)")
        return
    result = update_term_glossary(
        story_id=story_id,
        story_title=str(story.get("title") or ""),
        from_chapter=1,
        to_chapter=args.preflight_chapters,
        text_source="raw",
        ollama_url=args.ollama_url,
        model=args.model,
        genre=genre,
        unload_after=True,
    )
    _log(f"[GLOSSARY] seed status={result.get('status')} added={result.get('added', 0)}")


def run_gap_scan(args: argparse.Namespace, story: dict[str, Any], genre: str) -> dict[str, Any]:
    slug = story_slug(story)
    char_map_file = find_char_map_file(story_id=str(story["id"]), slug=slug)
    report = scan_story_gaps_from_db(
        story_id=str(story["id"]),
        slug=slug,
        mode=args.gap_mode,
        from_chapter=args.from_chapter or 0,
        to_chapter=args.to_chapter or 0,
        char_map_file=char_map_file,
        genre=genre,
    )
    _log(
        f"[GAP SCAN] chapters={report['chapters_scanned']} "
        f"en_chapters={report['en_chapter_count']} "
        f"term_issues={report['term_issue_count']} "
        f"priority={report['priority_term_issues']}"
    )
    if report["en_findings"][:3]:
        for item in report["en_findings"][:3]:
            frags = "; ".join(item["fragments"][:3])
            _log(f"  EN {item['chapter']}: {frags}")
    return report


def run_repolish(args: argparse.Namespace, story: dict[str, Any]) -> int:
    cmd = [
        sys.executable,
        str(SCRIPT_DIR / "repolish_story_from_db.py"),
        "--story-id", str(story["id"]),
        "--ollama-url", args.ollama_url,
        "--overwrite",
    ]
    if args.from_chapter:
        cmd.extend(["--from-chapter", str(args.from_chapter)])
    if args.to_chapter:
        cmd.extend(["--to-chapter", str(args.to_chapter)])
    return run_subprocess(cmd, label="repolish", timeout=args.timeout * 20)


def main() -> None:
    parser = argparse.ArgumentParser(description="Preflight char-map + glossary + gap scan before polish/audio.")
    parser.add_argument("--story-title", default="", help="Story title search (ILIKE). Required if --story-id omitted.")
    parser.add_argument("--story-id", default="")
    parser.add_argument("--source-code", default="wetriedtls")
    parser.add_argument("--from-chapter", type=int, default=0)
    parser.add_argument("--to-chapter", type=int, default=0)
    parser.add_argument("--preflight-chapters", type=int, default=20, help="Raw chapters for char-map/glossary seed.")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default="qwen3:14b")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--gap-mode", choices=("polished", "translated"), default="polished")
    parser.add_argument("--skip-char-map", action="store_true")
    parser.add_argument("--skip-glossary", action="store_true")
    parser.add_argument("--skip-gap-scan", action="store_true")
    parser.add_argument("--force-char-map", action="store_true")
    parser.add_argument("--force-glossary", action="store_true")
    parser.add_argument(
        "--repolish",
        action="store_true",
        help="Run repolish after preflight (also auto when gap scan finds priority issues).",
    )
    parser.add_argument("--apply", action="store_true", help="Write genre metadata backfill (default dry-run genre).")
    args = parser.parse_args()
    if not args.story_id and not args.story_title:
        parser.error("Provide --story-id or --story-title")

    story = repo.get_story_by_id(args.story_id) if args.story_id else find_story(args.story_title, args.source_code)
    slug = story_slug(story)
    _log(f"[PREFLIGHT] story={story.get('title')} id={story['id']} slug={slug}")

    genre = backfill_genre_metadata(story, args.source_code, apply=args.apply)
    if not genre:
        genre = resolve_genre(story, args.source_code)
    _log(f"[PREFLIGHT] genre={genre}")

    if not args.skip_char_map:
        seed_char_map(args, story, genre)
    if not args.skip_glossary:
        seed_glossary(args, story, genre)

    report: dict[str, Any] = {}
    if not args.skip_gap_scan:
        progress = repo.get_story_chapter_progress(str(story["id"]))
        if progress.get("polished_count") or progress.get("translated_count"):
            report = run_gap_scan(args, story, genre)
        else:
            _log("[GAP SCAN] skip — no polished/translated chapters in DB yet")

    do_repolish = args.repolish or report.get("should_repolish")
    if do_repolish:
        if report.get("should_repolish") and not args.repolish:
            _log("[REPOLISH] auto-triggered due to gap scan findings")
        rc = run_repolish(args, story)
        if rc != 0:
            raise SystemExit(rc)
    else:
        _log("[PREFLIGHT] done — no repolish requested")

    _log("[PREFLIGHT] ✓ complete")


if __name__ == "__main__":
    main()
