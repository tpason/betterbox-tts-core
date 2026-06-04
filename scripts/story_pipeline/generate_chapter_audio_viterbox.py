#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf
import torch


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_TOKEN", "dummy")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("GRADIO_TEMP_DIR", str(Path(tempfile.gettempdir()) / "betterbox_story_tmp"))
Path(os.environ["GRADIO_TEMP_DIR"]).mkdir(parents=True, exist_ok=True)

from viterbox import Viterbox  # noqa: E402
from viterbox.tts_helper.tts_extension import punc_norm  # noqa: E402
from viterbox.tts_helper.tts_precision import config_token_for_precision  # noqa: E402
from scripts.story_pipeline.story_text_markup import pack_for_viterbox_tts  # noqa: E402
from scripts.story_pipeline.viterbox_audiobook_stitch import synthesize_viterbox_audiobook  # noqa: E402


CHAPTER_PATTERN = re.compile(r"chapter(\d+)\.txt$", re.IGNORECASE)


def detect_device(forced_device: str | None = None) -> str:
    if forced_device:
        return forced_device
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def setup_cuda(device: str) -> None:
    if device != "cuda":
        return
    try:
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True
        torch.set_float32_matmul_precision("high")
    except Exception:
        pass


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


def split_text_blocks(text: str, max_chars: int, mode: str = "packed") -> list[str]:
    """Split story text for Viterbox audiobook generation.

    `packed` minimizes joins by packing sentences up to max_chars.
    `dia` mirrors Dia-style sentence/comma/word chunking for comparison.
    """
    text = re.sub(r"\n{3,}", "\n\n", text.strip())
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    blocks: list[str] = []

    sentence_pattern = re.compile(r"(?<=[.!?。！？…])\s+")
    for paragraph in paragraphs:
        sentences = sentence_pattern.split(paragraph)
        if mode == "packed":
            current = ""
            for sentence in sentences:
                sentence = sentence.strip()
                if not sentence:
                    continue
                candidate = f"{current} {sentence}".strip() if current else sentence
                if len(candidate) <= max_chars:
                    current = candidate
                    continue
                if current:
                    blocks.append(current)
                if len(sentence) <= max_chars:
                    current = sentence
                    continue
                words = sentence.split()
                current_words: list[str] = []
                current_len = 0
                for word in words:
                    word_len = len(word) + 1
                    if current_words and current_len + word_len > max_chars:
                        blocks.append(" ".join(current_words))
                        current_words = [word]
                        current_len = word_len
                    else:
                        current_words.append(word)
                        current_len += word_len
                current = " ".join(current_words)
            if current:
                blocks.append(current)
            continue

        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue

            if len(sentence) <= max_chars:
                blocks.append(sentence)
                continue

            sub_sentences = re.split(r"(?<=[,;，；:：])\s+", sentence)
            for sub_sentence in sub_sentences:
                sub_sentence = sub_sentence.strip()
                if not sub_sentence:
                    continue
                if len(sub_sentence) <= max_chars:
                    blocks.append(sub_sentence)
                    continue

                words = sub_sentence.split()
                current_words: list[str] = []
                current_len = 0
                for word in words:
                    word_len = len(word) + 1
                    if current_words and current_len + word_len > max_chars:
                        blocks.append(" ".join(current_words))
                        current_words = [word]
                        current_len = word_len
                    else:
                        current_words.append(word)
                        current_len += word_len
                if current_words:
                    blocks.append(" ".join(current_words))

    return blocks


def normalize_for_direct_tts(text: str, mode: str) -> str:
    if mode == "precision":
        return config_token_for_precision(text.casefold())
    if mode == "punc_norm":
        return punc_norm(text, True)
    if mode == "raw":
        return text
    raise ValueError(f"Unsupported direct normalizer: {mode}")


