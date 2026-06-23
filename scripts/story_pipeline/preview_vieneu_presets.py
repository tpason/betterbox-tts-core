#!/usr/bin/env python3
"""Generate VieNeu v3 voice previews for xianxia audiobook A/B listening.

Always checks CPU/RAM (and VRAM on CUDA) before loading the model and before
each profile to avoid OOM / system freeze when Ollama or crawlers are active.

Usage:
  viterbox/venv/bin/python scripts/story_pipeline/preview_vieneu_presets.py
  viterbox/venv/bin/python scripts/story_pipeline/preview_vieneu_presets.py \\
      --device cpu --skip-existing --output-dir /tmp/vieneu_voice_samples
"""
from __future__ import annotations

import argparse
import fcntl
import os
import socket
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("GRADIO_TEMP_DIR", str(Path(tempfile.gettempdir()) / "betterbox_story_tmp"))

from scripts.story_pipeline.resource_guard import (  # noqa: E402
    ResourceThresholds,
    wait_until_safe,
)
from scripts.story_pipeline.vieneu_voice_profiles import (  # noqa: E402
    DEFAULT_VIENEU_VOICE_PROFILE,
    get_vieneu_voice_profile,
    resolve_vieneu_voice_profile,
)
from scripts.story_pipeline.vieneu_audiobook_stitch import (  # noqa: E402
    DEFAULT_MAX_NEW_FRAMES,
    DEFAULT_VIENEU_VOICE,
    get_vieneu_sample_rate,
    synthesize_vieneu_audiobook,
)

XIANXIA_SAMPLE = (
    "Enkrid nhìn lên bầu trời, linh lực trong kinh mạch chầm chậm lưu chuyển. "
    "Trước mắt là con đường tu luyện dài vô tận, nhưng anh không do dự. "
    "Mỗi bước tiến đều phải trả bằng mồ hôi và ý chí sắt đá."
)

DEFAULT_COMPARE = (
    "phoaudiobook_lu_thu",
    "preset_trong_huu",
    "preset_binh_an",
    "xianxia_spirit_male",
    "xianxia_story_male",
    "dolly_steady_man",
    "dolly_reliable_man",
)


def acquire_gpu_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("w", encoding="utf-8")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    handle.write(f"{socket.gethostname()} {time.time():.0f}\n")
    handle.flush()
    return handle


def _use_cuda(device: str) -> bool:
    return device not in ("cpu",) and "cpu" not in device.lower()


def _configure_cpu_threads(device: str, num_threads: int) -> None:
    if num_threads <= 0 or _use_cuda(device):
        return
    os.environ.setdefault("OMP_NUM_THREADS", str(num_threads))
    os.environ.setdefault("MKL_NUM_THREADS", str(num_threads))
    os.environ.setdefault("OPENBLAS_NUM_THREADS", str(num_threads))
    try:
        import torch

        torch.set_num_threads(num_threads)
    except Exception:
        pass


