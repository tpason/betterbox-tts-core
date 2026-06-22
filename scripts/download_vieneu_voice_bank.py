#!/usr/bin/env python3
"""Survey and export Vietnamese narrator voices from Hugging Face for VieNeu clone profiles."""
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

KNOWN_DATASETS: dict[str, str] = {
    "vieneu-140h": "pnnbao-ump/VieNeu-TTS-140h",
    "phoaudiobook": "thivux/phoaudiobook",
    "vieneu-500h": "pnnbao-ump/VieNeu-TTS-500h-dialects",
    "dolly-1000h": "dolly-vn/dolly-audio-1000h-vietnamese",
}

NARRATIVE_KEYWORDS = (
    "tu luyện", "linh lực", "kiếm", "võ", "tiên", "ma", "thần", "linh", "cảnh giới",
    "đan", "môn phái", "sư phụ", "đệ tử", "huyền", "truyền thuyết", "chiến", "sát",
    "long", "phượng", "cung", "triều", "vương", "hoàng", "rừng", "núi", "sương",
    "anh ta", "cô ta", "hắn", "nàng", "thầm nghĩ", "mắt nhìn", "bước chân",
    "kể chuyện", "truyện", "chương", "sách",
)
NON_NARRATIVE_KEYWORDS = (
    "youtube", "facebook", "tiktok", "quảng cáo", "marketing", "startup",
    "crypto", "podcast", "unboxing", "đăng ký", "khóa học",
)

PROFILE_AUDIO_EXTS = {".wav", ".mp3", ".flac", ".ogg", ".m4a"}
BAD_TEXT_PATTERNS = [
    r"https?://",
    r"www\.",
    r"@",
    r"#\w+",
    r"\[[^\]]+\]",
]


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9À-ỹ]+", "_", value, flags=re.UNICODE)
    return value.strip("_") or "voice"


def import_datasets():
    try:
        from datasets import Audio, load_dataset, load_from_disk
    except ModuleNotFoundError as exc:
        raise SystemExit("Missing dependency: datasets — pip install datasets") from exc
    return Audio, load_dataset, load_from_disk


def import_soundfile():
    try:
        import soundfile as sf
    except ModuleNotFoundError as exc:
        raise SystemExit("Missing dependency: soundfile") from exc
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


def estimate_duration(text: str, duration_hint: float) -> float:
    if duration_hint > 0:
        return duration_hint
    return max(4.0, len(re.sub(r"\s+", " ", text).strip()) / 14.0)


def narrative_text_score(text: str) -> float:
    normalized = re.sub(r"\s+", " ", text).strip().lower()
    if not normalized:
        return -5.0
    score = min(8.0, sum(1.2 for kw in NARRATIVE_KEYWORDS if kw in normalized))
    if any(kw in normalized for kw in NON_NARRATIVE_KEYWORDS):
        score -= 4.0
    if len(normalized) >= 80:
        score += 1.0
    if "..." in text or "—" in text:
        score += 0.5
    return score


def text_quality_score(text: str, duration: float) -> float:
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return -100.0
    score = 0.0
    char_count = len(normalized)
    word_count = len(normalized.split())
    cps = char_count / max(duration, 0.1)
    if 35 <= char_count <= 220:
        score += 3.0
    elif 20 <= char_count <= 280:
        score += 1.0
    else:
        score -= 2.0
    if 4 <= word_count <= 40:
        score += 2.0
    if 8 <= cps <= 26:
        score += 2.0
    else:
        score -= 1.5
    if re.search(r"[,.!?;:…]$", normalized):
        score += 0.5
    if any(re.search(p, normalized, re.I) for p in BAD_TEXT_PATTERNS):
        score -= 3.0
    return score


def combined_clip_score(text: str, duration: float) -> float:
    return text_quality_score(text, duration) + narrative_text_score(text)


def speaker_passes_gender(row: dict[str, Any], gender_filter: str) -> bool:
    if gender_filter == "all":
        return True
    gender = str(row.get("gender") or "").strip().lower()
    if not gender:
        return True  # PhoAudiobook etc. — no gender column
    return gender == gender_filter


def row_passes_text_filter(text: str, duration: float, min_text_chars: int, max_text_chars: int) -> bool:
    normalized = re.sub(r"\s+", " ", text).strip()
    if len(normalized) < min_text_chars or len(normalized) > max_text_chars:
        return False
    if any(re.search(p, normalized, re.I) for p in BAD_TEXT_PATTERNS):
        return False
    cps = len(normalized) / max(duration, 0.1)
    return 6 <= cps <= 30


