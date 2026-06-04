#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import IO

import soundfile as sf
import torch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from story_db.story_pipeline_db import repository as repo
from viterbox import Viterbox
from scripts.story_pipeline.generate_chapter_audio_viterbox import detect_device, setup_cuda
from scripts.story_pipeline.viterbox_audiobook_stitch import (
    count_words,
    edge_fade,
    generate_unit_audio_with_retry,
    normalize_unit_for_viterbox,
    trim_edges,
    trim_excess_duration_for_unit,
    trim_trailing_artifact_for_unit,
)


def cuda_free_gb() -> float:
    if not torch.cuda.is_available():
        return 0.0
    free_bytes, _total_bytes = torch.cuda.mem_get_info()
    return free_bytes / 1024**3


def read_cpu_percent(interval: float = 0.5) -> float:
    def read_stat() -> tuple[int, int]:
        with open("/proc/stat", encoding="utf-8") as handle:
            parts = handle.readline().split()
        values = [int(value) for value in parts[1:]]
        idle = values[3] + values[4]
        total = sum(values)
        return idle, total

    idle1, total1 = read_stat()
    time.sleep(interval)
    idle2, total2 = read_stat()
    delta_total = total2 - total1
    if delta_total <= 0:
        return 0.0
    return 100.0 * (1.0 - ((idle2 - idle1) / delta_total))


def read_mem_free_gb() -> float:
    mem: dict[str, int] = {}
    with open("/proc/meminfo", encoding="utf-8") as handle:
        for line in handle:
            parts = line.split()
            if len(parts) >= 2:
                mem[parts[0].rstrip(":")] = int(parts[1])
    return mem.get("MemAvailable", mem.get("MemFree", 0)) / (1024 * 1024)


def wait_for_system_capacity(args: argparse.Namespace) -> None:
    while True:
        cpu_percent = read_cpu_percent(args.cpu_measure_seconds)
        mem_free_gb = read_mem_free_gb()
        if cpu_percent <= args.max_cpu_percent and mem_free_gb >= args.min_free_ram_gb:
            return
        print(
            f"[RESOURCES] cpu={cpu_percent:.1f}% max={args.max_cpu_percent:.1f}% "
            f"mem_free={mem_free_gb:.2f}GB required={args.min_free_ram_gb:.2f}GB, "
            f"sleep {args.resource_wait_seconds:.0f}s",
            flush=True,
        )
        time.sleep(args.resource_wait_seconds)


def nvidia_smi_free_gb() -> float | None:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        return int(lines[0]) / 1024
    except ValueError:
        return None


def free_vram_gb(args: argparse.Namespace) -> float:
    smi_free = nvidia_smi_free_gb()
    if smi_free is not None:
        return smi_free
    return cuda_free_gb()


def wait_for_gpu(args: argparse.Namespace, min_free_vram_gb: float | None = None) -> None:
    required = args.min_free_vram_gb if min_free_vram_gb is None else min_free_vram_gb
    if args.device != "cuda" or required <= 0:
        return
    while True:
        free_gb = free_vram_gb(args)
        if free_gb >= required:
            return
        print(f"[GPU] free={free_gb:.2f}GB < required={required:.2f}GB, sleep {args.gpu_wait_seconds:.0f}s", flush=True)
        time.sleep(args.gpu_wait_seconds)


def acquire_gpu_lock(lock_path: Path) -> IO[str]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("w", encoding="utf-8")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    handle.write(f"{socket.gethostname()} {time.time():.0f}\n")
    handle.flush()
    return handle


