#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import io
import re
import shutil
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


DATASET_ID = "pnnbao-ump/VieNeu-TTS-140h"

PROFILE_AUDIO_EXTS = {".wav", ".mp3", ".flac", ".ogg", ".m4a"}

BAD_TEXT_PATTERNS = [
    r"https?://",
    r"www\.",
    r"@",
    r"#\w+",
    r"\[[^\]]+\]",
    r"\([^)]+\)",
]


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    return value.strip("_") or "voice"


def import_datasets():
    try:
        from datasets import Audio, load_dataset, load_from_disk
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "Missing dependency: datasets\n"
            "Install in the active venv:\n"
            "  pip install datasets\n"
        ) from exc
    return Audio, load_dataset, load_from_disk


def import_soundfile():
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
    return sf


def decode_audio(audio: dict[str, Any]) -> dict[str, Any]:
    sf = import_soundfile()
    audio_bytes = audio.get("bytes")
    audio_path = audio.get("path")

    if audio_bytes:
        array, sr = sf.read(io.BytesIO(audio_bytes), dtype="float32", always_2d=False)
    elif audio_path and Path(audio_path).exists():
        array, sr = sf.read(audio_path, dtype="float32", always_2d=False)
    else:
        raise ValueError("Audio row has no embedded bytes or readable local path.")

    if getattr(array, "ndim", 1) == 2:
        array = array.mean(axis=1)

    return {"array": array.astype("float32", copy=False), "sampling_rate": int(sr)}


def write_pair(wav_path: Path, txt_path: Path, audio: dict[str, Any], text: str) -> float:
    sf = import_soundfile()
    array = audio["array"]
    sr = audio["sampling_rate"]
    duration = len(array) / sr
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(wav_path, array, sr)
    txt_path.write_text(text.strip() + "\n", encoding="utf-8")
    return duration


def copy_pair(src_wav: Path, src_txt: Path, dst_wav: Path, dst_txt: Path) -> None:
    dst_wav.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src_wav, dst_wav)
    shutil.copyfile(src_txt, dst_txt)


def get_speaker(row: dict[str, Any]) -> str:
    return str(row.get("speaker") or row.get("speaker_id") or row.get("client_id") or "").strip()


def get_text(row: dict[str, Any]) -> str:
    return str(row.get("text") or row.get("sentence") or row.get("transcription") or "").strip()