def synthesize_text_blocks(
    model: Viterbox,
    blocks: list[str],
    reference_audio: str,
    language: str,
    advance_tts: bool,
    exaggeration: float,
    cfg_weight: float,
    temperature: float,
    top_p: float,
    repetition_penalty: float,
    speed: float,
    pitch_shift: float,
    silence_ms: int,
    direct_audiobook: bool,
    edge_fade_in_ms: int,
    edge_fade_out_ms: int,
    direct_text_normalizer: str,
) -> np.ndarray:
    audio_blocks: list[np.ndarray] = []
    silence = np.zeros(int(model.sr * silence_ms / 1000), dtype=np.float32)

    if direct_audiobook:
        print("\n[Audiobook mode] Direct block generation, bypassing Viterbox.generate sentence stitching.")
        model.prepare_conditionals(reference_audio, exaggeration)
        if model.conds is not None and hasattr(model.conds.t3, "emotion_adv"):
            model.conds.t3.emotion_adv = exaggeration * torch.ones(1, 1, 1).to(model.device)

    for idx, block in enumerate(blocks, start=1):
        print(f"\n[{idx}/{len(blocks)}] TTS block {len(block)} chars")
        if direct_audiobook:
            spoken = normalize_for_direct_tts(block, direct_text_normalizer)
            audio_np = model._generate_single(
                text=spoken,
                language=language,
                cfg_weight=cfg_weight,
                temperature=temperature,
                top_p=top_p,
                repetition_penalty=repetition_penalty,
                speed=speed,
                pitch_shift=pitch_shift,
            )
            audio_np = model._smooth_generated_piece(
                audio_np,
                fade_in_ms=edge_fade_in_ms,
                fade_out_ms=edge_fade_out_ms,
            )
            audio_np = audio_np.astype(np.float32, copy=False)
        else:
            wav, status, _ = model.generate(
                text=block,
                language=language,
                audio_prompt=reference_audio,
                advance_tts=advance_tts,
                skip_processing=True,
                exaggeration=exaggeration,
                cfg_weight=cfg_weight,
                temperature=temperature,
                top_p=top_p,
                repetition_penalty=repetition_penalty,
                speed=speed,
                pitch_shift=pitch_shift,
            )
            print(status)
            audio_np = wav[0].cpu().numpy().astype(np.float32, copy=False)
        if len(audio_np) > 0:
            audio_blocks.append(audio_np)

    if not audio_blocks:
        return np.zeros(0, dtype=np.float32)

    merged: list[np.ndarray] = []
    group: list[np.ndarray] = []
    for idx, audio_np in enumerate(audio_blocks):
        group.append(np.concatenate([audio_np, silence]))
        if len(group) == 2 or idx == len(audio_blocks) - 1:
            merged.append(np.concatenate(group))
            group = []
    return np.concatenate(merged)