def synthesize_segment(model: Viterbox, text: str, args: argparse.Namespace) -> tuple[object, float]:
    word_count = count_words(text)
    spoken = normalize_unit_for_viterbox(text, word_count=word_count)
    audio_np, _token_scale, _attempts = generate_unit_audio_with_retry(
        model,
        spoken=spoken,
        unit=text,
        word_count=word_count,
        language=args.language,
        cfg_weight=args.cfg_weight,
        temperature=args.temperature,
        top_p=args.top_p,
        repetition_penalty=args.repetition_penalty,
        speed=args.speed,
        pitch_shift=args.pitch_shift,
    )
    audio_np = trim_edges(audio_np, threshold=args.trim_threshold, margin_ms=args.trim_margin_ms, sr=model.sr)
    audio_np, _tail_trim_ms = trim_trailing_artifact_for_unit(audio_np, sr=model.sr, word_count=word_count)
    audio_np, _duration_trim_ms = trim_excess_duration_for_unit(audio_np, sr=model.sr, word_count=word_count)
    audio_np = edge_fade(audio_np, model.sr, fade_in_ms=args.edge_fade_in_ms, fade_out_ms=args.edge_fade_out_ms)
    return audio_np, len(audio_np) / model.sr if len(audio_np) else 0.0


def stitch_chapter_audio(chapter_id: str, voice_key: str, output_dir: Path) -> None:
    """Merge all ready segments into one MP3 and set it as the chapter's audio_path.
    Called after all segments are done so subsequent listens use direct audio (no gaps).
    """
    all_segs = repo.list_all_chapter_audio_segments(chapter_id, voice_key=voice_key)
    ready_segs = [s for s in all_segs if s["status"] == "ready" and s["audio_path"]]
    if not ready_segs or len(ready_segs) != len(all_segs):
        return  # some segments still pending or failed — skip

    stitch_path = output_dir / "chapter_audio.mp3"
    concat_path = output_dir / "concat.txt"
    try:
        with concat_path.open("w", encoding="utf-8") as fh:
            for seg in sorted(ready_segs, key=lambda s: s["segment_index"]):
                abs_seg = Path(seg["audio_path"]).resolve()
                fh.write(f"file '{abs_seg.as_posix()}'\n")
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0",
                "-i", str(concat_path),
                "-c", "copy",  # no re-encode — all segments already MP3
                str(stitch_path),
            ],
            check=True,
            timeout=300,
        )
        repo.update_chapter_audio_output(str(chapter_id), audio_path=stitch_path.as_posix())
        print(f"[STITCH] chapter={chapter_id} → {stitch_path} ({len(ready_segs)} segments)")
    except Exception as exc:
        print(f"[STITCH] warning: stitch failed for chapter={chapter_id}: {exc}")
    finally:
        concat_path.unlink(missing_ok=True)


def process_job(job: dict, model: Viterbox, args: argparse.Namespace) -> None:
    payload = job.get("payload") or {}
    voice_key = payload.get("voice_key") or args.voice_key
    segments = repo.list_pending_chapter_audio_segments(job["chapter_id"], voice_key=voice_key)
    if not segments:
        repo.complete_story_job(job["id"], result_payload={"segments": "already_ready"})
        return

    model.prepare_conditionals(str(Path(args.reference_audio).resolve()), args.exaggeration)
    if model.conds is not None and hasattr(model.conds.t3, "emotion_adv"):
        model.conds.t3.emotion_adv = args.exaggeration * torch.ones(1, 1, 1).to(model.device)

    output_dir = Path(args.output_root) / str(job["chapter_id"]) / voice_key
    output_dir.mkdir(parents=True, exist_ok=True)

    ready_count = 0
    for segment in segments:
        repo.mark_chapter_audio_segment_running(segment["id"])
        wav_path = output_dir / f"segment_{int(segment['segment_index']):05d}.wav"
        mp3_path = wav_path.with_suffix(".mp3")
        try:
            wait_for_system_capacity(args)
            wait_for_gpu(args, args.min_runtime_free_vram_gb)
            audio_np, duration_seconds = synthesize_segment(model, segment["text_content"], args)
            sf.write(wav_path, audio_np, model.sr)
            subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(wav_path),
                    "-vn", "-c:a", "libmp3lame", "-b:a", "96k",
                    "-ar", str(model.sr),
                    str(mp3_path),
                ],
                check=True,
                timeout=30,
            )
            wav_path.unlink(missing_ok=True)
            output_path = mp3_path
            repo.complete_chapter_audio_segment(segment["id"], audio_path=output_path.as_posix(), duration_seconds=duration_seconds)
            ready_count += 1
            print(f"[SEGMENT] chapter={job['chapter_id']} index={segment['segment_index']} duration={duration_seconds:.2f}s")
        except Exception as exc:
            wav_path.unlink(missing_ok=True)
            repo.fail_chapter_audio_segment(segment["id"], str(exc))
            raise

    repo.complete_story_job(job["id"], result_payload={"ready_segments": ready_count, "voice_key": voice_key})
    stitch_chapter_audio(job["chapter_id"], voice_key, output_dir)


