#!/usr/bin/env python3
"""Automated wetriedtls pipeline: preflight → polish/repolish → full QA → next batch/story.

QA gate (each batch):
  1. gap scan + golden checklist
  2. deterministic quality scan (same as polish worker)
  3. LLM judge — 1 Ollama call/chapter (--skip-llm-judge to disable)

On QA pass → repolish next chapter batch (or next story).
On QA fail → repolish failed range and retry (up to --max-qa-retries).

Usage:
  # Auto: finish current story sample, QA, expand if pass
  viterbox/venv/bin/python scripts/story_pipeline/wetriedtls_auto_pipeline.py \\
      --story-id 1a1af87a-e85e-476f-87b7-1aeac2dadb1d \\
      --qa-from-chapter 1 --qa-to-chapter 5 \\
      --expand-batch-size 25

  # All wetriedtls stories (sequential)
  viterbox/venv/bin/python scripts/story_pipeline/wetriedtls_auto_pipeline.py \\
      --source-code wetriedtls --max-stories 5
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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from story_db.story_pipeline_db import repository as repo
from story_db.story_pipeline_db.db import connect
from wetriedtls_verify import run_qa_gate, print_qa_report


def _log(msg: str) -> None:
    print(msg, flush=True)


def _py() -> str:
    return sys.executable


def resolve_stories(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.story_id:
        story = repo.get_story_by_id(args.story_id)
        if not story:
            raise SystemExit(f"story_id={args.story_id} not found")
        return [story]
    if args.story_title:
        stories = repo.find_stories(
            title_contains=args.story_title,
            source_codes=[args.source_code],
            limit=args.max_stories or 20,
        )
        if not stories:
            raise SystemExit(f"No story matching {args.story_title!r}")
        return stories[: args.max_stories] if args.max_stories else stories
    stories = repo.list_active_stories(source_codes=[args.source_code], limit=args.max_stories or 0)
    if not stories:
        raise SystemExit(f"No active stories for source {args.source_code!r}")
    return stories


def count_pending_polish(story_id: str) -> int:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*)::int AS n FROM story_jobs
            WHERE story_id = %s AND job_type = 'polish_chapter'
              AND status IN ('pending', 'running')
            """,
            (story_id,),
        ).fetchone()
        return int(row["n"] or 0) if row else 0


def run_subprocess(cmd: list[str], *, label: str, timeout: int = 0) -> int:
    _log(f"[RUN] {label}")
    try:
        result = subprocess.run(cmd, cwd=ROOT, timeout=timeout or None)
        return int(result.returncode or 0)
    except subprocess.TimeoutExpired:
        _log(f"[WARN] {label} timed out")
        return 1


def run_preflight(story: dict[str, Any], args: argparse.Namespace) -> None:
    cmd = [
        _py(),
        str(SCRIPT_DIR / "wetriedtls_preflight.py"),
        "--story-id", str(story["id"]),
        "--story-title", str(story.get("title") or ""),
        "--source-code", args.source_code,
        "--ollama-url", args.ollama_url,
        "--model", args.vi_model,
        "--preflight-chapters", str(args.preflight_chapters),
        "--apply",
    ]
    run_subprocess(cmd, label=f"preflight {story.get('title')}", timeout=args.preflight_timeout)


def enqueue_polish_range(story: dict[str, Any], args: argparse.Namespace, from_ch: int, to_ch: int) -> int:
    """Reset chapter outputs in range and enqueue fresh translate+polish jobs."""
    from wetriedtls_reset_test_range import reset_story_test_range

    reset_story_test_range(
        str(story["id"]),
        from_chapter=from_ch,
        to_chapter=to_ch,
        clear_char_map=False,
        clear_glossary=False,
        clear_genre=False,
        dry_run=False,
    )
    meta = story.get("metadata") or {}
    slug = str(meta.get("slug") or story.get("source_story_id") or "a-regressors-tale-of-cultivation")
    cmd = [
        _py(),
        str(SCRIPT_DIR / "crawl_wetriedtls_chapters.py"),
        "--series-slug", slug,
        "--from-chapter", str(from_ch),
        "--to-chapter", str(to_ch),
        "--upsert-db", "--download-text", "--no-write-files",
        "--enqueue-polish", "--post-translate", "polish",
        "--translate-model", args.translate_model,
    ]
    return run_subprocess(cmd, label=f"enqueue polish ch{from_ch}-{to_ch}", timeout=600)