def choose_profile_rows(
    rows: list[dict[str, Any]], selection: str, max_seconds: float, max_clips: int
) -> list[dict[str, Any]]:
    if selection == "longest":
        ranked = sorted(rows, key=lambda item: item["duration_hint"], reverse=True)
    elif selection == "sequential":
        ranked = rows
    else:
        ranked = sorted(rows, key=lambda item: (item["quality_score"], item["duration_hint"]), reverse=True)
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
    if not pretrained_dir.exists():
        return None
    files = [
        p for p in pretrained_dir.iterdir()
        if p.is_file() and (p.suffix.lower() in PROFILE_AUDIO_EXTS or p.suffix.lower() == ".txt")
    ]
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
    if dataset_path.exists() and (dataset_path / "dataset_info.json").exists():
        ds = load_from_disk(dataset_id)
        if hasattr(ds, "keys") and "train" in ds:
            ds = ds["train"]
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
            "narrative_hits": 0,
        }
    )
    scanned = 0
    csv_path = Path(args.output_csv) if args.output_csv else None

    for row in load_stream(args.dataset):
        scanned += 1
        speaker = get_speaker(row)
        if not speaker or not speaker_passes_gender(row, args.gender):
            continue
        text = get_text(row)
        duration = estimate_duration(text, get_duration_hint(row))
        item = stats[speaker]
        item["count"] += 1
        item["duration"] += duration
        item["gender"] = str(row.get("gender") or "").strip().lower()
        if (
            duration >= args.min_seconds
            and duration <= args.max_seconds
            and row_passes_text_filter(text, duration, args.min_text_chars, args.max_text_chars)
        ):
            q = combined_clip_score(text, duration)
            item["usable_count"] += 1
            item["usable_duration"] += duration
            item["quality_sum"] += q
            if narrative_text_score(text) >= 2.0:
                item["narrative_hits"] += 1
        if text and len(item["examples"]) < 2:
            item["examples"].append(text[:160])
        if args.scan_limit and scanned >= args.scan_limit:
            break
        if scanned % 5000 == 0:
            print(f"[survey] scanned={scanned} speakers={len(stats)}", flush=True)

    def rank_key(item: tuple[str, dict[str, Any]]) -> tuple[float, float, float]:
        _sp, info = item
        avg_q = info["quality_sum"] / max(1, info["usable_count"])
        narrative = info["narrative_hits"] if args.prefer_narrative else 0
        return (narrative, info["usable_duration"] * avg_q, info["usable_duration"])

    ranked = sorted(stats.items(), key=rank_key, reverse=True)
    header = (
        "speaker,gender,usable_minutes,total_minutes,usable_clips,total_clips,"
        "avg_quality,narrative_hits,profile_ready,example"
    )
    print(header)
    lines = [header]
    for speaker, info in ranked[: args.topn]:
        example = " | ".join(info["examples"]).replace(",", ";")
        avg_quality = info["quality_sum"] / max(1, info["usable_count"])
        profile_ready = "yes" if info["usable_duration"] >= args.target_min_minutes * 60 else "no"
        line = (
            f"{speaker},{info['gender']},{info['usable_duration'] / 60:.2f},"
            f"{info['duration'] / 60:.2f},{info['usable_count']},{info['count']},"
            f"{avg_quality:.2f},{info['narrative_hits']},{profile_ready},{example}"
        )
        print(line)
        lines.append(line)
    if csv_path:
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\n[survey] wrote {csv_path}", flush=True)


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
        if get_speaker(row) != args.speaker:
            if args.scan_limit and scanned >= args.scan_limit:
                break
            continue
        text = get_text(row)
        audio_raw = row.get("audio")
        if not text or not audio_raw:
            continue
        duration_hint = estimate_duration(text, get_duration_hint(row))
        if duration_hint < args.min_seconds or duration_hint > args.max_seconds:
            continue
        if not row_passes_text_filter(text, duration_hint, args.min_text_chars, args.max_text_chars):
            continue
        candidate_rows.append({
            "row": row,
            "audio_raw": audio_raw,
            "text": text,
            "duration_hint": duration_hint,
            "quality_score": combined_clip_score(text, duration_hint),
        })
        # Early stop: enough high-quality candidates to fill profile.
        if len(candidate_rows) >= 150:
            top = sorted(candidate_rows, key=lambda x: x["quality_score"], reverse=True)
            if sum(x["duration_hint"] for x in top[:120]) >= max_seconds * 1.5:
                break
        if args.scan_limit and scanned >= args.scan_limit:
            break

    if not candidate_rows:
        raise SystemExit(f"No usable clips for speaker={args.speaker!r}")

    selected_rows = choose_profile_rows(candidate_rows, args.selection, max_seconds, args.max_clips)
    planned_seconds = sum(item["duration_hint"] for item in selected_rows)
    print(
        f"selected speaker={args.speaker} clips={len(selected_rows)} "
        f"planned={planned_seconds / 60:.2f}m candidates={len(candidate_rows)}"
    )
    if planned_seconds < args.target_min_minutes * 60:
        print(f"[WARN] only {planned_seconds / 60:.2f}m selected; target {args.target_min_minutes:.0f}m")

    if args.clear_pretrained:
        backup_dir = backup_pretrained_files(pretrained_dir, Path(args.pretrained_backup_dir))
        if backup_dir:
            print(f"moved viterbox/pretrained → {backup_dir}")

    if args.dry_run:
        print("dry-run only.")
        return

    total_seconds = 0.0
    clip_count = 0
    for item in selected_rows:
        try:
            audio = decode_audio(item["audio_raw"])
        except Exception as exc:
            print(f"skip decode: {exc}")
            continue
        duration = len(audio["array"]) / audio["sampling_rate"]
        if duration < args.min_seconds or duration > args.max_seconds:
            continue
        if total_seconds + duration > max_seconds and clip_count > 0:
            continue
        base_name = f"vieneu_{speaker_slug}_{clip_count:04d}"
        wav_path = out_dir / f"{base_name}.wav"
        txt_path = out_dir / f"{base_name}.txt"
        duration = write_pair(wav_path, txt_path, audio, item["text"])
        first_pair = first_pair or (wav_path, txt_path)
        if args.prepare_pretrained:
            write_pair(
                pretrained_dir / f"{args.pretrained_prefix}_{speaker_slug}_{clip_count:04d}.wav",
                pretrained_dir / f"{args.pretrained_prefix}_{speaker_slug}_{clip_count:04d}.txt",
                audio,
                item["text"],
            )
        manifest_rows.append({
            "speaker": args.speaker,
            "gender": str(item["row"].get("gender") or ""),
            "duration_seconds": f"{duration:.3f}",
            "quality_score": f"{item['quality_score']:.3f}",
            "wav_path": wav_path.as_posix(),
            "txt_path": txt_path.as_posix(),
            "text": item["text"],
        })
        total_seconds += duration
        clip_count += 1
        print(f"saved {wav_path} ({duration:.2f}s, total={total_seconds / 60:.2f}m)")

    manifest_path = out_dir / "manifest.csv"
    with manifest_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(manifest_rows[0].keys()) if manifest_rows else [])
        if manifest_rows:
            writer.writeheader()
            writer.writerows(manifest_rows)

    if args.copy_first_to_wavs and first_pair:
        copy_pair(first_pair[0], first_pair[1], wavs_dir / f"vieneu_{speaker_slug}.wav", wavs_dir / f"vieneu_{speaker_slug}.txt")

    print(f"\nDone. clips={clip_count}, minutes={total_seconds / 60:.2f}, manifest={manifest_path}")