def run_one(job: dict, model: Viterbox, args: argparse.Namespace) -> None:
    try:
        process_job(job, model, args)
    except Exception as exc:
        print(f"[ERROR] audio segment job={job.get('id')}: {exc}")
        repo.fail_story_job(job["id"], str(exc), retry_delay_seconds=args.retry_delay)


def main() -> None:
    parser = argparse.ArgumentParser(description="Worker tạo audio segment cho chapter bằng Viterbox.")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--idle-sleep", type=float, default=5.0)
    parser.add_argument("--retry-delay", type=int, default=180)
    parser.add_argument("--worker-id", default=f"audio-segment-{socket.gethostname()}")
    parser.add_argument("--device", default=None)
    parser.add_argument("--min-free-vram-gb", type=float, default=7.0)
    parser.add_argument("--min-runtime-free-vram-gb", type=float, default=1.0)
    parser.add_argument("--gpu-wait-seconds", type=float, default=20.0)
    parser.add_argument("--gpu-lock-path", default="/tmp/betterbox_tts_gpu.lock")
    parser.add_argument("--no-cpu-fallback", action="store_true", help="Nếu CUDA thiếu VRAM thì chờ thay vì fallback CPU.")
    parser.add_argument("--min-free-ram-gb", type=float, default=1.5)
    parser.add_argument("--max-cpu-percent", type=float, default=90.0)
    parser.add_argument("--cpu-measure-seconds", type=float, default=0.5)
    parser.add_argument("--resource-wait-seconds", type=float, default=10.0)
    parser.add_argument("--output-root", default="story_audio_segments")
    parser.add_argument("--voice-key", default="viterbox_default")
    parser.add_argument("--reference-audio", default="wavs/vieneu_alloy1512_1005.wav")
    parser.add_argument("--language", default="vi")
    parser.add_argument("--trim-threshold", type=float, default=0.006)
    parser.add_argument("--trim-margin-ms", type=int, default=100)
    parser.add_argument("--edge-fade-in-ms", type=int, default=5)
    parser.add_argument("--edge-fade-out-ms", type=int, default=22)
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
    reference_audio = Path(args.reference_audio)
    if not reference_audio.exists():
        raise SystemExit(f"Không tìm thấy reference audio: {reference_audio}")

    if args.device == "cuda" and not args.no_cpu_fallback and args.min_free_vram_gb > 0 and free_vram_gb(args) < args.min_free_vram_gb:
        print(
            f"[GPU] free={free_vram_gb(args):.2f}GB < required={args.min_free_vram_gb:.2f}GB, "
            "fallback CPU để tránh quá tải."
        )
        args.device = "cpu"

    wait_for_system_capacity(args)
    if args.device == "cuda":
        wait_for_gpu(args)
    print(f"worker={args.worker_id}, device={args.device}, reference={reference_audio}")
    lock_handle = acquire_gpu_lock(Path(args.gpu_lock_path)) if args.device == "cuda" else None
    try:
        model = Viterbox.from_pretrained(args.device)

        while True:
            wait_for_system_capacity(args)
            wait_for_gpu(args, args.min_runtime_free_vram_gb)
            jobs = repo.claim_story_jobs("audio_chapter_segments", args.worker_id, limit=1)
            if not jobs:
                if args.once:
                    print("No pending audio segment jobs.")
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
