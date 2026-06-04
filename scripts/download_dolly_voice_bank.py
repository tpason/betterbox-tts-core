from __future__ import annotations

import argparse
import csv
import io
import re
import shutil
from collections import defaultdict
from pathlib import Path

from datasets import Audio, load_dataset

try:
    import soundfile as sf
except ModuleNotFoundError as exc:
    raise SystemExit(
        "Missing dependency: soundfile\n"
        "Install project dependencies in the active venv:\n"
        "  pip install -r general/requirements.txt\n"
        "Or install only this package:\n"
        "  pip install soundfile\n"
    ) from exc


DATASET_ID = "dolly-vn/dolly-audio-1000h-vietnamese"

XIANXIA_RECOMMENDED = {
    "Wise Scholar",
    "Seasoned Man",
    "Reliable Man",
    "Thoughtful Man",
    "Steady Mentor",
    "Rational Man",
    "Wise Woman",
    "Sage Woman",
}


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    return value.strip("_") or "voice"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download a small local voice bank from Dolly Vietnamese audio. "
            "Each voice_id gets a few wav/txt reference samples for listening "
            "and later Viterbox profile building."
        )
    )
    parser.add_argument("--dataset", default=DATASET_ID)
    parser.add_argument("--samples-per-voice", type=int, default=2)
    parser.add_argument("--min-seconds", type=float, default=6.0)
    parser.add_argument("--max-seconds", type=float, default=15.0)
    parser.add_argument("--max-voices", type=int, default=0, help="0 means all voices.")
    parser.add_argument("--out-dir", default="voice_bank/dolly")
    parser.add_argument(
        "--copy-first-to-wavs",
        action="store_true",
        help="Also copy the first sample for each voice_id to wavs/ for the app dropdown.",
    )
    parser.add_argument(
        "--prepare-profile",
        default="",
        help="Voice ID to copy into viterbox/pretrained for building one profile.",
    )
    parser.add_argument("--profile-max-minutes", type=float, default=25.0)
    return parser.parse_args()


def write_pair(wav_path: Path, txt_path: Path, audio: dict, text: str) -> float:
    array = audio["array"]
    sr = audio["sampling_rate"]
    duration = len(array) / sr
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(wav_path, array, sr)
    txt_path.write_text(text, encoding="utf-8")
    return duration


def decode_audio(audio: dict) -> dict:
    """Decode a Hugging Face Audio(decode=False) value without torchcodec."""
    audio_bytes = audio.get("bytes")
    audio_path = audio.get("path")

    if audio_bytes:
        array, sr = sf.read(io.BytesIO(audio_bytes), dtype="float32", always_2d=False)
    elif audio_path and Path(audio_path).exists():
        array, sr = sf.read(audio_path, dtype="float32", always_2d=False)
    else:
        raise ValueError("Audio row has no embedded bytes or readable local path.")

    if array.ndim == 2:
        array = array.mean(axis=1)

    return {
        "array": array.astype("float32", copy=False),
        "sampling_rate": int(sr),
    }


def copy_pair(src_wav: Path, src_txt: Path, dst_wav: Path, dst_txt: Path) -> None:
    dst_wav.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src_wav, dst_wav)
    shutil.copyfile(src_txt, dst_txt)


def main() -> None:
    args = parse_args()
    out_dir = Path(args.out_dir)
    wavs_dir = Path("wavs")
    pretrained_dir = Path("viterbox/pretrained")
    manifest_path = out_dir / "manifest.csv"

    ds = load_dataset(args.dataset, split="train", streaming=True)
    ds = ds.cast_column("audio", Audio(decode=False))

    counts: dict[str, int] = defaultdict(int)
    voice_order: list[str] = []
    rows: list[dict[str, str]] = []
    first_samples: dict[str, tuple[Path, Path]] = {}
    profile_total_seconds = 0.0

    for row in ds:
        voice_id = str(row.get("voice_id") or "").strip()
        text = str(row.get("text") or "").strip()
        audio_raw = row.get("audio")
        if not voice_id or not text or not audio_raw:
            continue

        if voice_id not in counts and args.max_voices and len(voice_order) >= args.max_voices:
            continue

        try:
            audio = decode_audio(audio_raw)
        except Exception as exc:
            print(f"skip {voice_id}: cannot decode audio ({exc})")
            continue

        array = audio["array"]
        sr = audio["sampling_rate"]
        duration = len(array) / sr
        if duration < args.min_seconds or duration > args.max_seconds:
            continue

        if voice_id not in counts:
            voice_order.append(voice_id)

        voice_slug = slugify(voice_id)

        if counts[voice_id] < args.samples_per_voice:
            sample_index = counts[voice_id]
            base_name = f"{voice_slug}_{sample_index:02d}"
            wav_path = out_dir / voice_slug / f"{base_name}.wav"
            txt_path = out_dir / voice_slug / f"{base_name}.txt"
            duration = write_pair(wav_path, txt_path, audio, text)
            counts[voice_id] += 1

            first_samples.setdefault(voice_id, (wav_path, txt_path))
            rows.append(
                {
                    "voice_id": voice_id,
                    "recommended_for_tien_hiep": "yes" if voice_id in XIANXIA_RECOMMENDED else "no",
                    "duration_seconds": f"{duration:.3f}",
                    "wav_path": wav_path.as_posix(),
                    "txt_path": txt_path.as_posix(),
                    "text": text,
                }
            )
            print(f"saved {voice_id}: {wav_path}")

            if args.copy_first_to_wavs and sample_index == 0:
                app_base = f"dolly_{voice_slug}"
                copy_pair(
                    wav_path,
                    txt_path,
                    wavs_dir / f"{app_base}.wav",
                    wavs_dir / f"{app_base}.txt",
                )

        if args.prepare_profile and voice_id == args.prepare_profile:
            max_seconds = args.profile_max_minutes * 60.0
            if profile_total_seconds < max_seconds:
                profile_index = int(profile_total_seconds)
                base_name = f"profile_{voice_slug}_{profile_index:05d}"
                duration = write_pair(
                    pretrained_dir / f"{base_name}.wav",
                    pretrained_dir / f"{base_name}.txt",
                    audio,
                    text,
                )
                profile_total_seconds += duration

        if voice_order and all(counts[v] >= args.samples_per_voice for v in voice_order):
            if not args.max_voices or len(voice_order) >= args.max_voices:
                if not args.prepare_profile or profile_total_seconds >= args.profile_max_minutes * 60.0:
                    break

    out_dir.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w", encoding="utf-8", newline="") as f:
        fieldnames = [
            "voice_id",
            "recommended_for_tien_hiep",
            "duration_seconds",
            "wav_path",
            "txt_path",
            "text",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nDone. Manifest: {manifest_path}")
    print("Recommended voice_id for tien hiep:")
    for voice_id in sorted(XIANXIA_RECOMMENDED):
        print(f"- {voice_id}")


if __name__ == "__main__":
    main()