def synthesize_chapter(
    model: Viterbox,
    chapter_path: Path,
    output_path: Path,
    args: argparse.Namespace,
) -> None:
    text = chapter_path.read_text(encoding="utf-8").strip()
    if not text:
        print(f"[SKIP] File rỗng: {chapter_path}")
        return
    if not args.no_tts_markup:
        text = pack_for_viterbox_tts(
            text,
            max_clause_chars=args.tts_max_clause_chars,
            comma_every_chars=args.tts_comma_every_chars,
        )
        print(
            f"[TTS markup] chars={len(text)} max_clause_chars={args.tts_max_clause_chars} "
            f"comma_every_chars={args.tts_comma_every_chars}"
        )

    if args.audiobook_stitch:
        print(f"\n=== {chapter_path.name}: {len(text)} chars -> Viterbox audiobook stitch ===")
        audio_np = synthesize_viterbox_audiobook(
            model,
            text,
            language=args.language,
            reference_audio=args.reference_audio,
            exaggeration=args.exaggeration,
            cfg_weight=args.cfg_weight,
            temperature=args.temperature,
            top_p=args.top_p,
            repetition_penalty=args.repetition_penalty,
            speed=args.speed,
            pitch_shift=args.pitch_shift,
            max_chars_per_unit=args.max_chars_per_unit,
            min_chars_per_unit=args.min_chars_per_unit,
            sentence_pause_ms=args.sentence_pause_ms,
            crossfade_ms=args.crossfade_ms,
            trim_threshold=args.trim_threshold,
            trim_margin_ms=args.trim_margin_ms,
            edge_fade_in_ms=args.edge_fade_in_ms,
            edge_fade_out_ms=args.edge_fade_out_ms,
        )
    else:
        blocks = split_text_blocks(text, args.max_chars_per_block, mode=args.split_mode)
        print(f"\n=== {chapter_path.name}: {len(text)} chars -> {len(blocks)} blocks ===")

        audio_np = synthesize_text_blocks(
            model=model,
            blocks=blocks,
            reference_audio=args.reference_audio,
            language=args.language,
            advance_tts=args.advance_tts,
            exaggeration=args.exaggeration,
            cfg_weight=args.cfg_weight,
            temperature=args.temperature,
            top_p=args.top_p,
            repetition_penalty=args.repetition_penalty,
            speed=args.speed,
            pitch_shift=args.pitch_shift,
            silence_ms=args.block_silence_ms,
            direct_audiobook=not args.legacy_viterbox_generate,
            edge_fade_in_ms=args.edge_fade_in_ms,
            edge_fade_out_ms=args.edge_fade_out_ms,
            direct_text_normalizer=args.direct_text_normalizer,
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(output_path, audio_np, model.sr)
    print(f"Đã lưu {output_path} ({len(audio_np) / model.sr:.2f}s)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sinh audio chapter*.txt bằng BetterBox Viterbox.")
    parser.add_argument("--input-dir", required=True, help="Folder chứa chapterX.txt.")
    parser.add_argument("--output-root", default="story_audio")
    parser.add_argument("--reference-audio", default="wavs/dolly_wise_lady.wav")
    parser.add_argument("--chapter", type=int, default=0, help="0 nghĩa là dùng --all hoặc mặc định chapter1.")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--device", default=None)
    parser.add_argument("--language", default="vi")
    parser.add_argument("--audiobook-stitch", action="store_true")
    parser.add_argument("--max-chars-per-unit", type=int, default=None, help="Advanced/debug override. Omit to use auto audiobook split.")
    parser.add_argument("--min-chars-per-unit", type=int, default=None, help="Advanced/debug override. Omit to use auto audiobook split.")
    parser.add_argument("--sentence-pause-ms", type=int, default=500)
    parser.add_argument("--crossfade-ms", type=int, default=50)
    parser.add_argument("--trim-threshold", type=float, default=0.006)
    parser.add_argument("--trim-margin-ms", type=int, default=80)
    parser.add_argument("--split-mode", choices=["packed", "dia"], default="packed")
    parser.add_argument("--direct-text-normalizer", choices=["precision", "punc_norm", "raw"], default="precision")
    parser.add_argument("--max-chars-per-block", type=int, default=850)
    parser.add_argument("--block-silence-ms", type=int, default=350)
    parser.add_argument("--no-tts-markup", action="store_true")
    parser.add_argument("--tts-max-clause-chars", type=int, default=160)
    parser.add_argument("--tts-comma-every-chars", type=int, default=70)
    parser.add_argument("--legacy-viterbox-generate", action="store_true")
    parser.add_argument("--edge-fade-in-ms", type=int, default=5)
    parser.add_argument("--edge-fade-out-ms", type=int, default=18)
    parser.add_argument("--advance-tts", action="store_true")
    parser.add_argument("--exaggeration", type=float, default=0.5)
    parser.add_argument("--cfg-weight", type=float, default=0.5)
    parser.add_argument("--temperature", type=float, default=0.8)
    parser.add_argument("--top-p", type=float, default=0.9)
    parser.add_argument("--repetition-penalty", type=float, default=1.2)
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--pitch-shift", type=float, default=1.0)
    args = parser.parse_args()

    input_dir = Path(args.input_dir).resolve()
    if not input_dir.is_dir():
        raise SystemExit(f"Không tìm thấy input-dir: {input_dir}")

    reference_audio = Path(args.reference_audio)
    if not reference_audio.exists():
        raise SystemExit(f"Không tìm thấy reference audio: {reference_audio}")
    args.reference_audio = str(reference_audio.resolve())

    if args.all:
        chapter_files = list_chapter_files(input_dir)
    else:
        chapter_num = args.chapter or 1
        chapter_files = [input_dir / f"chapter{chapter_num}.txt"]

    chapter_files = [path for path in chapter_files if path.exists()]
    if not chapter_files:
        raise SystemExit("Không có chapter file để xử lý.")

    device = detect_device(args.device)
    setup_cuda(device)
    print(f"Loading Viterbox on {device}")
    model = Viterbox.from_pretrained(device)

    output_dir = Path(args.output_root) / input_dir.name
    for chapter_path in chapter_files:
        output_path = output_dir / f"{chapter_path.stem}.wav"
        if output_path.exists() and not args.overwrite:
            print(f"[SKIP] Đã tồn tại: {output_path}")
            sync_audio_to_db(chapter_path, output_path)
            continue
        try:
            synthesize_chapter(model, chapter_path, output_path, args)
            sync_audio_to_db(chapter_path, output_path)
        except Exception as exc:
            print(f"[ERROR] {chapter_path.name}: {exc}")

    print(f"\nHoàn tất. Audio nằm trong: {output_dir}")


if __name__ == "__main__":
    main()
