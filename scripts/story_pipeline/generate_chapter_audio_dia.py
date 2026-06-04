#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
import tempfile
from pathlib import Path


DEFAULT_DIA_ROOT = "/home/yuki/Desktop/python/Dia-Finetuning-Vietnamese"
CHAPTER_PATTERN = re.compile(r"chapter(\d+)\.txt$", re.IGNORECASE)


def chapter_number(path: Path) -> int:
    match = CHAPTER_PATTERN.match(path.name)
    return int(match.group(1)) if match else 0


def list_chapter_files(input_dir: Path) -> list[Path]:
    return sorted(
        [path for path in input_dir.glob("chapter*.txt") if CHAPTER_PATTERN.match(path.name)],
        key=chapter_number,
    )


def ensure_speaker_tag(text: str, speaker_tag: str) -> str:
    text = text.strip()
    if not speaker_tag:
        return text
    if text.startswith("["):
        return text
    return f"{speaker_tag} {text}"


def run_dia_for_text(text: str, output: Path, args: argparse.Namespace) -> None:
    dia_root = Path(args.dia_root).resolve()
    python_bin = dia_root / ".venv/bin/python"
    demo_script = dia_root / "demo_script.py"
    config = dia_root / "dia/config_inference.json"
    checkpoint = dia_root / "dia/model.safetensors"

    for path in [python_bin, demo_script, config, checkpoint]:
        if not path.exists():
            raise SystemExit(f"Không tìm thấy Dia dependency: {path}")

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as tmp:
        tmp.write(text)
        tmp_path = Path(tmp.name)

    cmd = [
        str(python_bin),
        str(demo_script),
        "--text-file",
        str(tmp_path),
        "--output",
        str(output.resolve()),
        "--config",
        str(config),
        "--checkpoint",
        str(checkpoint),
        "--device",
        args.device,
        "--max-tokens",
        str(args.max_tokens),
        "--cfg-scale",
        str(args.cfg_scale),
        "--temperature",
        str(args.temperature),
        "--top-p",
        str(args.top_p),
        "--cfg-filter-top-k",
        str(args.cfg_filter_top_k),
        "--speed-factor",
        str(args.speed_factor),
        "--max-chars-per-chunk",
        str(args.max_chars_per_chunk),
    ]
    if args.half:
        cmd.append("--half")
    if args.compile:
        cmd.append("--compile")

    try:
        subprocess.run(cmd, cwd=str(dia_root), check=True)
    finally:
        tmp_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sinh chapter audio bằng Dia backend.")
    parser.add_argument("--input-dir", required=True, help="Folder chứa chapterX.txt.")
    parser.add_argument("--output-root", default="story_audio_dia")
    parser.add_argument("--chapter", type=int, default=0, help="0 nghĩa là chapter1 nếu không dùng --all.")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dia-root", default=DEFAULT_DIA_ROOT)
    parser.add_argument("--speaker-tag", default="[W2WAnime]")
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--max-tokens", type=int, default=3072)
    parser.add_argument("--cfg-scale", type=float, default=3.0)
    parser.add_argument("--temperature", type=float, default=1.3)
    parser.add_argument("--top-p", type=float, default=0.95)
    parser.add_argument("--cfg-filter-top-k", type=int, default=35)
    parser.add_argument("--speed-factor", type=float, default=0.94)
    parser.add_argument("--max-chars-per-chunk", type=int, default=500)
    parser.add_argument("--half", action="store_true")
    parser.add_argument("--compile", action="store_true")
    args = parser.parse_args()

    input_dir = Path(args.input_dir).resolve()
    if not input_dir.is_dir():
        raise SystemExit(f"Không tìm thấy input-dir: {input_dir}")

    if args.all:
        chapter_files = list_chapter_files(input_dir)
    else:
        chapter_num = args.chapter or 1
        chapter_files = [input_dir / f"chapter{chapter_num}.txt"]

    chapter_files = [path for path in chapter_files if path.exists()]
    if not chapter_files:
        raise SystemExit("Không có chapter file để xử lý.")

    output_dir = Path(args.output_root) / input_dir.name
    for chapter_path in chapter_files:
        output_path = output_dir / f"{chapter_path.stem}.wav"
        if output_path.exists() and not args.overwrite:
            print(f"[SKIP] Đã tồn tại: {output_path}")
            continue
        text = ensure_speaker_tag(chapter_path.read_text(encoding="utf-8"), args.speaker_tag)
        print(f"\n=== Dia TTS {chapter_path.name} -> {output_path} ===")
        run_dia_for_text(text, output_path, args)

    print(f"\nHoàn tất. Audio Dia nằm trong: {output_dir}")


if __name__ == "__main__":
    main()
