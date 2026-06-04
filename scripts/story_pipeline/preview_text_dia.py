#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path


DEFAULT_DIA_ROOT = "/home/yuki/Desktop/python/Dia-Finetuning-Vietnamese"


def read_text_arg(args: argparse.Namespace) -> str:
    if args.text_file:
        text = Path(args.text_file).read_text(encoding="utf-8")
        return text[: args.max_chars] if args.max_chars > 0 else text
    if args.text:
        return args.text[: args.max_chars] if args.max_chars > 0 else args.text
    raise SystemExit("Cần truyền --text hoặc --text-file.")


def ensure_speaker_tag(text: str, speaker_tag: str) -> str:
    text = text.strip()
    if not speaker_tag:
        return text
    if text.startswith("["):
        return text
    return f"{speaker_tag} {text}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview text bằng Dia backend từ BetterBox story pipeline.")
    parser.add_argument("--text", default="")
    parser.add_argument("--text-file", default="")
    parser.add_argument("--output", default="story_audio/preview_dia.wav")
    parser.add_argument("--max-chars", type=int, default=0)
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

    dia_root = Path(args.dia_root).resolve()
    python_bin = dia_root / ".venv/bin/python"
    demo_script = dia_root / "demo_script.py"
    config = dia_root / "dia/config_inference.json"
    checkpoint = dia_root / "dia/model.safetensors"

    for path in [python_bin, demo_script, config, checkpoint]:
        if not path.exists():
            raise SystemExit(f"Không tìm thấy Dia dependency: {path}")

    text = ensure_speaker_tag(read_text_arg(args), args.speaker_tag)
    output = Path(args.output).resolve()
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
        str(output),
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

    print(f"Đã lưu Dia preview: {output}")


if __name__ == "__main__":
    main()
