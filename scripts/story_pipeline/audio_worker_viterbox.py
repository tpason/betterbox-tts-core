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

import torch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from story_db.story_pipeline_db import repository as repo
from viterbox import Viterbox
from scripts.story_pipeline.generate_chapter_audio_viterbox import (
    detect_device,
    setup_cuda,
    synthesize_chapter,
)


def cuda_free_gb() -> float:
    if not torch.cuda.is_available():
        return 0.0
    free_bytes, _total_bytes = torch.cuda.mem_get_info()
    return free_bytes / 1024**3


def wait_for_gpu(args: argparse.Namespace) -> None:
    if args.device != "cuda" or args.min_free_vram_gb <= 0:
        return
    while True:
        free_gb = cuda_free_gb()
        if free_gb >= args.min_free_vram_gb:
            return
        print(
            f"[GPU] free={free_gb:.2f}GB < required={args.min_free_vram_gb:.2f}GB, "
            f"sleep {args.gpu_wait_seconds:.0f}s"
        )
        time.sleep(args.gpu_wait_seconds)


def acquire_gpu_lock(lock_path: Path) -> IO[str]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("w", encoding="utf-8")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    handle.write(f"{socket.gethostname()} {time.time():.0f}\n")
    handle.flush()
    return handle


def build_tts_args(args: argparse.Namespace) -> Namespace:
    return Namespace(
        reference_audio=str(Path(args.reference_audio).resolve()),
        language=args.language,
        no_tts_markup=args.no_tts_markup,
        tts_max_clause_chars=args.tts_max_clause_chars,
        tts_comma_every_chars=args.tts_comma_every_chars,
        audiobook_stitch=True,
        max_chars_per_unit=None,
        min_chars_per_unit=None,
        sentence_pause_ms=args.sentence_pause_ms,
        crossfade_ms=args.crossfade_ms,
        trim_threshold=args.trim_threshold,
        trim_margin_ms=args.trim_margin_ms,
        edge_fade_in_ms=args.edge_fade_in_ms,
        edge_fade_out_ms=args.edge_fade_out_ms,
        split_mode="packed",
        direct_text_normalizer="precision",
        max_chars_per_block=850,
        block_silence_ms=350,
        advance_tts=False,
        legacy_viterbox_generate=False,
        exaggeration=args.exaggeration,
        cfg_weight=args.cfg_weight,
        temperature=args.temperature,
        top_p=args.top_p,
        repetition_penalty=args.repetition_penalty,
        speed=args.speed,
        pitch_shift=args.pitch_shift,
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


def process_job(job: dict, model: Viterbox, args: argparse.Namespace) -> None:
    output_path = Path(job["output_path"])

    if output_path.exists() and not args.overwrite:
        repo.update_chapter_audio_output(job["chapter_id"], audio_path=output_path.as_posix())
        repo.complete_story_job(job["id"], result_payload={"skipped": "output_exists", "audio_path": output_path.as_posix()})
        print(f"[SKIP] audio exists: {output_path}")
        return

    input_path, is_temp = resolve_input_text(job)
    try:
        wait_for_gpu(args)
        synthesize_chapter(model, input_path, output_path, build_tts_args(args))
    finally:
        if is_temp:
            input_path.unlink(missing_ok=True)

    repo.update_chapter_audio_output(job["chapter_id"], audio_path=output_path.as_posix())
    repo.complete_story_job(job["id"], result_payload={"audio_path": output_path.as_posix()})
    print(f"[DONE] audio_chapter {job['id']} -> {output_path}")


def run_one(job: dict, model: Viterbox, args: argparse.Namespace) -> None:
    try:
        process_job(job, model, args)
    except Exception as exc:
        print(f"[ERROR] audio job={job.get('id')}: {exc}")
        repo.fail_story_job(job["id"], str(exc), retry_delay_seconds=args.retry_delay)


def main() -> None:
    parser = argparse.ArgumentParser(description="Worker tạo audio chapter từ story_jobs bằng Viterbox.")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--idle-sleep", type=float, default=5.0)
    parser.add_argument("--retry-delay", type=int, default=300)
    parser.add_argument("--worker-id", default=f"audio-{socket.gethostname()}")
    parser.add_argument("--device", default=None)
    parser.add_argument("--min-free-vram-gb", type=float, default=7.0)
    parser.add_argument("--gpu-wait-seconds", type=float, default=30.0)
    parser.add_argument("--gpu-lock-path", default="/tmp/betterbox_tts_gpu.lock")
    parser.add_argument("--overwrite", action="store_true")

    parser.add_argument("--reference-audio", default="wavs/vieneu_alloy1512_1005.wav")
    parser.add_argument("--language", default="vi")
    parser.add_argument("--no-tts-markup", action="store_true", default=True)
    parser.add_argument("--tts-max-clause-chars", type=int, default=160)
    parser.add_argument("--tts-comma-every-chars", type=int, default=70)
    parser.add_argument("--sentence-pause-ms", type=int, default=420)
    parser.add_argument("--crossfade-ms", type=int, default=0)
    parser.add_argument("--trim-threshold", type=float, default=0.006)
    parser.add_argument("--trim-margin-ms", type=int, default=100)
    parser.add_argument("--edge-fade-in-ms", type=int, default=5)
    parser.add_argument("--edge-fade-out-ms", type=int, default=20)
    parser.add_argument("--exaggeration", type=float, default=0.25)
    parser.add_argument("--cfg-weight", type=float, default=0.50)
    parser.add_argument("--temperature", type=float, default=0.50)
    parser.add_argument("--top-p", type=float, default=0.85)
    parser.add_argument("--repetition-penalty", type=float, default=1.08)
    parser.add_argument("--speed", type=float, default=0.96)
    parser.add_argument("--pitch-shift", type=float, default=1.0)
    args = parser.parse_args()

    args.device = detect_device(args.device)
    setup_cuda(args.device)
    if args.device == "cuda":
        wait_for_gpu(args)

    reference_audio = Path(args.reference_audio)
    if not reference_audio.exists():
        raise SystemExit(f"Không tìm thấy reference audio: {reference_audio}")

    stale_reset = repo.reset_stale_running_jobs("audio_chapter", stale_after_minutes=240)
    if stale_reset:
        print(f"[STARTUP] reset {stale_reset} stale running audio jobs back to pending")

    print(f"worker={args.worker_id}, device={args.device}, reference={reference_audio}")
    lock_handle = acquire_gpu_lock(Path(args.gpu_lock_path)) if args.device == "cuda" else None
    try:
        print(f"Loading Viterbox on {args.device}")
        model = Viterbox.from_pretrained(args.device)
        while True:
            jobs = repo.claim_story_jobs("audio_chapter", args.worker_id, limit=1)
            if not jobs:
                if args.once:
                    print("No pending audio jobs.")
                    return
                time.sleep(args.idle_sleep)
                continue
            run_one(jobs[0], model, args)
            if args.once:
                return
    finally:
        if lock_handle is not None:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            lock_handle.close()


if __name__ == "__main__":
    main()