def resolve_dataset_id(raw: str) -> str:
    return KNOWN_DATASETS.get(raw, raw)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Survey/export HF voice samples for VieNeu clone profiles.")
    parser.add_argument("--dataset", default=DATASET_ID, help=f"HF id or alias: {', '.join(KNOWN_DATASETS)}")
    parser.add_argument("--survey-speakers", action="store_true")
    parser.add_argument("--speaker", default="")
    parser.add_argument("--gender", choices=["all", "male", "female"], default="all")
    parser.add_argument("--topn", type=int, default=30)
    parser.add_argument("--scan-limit", type=int, default=0)
    parser.add_argument("--out-dir", default="voice_bank/vieneu")
    parser.add_argument("--profile-max-minutes", type=float, default=25.0)
    parser.add_argument("--target-min-minutes", type=float, default=20.0)
    parser.add_argument("--min-seconds", type=float, default=4.0)
    parser.add_argument("--max-seconds", type=float, default=20.0)
    parser.add_argument("--min-text-chars", type=int, default=24)
    parser.add_argument("--max-text-chars", type=int, default=320)
    parser.add_argument("--max-clips", type=int, default=0)
    parser.add_argument("--selection", choices=["audiobook", "longest", "sequential"], default="audiobook")
    parser.add_argument("--prepare-pretrained", action="store_true")
    parser.add_argument("--clear-pretrained", action="store_true")
    parser.add_argument("--pretrained-backup-dir", default="voice_bank/pretrained_backup")
    parser.add_argument("--pretrained-prefix", default="profile")
    parser.add_argument("--copy-first-to-wavs", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output-csv", default="")
    parser.add_argument("--prefer-narrative", action="store_true", default=True)
    parser.add_argument("--no-prefer-narrative", dest="prefer_narrative", action="store_false")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.dataset = resolve_dataset_id(args.dataset)
    print(f"[dataset] {args.dataset}", flush=True)
    if args.survey_speakers:
        survey_speakers(args)
        return
    if not args.speaker:
        raise SystemExit("Use --survey-speakers then --speaker <id> to export.")
    export_speaker(args)


if __name__ == "__main__":
    main()
