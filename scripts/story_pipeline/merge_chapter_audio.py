#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path

import numpy as np
import soundfile as sf


CHAPTER_WAV_PATTERN = re.compile(r"chapter(\d+)\.wav$", re.IGNORECASE)


def chapter_number(path: Path) -> int:
    match = CHAPTER_WAV_PATTERN.match(path.name)
    return int(match.group(1)) if match else 0


def list_wavs(folder: Path) -> list[Path]:
    return sorted(
        [path for path in folder.glob("chapter*.wav") if CHAPTER_WAV_PATTERN.match(path.name)],
        key=chapter_number,
    )


def read_mono(path: Path) -> tuple[np.ndarray, int]:
    audio, sr = sf.read(path, dtype="float32", always_2d=False)
    if audio.ndim == 2:
        audio = audio.mean(axis=1)
    return audio.astype(np.float32, copy=False), int(sr)


def merge_chapters(source_folder: Path, output_path: Path, number: int, silence_ms: int) -> None:
    wav_files = list_wavs(source_folder)
    if number > 0:
        wav_files = wav_files[:number]
    if not wav_files:
        raise SystemExit(f"Không tìm thấy chapter*.wav trong {source_folder}")

    pieces: list[np.ndarray] = []
    target_sr: int | None = None

    for idx, wav_path in enumerate(wav_files):
        audio, sr = read_mono(wav_path)
        if target_sr is None:
            target_sr = sr
        elif sr != target_sr:
            raise SystemExit(
                f"Sample rate không khớp: {wav_path} có {sr} Hz, file đầu là {target_sr} Hz."
            )

        if idx and silence_ms > 0:
            pieces.append(np.zeros(int(target_sr * silence_ms / 1000), dtype=np.float32))
        pieces.append(audio)
        print(f"Thêm {wav_path.name}: {len(audio) / sr:.2f}s")

    assert target_sr is not None
    merged = np.concatenate(pieces)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(output_path, merged, target_sr)
    print(f"Hoàn tất: {output_path} ({len(merged) / target_sr:.2f}s)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge các file chapterX.wav theo thứ tự số chương.")
    parser.add_argument("--folder", required=True, help="Folder chứa chapterX.wav.")
    parser.add_argument("--number", type=int, default=0, help="0 nghĩa là merge toàn bộ.")
    parser.add_argument("--silence-ms", type=int, default=1000)
    parser.add_argument("--output", default="", help="Nếu bỏ trống sẽ lưu vào story_audio_merged/<folder>.wav")
    args = parser.parse_args()

    source_folder = Path(args.folder).resolve()
    if not source_folder.is_dir():
        raise SystemExit(f"Không tìm thấy folder: {source_folder}")

    if args.output:
        output_path = Path(args.output)
    else:
        suffix = f"_first_{args.number}" if args.number > 0 else "_all"
        output_path = Path("story_audio_merged") / f"{source_folder.name}{suffix}.wav"

    merge_chapters(source_folder, output_path, args.number, args.silence_ms)


if __name__ == "__main__":
    main()
