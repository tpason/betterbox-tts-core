#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import IO

import soundfile as sf

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from story_db.story_pipeline_db import repository as repo
from vieneu import Vieneu
from scripts.story_pipeline.story_text_markup import prepare_text_for_tts
from scripts.story_pipeline.vieneu_audiobook_stitch import (
    DEFAULT_MAX_NEW_FRAMES,
    DEFAULT_VIENEU_VOICE,
    count_words,
    edge_fade,
    generate_unit_audio_with_retry,
    get_vieneu_sample_rate,
    normalize_unit_for_vieneu,
    trim_edges,
    trim_excess_duration_for_unit,
    trim_trailing_artifact_for_unit,
)
from scripts.story_pipeline.vieneu_voice_profiles import DEFAULT_VIENEU_VOICE_PROFILE, resolve_vieneu_voice_profile
from story_db.story_pipeline_db.db import connect as _db_connect


def _get_chapter_output_info(chapter_id: str) -> tuple[str, int]:
    """Returns (story_slug, chapter_number) for a chapter_id. Used to build final MP3 path."""
    with _db_connect() as conn:
        row = conn.execute(
            """
            SELECT c.chapter_number, COALESCE(s.metadata->>'slug', s.source_story_id) AS slug
            FROM chapters c
            JOIN stories s ON s.id = c.story_id
            WHERE c.id = %s
            """,
            (chapter_id,),
        ).fetchone()
    if not row:
        raise RuntimeError(f"Chapter {chapter_id} not found in DB")
    return str(row["slug"] or chapter_id), int(row["chapter_number"])


def acquire_gpu_lock(lock_path: Path) -> IO[str]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("w", encoding="utf-8")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    handle.write(f"{socket.gethostname()} {time.time():.0f}\n")
    handle.flush()
    return handle


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


def read_vram_free_gb() -> float:
    try:
        from resource_guard import read_vram_free_mb

        mb = read_vram_free_mb()
        return mb / 1024.0 if mb >= 0 else -1.0
    except Exception:
        return -1.0


def wait_for_system_capacity(args: argparse.Namespace) -> None:
    while True:
        cpu_percent = read_cpu_percent(args.cpu_measure_seconds)
        mem_free_gb = read_mem_free_gb()
        vram_free_gb = read_vram_free_gb()
        vram_ok = (
            args.min_free_vram_gb <= 0
            or (vram_free_gb >= 0 and vram_free_gb >= args.min_free_vram_gb)
        )
        if cpu_percent <= args.max_cpu_percent and mem_free_gb >= args.min_free_ram_gb and vram_ok:
            return
        vram_note = (
            f"vram_free={vram_free_gb:.2f}GB required={args.min_free_vram_gb:.2f}GB"
            if args.min_free_vram_gb > 0
            else "vram=skip"
        )
        print(
            f"[RESOURCES] cpu={cpu_percent:.1f}% max={args.max_cpu_percent:.1f}% "
            f"mem_free={mem_free_gb:.2f}GB required={args.min_free_ram_gb:.2f}GB, "
            f"{vram_note}, sleep {args.resource_wait_seconds:.0f}s",
            flush=True,
        )
        time.sleep(args.resource_wait_seconds)


def synthesize_segment(tts, text: str, args: argparse.Namespace) -> tuple[object, float, int, int]:
    """Returns (audio_np, duration_seconds, attempts, word_count)."""
    import re as _re
    import numpy as _np
    text = prepare_text_for_tts(text)
    word_count = count_words(text)
    spoken = normalize_unit_for_vieneu(text)
    # Guard: nếu sau normalize không còn ký tự có nghĩa (vd: segment chỉ là '.'),
    # sinh silence ngắn thay vì gọi TTS → tránh model sinh tiếng random.
    if not _re.search(r"\w", spoken):
        sr = get_vieneu_sample_rate(tts)
        silence = _np.zeros(int(sr * 0.1), dtype=_np.float32)
        return silence, 0.0, 1, 0
    audio_np, _frames, attempts = generate_unit_audio_with_retry(
        tts,
        spoken=spoken,
        unit=text,
        word_count=word_count,
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
        apply_watermark=not args.no_watermark,
    )
    sr = get_vieneu_sample_rate(tts)
    audio_np = trim_edges(audio_np, threshold=args.trim_threshold, margin_ms=args.trim_margin_ms, sr=sr)
    audio_np, _tail_trim_ms = trim_trailing_artifact_for_unit(audio_np, sr=sr, word_count=word_count)
    audio_np, _duration_trim_ms = trim_excess_duration_for_unit(audio_np, sr=sr, word_count=word_count)
    audio_np = edge_fade(audio_np, sr, fade_in_ms=args.edge_fade_in_ms, fade_out_ms=args.edge_fade_out_ms)
    duration_seconds = len(audio_np) / sr if len(audio_np) else 0.0
    return audio_np, duration_seconds, attempts, word_count


