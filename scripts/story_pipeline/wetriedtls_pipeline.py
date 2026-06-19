#!/usr/bin/env python3
"""
wetriedtls_pipeline.py — Unified translate/polish + audio pipeline.

Mặc định chạy SEQUENTIAL (tiết kiệm VRAM):
  Phase 1: Polish subprocess (translate EN→VI + polish)
  → Unload Ollama model
  Phase 2: Audio subprocess (VieNeu TTS segments → stitch)

Dùng --parallel để chạy cả hai phase đồng thời (cần ≥18GB VRAM).

Business logic giữ nguyên — mọi state sống trong PostgreSQL.
Chapters đã được crawl trước bằng crawl_wetriedtls_chapters.py --enqueue-polish.

Usage:
  # Full pipeline sequential (mặc định, an toàn với 16GB VRAM):
  viterbox/venv/bin/python scripts/story_pipeline/wetriedtls_pipeline.py \\
      --story-title "A Regressor's Tale of Cultivation" \\
      --device cuda

  # Full pipeline parallel (cần nhiều VRAM hơn):
  viterbox/venv/bin/python scripts/story_pipeline/wetriedtls_pipeline.py \\
      --story-title "..." --parallel --device cuda

  # Chỉ audio (đã có polished chapters):
  viterbox/venv/bin/python scripts/story_pipeline/wetriedtls_pipeline.py \\
      --story-title "..." --skip-polish --device cuda

  # Chỉ polish (không generate audio):
  viterbox/venv/bin/python scripts/story_pipeline/wetriedtls_pipeline.py \\
      --story-title "..." --skip-audio

  # Từ chapter 50 đến 100:
  viterbox/venv/bin/python scripts/story_pipeline/wetriedtls_pipeline.py \\
      --story-title "..." --from-chapter 50 --to-chapter 100 --device cuda

Logs: stderr/stdout của từng subprocess in trực tiếp ra terminal.
      Dùng --log-dir để redirect vào file.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from story_db.story_pipeline_db import repository as repo
from story_db.story_pipeline_db.db import connect
from scripts.story_pipeline.enqueue_audio_jobs_from_db import split_chapter_into_segments
from scripts.story_pipeline.vieneu_voice_profiles import DEFAULT_VIENEU_VOICE_PROFILE


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def find_story(title: str, source_code: str) -> dict:
    stories = repo.find_stories(title_contains=title, source_codes=[source_code])
    if not stories:
        suggestions = repo.find_stories(source_codes=[source_code], limit=10)
        hint = ""
        if suggestions:
            names = [f"  - {s['title']} (id={s['id']})" for s in suggestions]
            hint = "\nMột số story hiện có trong source này:\n" + "\n".join(names)
        sys.exit(f"[ERROR] Không tìm thấy story '{title}' trong source '{source_code}'." + hint)
    if len(stories) > 1:
        names = [f"  - {s['title']} (id={s['id']})" for s in stories]
        sys.exit(
            f"[ERROR] Nhiều story khớp với '{title}':\n" + "\n".join(names) +
            "\nDùng tên đầy đủ hơn."
        )
    return stories[0]


def resolve_stories(args: argparse.Namespace) -> list[dict]:
    if args.story_title:
        return [find_story(args.story_title, args.source_code)]

    stories = repo.list_active_stories(source_codes=[args.source_code], limit=args.max_stories)
    if not stories:
        sys.exit(f"[ERROR] Không tìm thấy active story nào trong source '{args.source_code}'.")
    return stories


def count_pending_polish_jobs(story_id: str) -> int:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*)::int AS count
            FROM story_jobs
            WHERE job_type = 'polish_chapter'
              AND story_id = %s
              AND status IN ('pending', 'running')
            """,
            (story_id,),
        ).fetchone()
        return int(row["count"] or 0) if row else 0