def run_repolish(story: dict[str, Any], args: argparse.Namespace, from_ch: int, to_ch: int) -> int:
    """Full re-translate+polish for EN wetriedtls via job queue; inline repolish fallback."""
    if args.source_code == "wetriedtls" and not args.inline_repolish:
        rc = enqueue_polish_range(story, args, from_ch, to_ch)
        if rc != 0:
            return rc
        proc = start_polish_worker(str(story["id"]), args)
        wait_polish_drain(str(story["id"]), args, proc)
        return 0
    cmd = [
        _py(),
        str(SCRIPT_DIR / "repolish_story_from_db.py"),
        "--story-id", str(story["id"]),
        "--ollama-url", args.ollama_url,
        "--overwrite",
        "--from-chapter", str(from_ch),
        "--to-chapter", str(to_ch),
        "--vi-model", args.vi_model,
    ]
    return run_subprocess(
        cmd,
        label=f"repolish ch{from_ch}-{to_ch} {story.get('title')}",
        timeout=args.repolish_timeout,
    )


def start_polish_worker(story_id: str, args: argparse.Namespace) -> subprocess.Popen:
    cmd = [
        _py(),
        str(SCRIPT_DIR / "polish_worker.py"),
        "--story-id", story_id,
        "--source-code", args.source_code,
        "--ollama-url", args.ollama_url,
        "--vi-model", args.vi_model,
        "--translate-model", args.translate_model,
        "--no-save-files",
        "--overwrite",
        "--idle-sleep", "5",
        "--idle-log-interval", "120",
    ]
    _log(f"[WORKER] starting polish worker story_id={story_id}")
    log_path = Path(args.log_dir) / f"polish_{story_id[:8]}.log" if args.log_dir else None
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = log_path.open("a", encoding="utf-8")
        return subprocess.Popen(cmd, cwd=ROOT, stdout=fh, stderr=fh)
    return subprocess.Popen(cmd, cwd=ROOT)


def wait_polish_drain(story_id: str, args: argparse.Namespace, proc: subprocess.Popen | None) -> None:
    _log(f"[WORKER] waiting for polish queue drain (poll {args.poll_interval}s)")
    while True:
        pending = count_pending_polish(story_id)
        if pending == 0:
            _log("[WORKER] polish queue empty")
            break
        if pending:
            _log(f"[WORKER] pending={pending} running...")
        time.sleep(args.poll_interval)
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()


def mark_qa_passed(story_id: str, from_ch: int, to_ch: int) -> None:
    repo.update_story_metadata(
        story_id,
        {
            "qa_passed_at": datetime.now(timezone.utc).isoformat(),
            "qa_passed_from_chapter": from_ch,
            "qa_passed_to_chapter": to_ch,
        },
    )