def stitch_chapter_audio(
    chapter_id: str,
    voice_key: str,
    output_dir: Path,
    *,
    expected_segment_count: int | None = None,
    final_output_path: Path | None = None,
) -> None:
    """Merge all ready segments into one MP3 and set it as the chapter's audio_path.

    expected_segment_count: nếu được truyền, chỉ stitch các segment có index < count.
    final_output_path: nếu được truyền, MP3 được ghi vào path này thay vì output_dir/chapter_audio.mp3.
    Sau khi stitch thành công, các segment files được xóa để giải phóng disk.
    """
    all_segs = repo.list_all_chapter_audio_segments(chapter_id, voice_key=voice_key)
    if expected_segment_count is not None:
        all_segs = [s for s in all_segs if s["segment_index"] < expected_segment_count]
    ready_segs = [s for s in all_segs if s["status"] == "ready" and s["audio_path"]]
    if not ready_segs or len(ready_segs) != len(all_segs):
        return

    stitch_path = final_output_path if final_output_path is not None else output_dir / "chapter_audio.mp3"
    stitch_path.parent.mkdir(parents=True, exist_ok=True)
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
                "-c:a", "libmp3lame", "-b:a", "96k",
                str(stitch_path),
            ],
            check=True,
            timeout=600,
        )
        repo.update_chapter_audio_output(str(chapter_id), audio_path=stitch_path.as_posix())
        print(f"[STITCH] chapter={chapter_id} -> {stitch_path} ({len(ready_segs)} segments)")

        # Cleanup: xóa segment files sau khi stitch thành công
        deleted = 0
        for seg in ready_segs:
            seg_path = Path(seg["audio_path"])
            if seg_path.exists():
                seg_path.unlink()
                deleted += 1
        if deleted:
            print(f"[STITCH] cleaned up {deleted} segment file(s)")
        # Xóa thư mục nếu rỗng
        try:
            output_dir.rmdir()
        except OSError:
            pass

    except Exception as exc:
        concat_path.unlink(missing_ok=True)
        raise RuntimeError(f"stitch failed for chapter={chapter_id}: {exc}") from exc
    finally:
        concat_path.unlink(missing_ok=True)


def _build_final_output_path(chapter_id: str, audio_dir: str) -> Path | None:
    """Trả về path cho file MP3 cuối: {audio_dir}/{story_slug}/chapter{N:04d}.mp3"""
    try:
        story_slug, chapter_number = _get_chapter_output_info(chapter_id)
        return Path(audio_dir) / story_slug / f"chapter{chapter_number:04d}.mp3"
    except Exception as exc:
        print(f"[STITCH] WARN: cannot resolve output path for chapter={chapter_id}: {exc}")
        return None


def process_job(job: dict, tts, args: argparse.Namespace) -> None:
    payload = job.get("payload") or {}
    voice_key = payload.get("voice_key") or args.voice_key
    # segment_count từ payload dùng để lọc stale tail segments khi stitch.
    # Xem stitch_chapter_audio() để hiểu tại sao cần giá trị này.
    expected_segment_count: int | None = payload.get("segment_count")
    segments = repo.list_pending_chapter_audio_segments(job["chapter_id"], voice_key=voice_key)

    output_dir = Path(args.output_root) / str(job["chapter_id"]) / voice_key
    final_output_path = _build_final_output_path(job["chapter_id"], args.audio_dir)

    if not segments:
        # Không có segment pending — có thể vừa re-enqueue sau chapter rút ngắn.
        # Re-stitch để audio MP3 phản ánh đúng set segment hiện tại.
        output_dir.mkdir(parents=True, exist_ok=True)
        stitch_chapter_audio(
            job["chapter_id"], voice_key, output_dir,
            expected_segment_count=expected_segment_count,
            final_output_path=final_output_path,
        )
        repo.complete_story_job(job["id"], result_payload={"segments": "already_ready", "tts_backend": "vieneu"})
        return
    output_dir.mkdir(parents=True, exist_ok=True)
    sr = get_vieneu_sample_rate(tts)

    ready_count = 0
    stale_count = 0
    for segment in segments:
        claimed = repo.mark_chapter_audio_segment_running(segment["id"])
        if claimed is None:
            print(f"[SKIP] chapter={job['chapter_id']} index={segment['segment_index']} already claimed — skipping")
            continue
        wav_path = output_dir / f"segment_{int(segment['segment_index']):05d}.wav"
        mp3_path = wav_path.with_suffix(".mp3")
        try:
            wait_for_system_capacity(args)
            audio_np, duration_seconds, attempts, word_count = synthesize_segment(tts, segment["text_content"], args)
            sf.write(wav_path, audio_np, sr)
            subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(wav_path),
                    "-vn", "-c:a", "libmp3lame", "-b:a", "96k",
                    "-ar", str(sr),
                    str(mp3_path),
                ],
                check=True,
                timeout=30,
            )
            wav_path.unlink(missing_ok=True)
            output_path = mp3_path
            completed = repo.complete_chapter_audio_segment(
                segment["id"],
                audio_path=output_path.as_posix(),
                duration_seconds=duration_seconds,
                claimed_text_hash=segment.get("text_hash"),
            )
            if completed is None:
                # Chapter được re-polish trong khi đang chạy — segment đã có text_hash mới.
                # Đếm stale: sau loop sẽ fail job thay vì mark done, để re-enqueue xử lý lại.
                print(
                    f"[STALE] chapter={job['chapter_id']} index={segment['segment_index']} "
                    f"text_hash changed during synthesis — skipping stale audio"
                )
                stale_count += 1
                continue
            ready_count += 1
            retry_tag = f" retries={attempts - 1}" if attempts > 1 else ""
            print(
                f"[SEGMENT] chapter={job['chapter_id']} index={segment['segment_index']}"
                f" words={word_count} duration={duration_seconds:.2f}s{retry_tag}"
            )
        except Exception as exc:
            wav_path.unlink(missing_ok=True)
            repo.fail_chapter_audio_segment(segment["id"], str(exc))
            raise

    if stale_count > 0:
        # Không mark job done — có pending segments từ re-enqueue chưa được xử lý.
        # Raise để run_one() gọi fail_story_job() → job được retry sau retry_delay.
        raise RuntimeError(
            f"chapter={job['chapter_id']}: {stale_count} stale segment(s) detected "
            f"(chapter re-polished during audio generation) — job will retry"
        )

    stitch_chapter_audio(
        job["chapter_id"], voice_key, output_dir,
        expected_segment_count=expected_segment_count,
        final_output_path=final_output_path,
    )
    repo.complete_story_job(job["id"], result_payload={"ready_segments": ready_count, "voice_key": voice_key, "tts_backend": "vieneu"})