def count_pending_audio_jobs(story_id: str) -> int:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*)::int AS count
            FROM story_jobs
            WHERE job_type = 'audio_chapter_segments'
              AND story_id = %s
              AND status IN ('pending', 'running')
            """,
            (story_id,),
        ).fetchone()
        return int(row["count"] or 0) if row else 0


def count_pending_polish_jobs_for_stories(story_ids: list[str]) -> int:
    return sum(count_pending_polish_jobs(story_id) for story_id in story_ids)


def count_pending_audio_jobs_for_stories(story_ids: list[str]) -> int:
    return sum(count_pending_audio_jobs(story_id) for story_id in story_ids)


def enqueue_newly_polished(args: argparse.Namespace, story_id: str) -> int:
    """Enqueue audio_chapter_segments cho chapters vừa được polish. Returns số chapters enqueued."""
    rows = repo.list_polished_chapters_for_audio(
        story_id=story_id,
        from_chapter=args.from_chapter or None,
        to_chapter=args.to_chapter or None,
        include_existing_audio=False,
        limit=500,
    )
    enqueued = 0
    for row in rows:
        polished_text = row.get("polished_text_content") or repo.get_chapter_polished_content(row["id"])
        if not polished_text:
            continue
        segments = split_chapter_into_segments(polished_text)
        if not segments:
            print(f"[ENQUEUE] WARN: ch{row['chapter_number']:04d} — no segments after split, skip", flush=True)
            continue
        result = repo.enqueue_audio_segments_for_chapter(
            row["id"],
            str(row["story_id"]),
            segments,
            voice_key=args.voice_key,
            source_code=row.get("source_code") or args.source_code,
        )
        ch = row["chapter_number"]
        job_status = result["job"]["status"]
        if result["inserted"] + result["reset"] > 0:
            print(
                f"[ENQUEUE] ch{ch:04d}: {result['total']} segments "
                f"(+{result['inserted']} new, ~{result['reset']} reset, "
                f"={result['unchanged']} unchanged) job={job_status}",
                flush=True,
            )
            enqueued += 1
    return enqueued


def count_remaining_to_enqueue(args: argparse.Namespace, story_id: str) -> int:
    rows = repo.list_polished_chapters_for_audio(
        story_id=story_id,
        from_chapter=args.from_chapter or None,
        to_chapter=args.to_chapter or None,
        include_existing_audio=False,
        limit=1,
    )
    return len(rows)


def count_remaining_to_enqueue_for_stories(args: argparse.Namespace, story_ids: list[str]) -> int:
    return sum(count_remaining_to_enqueue(args, story_id) for story_id in story_ids)


def print_progress(stories: list[dict], args: argparse.Namespace) -> None:
    story_ids = [str(story["id"]) for story in stories]
    total_chapters = 0
    total_polished = 0
    for story_id in story_ids:
        progress = repo.get_story_chapter_progress(story_id)
        total_chapters += progress["chapter_count"]
        total_polished += progress["polished_count"]
    polish_pending = count_pending_polish_jobs_for_stories(story_ids)
    audio_pending = count_pending_audio_jobs_for_stories(story_ids)
    print(
        f"[PROGRESS] stories={len(stories)} "
        f"chapters={total_chapters} "
        f"polished={total_polished} "
        f"polish_pending={polish_pending} "
        f"audio_pending={audio_pending}",
        flush=True,
    )


def ensure_audio_output_root_writable(output_root: str) -> None:
    root = Path(output_root)
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".betterbox_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        sys.exit(
            f"[ERROR] Audio output root is not writable: {root}\n"
            f"Reason: {exc}\n"
            "Fix ownership/permissions, or rerun with a writable path, for example:\n"
            "  --audio-output-root story_audio_segments_local"
        )


def run_verify_gate(args: argparse.Namespace, stories: list[dict]) -> bool:
    """Run Phase 4 verify before audio enqueue. Returns True if gate passed."""
    if getattr(args, "skip_verify", False) or args.skip_audio:
        return True

    script = ROOT / "scripts/story_pipeline/wetriedtls_verify.py"
    from_ch = int(getattr(args, "verify_from_chapter", 0) or args.from_chapter or 1)
    to_ch = int(getattr(args, "verify_to_chapter", 0) or (args.to_chapter if args.to_chapter else 3))

    all_ok = True
    for story in stories:
        cmd = [
            sys.executable,
            str(script),
            "--story-id", str(story["id"]),
            "--source-code", args.source_code,
            "--from-chapter", str(from_ch),
            "--to-chapter", str(to_ch),
        ]
        print(f"\n[PIPELINE] Verify gate → {story.get('title')} (ch{from_ch}-{to_ch})", flush=True)
        rc = subprocess.run(cmd, cwd=ROOT).returncode
        if rc != 0:
            all_ok = False
            print(f"[PIPELINE] Verify FAILED for story id={story['id']}", flush=True)

    if not all_ok and getattr(args, "verify_strict", True):
        print(
            "[PIPELINE] Verify gate blocked audio phase. "
            "Fix polish/repolish or pass --skip-verify to override.",
            flush=True,
        )
    return all_ok


def run_preflight(args: argparse.Namespace, stories: list[dict]) -> None:
    """Seed char-map + glossary before polish worker starts."""
    if args.skip_preflight or args.skip_polish:
        return
    script = ROOT / "scripts/story_pipeline/wetriedtls_preflight.py"
    for story in stories:
        cmd = [
            sys.executable,
            str(script),
            "--story-id", str(story["id"]),
            "--story-title", str(story.get("title") or ""),
            "--source-code", args.source_code,
            "--ollama-url", args.ollama_url,
            "--model", args.vi_model,
            "--preflight-chapters", str(args.preflight_chapters),
            "--from-chapter", str(args.from_chapter or 0),
            "--to-chapter", str(args.to_chapter or 0),
        ]
        if args.preflight_apply:
            cmd.append("--apply")
        if args.preflight_repolish:
            cmd.append("--repolish")
        print(f"\n[PIPELINE] Preflight → {story.get('title')}", flush=True)
        rc = subprocess.run(cmd, cwd=ROOT).returncode
        if rc != 0:
            print(f"[PIPELINE] WARN: preflight exited rc={rc} (continuing)", flush=True)


# ---------------------------------------------------------------------------
# Subprocess launchers
# ---------------------------------------------------------------------------

def _open_log(log_dir: str, name: str):
    """Open log file in append mode. Returns file handle or None."""
    if not log_dir:
        return None
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    path = Path(log_dir) / f"{name}.log"
    print(f"[PIPELINE] Log → {path}", flush=True)
    return path.open("a", encoding="utf-8")


def start_polish_subprocess(args: argparse.Namespace) -> subprocess.Popen:
    cmd = [
        sys.executable,
        str(ROOT / "scripts/story_pipeline/polish_worker.py"),
        "--ollama-url", args.ollama_url,
        "--source-code", args.source_code,
        "--vi-model", args.vi_model,
        "--translate-model", args.translate_model,
        "--no-save-files",
        "--no-resource-check",
        "--idle-sleep", "5",
        "--idle-log-interval", "60",
        "--prompt-profile", args.prompt_profile,
    ]
    if args.workers > 1:
        cmd.extend(["--workers", str(args.workers)])
    if args.overwrite:
        cmd.append("--overwrite")
    for story_id in args.story_ids:
        cmd.extend(["--story-id", story_id])
    log = _open_log(args.log_dir, f"polish_{args.source_code}")
    stdout = log or None
    stderr = log or None
    print(
        f"[PIPELINE] Polish worker: source={args.source_code} model={args.vi_model} workers={args.workers}",
        flush=True,
    )
    return subprocess.Popen(cmd, stdout=stdout, stderr=stderr)


def start_audio_subprocess(args: argparse.Namespace) -> subprocess.Popen:
    cmd = [
        sys.executable,
        str(ROOT / "scripts/story_pipeline/audio_segment_worker_vieneu.py"),
        "--device", args.device,
        "--backend", args.backend,
        "--output-root", args.audio_output_root,
        "--voice-key", args.voice_key,
        "--voice-profile", args.voice_profile or "",
        "--idle-sleep", "5",
    ]
    if args.reference_audio:
        cmd.extend(["--reference-audio", args.reference_audio])
    for story_id in args.story_ids:
        cmd.extend(["--story-id", story_id])
    log = _open_log(args.log_dir, f"audio_{args.source_code}")
    stdout = log or None
    stderr = log or None
    print(
        f"[PIPELINE] Audio worker: device={args.device} backend={args.backend} voice_key={args.voice_key}",
        flush=True,
    )
    return subprocess.Popen(cmd, stdout=stdout, stderr=stderr)


def stop_subprocess(proc: subprocess.Popen, name: str, timeout: int = 30) -> None:
    if proc.poll() is not None:
        return
    print(f"[PIPELINE] Stopping {name} (pid={proc.pid})...", flush=True)
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
        print(f"[PIPELINE] {name} stopped.", flush=True)
    except subprocess.TimeoutExpired:
        print(f"[PIPELINE] {name} did not stop, sending SIGKILL...", flush=True)
        proc.kill()
        proc.wait()


def unload_ollama_model(ollama_url: str, model: str) -> None:
    """Unload model from Ollama to free VRAM before starting audio phase."""
    try:
        payload = json.dumps({"model": model, "keep_alive": 0}).encode()
        req = urllib.request.Request(
            f"{ollama_url.rstrip('/')}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        print(f"[PIPELINE] Ollama model '{model}' unloaded (VRAM freed).", flush=True)
    except Exception as exc:
        print(f"[PIPELINE] WARN: could not unload Ollama model '{model}': {exc}", flush=True)


# ---------------------------------------------------------------------------
# Pipeline execution helpers
# ---------------------------------------------------------------------------

def _poll_loop_until_done(
    args: argparse.Namespace,
    story_ids: list[str],
    stories: list[dict],
    *,
    polish_proc: subprocess.Popen | None,
    audio_proc: subprocess.Popen | None,
    expect_audio: bool,
    verify_state: dict[str, bool] | None = None,
) -> None:
    """Shared poll loop used by both parallel and sequential audio phases."""
    if verify_state is None:
        verify_state = {"done": False, "passed": True}
    iteration = 0
    while True:
        iteration += 1

        if expect_audio:
            pending_polish = count_pending_polish_jobs_for_stories(story_ids)
            if not verify_state["done"] and pending_polish == 0:
                verify_state["passed"] = run_verify_gate(args, stories)
                verify_state["done"] = True
                if not verify_state["passed"] and getattr(args, "verify_strict", True):
                    if polish_proc and polish_proc.poll() is None:
                        stop_subprocess(polish_proc, "polish worker")
                    if audio_proc and audio_proc.poll() is None:
                        stop_subprocess(audio_proc, "audio worker")
                    sys.exit(1)

            newly_enqueued = 0
            if verify_state["done"] and verify_state["passed"]:
                for story_id in story_ids:
                    newly_enqueued += enqueue_newly_polished(args, story_id)
            if newly_enqueued:
                print(f"[ENQUEUE] Enqueued {newly_enqueued} chapter(s) for audio.", flush=True)

        pending_polish = count_pending_polish_jobs_for_stories(story_ids)

        # Polish worker idles forever after queue drains — stop from DB state, not process exit.
        if polish_proc is not None and pending_polish == 0 and polish_proc.poll() is None:
            print("[PIPELINE] Polish queue drained — stopping worker.", flush=True)
            stop_subprocess(polish_proc, "polish worker")
            polish_proc = None

        if polish_proc is not None and polish_proc.poll() is not None:
            rc = polish_proc.returncode
            remaining_polish = count_pending_polish_jobs_for_stories(story_ids)
            if rc != 0:
                print(
                    f"[PIPELINE] WARN: Polish worker exited with code {rc} "
                    f"({remaining_polish} polish job(s) still pending in DB)",
                    flush=True,
                )
            elif remaining_polish > 0:
                print(
                    f"[PIPELINE] WARN: Polish worker exited OK but {remaining_polish} job(s) "
                    "still pending — may have been interrupted. Restart to resume.",
                    flush=True,
                )
            else:
                print("[PIPELINE] Polish worker finished.", flush=True)
            polish_proc = None

        polish_done = polish_proc is None
        audio_done = not expect_audio

        if expect_audio:
            pending_audio = count_pending_audio_jobs_for_stories(story_ids)
            remaining_to_enqueue = count_remaining_to_enqueue_for_stories(args, story_ids)
            audio_done = (
                polish_done
                and pending_polish == 0
                and remaining_to_enqueue == 0
                and pending_audio == 0
            )

        if polish_done and audio_done:
            print("\n[PIPELINE] ✓ Phase complete.", flush=True)
            break

        if iteration % 5 == 1:
            print_progress_for_ids(story_ids, args)

        time.sleep(args.poll_interval)


def print_progress_for_ids(story_ids: list[str], args: argparse.Namespace) -> None:
    total_chapters = 0
    total_polished = 0
    for story_id in story_ids:
        progress = repo.get_story_chapter_progress(story_id)
        total_chapters += progress["chapter_count"]
        total_polished += progress["polished_count"]
    polish_pending = count_pending_polish_jobs_for_stories(story_ids)
    audio_pending = count_pending_audio_jobs_for_stories(story_ids)
    print(
        f"[PROGRESS] chapters={total_chapters} polished={total_polished} "
        f"polish_pending={polish_pending} audio_pending={audio_pending}",
        flush=True,
    )


def _run_sequential(args: argparse.Namespace, story_ids: list[str], stories: list[dict]) -> None:
    """Phase 1: polish → verify → unload Ollama → Phase 2: audio."""
    # ── Phase 1: Polish ──────────────────────────────────────────────────────
    print(f"\n[PIPELINE] ── Phase 1: Polish ──────────────────────────────────", flush=True)
    polish_proc = start_polish_subprocess(args)
    print(f"\n[PIPELINE] Polish running — poll every {args.poll_interval}s\n", flush=True)
    _poll_loop_until_done(
        args, story_ids, stories,
        polish_proc=polish_proc,
        audio_proc=None,
        expect_audio=False,
    )
    if polish_proc.poll() is None:
        stop_subprocess(polish_proc, "polish worker")

    if not args.skip_audio:
        if not run_verify_gate(args, stories) and getattr(args, "verify_strict", True):
            sys.exit(1)

    # Free VRAM before starting TTS
    unload_ollama_model(args.ollama_url, args.vi_model)
    if args.translate_model != args.vi_model:
        unload_ollama_model(args.ollama_url, args.translate_model)

    # ── Phase 2: Audio ───────────────────────────────────────────────────────
    print(f"\n[PIPELINE] ── Phase 2: Audio ───────────────────────────────────", flush=True)
    audio_proc = start_audio_subprocess(args)
    print(f"\n[PIPELINE] Audio running — poll every {args.poll_interval}s\n", flush=True)
    _poll_loop_until_done(
        args, story_ids, stories,
        polish_proc=None,
        audio_proc=audio_proc,
        expect_audio=True,
    )
    if audio_proc.poll() is None:
        stop_subprocess(audio_proc, "audio worker")


def _run_parallel(
    args: argparse.Namespace,
    story_ids: list[str],
    stories: list[dict],
    polish_proc: subprocess.Popen | None,
    audio_proc: subprocess.Popen | None,
) -> None:
    """Start both workers simultaneously and poll until both are done."""
    if not args.skip_polish:
        polish_proc = start_polish_subprocess(args)
    if not args.skip_audio:
        audio_proc = start_audio_subprocess(args)

    print(f"\n[PIPELINE] Running (parallel) — poll every {args.poll_interval}s\n", flush=True)
    _poll_loop_until_done(
        args, story_ids, stories,
        polish_proc=polish_proc,
        audio_proc=audio_proc,
        expect_audio=not args.skip_audio,
    )
    if audio_proc and audio_proc.poll() is None:
        stop_subprocess(audio_proc, "audio worker")
    if polish_proc and polish_proc.poll() is None:
        stop_subprocess(polish_proc, "polish worker")


# ---------------------------------------------------------------------------
# Main pipeline loop
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Unified translate/polish + audio pipeline cho wetriedtls (hoặc bất kỳ EN source).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # Story selection
    g = parser.add_argument_group("Story")
    g.add_argument(
        "--story-title",
        default="",
        help="Tên truyện (ILIKE search). Bỏ trống để chạy tất cả active stories của source.",
    )
    g.add_argument("--source-code", default="wetriedtls", help="Source code trong DB.")
    g.add_argument("--max-stories", type=int, default=0, help="Giới hạn số story khi không truyền --story-title (0 = tất cả).")
    g.add_argument("--from-chapter", type=int, default=0, help="Bắt đầu từ chapter N (0 = không giới hạn).")
    g.add_argument("--to-chapter", type=int, default=0, help="Kết thúc ở chapter N (0 = không giới hạn).")

    # Polish
    g = parser.add_argument_group("Polish / Translate")
    g.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    g.add_argument("--vi-model", default="qwen3:14b", help="Model polish Vietnamese.")
    g.add_argument("--translate-model", default="translategemma:12b", help="Model translate EN→VI.")
    g.add_argument("--workers", type=int, default=1, help="Số thread polish song song.")
    g.add_argument("--prompt-profile", default="full", choices=["full", "fast"], help="Prompt profile.")
    g.add_argument("--overwrite", action="store_true", help="Re-polish chapters đã có.")
    g.add_argument("--skip-polish", action="store_true", help="Bỏ qua bước polish, chỉ chạy audio.")

    # Preflight (char-map + glossary seed before polish)
    g = parser.add_argument_group("Preflight")
    g.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Bỏ qua wetriedtls_preflight (char-map/glossary seed).",
    )
    g.add_argument(
        "--preflight-repolish",
        action="store_true",
        help="Preflight: luôn chạy repolish sau seed (mặc định chỉ repolish khi gap scan phát hiện lỗi).",
    )
    g.add_argument(
        "--preflight-apply",
        action="store_true",
        help="Preflight: ghi genre metadata backfill vào DB.",
    )
    g.add_argument("--preflight-chapters", type=int, default=20, help="Số chapter raw cho char-map/glossary seed.")

    # Verify gate (Phase 4 — before audio enqueue)
    g = parser.add_argument_group("Verify (pre-audio gate)")
    g.add_argument(
        "--skip-verify",
        action="store_true",
        help="Bỏ qua verify gate (gap scan + golden checklist) trước audio.",
    )
    g.add_argument(
        "--verify-strict",
        action="store_true",
        default=True,
        help="Exit nếu verify fail (default). Dùng --no-verify-strict để chỉ warn.",
    )
    g.add_argument(
        "--no-verify-strict",
        action="store_false",
        dest="verify_strict",
        help="Verify fail → warn và vẫn chạy audio.",
    )
    g.add_argument("--verify-from-chapter", type=int, default=1, help="Verify từ chapter N (default 1).")
    g.add_argument("--verify-to-chapter", type=int, default=3, help="Verify đến chapter N (default 3).")

    # Audio
    g = parser.add_argument_group("Audio")
    g.add_argument("--device", default="cuda", help="TTS device: cuda / cpu / auto.")
    g.add_argument("--backend", default="auto", choices=["auto", "onnx", "pytorch"])
    g.add_argument("--audio-output-root", default="story_audio_segments")
    g.add_argument("--voice-key", default=DEFAULT_VIENEU_VOICE_PROFILE)
    g.add_argument("--voice-profile", default=DEFAULT_VIENEU_VOICE_PROFILE)
    g.add_argument("--reference-audio", default=None, help="Optional: reference WAV cho voice clone.")
    g.add_argument("--skip-audio", action="store_true", help="Bỏ qua bước audio, chỉ chạy polish.")

    # Pipeline control
    g = parser.add_argument_group("Pipeline")
    g.add_argument("--poll-interval", type=int, default=30, help="Giây giữa các lần enqueue poll.")
    g.add_argument("--log-dir", default="", help="Nếu set, redirect subprocess logs vào thư mục này.")
    g.add_argument(
        "--parallel",
        action="store_true",
        default=False,
        help=(
            "Chạy polish và audio song song. Mặc định: sequential (polish xong → unload Ollama → audio). "
            "Sequential tiết kiệm ~10GB VRAM so với parallel. Cần --parallel nếu GPU ≥18GB."
        ),
    )

    args = parser.parse_args()

    # Resolve stories
    stories = resolve_stories(args)
    story_ids = [str(story["id"]) for story in stories]
    args.story_ids = story_ids
    print(
        f"\n[PIPELINE] ══════════════════════════════════════════",
        flush=True,
    )
    if args.story_title:
        story = stories[0]
        print(f"[PIPELINE] Story    : {story['title']}", flush=True)
        print(f"[PIPELINE] ID       : {story_ids[0]}", flush=True)
    else:
        print(f"[PIPELINE] Stories  : {len(stories)} active story(s)", flush=True)
        for story in stories[:10]:
            print(f"[PIPELINE]   - {story['title']} (id={story['id']})", flush=True)
        if len(stories) > 10:
            print(f"[PIPELINE]   ... {len(stories) - 10} more", flush=True)
    print(f"[PIPELINE] Source   : {args.source_code}", flush=True)
    if args.from_chapter or args.to_chapter:
        print(f"[PIPELINE] Chapters : {args.from_chapter or '(start)'} → {args.to_chapter or '(end)'}", flush=True)
    skip = []
    if args.skip_polish:
        skip.append("polish")
    if args.skip_audio:
        skip.append("audio")
    if skip:
        print(f"[PIPELINE] Skip     : {', '.join(skip)}", flush=True)
    mode = "parallel" if args.parallel else "sequential"
    if not (args.skip_polish or args.skip_audio):
        print(f"[PIPELINE] Mode     : {mode}", flush=True)
    print(f"[PIPELINE] ══════════════════════════════════════════\n", flush=True)

    print_progress(stories, args)
    if not args.skip_audio:
        ensure_audio_output_root_writable(args.audio_output_root)

    run_preflight(args, stories)

    polish_proc: subprocess.Popen | None = None
    audio_proc: subprocess.Popen | None = None

    def _shutdown(signum, frame):
        print("\n[PIPELINE] Interrupted — shutting down subprocesses...", flush=True)
        if audio_proc:
            stop_subprocess(audio_proc, "audio worker")
        if polish_proc:
            stop_subprocess(polish_proc, "polish worker")
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        if args.parallel or args.skip_polish or args.skip_audio:
            _run_parallel(args, story_ids, stories, polish_proc, audio_proc)
        else:
            _run_sequential(args, story_ids, stories)

        # Final status
        print("", flush=True)
        print_progress(stories, args)
        print("[PIPELINE] Done.", flush=True)

    finally:
        if audio_proc:
            stop_subprocess(audio_proc, "audio worker")
        if polish_proc:
            stop_subprocess(polish_proc, "polish worker")


if __name__ == "__main__":
    main()