def get_duration_hint(row: dict[str, Any]) -> float:
    try:
        return float(row.get("duration") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def text_quality_score(text: str, duration: float) -> float:
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return -100.0

    score = 0.0
    char_count = len(normalized)
    word_count = len(normalized.split())
    chars_per_second = char_count / max(duration, 0.1)

    if 35 <= char_count <= 180:
        score += 3.0
    elif 20 <= char_count <= 240:
        score += 1.0
    else:
        score -= 2.0

    if 4 <= word_count <= 35:
        score += 2.0
    else:
        score -= 1.0

    if 8 <= chars_per_second <= 24:
        score += 2.0
    else:
        score -= 1.5

    if re.search(r"[,.!?;:…]$", normalized):
        score += 0.5
    if any(re.search(pattern, normalized, flags=re.IGNORECASE) for pattern in BAD_TEXT_PATTERNS):
        score -= 3.0
    if re.search(r"\d{4,}|[_*/\\{}<>|]", normalized):
        score -= 1.0
    if sum(1 for char in normalized if char.isupper()) > max(8, char_count * 0.25):
        score -= 1.0

    return score


def row_passes_text_filter(text: str, duration: float, min_text_chars: int, max_text_chars: int) -> bool:
    normalized = re.sub(r"\s+", " ", text).strip()
    if len(normalized) < min_text_chars or len(normalized) > max_text_chars:
        return False
    if any(re.search(pattern, normalized, flags=re.IGNORECASE) for pattern in BAD_TEXT_PATTERNS):
        return False
    chars_per_second = len(normalized) / max(duration, 0.1)
    return 6 <= chars_per_second <= 30


def choose_profile_rows(rows: list[dict[str, Any]], selection: str, max_seconds: float, max_clips: int) -> list[dict[str, Any]]:
    if selection == "longest":
        ranked = sorted(rows, key=lambda item: item["duration_hint"], reverse=True)
    elif selection == "sequential":
        ranked = rows
    else:
        ranked = sorted(
            rows,
            key=lambda item: (item["quality_score"], item["duration_hint"]),
            reverse=True,
        )

    selected: list[dict[str, Any]] = []
    total_seconds = 0.0
    for row in ranked:
        duration = row["duration_hint"]
        if duration <= 0:
            continue
        if max_clips and len(selected) >= max_clips:
            break
        if total_seconds + duration > max_seconds and selected:
            continue
        selected.append(row)
        total_seconds += duration
        if total_seconds >= max_seconds:
            break
    return selected


def backup_pretrained_files(pretrained_dir: Path, backup_root: Path) -> Path | None:
    files = [
        path
        for path in pretrained_dir.iterdir()
        if path.is_file() and (path.suffix.lower() in PROFILE_AUDIO_EXTS or path.suffix.lower() == ".txt")
    ] if pretrained_dir.exists() else []
    if not files:
        return None

    backup_dir = backup_root / datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir.mkdir(parents=True, exist_ok=True)
    for path in files:
        shutil.move(path.as_posix(), (backup_dir / path.name).as_posix())
    return backup_dir


def load_stream(dataset_id: str):
    Audio, load_dataset, load_from_disk = import_datasets()
    dataset_path = Path(dataset_id)
    if dataset_path.exists():
        if (dataset_path / "dataset_info.json").exists() or (dataset_path / "state.json").exists():
            ds = load_from_disk(dataset_id)
            if hasattr(ds, "keys") and "train" in ds:
                ds = ds["train"]
        else:
            ds = load_dataset(dataset_id, split="train")
    else:
        ds = load_dataset(dataset_id, split="train", streaming=True)
    return ds.cast_column("audio", Audio(decode=False))


def survey_speakers(args: argparse.Namespace) -> None:
    stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "count": 0,
            "duration": 0.0,
            "usable_count": 0,
            "usable_duration": 0.0,
            "gender": "",
            "examples": [],
            "quality_sum": 0.0,
        }
    )
    scanned = 0

    for row in load_stream(args.dataset):
        scanned += 1
        speaker = get_speaker(row)
        if not speaker:
            continue
        gender = str(row.get("gender") or "").strip().lower()
        if args.gender != "all" and gender != args.gender:
            continue
        duration = get_duration_hint(row)
        text = get_text(row)

        item = stats[speaker]
        item["count"] += 1
        item["duration"] += duration
        item["gender"] = gender
        if (
            duration >= args.min_seconds
            and duration <= args.max_seconds
            and row_passes_text_filter(text, duration, args.min_text_chars, args.max_text_chars)
        ):
            item["usable_count"] += 1
            item["usable_duration"] += duration
            item["quality_sum"] += text_quality_score(text, duration)
        if text and len(item["examples"]) < 2:
            item["examples"].append(text)

        if args.scan_limit and scanned >= args.scan_limit:
            break

    ranked = sorted(
        stats.items(),
        key=lambda item: (item[1]["usable_duration"], item[1]["duration"]),
        reverse=True,
    )
    print("speaker,gender,usable_minutes,total_minutes,usable_clips,total_clips,avg_quality,profile_ready,example")
    for speaker, info in ranked[: args.topn]:
        example = " | ".join(info["examples"])
        avg_quality = info["quality_sum"] / max(1, info["usable_count"])
        profile_ready = "yes" if info["usable_duration"] >= args.target_min_minutes * 60 else "no"
        print(
            f"{speaker},{info['gender']},{info['usable_duration'] / 60:.2f},"
            f"{info['duration'] / 60:.2f},{info['usable_count']},{info['count']},"
            f"{avg_quality:.2f},{profile_ready},{example}"
        )