def run_one(job: dict, tts, args: argparse.Namespace) -> None:
    try:
        process_job(job, tts, args)
    except Exception as exc:
        print(f"[ERROR] audio segment job={job.get('id')}: {exc}")
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
    parser = argparse.ArgumentParser(description="Worker tạo audio segment cho chapter bằng VieNeu-TTS v3.")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--idle-sleep", type=float, default=5.0)
    parser.add_argument("--retry-delay", type=int, default=180)
    parser.add_argument("--worker-id", default=f"audio-segment-vieneu-{socket.gethostname()}")
    parser.add_argument(
        "--story-id",
        action="append",
        default=[],
        help="Only claim jobs for this story id. Repeat for multiple stories.",
    )
    parser.add_argument("--mode", default="v3turbo")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--backend", choices=["auto", "onnx", "pytorch"], default="auto")
    parser.add_argument("--onnx-dir", default=None)
    parser.add_argument("--hf-token", default=None)
    parser.add_argument("--gpu-lock-path", default="/tmp/betterbox_tts_gpu.lock")
    parser.add_argument("--min-free-ram-gb", type=float, default=1.5)
    parser.add_argument(
        "--min-free-vram-gb",
        type=float,
        default=float(os.environ.get("AUDIO_SEGMENT_VIENEU_MIN_FREE_VRAM_GB", "6")),
        help="Wait until at least this much VRAM is free (0=disable). Default 6GB.",
    )
    parser.add_argument("--max-cpu-percent", type=float, default=90.0)
    parser.add_argument("--cpu-measure-seconds", type=float, default=0.5)
    parser.add_argument("--resource-wait-seconds", type=float, default=10.0)
    parser.add_argument("--output-root", default="story_audio_segments", help="Thư mục chứa segment files tạm thời.")
    parser.add_argument("--audio-dir", default="story_audio", help="Thư mục output MP3 cuối: {audio-dir}/{story_slug}/chapterNNNN.mp3")
    parser.add_argument("--voice-key", default=DEFAULT_VIENEU_VOICE_PROFILE)
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

    wait_for_system_capacity(args)
    print(
        f"worker={args.worker_id}, device={args.device}, backend={args.backend}, "
        f"voice_key={args.voice_key}, voice_profile={args.voice_profile or '-'}, voice={args.voice}, "
        f"story={','.join(args.story_id) if args.story_id else 'all'}"
    )

    stale_reset = repo.reset_stale_running_jobs("audio_chapter_segments", stale_after_minutes=240)
    if stale_reset:
        print(f"[STARTUP] reset {stale_reset} stale running audio segment jobs back to pending")
    seg_reset = repo.reset_stale_running_chapter_audio_segments(stale_after_minutes=120)
    if seg_reset:
        print(f"[STARTUP] reset {seg_reset} stale running segment rows back to pending")

    use_gpu_lock = args.backend == "pytorch" or "cuda" in str(args.device).lower()
    lock_handle = acquire_gpu_lock(Path(args.gpu_lock_path)) if use_gpu_lock else None
    try:
        tts = load_vieneu(args)
        while True:
            wait_for_system_capacity(args)
            jobs = repo.claim_story_jobs(
                "audio_chapter_segments",
                args.worker_id,
                limit=1,
                story_ids=args.story_id,
            )
            if not jobs:
                if args.once:
                    print("No pending audio segment jobs.")
                    return
                time.sleep(args.idle_sleep)
                continue
            run_one(jobs[0], tts, args)
            if args.once:
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
