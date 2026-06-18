#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import socket
import sys
import tempfile
import time
from argparse import Namespace
from pathlib import Path
from typing import IO


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from story_db.story_pipeline_db import repository as repo
from vieneu import Vieneu
from scripts.story_pipeline.audio_quality import check_wav
from scripts.story_pipeline.generate_chapter_audio_vieneu import synthesize_chapter
from scripts.story_pipeline.vieneu_audiobook_stitch import (
    DEFAULT_MAX_NEW_FRAMES,
    DEFAULT_VIENEU_VOICE,
)
from scripts.story_pipeline.vieneu_voice_profiles import DEFAULT_VIENEU_VOICE_PROFILE, resolve_vieneu_voice_profile


def acquire_gpu_lock(lock_path: Path) -> IO[str]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("w", encoding="utf-8")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    handle.write(f"{socket.gethostname()} {time.time():.0f}\n")
    handle.flush()
    return handle


def build_tts_args(args: argparse.Namespace) -> Namespace:
    return Namespace(
        voice=args.voice,
        reference_audio=args.reference_audio,
        reference_text=args.reference_text,
        voice_profile=args.voice_profile,
        emotion=args.emotion,
        temperature=args.temperature,
        top_k=args.top_k,
        top_p=args.top_p,
        max_new_frames=args.max_new_frames,
        repetition_penalty=args.repetition_penalty,
        max_chars=args.max_chars,
        no_watermark=args.no_watermark,
        max_chars_per_unit=None,
        min_chars_per_unit=None,
        sentence_pause_ms=args.sentence_pause_ms,
        crossfade_ms=args.crossfade_ms,
        trim_threshold=args.trim_threshold,
        trim_margin_ms=args.trim_margin_ms,
        edge_fade_in_ms=args.edge_fade_in_ms,
        edge_fade_out_ms=args.edge_fade_out_ms,
    )


def resolve_input_text(job: dict) -> tuple[Path, bool]:
    """Return (input_path, is_temp). If is_temp=True, caller must delete the file after use."""
    input_path = Path(job["input_path"]) if job.get("input_path") else None
    if input_path and input_path.exists():
        return input_path, False

    content = repo.get_chapter_polished_content(job["chapter_id"])
    if not content:
        raise FileNotFoundError(
            f"Polished content missing for chapter_id={job['chapter_id']}: "
            f"file not found at {input_path} and no DB content"
        )
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", encoding="utf-8", delete=False
    )
    tmp.write(content)
    tmp.close()
    print(f"[TMP] wrote polished content from DB -> {tmp.name}")
    return Path(tmp.name), True


def process_job(job: dict, tts, args: argparse.Namespace) -> None:
    output_path = Path(job["output_path"])
    max_quality_retries = getattr(args, "max_quality_retries", 3)

    if output_path.exists() and not args.overwrite:
        repo.update_chapter_audio_output(job["chapter_id"], audio_path=output_path.as_posix())
        repo.complete_story_job(job["id"], result_payload={"skipped": "output_exists", "audio_path": output_path.as_posix()})
        print(f"[SKIP] audio exists: {output_path}")
        return

    input_path, is_temp = resolve_input_text(job)
    try:
        text_content = input_path.read_text(encoding="utf-8")

        for q_attempt in range(max_quality_retries + 1):
            if q_attempt > 0:
                output_path.unlink(missing_ok=True)
                print(f"[QUALITY] retry {q_attempt}/{max_quality_retries}: re-synthesizing {output_path.name}")

            synthesize_chapter(tts, input_path, output_path, build_tts_args(args))

            try:
                all_issues, bad = check_wav(output_path, text_content)
            except Exception as exc:
                print(f"[QUALITY] check error (ignoring): {exc}")
                break

            if not bad:
                if all_issues:
                    print(f"[QUALITY] OK (review flags: {', '.join(all_issues)})")
                break

            if q_attempt < max_quality_retries:
                print(f"[QUALITY] attempt {q_attempt+1}/{max_quality_retries} failed - {', '.join(bad)}")
            else:
                print(f"[QUALITY] max retries reached, accepting: {', '.join(bad)}")
    finally:
        if is_temp:
            input_path.unlink(missing_ok=True)

    repo.update_chapter_audio_output(job["chapter_id"], audio_path=output_path.as_posix())
    repo.complete_story_job(job["id"], result_payload={"audio_path": output_path.as_posix(), "tts_backend": "vieneu"})
    print(f"[DONE] audio_chapter {job['id']} -> {output_path}")


def run_one(job: dict, tts, args: argparse.Namespace) -> None:
    try:
        process_job(job, tts, args)
    except Exception as exc:
        print(f"[ERROR] audio job={job.get('id')}: {exc}")
        repo.fail_story_job(job["id"], str(exc), retry_delay_seconds=args.retry_delay)


