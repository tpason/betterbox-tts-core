#!/usr/bin/env python3
"""Fleet-wide sequential translate/polish quality pipeline.

Mục tiêu: mọi story có polished output đủ tốt cho story_reader + TTS.

Commands:
  status          — hàng đợi + tiến độ
  qa              — QA gate một lần
  repolish-batch  — repolish một dải chapter
  run             — repolish + QA cho story/batch (có thể giới hạn batch)
  auto            — **unattended daemon**: resource check → batch → QA → story tiếp → lặp

Auto mode kiểm tra GPU VRAM / RAM / CPU trước mỗi batch để tránh OOM crash.

Usage:
  # Chạy tự động (khuyến nghị — tmux)
  bash scripts/story_pipeline/run_fleet_quality_daemon.sh --tmux

  # Hoặc trực tiếp
  viterbox/venv/bin/python scripts/story_pipeline/story_quality_pipeline.py auto

  # Xem hàng đợi
  viterbox/venv/bin/python scripts/story_pipeline/story_quality_pipeline.py status
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
for p in (str(ROOT), str(SCRIPT_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from story_db.story_pipeline_db import repository as repo

from story_quality_common import (
    pipeline_mode,
    quality_meta,
    resolve_golden_profile,
    story_source_code,
    update_quality_meta,
)
from story_quality_verify import (
    format_status_row,
    list_stories_for_pipeline,
    run_story_qa,
    story_progress,
)
from resource_guard import ResourceThresholds, snapshot, wait_until_safe

# Core stories — luôn đứng đầu hàng đợi fleet repolish.
DEFAULT_PRIORITY_STORY_IDS = [
    "13cc6d36-4fe7-4dc1-980f-c001ecd9e535",  # Vĩnh Thoái Hiệp Sĩ (hako, western_fantasy)
    "1a1af87a-e85e-476f-87b7-1aeac2dadb1d",  # A Regressor's Tale (wetriedtls)
]

DEFAULT_SOURCE_CODES = [
    "hako",
    "wetriedtls",
    "skydemonorder",
    "royalroad",
    "lightnovelpub",
    "truyenfull_today",
]


def _log(msg: str) -> None:
    print(msg, flush=True)


def ensure_resources(args: argparse.Namespace, *, phase: str) -> None:
    """Wait until GPU/RAM/CPU safe before heavy work."""
    if args.no_resource_check:
        return
    if phase == "polish":
        thresholds = ResourceThresholds.polish()
        require_gpu = True
    elif phase == "qa" and not args.skip_llm_judge:
        thresholds = ResourceThresholds.qa_llm()
        require_gpu = True
    else:
        thresholds = ResourceThresholds.qa_deterministic()
        require_gpu = False

    unload = []
    if phase == "polish" and args.unload_ollama_before_polish:
        unload = [args.vi_model, args.translate_model, args.judge_model]

    wait_until_safe(
        thresholds,
        label=phase,
        poll_seconds=args.resource_poll,
        max_wait_seconds=args.resource_max_wait,
        ollama_url=args.ollama_url,
        unload_models=unload,
        wait_for_workers=args.wait_for_gpu_workers,
        require_gpu=require_gpu,
        log=_log,
    )


def story_is_complete(story: dict[str, Any]) -> bool:
    meta = quality_meta(story)
    if meta.get("status") == "complete":
        return True
    story_id = str(story["id"])
    progress = story_progress(story_id)
    max_ch = int(progress.get("max_chapter") or 0)
    qa_to = int(meta.get("qa_passed_to_chapter") or 0)
    return max_ch > 0 and qa_to >= max_ch


def filter_incomplete(stories: list[dict[str, Any]], *, include_complete: bool) -> list[dict[str, Any]]:
    if include_complete:
        return stories
    return [s for s in stories if not story_is_complete(s)]


def _py() -> str:
    return sys.executable


def resolve_stories(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.story_id:
        story = repo.get_story_by_id(args.story_id)
        if not story:
            raise SystemExit(f"story_id={args.story_id} not found")
        return [story]
    if args.story_title:
        codes = [args.source_code] if args.source_code else None
        stories = repo.find_stories(
            title_contains=args.story_title,
            source_codes=codes,
            limit=args.max_stories or 20,
        )
        if not stories:
            raise SystemExit(f"No story matching {args.story_title!r}")
        return stories[: args.max_stories] if args.max_stories else stories

    priority = list(DEFAULT_PRIORITY_STORY_IDS)
    if args.priority_ids:
        priority = [x.strip() for x in args.priority_ids.split(",") if x.strip()]

    source_codes = None
    if args.source_code:
        source_codes = [args.source_code]
    elif not args.all_sources:
        source_codes = DEFAULT_SOURCE_CODES

    return list_stories_for_pipeline(
        source_codes=source_codes,
        min_polished_chapters=args.min_polished_chapters,
        limit=args.max_stories,
        priority_ids=priority,
    )


def run_vi_repolish(story: dict[str, Any], args: argparse.Namespace, from_ch: int, to_ch: int) -> int:
    cmd = [
        _py(),
        str(SCRIPT_DIR / "repolish_story_from_db.py"),
        "--story-id", str(story["id"]),
        "--ollama-url", args.ollama_url,
        "--overwrite",
        "--source-vi",
        "--from-chapter", str(from_ch),
        "--to-chapter", str(to_ch),
        "--vi-model", args.vi_model,
        "--prompt-profile", args.prompt_profile,
    ]
    if args.log_file:
        cmd.extend(["--log-file", args.log_file])
    _log(f"[VI-REPOLISH] {story.get('title')} ch{from_ch}-{to_ch}")
    result = subprocess.run(cmd, cwd=ROOT, timeout=args.repolish_timeout or None)
    return int(result.returncode or 0)


def run_en_repolish(story: dict[str, Any], args: argparse.Namespace, from_ch: int, to_ch: int) -> int:
    """EN translate+polish via wetriedtls_auto repolish (no QA)."""
    from wetriedtls_auto_pipeline import run_repolish, run_preflight

    en_args = argparse.Namespace(
        source_code=story_source_code(story) or "wetriedtls",
        ollama_url=args.ollama_url,
        vi_model=args.vi_model,
        translate_model=args.translate_model,
        preflight_chapters=args.preflight_chapters,
        preflight_timeout=3600,
        repolish_timeout=args.repolish_timeout,
        inline_repolish=args.inline_repolish,
        log_dir=args.log_dir,
        skip_preflight=args.skip_preflight,
    )
    _log(f"[EN-REPOLISH] {story.get('title')} ch{from_ch}-{to_ch}")
    if not args.skip_preflight:
        run_preflight(story, en_args)
    return run_repolish(story, en_args, from_ch, to_ch)


def repolish_batch(story: dict[str, Any], args: argparse.Namespace, from_ch: int, to_ch: int) -> bool:
    story_id = str(story["id"])
    update_quality_meta(story_id, {
        "status": "repolishing",
        "last_batch_from": from_ch,
        "last_batch_to": to_ch,
        "last_batch_started_at": datetime.now(timezone.utc).isoformat(),
    })
    mode = pipeline_mode(story)
    for attempt in range(1, args.max_retries + 1):
        try:
            ensure_resources(args, phase="polish")
            if mode == "vi_polish":
                rc = run_vi_repolish(story, args, from_ch, to_ch)
            else:
                rc = run_en_repolish(story, args, from_ch, to_ch)
        except RuntimeError as exc:
            _log(f"[REPOLISH] resource wait failed (attempt {attempt}): {exc}")
            rc = 1
        if rc == 0:
            update_quality_meta(story_id, {
                "status": "repolished",
                "last_repolish_at": datetime.now(timezone.utc).isoformat(),
            })
            return True
        _log(f"[REPOLISH] attempt {attempt}/{args.max_retries} failed rc={rc}")
        if attempt < args.max_retries:
            time.sleep(args.retry_pause)
    update_quality_meta(story_id, {"status": "repolish_failed"})
    return False


def qa_batch(story: dict[str, Any], args: argparse.Namespace, from_ch: int, to_ch: int) -> bool:
    story_id = str(story["id"])
    update_quality_meta(story_id, {"status": "qa_running"})
    try:
        ensure_resources(args, phase="qa")
    except RuntimeError as exc:
        _log(f"[QA] resource wait failed: {exc}")
        update_quality_meta(story_id, {"status": "qa_resource_blocked"})
        return False
    result = run_story_qa(
        story,
        from_chapter=from_ch,
        to_chapter=to_ch,
        skip_llm_judge=args.skip_llm_judge,
        ollama_url=args.ollama_url,
        judge_model=args.judge_model,
    )
    passed = bool(result.get("passed"))
    patch: dict[str, Any] = {
        "status": "qa_passed" if passed else "qa_failed",
        "last_qa_passed": passed,
    }
    if passed:
        patch["qa_passed_at"] = datetime.now(timezone.utc).isoformat()
        patch["qa_passed_to_chapter"] = to_ch
    update_quality_meta(story_id, patch)
    return passed


def next_batch_range(story_id: str, current_to: int, batch_size: int) -> tuple[int, int] | None:
    progress = story_progress(story_id)
    max_ch = int(progress.get("max_chapter") or 0)
    if not max_ch or current_to >= max_ch:
        return None
    start = current_to + 1
    end = min(max_ch, start + batch_size - 1)
    return start, end


def resume_from_chapter(story: dict[str, Any], args: argparse.Namespace) -> int:
    meta = quality_meta(story)
    if args.from_chapter:
        return args.from_chapter
    qa_to = int(meta.get("qa_passed_to_chapter") or 0)
    if qa_to > 0:
        return qa_to + 1
    return 1


def process_story(story: dict[str, Any], args: argparse.Namespace) -> bool:
    """Run batch loop for one story. Returns True if story fully QA-passed."""
    story_id = str(story["id"])
    progress = story_progress(story_id)
    max_ch = int(progress.get("max_chapter") or 0)
    if not max_ch:
        _log(f"[SKIP] {story.get('title')} — no chapters")
        return False

    from_ch = resume_from_chapter(story, args)
    batch_size = args.batch_size
    to_ch = min(max_ch, from_ch + batch_size - 1)
    batches = 0

    _log(f"\n{'='*70}\n[STORY] {story.get('title')} ({pipeline_mode(story)}) "
         f"ch{from_ch}→{max_ch} batch_size={batch_size}\n{'='*70}")

    update_quality_meta(story_id, {"status": "in_progress", "mode": pipeline_mode(story)})

    while from_ch <= max_ch:
        batches += 1
        if args.max_batches_per_story and batches > args.max_batches_per_story:
            _log(f"[STORY] max_batches_per_story={args.max_batches_per_story} reached")
            break

        _log(f"\n[BATCH {batches}] ch{from_ch}-{to_ch}")
        if not args.qa_only:
            if not repolish_batch(story, args, from_ch, to_ch):
                _log(f"[STOP] repolish failed ch{from_ch}-{to_ch}")
                return False

        qa_from = from_ch
        qa_to = min(to_ch, from_ch + args.qa_sample - 1) if args.qa_sample else to_ch
        if not qa_batch(story, args, qa_from, qa_to):
            _log(f"[STOP] QA failed ch{qa_from}-{qa_to}")
            if args.stop_on_qa_fail:
                update_quality_meta(story_id, {"status": "qa_blocked"})
                if args.auto_continue_on_qa_fail:
                    _log("[AUTO] QA blocked — skip to next story")
                    return False
                return False
            _log("[RETRY] repolish same batch once more")
            if not repolish_batch(story, args, from_ch, to_ch):
                return False
            if not qa_batch(story, args, qa_from, qa_to):
                _log(f"[STOP] QA still failed after retry")
                return False

        _log(f"[OK] batch ch{from_ch}-{to_ch} QA passed (sample ch{qa_from}-{qa_to})")

        if to_ch >= max_ch:
            update_quality_meta(story_id, {"status": "complete", "completed_at": datetime.now(timezone.utc).isoformat()})
            _log(f"[DONE] story complete through ch{max_ch}")
            return True

        nxt = next_batch_range(story_id, to_ch, batch_size)
        if not nxt:
            break
        from_ch, to_ch = nxt
        time.sleep(args.batch_pause)

    return False


def cmd_status(args: argparse.Namespace) -> None:
    snap = snapshot()
    _log(f"[RESOURCES] {snap.summary()}")
    stories = resolve_stories(args)
    _log(f"{'TITLE':<48} | {'SOURCE':<16} | {'MODE':<20} | PROGRESS           | QA   | STATUS")
    _log("-" * 120)
    for story in stories:
        _log(format_status_row(story))


def cmd_qa(args: argparse.Namespace) -> None:
    ensure_resources(args, phase="qa")
    stories = resolve_stories(args)
    for story in stories:
        run_story_qa(
            story,
            from_chapter=args.from_chapter,
            to_chapter=args.to_chapter,
            skip_llm_judge=args.skip_llm_judge,
            ollama_url=args.ollama_url,
            judge_model=args.judge_model,
            json_out=args.json_out,
        )


def cmd_repolish_batch(args: argparse.Namespace) -> None:
    stories = resolve_stories(args)
    if not args.from_chapter or not args.to_chapter:
        raise SystemExit("--from-chapter and --to-chapter required for repolish-batch")
    for story in stories:
        ok = repolish_batch(story, args, args.from_chapter, args.to_chapter)
        if not ok:
            raise SystemExit(1)


def cmd_run(args: argparse.Namespace) -> None:
    stories = filter_incomplete(resolve_stories(args), include_complete=args.include_complete)
    _log(f"[RUN] {len(stories)} story(s) sequential pipeline")
    for i, story in enumerate(stories, 1):
        _log(f"\n[RUN] story {i}/{len(stories)}")
        process_story(story, args)
    _log("[RUN] fleet pipeline finished")


def cmd_auto(args: argparse.Namespace) -> None:
    """Unattended daemon: resource-safe batch loop across entire fleet."""
    _log("[AUTO] fleet quality daemon started")
    _log(f"[AUTO] resources: {snapshot().summary()}")
    cycle = 0
    while True:
        cycle += 1
        stories = filter_incomplete(resolve_stories(args), include_complete=False)
        if not stories:
            _log(f"[AUTO] cycle {cycle}: no incomplete stories — sleep {args.daemon_idle}s")
            time.sleep(args.daemon_idle)
            continue

        _log(f"\n[AUTO] cycle {cycle}: {len(stories)} story(s) in queue")
        for i, story in enumerate(stories, 1):
            if story_is_complete(story):
                continue
            _log(f"\n[AUTO] story {i}/{len(stories)}: {story.get('title')}")
            args.max_batches_per_story = 0  # unlimited in auto mode
            process_story(story, args)

        _log(f"[AUTO] cycle {cycle} done — sleep {args.daemon_idle}s before re-scan")
        time.sleep(args.daemon_idle)


def add_resource_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--no-resource-check", action="store_true", help="Skip GPU/RAM/CPU wait")
    parser.add_argument("--resource-poll", type=int, default=30, help="Seconds between resource polls")
    parser.add_argument(
        "--resource-max-wait",
        type=int,
        default=0,
        help="Max seconds to wait for resources (0=infinite, auto mode default)",
    )
    parser.add_argument(
        "--wait-for-gpu-workers",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Wait until audio workers finish before polish",
    )
    parser.add_argument(
        "--unload-ollama-before-polish",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Unload Ollama models before polish if still in VRAM",
    )
    parser.add_argument("--max-retries", type=int, default=3, help="Retries per batch on repolish failure")
    parser.add_argument("--retry-pause", type=int, default=60, help="Seconds between retries")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fleet translate/polish quality pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--story-id", default="")
    common.add_argument("--story-title", default="")
    common.add_argument("--source-code", default="", help="Filter single source")
    common.add_argument("--all-sources", action="store_true", help="Include all sources (slow)")
    common.add_argument("--max-stories", type=int, default=0)
    common.add_argument("--min-polished-chapters", type=int, default=1)
    common.add_argument("--priority-ids", default="", help="Comma-separated story UUIDs first")
    common.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    common.add_argument("--vi-model", default="qwen3:14b")
    common.add_argument("--translate-model", default="qwen3:14b")
    common.add_argument("--judge-model", default="qwen3:14b")
    common.add_argument("--prompt-profile", default="full")
    common.add_argument("--skip-llm-judge", action="store_true")
    common.add_argument("--from-chapter", type=int, default=0)
    common.add_argument("--to-chapter", type=int, default=0)
    common.add_argument("--json-out", default="")
    add_resource_args(common)

    sub.add_parser("status", parents=[common], help="Show pipeline queue + progress")

    sub.add_parser("qa", parents=[common], help="Run QA gate for story/range")

    repolish_p = sub.add_parser("repolish-batch", parents=[common], help="Repolish one chapter range")
    repolish_p.add_argument("--repolish-timeout", type=int, default=0)
    repolish_p.add_argument("--log-file", default="")
    repolish_p.add_argument("--skip-preflight", action="store_true")
    repolish_p.add_argument("--inline-repolish", action="store_true")
    repolish_p.add_argument("--preflight-chapters", type=int, default=20)
    repolish_p.add_argument("--log-dir", default="/tmp/story_quality_pipeline")

    run_p = sub.add_parser("run", parents=[common], help="Sequential repolish + QA loop")
    run_p.add_argument("--batch-size", type=int, default=30, help="Chapters per repolish batch")
    run_p.add_argument("--qa-sample", type=int, default=20, help="Chapters to QA per batch (0=all)")
    run_p.add_argument("--max-batches-per-story", type=int, default=0, help="0=unlimited")
    run_p.add_argument("--repolish-timeout", type=int, default=0)
    run_p.add_argument("--preflight-chapters", type=int, default=20)
    run_p.add_argument("--skip-preflight", action="store_true")
    run_p.add_argument("--inline-repolish", action="store_true")
    run_p.add_argument("--qa-only", action="store_true", help="Skip repolish, only QA")
    run_p.add_argument("--stop-on-qa-fail", action="store_true", default=True)
    run_p.add_argument("--no-stop-on-qa-fail", dest="stop_on_qa_fail", action="store_false")
    run_p.add_argument("--batch-pause", type=int, default=2, help="Seconds between batches")
    run_p.add_argument("--log-dir", default="/tmp/story_quality_pipeline")
    run_p.add_argument("--log-file", default="")
    run_p.add_argument("--include-complete", action="store_true", help="Reprocess completed stories")
    run_p.add_argument(
        "--auto-continue-on-qa-fail",
        action="store_true",
        default=True,
        help="In auto mode: skip story on QA fail, continue fleet",
    )
    run_p.add_argument("--no-auto-continue-on-qa-fail", dest="auto_continue_on_qa_fail", action="store_false")

    auto_p = sub.add_parser("auto", parents=[common], help="Unattended daemon with resource guards")
    auto_p.add_argument("--batch-size", type=int, default=30)
    auto_p.add_argument("--qa-sample", type=int, default=20)
    auto_p.add_argument("--daemon-idle", type=int, default=300, help="Seconds between fleet re-scans")
    auto_p.add_argument("--repolish-timeout", type=int, default=0)
    auto_p.add_argument("--preflight-chapters", type=int, default=20)
    auto_p.add_argument("--skip-preflight", action="store_true")
    auto_p.add_argument("--inline-repolish", action="store_true")
    auto_p.add_argument("--log-dir", default="/tmp/story_quality_pipeline")
    auto_p.add_argument("--stop-on-qa-fail", action="store_true", default=True)
    auto_p.add_argument("--no-stop-on-qa-fail", dest="stop_on_qa_fail", action="store_false")
    auto_p.add_argument("--auto-continue-on-qa-fail", action="store_true", default=True)
    auto_p.add_argument("--no-auto-continue-on-qa-fail", dest="auto_continue_on_qa_fail", action="store_false")
    auto_p.add_argument("--batch-pause", type=int, default=5)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    cmds = {
        "status": cmd_status,
        "qa": cmd_qa,
        "repolish-batch": cmd_repolish_batch,
        "run": cmd_run,
        "auto": cmd_auto,
    }
    cmds[args.command](args)


if __name__ == "__main__":
    main()
