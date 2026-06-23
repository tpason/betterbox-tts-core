#!/usr/bin/env python3
"""Worker: audit polished chapters and enqueue repairs for failures."""
from __future__ import annotations

import argparse
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
for p in (str(ROOT), str(SCRIPT_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from quality_audit import (
    DEFAULT_PRIORITY_STORY_IDS,
    audit_story_range,
    backfill_priority_stories,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Quality audit worker (Ollama local, tiered QA)")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--story-id", action="append", default=[])
    parser.add_argument("--from-chapter", type=int, default=0)
    parser.add_argument("--to-chapter", type=int, default=0)
    parser.add_argument("--limit", type=int, default=50, help="Max chapters per story per cycle")
    parser.add_argument("--judge-sample", type=int, default=5)
    parser.add_argument("--no-repair", action="store_true", help="Audit only, do not enqueue repairs")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--backfill-priority", action="store_true")
    parser.add_argument("--idle-sleep", type=float, default=30.0)
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--judge-model", default="qwen3:14b")
    args = parser.parse_args()

    repair = not args.no_repair
    story_ids = args.story_id or DEFAULT_PRIORITY_STORY_IDS

    if args.backfill_priority:
        backfill_priority_stories(
            judge_sample=args.judge_sample,
            repair=repair,
            dry_run=args.dry_run,
            limit_per_story=args.limit,
            story_ids=story_ids,
        )
        return

    worker_id = f"qa-{socket.gethostname()}"
    print(f"[QA_WORKER] {worker_id} stories={len(story_ids)} repair={repair} judge_sample={args.judge_sample}")

    while True:
        any_work = False
        for sid in story_ids:
            summary = audit_story_range(
                sid,
                from_chapter=args.from_chapter,
                to_chapter=args.to_chapter,
                only_needing_audit=True,
                limit=args.limit,
                judge_sample=args.judge_sample,
                repair=repair,
                dry_run=args.dry_run,
                ollama_url=args.ollama_url,
                judge_model=args.judge_model,
            )
            if summary["audited"] > 0:
                any_work = True
                print(
                    f"[QA_WORKER] {summary.get('story_title', sid)[:40]}: "
                    f"audited={summary['audited']} passed={summary['passed']} "
                    f"failed={summary['failed']} repaired={summary['repaired']}"
                )
        if args.once:
            break
        if not any_work:
            print(f"[QA_WORKER] idle — sleep {args.idle_sleep}s")
            time.sleep(args.idle_sleep)


if __name__ == "__main__":
    main()