def polished_in_range(story_id: str, from_ch: int, to_ch: int) -> int:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*)::int AS n FROM chapters
            WHERE story_id = %s AND chapter_number BETWEEN %s AND %s
              AND is_polished AND polished_text_content IS NOT NULL
            """,
            (story_id, from_ch, to_ch),
        ).fetchone()
        return int(row["n"] or 0) if row else 0


def process_story_batch(
    story: dict[str, Any],
    args: argparse.Namespace,
    from_ch: int,
    to_ch: int,
) -> bool:
    """Polish/repolish range → full QA. Returns True if QA passed."""
    story_id = str(story["id"])
    expected = max(0, to_ch - from_ch + 1)
    _log(f"\n{'='*60}\n[BATCH] {story.get('title')} ch{from_ch}-{to_ch}\n{'='*60}")

    if not args.skip_preflight:
        run_preflight(story, args)

    pending = count_pending_polish(story_id)
    polished = polished_in_range(story_id, from_ch, to_ch)
    proc: subprocess.Popen | None = None

    if args.force_repolish:
        with connect() as conn:
            conn.execute(
                """
                DELETE FROM story_jobs
                WHERE story_id = %s AND job_type = 'polish_chapter'
                  AND status IN ('pending', 'running')
                """,
                (story_id,),
            )
        pending = 0
        _log("[BATCH] force-repolish — cleared pending/running polish jobs")

    if pending > 0:
        proc = start_polish_worker(story_id, args)
        wait_polish_drain(story_id, args, proc)
    elif polished < expected or args.always_repolish:
        _log(f"[BATCH] repolish (polished {polished}/{expected} in range)")
        if run_repolish(story, args, from_ch, to_ch) != 0:
            return False
    else:
        _log(f"[BATCH] skip repolish — {polished}/{expected} already polished")

    for attempt in range(1, args.max_qa_retries + 1):
        _log(f"\n[QA] attempt {attempt}/{args.max_qa_retries} ch{from_ch}-{to_ch}")
        result = run_qa_gate(
            story,
            source_code=args.source_code,
            from_chapter=from_ch,
            to_chapter=to_ch,
            llm_judge=not args.skip_llm_judge,
            ollama_url=args.ollama_url,
            judge_model=args.judge_model,
        )
        print_qa_report(result)
        if result["passed"]:
            mark_qa_passed(story_id, from_ch, to_ch)
            return True
        if attempt < args.max_qa_retries:
            _log("[QA] fail → repolish retry")
            if run_repolish(story, args, from_ch, to_ch) != 0:
                _log("[QA] repolish failed")
                return False
    return False


def next_expand_range(story_id: str, current_to: int, batch_size: int) -> tuple[int, int] | None:
    progress = repo.get_story_chapter_progress(story_id)
    max_ch = int(progress.get("max_chapter") or 0)
    if not max_ch or current_to >= max_ch:
        return None
    start = current_to + 1
    end = min(max_ch, start + batch_size - 1)
    return start, end


def main() -> None:
    parser = argparse.ArgumentParser(description="Automated wetriedtls preflight + repolish + QA loop.")
    parser.add_argument("--story-id", default="")
    parser.add_argument("--story-title", default="")
    parser.add_argument("--source-code", default="wetriedtls")
    parser.add_argument("--max-stories", type=int, default=0)
    parser.add_argument("--qa-from-chapter", type=int, default=1)
    parser.add_argument("--qa-to-chapter", type=int, default=5, help="Initial QA sample range.")
    parser.add_argument("--expand-batch-size", type=int, default=25, help="Chapters per batch after QA pass (0=stop).")
    parser.add_argument("--max-batches", type=int, default=10, help="Max expand batches per story.")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--vi-model", default="qwen3:14b")
    parser.add_argument("--translate-model", default="qwen3:14b")
    parser.add_argument("--judge-model", default="qwen3:14b")
    parser.add_argument("--preflight-chapters", type=int, default=20)
    parser.add_argument("--preflight-timeout", type=int, default=3600)
    parser.add_argument("--repolish-timeout", type=int, default=7200)
    parser.add_argument("--max-qa-retries", type=int, default=2)
    parser.add_argument("--poll-interval", type=int, default=30)
    parser.add_argument("--skip-preflight", action="store_true")
    parser.add_argument("--skip-llm-judge", action="store_true")
    parser.add_argument("--always-repolish", action="store_true", help="Repolish range even if chapters already polished.")
    parser.add_argument(
        "--force-repolish",
        action="store_true",
        help="Cancel pending polish jobs and repolish range before QA (fresh pass).",
    )
    parser.add_argument(
        "--inline-repolish",
        action="store_true",
        help="Use repolish_story_from_db.py instead of crawl+enqueue+worker (polish-only).",
    )
    parser.add_argument("--log-dir", default="/tmp/wetriedtls_auto")
    args = parser.parse_args()

    stories = resolve_stories(args)
    _log(f"[AUTO] {len(stories)} story(s) source={args.source_code}")

    for story in stories:
        story_id = str(story["id"])
        from_ch = args.qa_from_chapter
        to_ch = args.qa_to_chapter
        batches = 0

        while True:
            ok = process_story_batch(story, args, from_ch, to_ch)
            if not ok:
                _log(f"[AUTO] STOP story={story.get('title')} — QA failed ch{from_ch}-{to_ch}")
                break
            _log(f"[AUTO] ✓ QA passed ch{from_ch}-{to_ch}")
            batches += 1
            if args.expand_batch_size <= 0 or batches >= args.max_batches:
                break
            nxt = next_expand_range(story_id, to_ch, args.expand_batch_size)
            if not nxt:
                _log(f"[AUTO] story complete (no more chapters after {to_ch})")
                break
            from_ch, to_ch = nxt
            _log(f"[AUTO] expand → repolish ch{from_ch}-{to_ch}")

    _log("[AUTO] pipeline finished")


if __name__ == "__main__":
    main()