def export_speaker(args: argparse.Namespace) -> None:
    speaker_slug = slugify(args.speaker)
    out_dir = Path(args.out_dir) / speaker_slug
    wavs_dir = Path("wavs")
    pretrained_dir = Path("viterbox/pretrained")
    manifest_rows: list[dict[str, str]] = []
    first_pair: tuple[Path, Path] | None = None
    max_seconds = args.profile_max_minutes * 60.0
    candidate_rows: list[dict[str, Any]] = []
    scanned = 0

    for row in load_stream(args.dataset):
        scanned += 1
        speaker = get_speaker(row)
        if speaker != args.speaker:
            if args.scan_limit and scanned >= args.scan_limit:
                break
            continue
        text = get_text(row)
        audio_raw = row.get("audio")
        if not text or not audio_raw:
            continue

        duration_hint = get_duration_hint(row)
        if duration_hint and (duration_hint < args.min_seconds or duration_hint > args.max_seconds):
            continue
        if duration_hint and not row_passes_text_filter(
            text, duration_hint, args.min_text_chars, args.max_text_chars
        ):
            continue

        candidate_rows.append(
            {
                "row": row,
                "audio_raw": audio_raw,
                "text": text,
                "duration_hint": duration_hint,
                "quality_score": text_quality_score(text, duration_hint) if duration_hint else 0.0,
            }
        )

        if args.scan_limit and scanned >= args.scan_limit:
            break

    if not candidate_rows:
        raise SystemExit(f"No usable clips found for speaker={args.speaker!r}. Try relaxing duration/text filters.")

    selected_rows = choose_profile_rows(candidate_rows, args.selection, max_seconds, args.max_clips)
    planned_seconds = sum(item["duration_hint"] for item in selected_rows)
    print(
        f"selected speaker={args.speaker} clips={len(selected_rows)} "
        f"planned={planned_seconds / 60:.2f}m candidates={len(candidate_rows)}"
    )
    if planned_seconds < args.target_min_minutes * 60:
        print(
            f"[WARN] selected audio is only {planned_seconds / 60:.2f}m; "
            f"target minimum is {args.target_min_minutes:.2f}m."
        )

    if args.clear_pretrained:
        backup_dir = backup_pretrained_files(pretrained_dir, Path(args.pretrained_backup_dir))
        if backup_dir:
            print(f"moved existing viterbox/pretrained audio/text to {backup_dir}")

    if args.dry_run:
        print("dry-run only. No wav/txt files written.")
        return

    total_seconds = 0.0
    clip_count = 0
    for item in selected_rows:
        row = item["row"]
        text = item["text"]
        audio_raw = item["audio_raw"]

        try:
            audio = decode_audio(audio_raw)
        except Exception as exc:
            print(f"skip {args.speaker}: cannot decode audio ({exc})")
            continue

        duration = len(audio["array"]) / audio["sampling_rate"]
        if duration < args.min_seconds or duration > args.max_seconds:
            continue
        if total_seconds + duration > max_seconds and clip_count > 0:
            continue

        base_name = f"vieneu_{speaker_slug}_{clip_count:04d}"
        wav_path = out_dir / f"{base_name}.wav"
        txt_path = out_dir / f"{base_name}.txt"
        duration = write_pair(wav_path, txt_path, audio, text)
        first_pair = first_pair or (wav_path, txt_path)

        if args.prepare_pretrained:
            write_pair(
                pretrained_dir / f"{args.pretrained_prefix}_{speaker_slug}_{clip_count:04d}.wav",
                pretrained_dir / f"{args.pretrained_prefix}_{speaker_slug}_{clip_count:04d}.txt",
                audio,
                text,
            )

        manifest_rows.append(
            {
                "speaker": args.speaker,
                "gender": str(row.get("gender") or ""),
                "duration_seconds": f"{duration:.3f}",
                "quality_score": f"{item['quality_score']:.3f}",
                "wav_path": wav_path.as_posix(),
                "txt_path": txt_path.as_posix(),
                "text": text,
            }
        )
        total_seconds += duration
        clip_count += 1
        print(f"saved {wav_path} ({duration:.2f}s, total={total_seconds / 60:.2f}m)")

    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.csv"
    with manifest_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "speaker",
                "gender",
                "duration_seconds",
                "quality_score",
                "wav_path",
                "txt_path",
                "text",
            ],
        )
        writer.writeheader()
        writer.writerows(manifest_rows)

    if args.copy_first_to_wavs and first_pair:
        app_base = f"vieneu_{speaker_slug}"
        copy_pair(first_pair[0], first_pair[1], wavs_dir / f"{app_base}.wav", wavs_dir / f"{app_base}.txt")

    print(f"\nDone. clips={clip_count}, minutes={total_seconds / 60:.2f}, manifest={manifest_path}")
    if args.prepare_pretrained:
        print("Profile source copied to viterbox/pretrained/. Build conds.pt with:")
        print("  python viterbox/pretrain_voice_builder.py --copy_to_model")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Survey/export single-speaker VieNeu-TTS-140h voice samples for BetterBox Viterbox profile building."
    )
    parser.add_argument("--dataset", default=DATASET_ID)
    parser.add_argument("--survey-speakers", action="store_true")
    parser.add_argument("--speaker", default="", help="Exact speaker id to export, e.g. jellyfish1010_0041.")
    parser.add_argument("--gender", choices=["all", "male", "female"], default="male")
    parser.add_argument("--topn", type=int, default=30)
    parser.add_argument("--scan-limit", type=int, default=0, help="0 means scan all rows.")
    parser.add_argument("--out-dir", default="voice_bank/vieneu")
    parser.add_argument("--profile-max-minutes", type=float, default=25.0)
    parser.add_argument(
        "--target-min-minutes",
        type=float,
        default=20.0,
        help="Warn if the selected/exported speaker has less usable audio than this target.",
    )
    parser.add_argument("--min-seconds", type=float, default=4.0)
    parser.add_argument("--max-seconds", type=float, default=12.0)
    parser.add_argument("--min-text-chars", type=int, default=24)
    parser.add_argument("--max-text-chars", type=int, default=220)
    parser.add_argument("--max-clips", type=int, default=0, help="0 means unlimited until max minutes.")
    parser.add_argument(
        "--selection",
        choices=["audiobook", "longest", "sequential"],
        default="audiobook",
        help="Clip selection strategy for profile export.",
    )
    parser.add_argument("--prepare-pretrained", action="store_true", help="Also copy exported clips to viterbox/pretrained.")
    parser.add_argument(
        "--clear-pretrained",
        action="store_true",
        help="Move existing viterbox/pretrained audio/text to a timestamped backup before preparing this speaker.",
    )
    parser.add_argument("--pretrained-backup-dir", default="voice_bank/pretrained_backup")
    parser.add_argument("--pretrained-prefix", default="profile")
    parser.add_argument("--copy-first-to-wavs", action="store_true", help="Copy first clip to wavs/ for app dropdown.")
    parser.add_argument("--dry-run", action="store_true", help="Select rows and print summary without writing files.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.survey_speakers:
        survey_speakers(args)
        return
    if not args.speaker:
        raise SystemExit("Use --survey-speakers first, then pass --speaker <speaker_id> to export one voice.")
    export_speaker(args)


if __name__ == "__main__":
    main()
