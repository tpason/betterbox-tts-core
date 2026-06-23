#!/usr/bin/env python3
"""Pre-flight checks before running translate/polish in production.

Runs unit tests, deterministic quality gates, and optional live Ollama smoke.
Exit 0 = safe to run workers; exit 1 = fix before production.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SP = ROOT / "scripts" / "story_pipeline"
VENV_PY = ROOT / "viterbox" / "venv" / "bin" / "python"


def _run(cmd: list[str], *, label: str) -> bool:
    print(f"\n=== {label} ===")
    print(" ".join(cmd))
    r = subprocess.run(cmd, cwd=ROOT)
    ok = r.returncode == 0
    print(f"{'PASS' if ok else 'FAIL'}: {label}")
    return ok


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify quality pipeline before production run")
    parser.add_argument("--skip-pytest", action="store_true")
    parser.add_argument("--live-smoke", action="store_true", help="Run 1-chunk novel translation (needs Ollama)")
    parser.add_argument("--story-id", default="1a1af87a-e85e-476f-87b7-1aeac2dadb1d")
    parser.add_argument("--chapter", type=int, default=50)
    args = parser.parse_args()

    py = str(VENV_PY if VENV_PY.exists() else sys.executable)
    ok = True

    if not args.skip_pytest:
        ok &= _run(
            [
                py, "-m", "pytest",
                "tests/story_db/test_apply_migrations_order.py",
                "tests/story_pipeline/test_chapter_save_guard.py",
                "tests/story_pipeline/test_novel_translation_adapter.py",
                "tests/story_pipeline/test_polish_worker_save_gate.py",
                "tests/story_pipeline/test_term_alignment_check.py",
                "tests/story_pipeline/test_quality_audit.py",
                "tests/story_pipeline/test_quality_remediation.py",
                "-q",
            ],
            label="unit tests (22)",
        )

    # Deterministic gate: bad translation must not pass save guard
    sys.path.insert(0, str(SP))
    from chapter_save_guard import SaveGateError, verify_chapter_save_allowed  # noqa: E402

    bad_vi = "Tôi tạo thế kí. " + ("Anh ta bước vào phòng. " * 30)
    source = "I formed a hand seal."
    try:
        verify_chapter_save_allowed(
            bad_vi,
            source_text=source,
            genre="korean_cultivation",
        )
        print("\nFAIL: save guard allowed hand seal → thế kí")
        ok = False
    except SaveGateError:
        print("\nPASS: save guard blocks hand seal → thế kí")

    if args.live_smoke:
        ok &= _run(
            [
                py, str(SP / "run_novel_translation.py"),
                "--story-id", args.story_id,
                "--chapter", str(args.chapter),
                "--max-chunks", "1",
            ],
            label=f"live smoke ch{args.chapter} (1 chunk, no save)",
        )
        print(
            "\nNote: partial run success=False is OK if final_blocking lists term issues "
            "and output is NOT saved to DB."
        )

    print("\n" + ("READY for production worker" if ok else "NOT READY — fix failures above"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
