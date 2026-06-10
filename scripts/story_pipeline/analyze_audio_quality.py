#!/usr/bin/env python3
"""
Signal-level audio quality analyzer for generated TTS chapters.

Checks:
- Duration vs word-count estimate (RTF proxy, EOS-miss proxy)
- RMS level and dynamic range
- Clipping ratio
- Silence ratio (leading/trailing/internal)
- Spectral centroid (EQ health)
- Per-chapter summary table

Usage:
    python scripts/story_pipeline/analyze_audio_quality.py \
        --audio-dir story_audio/a-regressors-tale-of-cultivation \
        [--polished-dir story_data/polished/a-regressors-tale-of-cultivation]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ── Vietnamese TTS cadence constants (calibrated for Viterbox) ──────────────
# Average: ~3.5 chars/word in Vietnamese, ~0.30s/word at normal speed
CHARS_PER_WORD = 3.8
SECS_PER_WORD  = 0.32   # generous — slow narrative delivery
SECS_PER_WORD_MIN = 0.18  # unusually fast — may have been cut short

SILENCE_THRESHOLD = 0.003   # amplitude below = silence
CLIP_THRESHOLD    = 0.98    # amplitude above = near-clipping
SILENCE_WARN_RATIO = 0.25   # warn if >25% of audio is silence
RMS_LOW_WARN  = 0.03        # warn if RMS below this (too quiet)
RMS_HIGH_WARN = 0.35        # warn if RMS above this (too loud / possibly clipped)


def load_wav(path: Path) -> tuple[np.ndarray, int]:
    audio, sr = sf.read(str(path), dtype="float32", always_2d=False)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    return audio, sr


def estimate_word_count(text_path: Path | None, audio_chars: int = 0) -> int:
    if text_path and text_path.exists():
        text = text_path.read_text(encoding="utf-8")
        # rough tokenize: split on spaces + punctuation clusters
        import re
        words = re.findall(r"\w+", text, flags=re.UNICODE)
        return len(words)
    # fallback: estimate from char count
    return max(1, audio_chars // int(CHARS_PER_WORD))


def analyze_wav(audio: np.ndarray, sr: int) -> dict:
    duration = len(audio) / sr

    # RMS
    rms = float(np.sqrt(np.mean(audio ** 2)))

    # Peak
    peak = float(np.max(np.abs(audio)))

    # Dynamic range (dB)
    if rms > 0 and peak > 0:
        dr_db = 20 * np.log10(peak / max(rms, 1e-9))
    else:
        dr_db = 0.0

    # Clipping ratio
    clip_ratio = float(np.mean(np.abs(audio) > CLIP_THRESHOLD))

    # Silence analysis using frame-level RMS (30ms frames)
    frame_ms = 30
    frame_size = max(1, int(sr * frame_ms / 1000))
    usable = (len(audio) // frame_size) * frame_size
    if usable > 0:
        frames = audio[:usable].reshape(-1, frame_size)
        frame_rms = np.sqrt(np.mean(frames ** 2, axis=1))
        silent_frames = np.sum(frame_rms < SILENCE_THRESHOLD)
        silence_ratio = float(silent_frames / len(frame_rms))

        # Leading/trailing silence
        voiced = np.flatnonzero(frame_rms >= SILENCE_THRESHOLD)
        leading_silence_ms = (int(voiced[0]) * frame_ms) if len(voiced) > 0 else duration * 1000
        trailing_silence_ms = ((len(frame_rms) - 1 - int(voiced[-1])) * frame_ms) if len(voiced) > 0 else 0
    else:
        silence_ratio = 0.0
        leading_silence_ms = 0.0
        trailing_silence_ms = 0.0

    # Spectral centroid (crude — measures brightness, EQ proxy)
    fft = np.abs(np.fft.rfft(audio[:min(len(audio), sr * 5)]))  # first 5s
    freqs = np.fft.rfftfreq(min(len(audio), sr * 5), d=1.0 / sr)
    spectral_centroid = float(np.sum(freqs * fft) / np.sum(fft)) if np.sum(fft) > 0 else 0.0

    return {
        "duration_s": duration,
        "rms": rms,
        "peak": peak,
        "dr_db": dr_db,
        "clip_ratio": clip_ratio,
        "silence_ratio": silence_ratio,
        "leading_silence_ms": leading_silence_ms,
        "trailing_silence_ms": trailing_silence_ms,
        "spectral_centroid_hz": spectral_centroid,
        "sr": sr,
    }


def grade_chapter(stats: dict, word_count: int) -> list[str]:
    issues: list[str] = []
    d = stats["duration_s"]

    # Duration sanity
    expected_min = word_count * SECS_PER_WORD_MIN
    expected_max = word_count * SECS_PER_WORD * 2.2
    expected_nominal = word_count * SECS_PER_WORD
    if d < expected_min:
        issues.append(f"TOO_SHORT: {d:.0f}s vs expected≥{expected_min:.0f}s ({word_count}w) — EOS miss / truncated?")
    elif d > expected_max:
        issues.append(f"TOO_LONG: {d:.0f}s vs expected≤{expected_max:.0f}s ({word_count}w) — repeat artifact?")

    # RMS level
    if stats["rms"] < RMS_LOW_WARN:
        issues.append(f"LOW_VOLUME: RMS={stats['rms']:.4f} (too quiet, may need boost)")
    elif stats["rms"] > RMS_HIGH_WARN:
        issues.append(f"HIGH_VOLUME: RMS={stats['rms']:.4f} (risk of distortion)")

    # Clipping
    if stats["clip_ratio"] > 0.001:
        issues.append(f"CLIPPING: {stats['clip_ratio']*100:.2f}% samples near clip ({stats['peak']:.3f} peak)")

    # Silence ratio
    silence_warn = 0.30
    if stats["silence_ratio"] > silence_warn:
        issues.append(f"EXCESS_SILENCE: {stats['silence_ratio']*100:.1f}% of frames silent (>{silence_warn*100:.0f}%)")

    # Leading/trailing silence
    if stats["leading_silence_ms"] > 300:
        issues.append(f"LEADING_SILENCE: {stats['leading_silence_ms']:.0f}ms — trim gap at start")
    if stats["trailing_silence_ms"] > 500:
        issues.append(f"TRAILING_SILENCE: {stats['trailing_silence_ms']:.0f}ms — trim gap at end")

    # Spectral centroid (for Vietnamese TTS: typically 1500-4000 Hz)
    sc = stats["spectral_centroid_hz"]
    if sc < 800:
        issues.append(f"DARK_SPECTRUM: centroid={sc:.0f}Hz — muffled / bass-heavy EQ?")
    elif sc > 5500:
        issues.append(f"BRIGHT_SPECTRUM: centroid={sc:.0f}Hz — tinny / treble-heavy EQ?")

    return issues


def format_row(name: str, stats: dict, word_count: int, issues: list[str]) -> str:
    d = stats["duration_s"]
    mins = int(d // 60)
    secs = d % 60
    speed = d / max(word_count, 1)
    issue_tag = " ⚠" if issues else " ✓"
    return (
        f"  {name:<20} {mins:2d}m{secs:04.1f}s  "
        f"RMS={stats['rms']:.3f}  peak={stats['peak']:.3f}  "
        f"silence={stats['silence_ratio']*100:4.1f}%  "
        f"centroid={stats['spectral_centroid_hz']:5.0f}Hz  "
        f"{speed:.2f}s/w  {word_count:5d}w{issue_tag}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio-dir", required=True)
    parser.add_argument("--polished-dir", default=None)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    audio_dir = Path(args.audio_dir)
    polished_dir = Path(args.polished_dir) if args.polished_dir else None

    wav_files = sorted(audio_dir.glob("*.wav"))
    if not wav_files:
        print(f"No WAV files in {audio_dir}")
        return

    if args.limit:
        wav_files = wav_files[: args.limit]

    print(f"\n=== Audio Quality Report: {audio_dir.name} ({len(wav_files)} files) ===\n")
    print(
        f"  {'Chapter':<20} {'Duration':<12} {'RMS':<10} {'Peak':<10} "
        f"{'Silence':>8}  {'SpectralC':>10}  {'s/word':>7}  {'Words':>6}"
    )
    print("  " + "-" * 100)

    all_issues: dict[str, list[str]] = {}
    total_duration = 0.0
    total_words = 0

    for wav_path in wav_files:
        try:
            audio, sr = load_wav(wav_path)
        except Exception as exc:
            print(f"  {wav_path.name:<20} ERROR: {exc}")
            continue

        # Find matching polished text for word count
        stem = wav_path.stem  # e.g. chapter0001
        txt_path = None
        if polished_dir:
            txt_path = polished_dir / f"{stem}.txt"

        word_count = estimate_word_count(txt_path)
        stats = analyze_wav(audio, sr)
        issues = grade_chapter(stats, word_count)

        print(format_row(wav_path.name, stats, word_count, issues))
        if issues:
            for issue in issues:
                print(f"    ↳ {issue}")

        all_issues[wav_path.name] = issues
        total_duration += stats["duration_s"]
        total_words += word_count

    print("  " + "-" * 100)
    total_mins = int(total_duration // 60)
    total_secs = total_duration % 60
    flagged = sum(1 for v in all_issues.values() if v)
    print(f"\nTotal: {total_mins}m{total_secs:.0f}s audio | {total_words} words | {flagged}/{len(wav_files)} chapters flagged\n")

    if flagged == 0:
        print("  All chapters passed signal-level checks. Listen to flagged prosody / pronunciation manually.")
    else:
        print("  ⚠ Flagged chapters need review (listen or re-generate).")


if __name__ == "__main__":
    main()
