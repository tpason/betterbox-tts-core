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
from scripts.story_pipeline.story_text_markup import normalize_story_markup, pack_for_viterbox_tts  # noqa: E402
from scripts.story_pipeline.viterbox_audiobook_stitch import synthesize_viterbox_audiobook  # noqa: E402


def read_text_arg(args: argparse.Namespace) -> str:
    if args.text_file:
        text = Path(args.text_file).read_text(encoding="utf-8")
        return text[: args.max_chars] if args.max_chars > 0 else text
    if args.text:
        return args.text[: args.max_chars] if args.max_chars > 0 else args.text
    stdin_text = sys.stdin.read()
    if stdin_text.strip():
        return stdin_text[: args.max_chars] if args.max_chars > 0 else stdin_text
    raise SystemExit("Cần truyền --text, --text-file, hoặc pipe nội dung qua stdin.")


def detect_device(forced_device: str | None = None) -> str:
    if forced_device:
        return forced_device
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def split_text_blocks(text: str, max_chars: int, mode: str = "packed") -> list[str]:
    """Split story text for Viterbox audiobook generation.

    `packed` is the default for Viterbox: keep several sentences in one model
    call and only split near max_chars, minimizing join artifacts.
    `dia` mirrors Dia's sentence/chunk strategy and is kept for comparison.
    """
    text = re.sub(r"\s+", " ", text.strip())
    sentences = re.split(r"(?<=[.!?。！？…])\s+", text)
    if mode == "packed":
        blocks: list[str] = []
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
        return blocks

    blocks: list[str] = []

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


def synthesize_audiobook_preview(
    model: Viterbox,
    text: str,
    args: argparse.Namespace,
    reference_audio: Path,
) -> np.ndarray:
    print("\n[Audiobook mode] Direct block generation, bypassing Viterbox.generate sentence stitching.")
    model.prepare_conditionals(str(reference_audio.resolve()), args.exaggeration)
    if model.conds is not None and hasattr(model.conds.t3, "emotion_adv"):
        model.conds.t3.emotion_adv = args.exaggeration * torch.ones(1, 1, 1).to(model.device)

    blocks = split_text_blocks(text, args.max_chars_per_block, mode=args.split_mode)
    print(f"[Audiobook mode] {len(text)} chars -> {len(blocks)} direct TTS blocks")

    audio_blocks: list[np.ndarray] = []
    for idx, block in enumerate(blocks, start=1):
        print(f"\n[{idx}/{len(blocks)}] Direct TTS block {len(block)} chars")
        spoken = normalize_for_direct_tts(block, args.direct_text_normalizer)
        audio_np = model._generate_single(
            text=spoken,
            language=args.language,
            cfg_weight=args.cfg_weight,
            temperature=args.temperature,
            top_p=args.top_p,
            repetition_penalty=args.repetition_penalty,
            speed=args.speed,
            pitch_shift=args.pitch_shift,
        )
        audio_np = model._smooth_generated_piece(
            audio_np,
            fade_in_ms=args.edge_fade_in_ms,
            fade_out_ms=args.edge_fade_out_ms,
        )
        if len(audio_np) > 0:
            audio_blocks.append(audio_np.astype(np.float32, copy=False))

    if not audio_blocks:
        return np.zeros(0, dtype=np.float32)

    pause = np.zeros(int(model.sr * args.block_silence_ms / 1000), dtype=np.float32)
    merged: list[np.ndarray] = []
    group: list[np.ndarray] = []
    for idx, audio_np in enumerate(audio_blocks):
        group.append(np.concatenate([audio_np, pause]))
        if len(group) == 2 or idx == len(audio_blocks) - 1:
            merged.append(np.concatenate(group))
            group = []
    return np.concatenate(merged)


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview một đoạn text bằng BetterBox Viterbox.")
    parser.add_argument("--text", default="")
    parser.add_argument("--text-file", default="")
    parser.add_argument("--output", default="story_audio/preview.wav")
    parser.add_argument("--reference-audio", default="wavs/dolly_wise_lady.wav")
    parser.add_argument("--max-chars", type=int, default=0, help="0 nghĩa là preview toàn bộ text.")
    parser.add_argument("--device", default=None)
    parser.add_argument("--language", default="vi")
    parser.add_argument("--no-normalize-markup", action="store_true")
    parser.add_argument("--no-tts-markup", action="store_true")
    parser.add_argument("--tts-max-clause-chars", type=int, default=160)
    parser.add_argument("--tts-comma-every-chars", type=int, default=70)
    parser.add_argument("--legacy-viterbox-generate", action="store_true")
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

    text = read_text_arg(args)
    if not args.no_normalize_markup:
        text = normalize_story_markup(text)
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

    reference_audio = Path(args.reference_audio)
    if not reference_audio.exists():
        raise SystemExit(f"Không tìm thấy reference audio: {reference_audio}")

    device = detect_device(args.device)
    print(f"Loading Viterbox on {device}")
    model = Viterbox.from_pretrained(device)

    if args.legacy_viterbox_generate:
        wav, status, _ = model.generate(
            text=text,
            language=args.language,
            audio_prompt=str(reference_audio.resolve()),
            advance_tts=args.advance_tts,
            skip_processing=True,
            exaggeration=args.exaggeration,
            cfg_weight=args.cfg_weight,
            temperature=args.temperature,
            top_p=args.top_p,
            repetition_penalty=args.repetition_penalty,
            speed=args.speed,
            pitch_shift=args.pitch_shift,
        )
        print(status)
        audio_np = wav[0].cpu().numpy()
    elif args.audiobook_stitch:
        audio_np = synthesize_viterbox_audiobook(
            model,
            text,
            language=args.language,
            reference_audio=str(reference_audio.resolve()),
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
        audio_np = synthesize_audiobook_preview(model, text, args, reference_audio)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(output_path, audio_np, model.sr)
    print(f"Đã lưu preview: {output_path} ({len(audio_np) / model.sr:.2f}s)")


if __name__ == "__main__":
    main()
