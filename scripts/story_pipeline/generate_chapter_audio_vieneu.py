#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path

import soundfile as sf


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("GRADIO_TEMP_DIR", str(Path(tempfile.gettempdir()) / "betterbox_story_tmp"))
Path(os.environ["GRADIO_TEMP_DIR"]).mkdir(parents=True, exist_ok=True)

from vieneu import Vieneu  # noqa: E402

from scripts.story_pipeline.story_text_markup import prepare_text_for_tts  # noqa: E402
from scripts.story_pipeline.vieneu_audiobook_stitch import (  # noqa: E402
    DEFAULT_MAX_NEW_FRAMES,
    DEFAULT_VIENEU_VOICE,
    get_vieneu_sample_rate,
    synthesize_vieneu_audiobook,
)
from scripts.story_pipeline.vieneu_voice_profiles import (  # noqa: E402
    DEFAULT_VIENEU_VOICE_PROFILE,
    resolve_vieneu_voice_profile,
)


CHAPTER_PATTERN = re.compile(r"chapter(\d+)\.txt$", re.IGNORECASE)


def chapter_number(path: Path) -> int:
    match = CHAPTER_PATTERN.match(path.name)
    return int(match.group(1)) if match else 0


def list_chapter_files(input_dir: Path) -> list[Path]:
    return sorted(
        [path for path in input_dir.glob("chapter*.txt") if CHAPTER_PATTERN.match(path.name)],
        key=chapter_number,
    )


def path_candidates(path: Path) -> list[str]:
    resolved = path.resolve()
    candidates = [path.as_posix(), resolved.as_posix()]
    try:
        candidates.append(resolved.relative_to(PROJECT_ROOT).as_posix())
    except ValueError:
        pass
    return list(dict.fromkeys(candidates))


def sync_audio_to_db(chapter_path: Path, output_path: Path) -> None:
    try:
        from story_db.story_pipeline_db import repository as repo

        row = repo.update_chapter_audio_by_polished_path(
            path_candidates(chapter_path),
            audio_path=output_path.as_posix(),
        )
        if row:
            print(f"[DB] synced audio chapter: {chapter_path.name}")
        else:
            print(f"[DB] no chapter matched polished path: {chapter_path}")
    except Exception as exc:
        print(f"[DB WARN] không sync được audio path: {exc}")


def synthesize_chapter(tts, chapter_path: Path, output_path: Path, args: argparse.Namespace) -> None:
    text = chapter_path.read_text(encoding="utf-8").strip()
    if not text:
        print(f"[SKIP] File rỗng: {chapter_path}")
        return
    original_len = len(text)
    text = prepare_text_for_tts(text)
    if not text:
        print(f"[SKIP] Text rỗng sau prepare_text_for_tts: {chapter_path}")
        return
    print(f"[PREP] {chapter_path.name}: {original_len} -> {len(text)} chars after TTS prep")

    print(f"\n=== {chapter_path.name}: {len(text)} chars -> VieNeu audiobook stitch ===")
    audio_np = synthesize_vieneu_audiobook(
        tts,
        text,
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
        max_chars_per_unit=args.max_chars_per_unit,
        min_chars_per_unit=args.min_chars_per_unit,
        sentence_pause_ms=args.sentence_pause_ms,
        crossfade_ms=args.crossfade_ms,
        trim_threshold=args.trim_threshold,
        trim_margin_ms=args.trim_margin_ms,
        edge_fade_in_ms=args.edge_fade_in_ms,
        edge_fade_out_ms=args.edge_fade_out_ms,
    )

    sr = get_vieneu_sample_rate(tts)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(output_path, audio_np, sr)
    print(f"Đã lưu {output_path} ({len(audio_np) / sr:.2f}s, {sr}Hz)")


def load_vieneu(args: argparse.Namespace):
    kwargs: dict[str, str] = {
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
    parser = argparse.ArgumentParser(description="Sinh audio chapter*.txt bằng VieNeu-TTS v3 Turbo.")
    parser.add_argument("--input-dir", required=True, help="Folder chứa chapterX.txt.")
    parser.add_argument("--output-root", default="story_audio")
    parser.add_argument("--chapter", type=int, default=0, help="0 nghĩa là dùng --all hoặc mặc định chapter1.")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--mode", default="v3turbo")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--backend", choices=["auto", "onnx", "pytorch"], default="auto")
    parser.add_argument("--onnx-dir", default=None)
    parser.add_argument("--hf-token", default=None)
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
    parser.add_argument("--max-chars", type=int, default=256, help="VieNeu internal chunk max chars.")
    parser.add_argument("--no-watermark", action="store_true")
    parser.add_argument("--max-chars-per-unit", type=int, default=None, help="Advanced/debug override. Omit to use auto audiobook split.")
    parser.add_argument("--min-chars-per-unit", type=int, default=None, help="Advanced/debug override. Omit to use auto audiobook split.")
    parser.add_argument("--sentence-pause-ms", type=int, default=500)
    parser.add_argument("--crossfade-ms", type=int, default=50)
    parser.add_argument("--trim-threshold", type=float, default=0.006)
    parser.add_argument("--trim-margin-ms", type=int, default=80)
    parser.add_argument("--edge-fade-in-ms", type=int, default=5)
    parser.add_argument("--edge-fade-out-ms", type=int, default=22)
    args = parser.parse_args()
    args.voice_profile = args.voice_profile or None

    input_dir = Path(args.input_dir).resolve()
    if not input_dir.is_dir():
        raise SystemExit(f"Không tìm thấy input-dir: {input_dir}")

    if args.reference_audio:
        reference_audio = Path(args.reference_audio)
        if not reference_audio.exists():
            raise SystemExit(f"Không tìm thấy reference audio: {reference_audio}")
        args.reference_audio = str(reference_audio.resolve())
    elif args.voice_profile:
        resolve_vieneu_voice_profile(args.voice_profile)

    if args.all:
        chapter_files = list_chapter_files(input_dir)
    else:
        chapter_num = args.chapter or 1
        chapter_files = [input_dir / f"chapter{chapter_num}.txt"]

    chapter_files = [path for path in chapter_files if path.exists() and CHAPTER_PATTERN.match(path.name)]
    if not chapter_files:
        raise SystemExit("Không có chapter file để xử lý.")

    tts = load_vieneu(args)
    output_dir = Path(args.output_root) / input_dir.name
    for chapter_path in sorted(chapter_files, key=chapter_number):
        output_path = output_dir / f"{chapter_path.stem}.wav"
        if output_path.exists() and not args.overwrite:
            print(f"[SKIP] Đã tồn tại: {output_path}")
            sync_audio_to_db(chapter_path, output_path)
            continue
        try:
            synthesize_chapter(tts, chapter_path, output_path, args)
            sync_audio_to_db(chapter_path, output_path)
        except Exception as exc:
            print(f"[ERROR] {chapter_path.name}: {exc}")

    close = getattr(tts, "close", None)
    if callable(close):
        close()
    print(f"\nHoàn tất. Audio nằm trong: {output_dir}")


if __name__ == "__main__":
    main()
