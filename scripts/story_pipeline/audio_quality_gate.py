#!/usr/bin/env python3
"""
Audio quality gate: batch-analyze generated WAV files, optionally re-enqueue bad chapters.

NOTE: For new chapters the quality gate is built into audio_worker_viterbox.py — each chapter
is automatically re-synthesized inline if quality checks fail (up to --max-quality-retries times).

This script is for *post-hoc* batch repair of already-generated chapters that bypassed the
inline gate (e.g. chapters generated before the gate was added, or imported from old files).

Flow:
  1. Scan audio dir for WAV files, match to polished text for word count
  2. Run signal-level analysis per chapter
  3. Print quality report
  4. With --auto-regen: reset DB + delete bad WAVs → run audio worker (which now has
     inline quality retry) → repeat until all pass or max-regen-rounds reached

Auto-regen triggers (TOO_LONG / TOO_SHORT / CLIPPING) are defined in audio_quality.py.

Usage:
  # Analyze only
  python scripts/story_pipeline/audio_quality_gate.py \\
    --story-id <uuid> \\
    --audio-dir story_audio/<slug> \\
    --polished-dir story_data/polished/<slug>

  # Analyze + auto regenerate bad chapters
  python scripts/story_pipeline/audio_quality_gate.py \\
    --story-id <uuid> \\
    --audio-dir story_audio/<slug> \\
    --polished-dir story_data/polished/<slug> \\
    --auto-regen \\
    --reference-audio wavs/vieneu_alloy1512_1005.wav
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from story_db.story_pipeline_db import repository as repo
from story_db.story_pipeline_db.db import connect
from scripts.story_pipeline.audio_quality import (
    REGEN_TRIGGERS,
    load_wav,
    analyze_wav,
    grade,
    regen_issues as filter_regen_issues,
    count_words,
    fmt_duration,
)


# ── DB helpers ────────────────────────────────────────────────────────────────

def load_chapters_for_story(story_id: str) -> dict[int, dict]:
    """Load all chapters for a story in one query. Returns {chapter_number: row_dict}."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, chapter_number, polished_text_path, audio_path,
                   is_audio_generated, polished_text_content
            FROM chapters
            WHERE story_id = %s
            ORDER BY chapter_number
            """,
            (story_id,),
        ).fetchall()
        return {r["chapter_number"]: dict(r) for r in rows}


def reset_chapter_for_regen(chapter_id: str, audio_path: str, story_id: str) -> None:
    """Reset is_audio_generated and force-reset job to pending (bypasses ON CONFLICT done guard)."""
    with connect() as conn:
        conn.execute(
            """
            UPDATE chapters
            SET is_audio_generated = FALSE,
                audio_path = NULL,
                audio_generated_at = NULL,
                updated_at = now()
            WHERE id = %s
            """,
            (chapter_id,),
        )
        conn.execute(
            """
            INSERT INTO story_jobs
                (job_type, chapter_id, story_id, output_path, priority, max_attempts)
            VALUES
                ('audio_chapter', %s, %s, %s, 50, 5)
            ON CONFLICT (job_type, chapter_id) DO UPDATE SET
                status      = 'pending',
                attempts    = 0,
                run_after   = now(),
                output_path = EXCLUDED.output_path,
                priority    = LEAST(story_jobs.priority, EXCLUDED.priority),
                updated_at  = now()
            """,
            (chapter_id, story_id, audio_path),
        )


# ── Analysis ──────────────────────────────────────────────────────────────────

def run_analysis(
    audio_dir: Path,
    polished_dir: Path | None,
    story_id: str,
    regen_triggers: set[str],
) -> list[dict]:
    wav_files = sorted(audio_dir.glob("*.wav"))
    results: list[dict] = []

    # One DB query for all chapters — avoids N round-trips for N WAV files
    chapters_by_num = load_chapters_for_story(story_id)

    for wav in wav_files:
        m = re.search(r"chapter(\d+)", wav.stem)  # explicit prefix — avoids false matches
        chapter_number = int(m.group(1)) if m else None
        chapter_db = chapters_by_num.get(chapter_number) if chapter_number else None

        # Resolve polished text: file → DB content column → skip duration checks
        txt_path = (polished_dir / wav.with_suffix(".txt").name) if polished_dir else None
        if txt_path and txt_path.exists():
            wc = count_words(txt_path.read_text(encoding="utf-8"))
        elif chapter_db and chapter_db.get("polished_text_content"):
            wc = count_words(chapter_db["polished_text_content"])
        else:
            wc = 0  # duration checks skipped

        try:
            audio, sr = load_wav(wav)
        except Exception as exc:
            results.append({
                "name": wav.name, "wav": wav, "chapter_db": chapter_db,
                "stats": {}, "all_issues": [f"LOAD_ERROR:{exc}"],
                "regen_issues": [], "review_issues": [],
            })
            continue

        stats = analyze_wav(audio, sr)
        all_issues = grade(stats, wc, audio=audio, sr=sr)
        ri  = filter_regen_issues(all_issues)
        rev = [i for i in all_issues if i not in ri]

        results.append({
            "name": wav.name, "wav": wav, "chapter_db": chapter_db,
            "stats": stats, "word_count": wc,
            "all_issues": all_issues, "regen_issues": ri, "review_issues": rev,
        })

    return results


# ── Report ────────────────────────────────────────────────────────────────────

def print_report(results: list[dict]) -> None:
    print(f"\n{'Chapter':<22} {'Duration':<12} {'RMS':<7} {'Silence':>8} {'Centroid':>10}  Status")
    print("  " + "-" * 90)
    for r in results:
        if not r["stats"]:
            print(f"  {r['name']:<20} ERROR: {r['all_issues']}")
            continue
        tag = " ✓" if not r["regen_issues"] and not r["review_issues"] else (
            " ✖ REGEN" if r["regen_issues"] else " ⚠ review"
        )
        print(
            f"  {r['name']:<20} {fmt_duration(r['stats']['duration_s']):<12} "
            f"{r['stats']['rms']:.3f}  {r['stats']['silence_ratio']*100:5.1f}%  "
            f"{r['stats']['centroid_hz']:7.0f}Hz{tag}"
        )
        for issue in r["regen_issues"]:
            print(f"    ✖ {issue}")
        for issue in r["review_issues"]:
            print(f"    ⚠ {issue}")
    print()


# ── Regen ─────────────────────────────────────────────────────────────────────

def do_regen(bad: list[dict], story_id: str, dry_run: bool) -> int:
    count = 0
    for r in bad:
        wav: Path = r["wav"]
        ch = r["chapter_db"]
        if ch is None:
            print(f"  [SKIP] {r['name']}: chapter not found in DB")
            continue

        print(f"  [REGEN] ch{ch['chapter_number']:04d} — {', '.join(r['regen_issues'])}")
        if dry_run:
            print(f"    dry-run: would reset DB + delete {wav.name}")
            count += 1
            continue

        reset_chapter_for_regen(ch["id"], str(wav.resolve()), story_id)
        wav.unlink(missing_ok=True)
        print(f"    reset DB + deleted {wav.name}")
        count += 1

    return count


def run_audio_worker(reference_audio: str, n_jobs: int) -> None:
    """Run audio worker to process exactly n_jobs chapters (one model load)."""
    cmd = [sys.executable, "scripts/story_pipeline/audio_worker_viterbox.py",
           "--max-jobs", str(n_jobs),
           "--reference-audio", reference_audio]
    print(f"\n[WORKER] {' '.join(cmd)}\n")
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        print(f"[WORKER] exited with code {result.returncode} — check logs above")


def count_pending_audio_jobs(story_id: str) -> int:
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as n FROM story_jobs WHERE story_id=%s AND job_type='audio_chapter' AND status='pending'",
            (story_id,),
        ).fetchone()
        return int(row["n"]) if row else 0


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Batch-analyze audio quality; re-enqueue bad chapters."
    )
    parser.add_argument("--story-id", required=True)
    parser.add_argument("--audio-dir", required=True)
    parser.add_argument("--polished-dir", default=None)
    parser.add_argument("--auto-regen", action="store_true",
                        help="Reset DB + re-run audio worker for failed chapters")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-regen-rounds", type=int, default=3)
    parser.add_argument("--regen-on", default=",".join(sorted(REGEN_TRIGGERS)),
                        help="Comma-separated issue prefixes that trigger regen")
    parser.add_argument("--reference-audio", default="wavs/vieneu_alloy1512_1005.wav")
    args = parser.parse_args()

    audio_dir    = Path(args.audio_dir)
    polished_dir = Path(args.polished_dir) if args.polished_dir else None
    regen_triggers = set(args.regen_on.upper().split(","))
    story_id     = args.story_id

    if not audio_dir.exists():
        sys.exit(f"Audio dir not found: {audio_dir}")

    if not args.auto_regen:
        results = run_analysis(audio_dir, polished_dir, story_id, regen_triggers)
        total_d = sum(r["stats"].get("duration_s", 0) for r in results)
        print(f"\n=== Audio Quality Report: {audio_dir.name} "
              f"({len(results)} files, {fmt_duration(total_d)}) ===")
        print_report(results)
        need_regen  = [r for r in results if r["regen_issues"]]
        need_review = [r for r in results if r["review_issues"] and not r["regen_issues"]]
        ok_count    = len(results) - len(need_regen) - len(need_review)
        print(f"Summary: {ok_count} OK  |  {len(need_review)} review  |  {len(need_regen)} regen")
        if need_regen:
            print(f"\nRe-run with --auto-regen to regenerate {len(need_regen)} chapter(s).")
        return

    for rnd in range(1, args.max_regen_rounds + 1):
        print(f"\n{'='*60}  Round {rnd}/{args.max_regen_rounds}  {'='*60}")
        results = run_analysis(audio_dir, polished_dir, story_id, regen_triggers)
        total_d = sum(r["stats"].get("duration_s", 0) for r in results)
        print(f"\n=== Quality Report (round {rnd}): {audio_dir.name} "
              f"({len(results)} files, {fmt_duration(total_d)}) ===")
        print_report(results)

        bad = [r for r in results if r["regen_issues"]]
        print(f"  {len(results)-len(bad)}/{len(results)} OK  |  {len(bad)} flagged")

        if not bad:
            print("\n✓ All chapters passed.\n")
            break

        regenned = do_regen(bad, story_id, dry_run=args.dry_run)
        if args.dry_run:
            print(f"\n[dry-run] Would regenerate {regenned} chapters.\n")
            break
        if regenned == 0:
            print("\nNo chapters could be re-enqueued (DB mismatch). Exiting.\n")
            break
        # Always run worker after regen — even on last round — so WAVs are generated
        run_audio_worker(args.reference_audio, n_jobs=regenned)
        if rnd == args.max_regen_rounds:
            print(f"\nMax regen rounds ({args.max_regen_rounds}) reached.\n")

    if args.auto_regen and not args.dry_run:
        print("\n=== Final Quality Report ===")
        results = run_analysis(audio_dir, polished_dir, story_id, regen_triggers)
        total_d = sum(r["stats"].get("duration_s", 0) for r in results)
        print_report(results)
        still_bad = [r for r in results if r["regen_issues"]]
        pending = count_pending_audio_jobs(story_id)
        print(f"Final: {len(results)-len(still_bad)}/{len(results)} WAVs OK  |  "
              f"{len(still_bad)} still flagged")
        if pending:
            print(f"  ⚠ {pending} chapter(s) still pending in job queue (no WAV yet) — run worker to generate")
        print()


if __name__ == "__main__":
    main()
