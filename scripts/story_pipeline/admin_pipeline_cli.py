#!/usr/bin/env python3
"""Thin admin CLI — delegates to existing pipeline scripts, no duplicate business logic."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from story_db.story_pipeline_db import repository as repo
from scripts.story_pipeline.check_translation_quality import (
    list_chapters_for_pipeline_reset,
    reset_polished_for_repolish,
    retranslate_bad_chapters,
)


def _parse_chapter_numbers(raw: str) -> list[int]:
    if not raw.strip():
        return []
    out: list[int] = []
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        value = int(part)
        if value > 0:
            out.append(value)
    return sorted(set(out))


def _chapter_filter_args(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "story_id": args.story_id,
        "from_chapter": args.from_chapter or 0,
        "to_chapter": args.to_chapter or 0,
        "chapter_numbers": _parse_chapter_numbers(args.chapter_numbers or ""),
    }


def _emit(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_repolish(args: argparse.Namespace) -> int:
    if args.quality_only:
        cmd = [
            sys.executable,
            str(ROOT / "scripts/story_pipeline/check_translation_quality.py"),
            "--story-id",
            args.story_id,
            "--repolish-bad",
        ]
        if args.from_chapter:
            cmd.extend(["--from-chapter", str(args.from_chapter)])
        if args.to_chapter:
            cmd.extend(["--to-chapter", str(args.to_chapter)])
        if args.force_running:
            cmd.append("--force-running")
        if args.dry_run:
            cmd.append("--dry-run")
        rc = subprocess.call(cmd, cwd=ROOT)
        payload = {"ok": rc == 0, "action": "repolish", "quality_only": True, "exit_code": rc}
        _emit(payload, as_json=args.json)
        return rc

    rows = list_chapters_for_pipeline_reset(**_chapter_filter_args(args))
    if not rows:
        _emit({"ok": True, "action": "repolish", "count": 0, "chapter_numbers": []}, as_json=args.json)
        return 0

    chapter_ids = [str(r["chapter_id"]) for r in rows if r.get("chapter_id")]
    count = reset_polished_for_repolish(
        chapter_ids, dry_run=args.dry_run, force_running=args.force_running
    )
    payload = {
        "ok": True,
        "action": "repolish",
        "count": count,
        "chapter_numbers": [r["chapter_number"] for r in rows],
        "dry_run": args.dry_run,
    }
    _emit(payload, as_json=args.json)
    return 0


def cmd_retranslate(args: argparse.Namespace) -> int:
    if args.quality_only:
        cmd = [
            sys.executable,
            str(ROOT / "scripts/story_pipeline/check_translation_quality.py"),
            "--story-id",
            args.story_id,
            "--retranslate-bad",
        ]
        if args.from_chapter:
            cmd.extend(["--from-chapter", str(args.from_chapter)])
        if args.to_chapter:
            cmd.extend(["--to-chapter", str(args.to_chapter)])
        if args.force_running:
            cmd.append("--force-running")
        if args.dry_run:
            cmd.append("--dry-run")
        rc = subprocess.call(cmd, cwd=ROOT)
        payload = {"ok": rc == 0, "action": "retranslate", "quality_only": True, "exit_code": rc}
        _emit(payload, as_json=args.json)
        return rc

    rows = list_chapters_for_pipeline_reset(**_chapter_filter_args(args))
    if not rows:
        _emit({"ok": True, "action": "retranslate", "count": 0, "chapter_numbers": []}, as_json=args.json)
        return 0

    count = retranslate_bad_chapters(rows, dry_run=args.dry_run, force_running=args.force_running)
    payload = {
        "ok": True,
        "action": "retranslate",
        "count": count,
        "chapter_numbers": [r["chapter_number"] for r in rows],
        "dry_run": args.dry_run,
    }
    _emit(payload, as_json=args.json)
    return 0


def cmd_recrawl_story(args: argparse.Namespace) -> int:
    story = repo.request_story_recrawl(args.story_id)
    if not story:
        _emit({"ok": False, "error": "Story not found"}, as_json=args.json)
        return 1
    _emit({"ok": True, "action": "recrawl", "story": story}, as_json=args.json)
    return 0


def cmd_recrawl_chapters(args: argparse.Namespace) -> int:
    updated = repo.request_chapter_recrawl(
        args.story_id,
        chapter_numbers=_parse_chapter_numbers(args.chapter_numbers or "") or None,
        from_chapter=args.from_chapter or 0,
        to_chapter=args.to_chapter or 0,
        clear_raw=args.clear_raw,
        touch_story_catalog=True,
    )
    _emit(
        {
            "ok": True,
            "action": "recrawl_chapters",
            "updated": updated,
            "clear_raw": args.clear_raw,
        },
        as_json=args.json,
    )
    return 0


def cmd_translate_metadata(args: argparse.Namespace) -> int:
    cmd = [
        sys.executable,
        str(ROOT / "scripts/story_pipeline/backfill_metadata_titles.py"),
        "--story-id",
        args.story_id,
        "--ollama-url",
        args.ollama_url,
        "--translate-model",
        args.translate_model,
    ]
    if args.story_model:
        cmd.extend(["--story-model", args.story_model])
    if args.from_chapter:
        cmd.extend(["--from-chapter", str(args.from_chapter)])
    if args.to_chapter:
        cmd.extend(["--to-chapter", str(args.to_chapter)])
    if args.skip_story:
        cmd.append("--skip-story")
    if args.skip_chapters:
        cmd.append("--skip-chapters")
    if args.apply:
        cmd.append("--apply")
    rc = subprocess.call(cmd, cwd=ROOT)
    payload = {"ok": rc == 0, "action": "translate_metadata", "exit_code": rc}
    _emit(payload, as_json=args.json)
    return rc


def _split_csv(raw: str) -> list[str]:
    return [part.strip() for part in raw.replace(",", " ").split() if part.strip()]


def _build_discover_cmd(args: argparse.Namespace) -> list[str]:
    cmd = [
        sys.executable,
        str(ROOT / "scripts/story_pipeline/discover_hot_stories.py"),
        "--pages",
        str(args.pages),
        "--min-chapters",
        str(args.min_chapters),
        "--no-url-skip",
    ]
    if args.sources:
        cmd.extend(["--sources", *args.sources])
    if args.timeout:
        cmd.extend(["--timeout", str(args.timeout)])
    return cmd


def cmd_discover(args: argparse.Namespace) -> int:
    cmd = _build_discover_cmd(args)
    rc = subprocess.call(cmd, cwd=ROOT)
    payload = {"ok": rc == 0, "action": "discover", "exit_code": rc, "command": cmd}
    _emit(payload, as_json=args.json)
    return rc


def _build_crawl_cmd(args: argparse.Namespace, *, story_url: str = "") -> list[str]:
    cmd = [
        sys.executable,
        str(ROOT / "scripts/story_pipeline/crawl_stories_from_db.py"),
        "--workers",
        str(args.workers),
        "--timeout",
        str(args.timeout),
        "--retries",
        str(args.retries),
        "--retry-sleep",
        str(args.retry_sleep),
        "--chapter-delay",
        str(args.chapter_delay),
        "--chapter-workers",
        str(args.chapter_workers),
        "--max-consecutive-content-misses",
        str(args.max_consecutive_content_misses),
        "--post-translate",
        args.post_translate,
        "--min-catalog-check-hours",
        str(args.min_catalog_check_hours),
        "--claim-finished-cooldown-minutes",
        str(args.claim_finished_cooldown_minutes),
    ]
    if args.sources:
        cmd.extend(["--sources", *args.sources])
    if args.only_incomplete:
        cmd.append("--only-incomplete")
    if args.limit_stories:
        cmd.extend(["--limit-stories", str(args.limit_stories)])
    if args.max_chapters:
        cmd.extend(["--max-chapters", str(args.max_chapters)])
    if story_url:
        cmd.extend(["--story-url", story_url])
    elif args.title_contains:
        cmd.extend(["--title-contains", args.title_contains])
    return cmd


def cmd_crawl_stories(args: argparse.Namespace) -> int:
    cmd = _build_crawl_cmd(args)
    rc = subprocess.call(cmd, cwd=ROOT)
    payload = {"ok": rc == 0, "action": "crawl_stories", "exit_code": rc, "command": cmd}
    _emit(payload, as_json=args.json)
    return rc


def cmd_crawl_story(args: argparse.Namespace) -> int:
    story = repo.get_story_by_id(args.story_id)
    if not story:
        _emit({"ok": False, "error": "Story not found"}, as_json=args.json)
        return 1
    source_url = (story.get("source_url") or "").strip()
    if not source_url:
        _emit({"ok": False, "error": "Story has no source_url"}, as_json=args.json)
        return 1
    cmd = _build_crawl_cmd(args, story_url=source_url)
    rc = subprocess.call(cmd, cwd=ROOT)
    payload = {
        "ok": rc == 0,
        "action": "crawl_story",
        "exit_code": rc,
        "story_id": args.story_id,
        "story_title": story.get("title"),
        "command": cmd,
    }
    _emit(payload, as_json=args.json)
    return rc


def _add_crawl_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--only-incomplete", action="store_true")
    parser.add_argument("--limit-stories", type=int, default=0)
    parser.add_argument("--max-chapters", type=int, default=0)
    parser.add_argument("--sources", nargs="*", default=[])
    parser.add_argument("--title-contains", default="")
    parser.add_argument("--min-catalog-check-hours", type=int, default=0)
    parser.add_argument("--claim-finished-cooldown-minutes", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--retry-sleep", type=float, default=2.0)
    parser.add_argument("--chapter-delay", type=float, default=1.5)
    parser.add_argument("--chapter-workers", type=int, default=2)
    parser.add_argument("--max-consecutive-content-misses", type=int, default=1)
    parser.add_argument("--post-translate", choices=("polish", "copy"), default="polish")
    parser.add_argument("--json", action="store_true")


def _add_chapter_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--story-id", required=True)
    parser.add_argument("--from-chapter", type=int, default=0)
    parser.add_argument("--to-chapter", type=int, default=0)
    parser.add_argument("--chapter-numbers", default="", help="Comma-separated chapter numbers")
    parser.add_argument("--quality-only", action="store_true", help="Use check_translation_quality scan")
    parser.add_argument("--force-running", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true", help="Machine-readable JSON on stdout")


def main() -> None:
    parser = argparse.ArgumentParser(description="Admin pipeline actions (thin wrapper)")
    sub = parser.add_subparsers(dest="command", required=True)

    repolish_p = sub.add_parser("repolish", help="Reset + re-queue polish jobs")
    _add_chapter_filters(repolish_p)

    retranslate_p = sub.add_parser("retranslate", help="Reset translate/polish + re-queue jobs")
    _add_chapter_filters(retranslate_p)

    recrawl_p = sub.add_parser("recrawl-story", help="Request catalog re-crawl via scheduler")
    recrawl_p.add_argument("--story-id", required=True)
    recrawl_p.add_argument("--json", action="store_true")

    recrawl_ch_p = sub.add_parser("recrawl-chapters", help="Mark chapters for re-download")
    recrawl_ch_p.add_argument("--story-id", required=True)
    recrawl_ch_p.add_argument("--from-chapter", type=int, default=0)
    recrawl_ch_p.add_argument("--to-chapter", type=int, default=0)
    recrawl_ch_p.add_argument("--chapter-numbers", default="")
    recrawl_ch_p.add_argument("--clear-raw", action="store_true")
    recrawl_ch_p.add_argument("--json", action="store_true")

    meta_p = sub.add_parser("translate-metadata", help="backfill_metadata_titles.py wrapper")
    meta_p.add_argument("--story-id", required=True)
    meta_p.add_argument("--from-chapter", type=int, default=0)
    meta_p.add_argument("--to-chapter", type=int, default=0)
    meta_p.add_argument("--skip-story", action="store_true")
    meta_p.add_argument("--skip-chapters", action="store_true")
    meta_p.add_argument("--apply", action="store_true")
    meta_p.add_argument("--dry-run", action="store_true")
    meta_p.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    meta_p.add_argument("--translate-model", default="qwen3:14b")
    meta_p.add_argument("--story-model", default="")

    discover_p = sub.add_parser("discover", help="discover_hot_stories.py — tìm truyện mới")
    discover_p.add_argument("--pages", type=int, default=2)
    discover_p.add_argument("--min-chapters", type=int, default=30)
    discover_p.add_argument("--sources", nargs="*", default=[])
    discover_p.add_argument("--timeout", type=int, default=30)
    discover_p.add_argument("--json", action="store_true")

    crawl_p = sub.add_parser("crawl-stories", help="crawl_stories_from_db.py — crawl chapter mới")
    _add_crawl_options(crawl_p)

    crawl_one_p = sub.add_parser("crawl-story", help="Crawl một story theo --story-id")
    crawl_one_p.add_argument("--story-id", required=True)
    _add_crawl_options(crawl_one_p)

    args = parser.parse_args()
    handlers = {
        "repolish": cmd_repolish,
        "retranslate": cmd_retranslate,
        "recrawl-story": cmd_recrawl_story,
        "recrawl-chapters": cmd_recrawl_chapters,
        "translate-metadata": cmd_translate_metadata,
        "discover": cmd_discover,
        "crawl-stories": cmd_crawl_stories,
        "crawl-story": cmd_crawl_story,
    }
    raise SystemExit(handlers[args.command](args))


if __name__ == "__main__":
    main()