def load_vieneu(args: argparse.Namespace):
    kwargs = {
        "mode": args.mode,
        "device": args.device,
        "backend": args.backend,
    }
    if args.onnx_dir:
        kwargs["onnx_dir"] = str(Path(args.onnx_dir).resolve())
    if args.hf_token:
        kwargs["hf_token"] = args.hf_token
    print(f"Loading VieNeu mode={args.mode} device={args.device} backend={args.backend}")
    return Vieneu(**kwargs)


def main() -> None:
    parser = argparse.ArgumentParser(description="Worker tạo audio chapter từ story_jobs bằng VieNeu-TTS v3.")
    parser.add_argument("--once", action="store_true",
                        help="Process one job then exit (alias for --max-jobs 1)")
    parser.add_argument("--max-jobs", type=int, default=0,
                        help="Exit after processing N jobs (0=run until no pending jobs)")
    parser.add_argument("--idle-sleep", type=float, default=5.0)
    parser.add_argument("--retry-delay", type=int, default=300)
    parser.add_argument("--worker-id", default=f"audio-vieneu-{socket.gethostname()}")
    parser.add_argument("--mode", default="v3turbo")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--backend", choices=["auto", "onnx", "pytorch"], default="auto")
    parser.add_argument("--onnx-dir", default=None)
    parser.add_argument("--hf-token", default=None)
    parser.add_argument("--gpu-lock-path", default="/tmp/betterbox_tts_gpu.lock")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--max-quality-retries", type=int, default=3,
                        help="Max re-synthesis attempts per chapter if quality check fails (default 3)")

    parser.add_argument("--voice", default=DEFAULT_VIENEU_VOICE)
    parser.add_argument(
        "--voice-profile",
        default=DEFAULT_VIENEU_VOICE_PROFILE,
        help="BetterBox VieNeu voice-clone profile key. Empty string disables profiles and uses --voice.",
    )
    parser.add_argument("--reference-audio", default=None, help="Optional voice-clone reference WAV. Overrides --voice.")
    parser.add_argument("--reference-text", default=None, help="Optional transcript for --reference-audio.")
    parser.add_argument("--emotion", default="natural")
    parser.add_argument("--temperature", type=float, default=0.8)
    parser.add_argument("--top-k", type=int, default=25)
    parser.add_argument("--top-p", type=float, default=0.95)
    parser.add_argument("--max-new-frames", type=int, default=DEFAULT_MAX_NEW_FRAMES)
    parser.add_argument("--repetition-penalty", type=float, default=1.2)
    parser.add_argument("--max-chars", type=int, default=256)
    parser.add_argument("--no-watermark", action="store_true")
    parser.add_argument("--sentence-pause-ms", type=int, default=420)
    parser.add_argument("--crossfade-ms", type=int, default=0)
    parser.add_argument("--trim-threshold", type=float, default=0.006)
    parser.add_argument("--trim-margin-ms", type=int, default=100)
    parser.add_argument("--edge-fade-in-ms", type=int, default=5)
    parser.add_argument("--edge-fade-out-ms", type=int, default=22)
    args = parser.parse_args()
    args.voice_profile = args.voice_profile or None

    if args.reference_audio:
        reference_audio = Path(args.reference_audio)
        if not reference_audio.exists():
            raise SystemExit(f"Không tìm thấy reference audio: {reference_audio}")
        args.reference_audio = str(reference_audio.resolve())
    elif args.voice_profile:
        resolve_vieneu_voice_profile(args.voice_profile)

    stale_reset = repo.reset_stale_running_jobs("audio_chapter", stale_after_minutes=240)
    if stale_reset:
        print(f"[STARTUP] reset {stale_reset} stale running audio jobs back to pending")

    print(
        f"worker={args.worker_id}, device={args.device}, backend={args.backend}, "
        f"voice_profile={args.voice_profile or '-'}, voice={args.voice}"
    )
    use_gpu_lock = args.backend == "pytorch" or "cuda" in str(args.device).lower()
    lock_handle = acquire_gpu_lock(Path(args.gpu_lock_path)) if use_gpu_lock else None
    try:
        tts = load_vieneu(args)
        max_jobs = 1 if args.once else args.max_jobs
        jobs_done = 0
        while True:
            jobs = repo.claim_story_jobs("audio_chapter", args.worker_id, limit=1)
            if not jobs:
                if max_jobs > 0:
                    print("No pending audio jobs.")
                    return
                time.sleep(args.idle_sleep)
                continue
            run_one(jobs[0], tts, args)
            jobs_done += 1
            if max_jobs > 0 and jobs_done >= max_jobs:
                return
    finally:
        close = getattr(locals().get("tts"), "close", None)
        if callable(close):
            close()
        if lock_handle is not None:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            lock_handle.close()


if __name__ == "__main__":
    main()