def _wait_for_resources(args: argparse.Namespace, *, label: str) -> None:
    if args.force:
        print(f"[WARN] --force: skipping resource check ({label})", flush=True)
        return
    if _use_cuda(args.device):
        thresholds = ResourceThresholds.tts_vieneu()
        if args.min_free_vram_mb > 0:
            thresholds = replace(thresholds, min_vram_mb=args.min_free_vram_mb)
        unload_models = [args.ollama_model] if args.unload_ollama else []
        wait_until_safe(
            thresholds,
            label=label,
            poll_seconds=args.poll_seconds,
            max_wait_seconds=args.max_wait_seconds,
            ollama_url=args.ollama_url,
            unload_models=unload_models,
            unload_all_ollama=args.unload_ollama,
            wait_for_workers=True,
            wait_for_ollama_users=not args.allow_ollama_users,
            require_gpu=True,
            max_gpu_util_percent=args.max_gpu_util_percent,
        )
    else:
        wait_until_safe(
            ResourceThresholds.tts_cpu(),
            label=label,
            poll_seconds=args.poll_seconds,
            max_wait_seconds=args.max_wait_seconds,
            wait_for_workers=True,
            wait_for_ollama_users=False,
            require_gpu=False,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Preview VieNeu voices for xianxia audiobook.")
    parser.add_argument("--output-dir", default="story_data/voice_samples")
    parser.add_argument("--text", default=XIANXIA_SAMPLE)
    parser.add_argument("--profiles", default=",".join(DEFAULT_COMPARE))
    parser.add_argument("--device", default="cpu", help="cpu (safe default) or auto/cuda")
    parser.add_argument("--emotion", default="natural")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Skip resource checks (may freeze/crash machine — not recommended)",
    )
    parser.add_argument(
        "--min-free-vram-mb",
        type=int,
        default=0,
        help="Override TTS_MIN_VRAM_MB for CUDA (default: env or 6144)",
    )
    parser.add_argument(
        "--max-gpu-util-percent",
        type=float,
        default=float(os.environ.get("TTS_MAX_GPU_UTIL_PCT", "20")),
        help="CUDA: wait until GPU util <= this (default 20%%)",
    )
    parser.add_argument("--gpu-lock-path", default="/tmp/betterbox_tts_gpu.lock")
    parser.add_argument(
        "--unload-ollama",
        action="store_true",
        help="CUDA: unload all Ollama models each poll",
    )
    parser.add_argument(
        "--allow-ollama-users",
        action="store_true",
        help="Do not wait for crawl/polish processes that keep Ollama loaded",
    )
    parser.add_argument(
        "--ollama-url",
        default=os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434"),
    )
    parser.add_argument(
        "--ollama-model",
        default=os.environ.get("OLLAMA_POLISH_MODEL", "qwen3:14b"),
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=int(os.environ.get("TTS_RESOURCE_POLL_SECONDS", "20")),
    )
    parser.add_argument(
        "--max-wait-seconds",
        type=int,
        default=int(os.environ.get("TTS_RESOURCE_MAX_WAIT_SECONDS", "7200")),
        help="Give up after N seconds waiting (default 7200 = 2h)",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip profiles whose WAV already exists in output-dir",
    )
    parser.add_argument(
        "--cooldown-seconds",
        type=float,
        default=float(os.environ.get("TTS_PROFILE_COOLDOWN_SECONDS", "10")),
        help="Pause between profiles to let CPU/RAM recover",
    )
    parser.add_argument(
        "--torch-threads",
        type=int,
        default=int(os.environ.get("TTS_TORCH_THREADS", "2")),
        help="CPU inference thread cap (default 2 — reduces system freeze risk)",
    )
    args = parser.parse_args()

    _configure_cpu_threads(args.device, args.torch_threads)

    keys = [k.strip() for k in args.profiles.split(",") if k.strip()]
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    pending = []
    for key in keys:
        out_path = out_dir / f"{key}.wav"
        if args.skip_existing and out_path.exists() and out_path.stat().st_size > 1000:
            print(f"[SKIP] existing {out_path}")
            continue
        pending.append(key)
    if not pending:
        print(f"[DONE] all profiles exist in {out_dir}")
        return 0

    lock_handle = None
    _wait_for_resources(args, label="tts-preview-start")

    if _use_cuda(args.device):
        lock_handle = acquire_gpu_lock(Path(args.gpu_lock_path))
        print(f"[GPU] lock acquired: {args.gpu_lock_path}")

    from vieneu import Vieneu  # noqa: WPS433

    try:
        print(f"[LOAD] VieNeu device={args.device}")
        tts = Vieneu(device=args.device) if args.device != "auto" else Vieneu()
        sr = get_vieneu_sample_rate(tts)

        print(f"[PREVIEW] output={out_dir} pending={len(pending)} default={DEFAULT_VIENEU_VOICE_PROFILE}")
        print(f"[TEXT] {args.text[:80]}...")
        print()

        import soundfile as sf

        for idx, key in enumerate(pending, start=1):
            _wait_for_resources(args, label=f"tts-preview-{key}")
            profile = get_vieneu_voice_profile(key)
            if profile is None:
                print(f"[SKIP] unknown profile: {key}")
                continue
            try:
                resolve_vieneu_voice_profile(key)
            except (FileNotFoundError, ValueError) as exc:
                print(f"[SKIP] {key}: {exc}")
                continue
            out_path = out_dir / f"{key}.wav"
            print(f"[{idx}/{len(pending)}] synthesizing {key} ...")
            try:
                audio = synthesize_vieneu_audiobook(
                    tts,
                    args.text,
                    voice=DEFAULT_VIENEU_VOICE,
                    reference_audio=None,
                    reference_text=None,
                    voice_profile=key,
                    emotion=args.emotion,
                    temperature=0.8,
                    top_k=25,
                    top_p=0.95,
                    max_new_frames=DEFAULT_MAX_NEW_FRAMES,
                    repetition_penalty=1.2,
                    max_chars=256,
                    apply_watermark=True,
                    max_chars_per_unit=None,
                    min_chars_per_unit=None,
                    sentence_pause_ms=500,
                    crossfade_ms=50,
                    trim_threshold=0.006,
                    trim_margin_ms=80,
                    edge_fade_in_ms=5,
                    edge_fade_out_ms=22,
                )
                sf.write(out_path, audio, sr)
                print(f"[OK] {key:28s} → {out_path} ({profile.label})")
            except Exception as exc:
                print(f"[FAIL] {key}: {exc}")
            if idx < len(pending) and args.cooldown_seconds > 0:
                time.sleep(args.cooldown_seconds)

        print()
        print(f"Output: {out_dir}/")
        print("Nghe file WAV → chọn profile → set DEFAULT_VIENEU_VOICE_PROFILE + docker env.")
        return 0
    finally:
        if lock_handle is not None:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            lock_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
