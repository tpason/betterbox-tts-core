#!/usr/bin/env python3
"""
Regression tests for audio_quality.py.

Tests each quality check with synthetic audio so we can verify detection
works AND doesn't false-positive on good audio. Run this after any change
to audio_quality.py.

Usage:
    python scripts/story_pipeline/test_audio_quality.py
    python scripts/story_pipeline/test_audio_quality.py -v   # verbose
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path
from typing import Callable

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.story_pipeline.audio_quality import (
    REGEN_TRIGGERS,
    analyze_wav,
    check_wav,
    count_words,
    detect_repetition,
    grade,
    load_wav,
)
from scripts.story_pipeline.viterbox_audiobook_stitch import (
    expected_unit_duration_seconds,
    should_retry_short_unit,
    trim_excess_duration_for_unit,
)
from scripts.story_pipeline.story_text_markup import (
    normalize_numbers_for_tts,
    normalize_story_markup,
    _number_to_vietnamese,
)

SR = 24000  # match Viterbox output sample rate
VERBOSE = "-v" in sys.argv


# ── Synthetic audio helpers ───────────────────────────────────────────────────

def _speech_noise(duration_s: float, rms: float = 0.12) -> np.ndarray:
    """Bandpass noise 300-3400 Hz — mimics speech spectral shape."""
    n = int(duration_s * SR)
    white = np.random.default_rng(42).standard_normal(n).astype(np.float32)
    # Simple 1-pole bandpass approximation: diff + low-pass
    audio = np.diff(white, prepend=white[:1])
    audio = np.convolve(audio, np.ones(20) / 20, mode="same")
    cur_rms = float(np.sqrt(np.mean(audio ** 2)))
    if cur_rms > 0:
        audio *= rms / cur_rms
    return audio


def _make_repetition(segment_s: float = 30.0, repeats: int = 6) -> tuple[np.ndarray, int]:
    """Repeat same speech segment N times — simulates TTS loop artifact."""
    seg = _speech_noise(segment_s)
    # Add a brief silence between repetitions (like sentence pauses)
    silence = np.zeros(int(0.5 * SR), dtype=np.float32)
    pieces = [seg, silence] * repeats
    return np.concatenate(pieces), SR


def _make_good(duration_s: float = 180.0, rms_target: float = 0.12) -> tuple[np.ndarray, int]:
    """Non-periodic speech-like audio that should pass all checks.

    Uses random segment lengths and random per-segment RMS to avoid creating
    a periodic envelope pattern that would trigger REPETITION false-positives.
    """
    rng = np.random.default_rng(17)
    n = int(duration_s * SR)
    pieces: list[np.ndarray] = []
    filled = 0
    while filled < n:
        # Random segment 1-8s, random filter width, random amplitude
        seg_n = min(n - filled, int(rng.uniform(1.0, 8.0) * SR))
        width = int(rng.uniform(8, 60))
        seg = rng.standard_normal(seg_n).astype(np.float32)
        seg = np.convolve(seg, np.ones(width) / width, mode="same")
        seg_rms = float(np.sqrt(np.mean(seg ** 2)))
        if seg_rms > 0:
            seg *= rng.uniform(0.04, 0.20) / seg_rms
        pieces.append(seg)
        filled += seg_n
        # Random silence 0-0.8s between segments
        sil_n = min(n - filled, int(rng.uniform(0, 0.8) * SR))
        if sil_n > 0:
            pieces.append(np.zeros(sil_n, dtype=np.float32))
            filled += sil_n
    audio = np.concatenate(pieces)[:n]
    cur = float(np.sqrt(np.mean(audio ** 2)))
    if cur > 0:
        audio *= rms_target / cur
    return audio, SR


def _text_for_duration(duration_s: float, pace_secs_per_word: float = 0.26) -> str:
    """Return dummy text with word count matching expected duration."""
    n_words = max(1, int(duration_s / pace_secs_per_word))
    return " ".join(["từ"] * n_words)


# ── Test runner ───────────────────────────────────────────────────────────────

_passed = 0
_failed = 0


def test(name: str, fn: Callable[[], None]) -> None:
    global _passed, _failed
    try:
        fn()
        print(f"  PASS  {name}")
        _passed += 1
    except AssertionError as e:
        print(f"  FAIL  {name}: {e}")
        if VERBOSE:
            traceback.print_exc()
        _failed += 1
    except Exception as e:
        print(f"  ERROR {name}: {type(e).__name__}: {e}")
        if VERBOSE:
            traceback.print_exc()
        _failed += 1


def _assert_eq(actual: str, expected: str) -> None:
    assert actual == expected, f"Expected {expected!r}, got {actual!r}"


def _assert_in(text: str, substr: str) -> None:
    assert substr in text, f"{substr!r} not found in {text!r}"


def _assert_not_in(text: str, substr: str) -> None:
    assert substr not in text, f"{substr!r} unexpectedly found in {text!r}"


def assert_flags(issues: list[str], expected_prefix: str, present: bool = True) -> None:
    found = any(i.startswith(expected_prefix) for i in issues)
    if present and not found:
        raise AssertionError(f"{expected_prefix!r} not in issues: {issues}")
    if not present and found:
        raise AssertionError(f"{expected_prefix!r} unexpectedly found in issues: {issues}")


# ── count_words tests ─────────────────────────────────────────────────────────

def _test_count_words_empty():
    assert count_words("") == 0
    assert count_words("   ") == 0
    assert count_words("\n\t") == 0


def _test_count_words_normal():
    assert count_words("xin chào thế giới") == 4
    assert count_words("Enkrid nói: 'Được rồi.'") >= 3


def _test_count_words_no_false_one():
    # Old bug: max(1, ...) returned 1 for empty → caused TOO_LONG on empty text
    # Now must return 0
    assert count_words("") == 0, "empty text must return 0, not 1"


# ── Duration checks ───────────────────────────────────────────────────────────

def _test_too_short():
    # 5s audio, 500 words → min expected = 500 * 0.18 = 90s → TOO_SHORT
    audio, sr = _speech_noise(5.0), SR
    stats = analyze_wav(audio, sr)
    issues = grade(stats, word_count=500)
    assert_flags(issues, "TOO_SHORT")
    assert "TOO_SHORT" in REGEN_TRIGGERS


def _test_too_long():
    # 1000s audio, 50 words → max expected = 50 * 0.32 * 2.2 = 35.2s → TOO_LONG
    audio = _speech_noise(1000.0, rms=0.12)
    stats = analyze_wav(audio, SR)
    issues = grade(stats, word_count=50)
    assert_flags(issues, "TOO_LONG")
    assert "TOO_LONG" in REGEN_TRIGGERS


def _test_duration_ok():
    # 120s audio, 400 words → 0.30 s/word → within [0.18, 0.704]
    audio = _speech_noise(120.0)
    stats = analyze_wav(audio, SR)
    text = _text_for_duration(120.0)
    issues = grade(stats, word_count=count_words(text))
    assert_flags(issues, "TOO_SHORT", present=False)
    assert_flags(issues, "TOO_LONG", present=False)


def _test_wc_zero_skips_duration():
    # word_count=0 must not trigger TOO_SHORT or TOO_LONG
    audio = _speech_noise(3.0)  # very short — would be TOO_SHORT if wc>0
    stats = analyze_wav(audio, SR)
    issues = grade(stats, word_count=0)
    assert_flags(issues, "TOO_SHORT", present=False)
    assert_flags(issues, "TOO_LONG", present=False)


# ── CLIPPING ──────────────────────────────────────────────────────────────────

def _test_clipping_detected():
    audio = np.ones(SR * 10, dtype=np.float32)  # 100% clipped
    stats = analyze_wav(audio, SR)
    issues = grade(stats, word_count=0)
    assert_flags(issues, "CLIPPING")
    assert "CLIPPING" in REGEN_TRIGGERS


def _test_no_clipping_on_good():
    audio, sr = _make_good(60.0)
    stats = analyze_wav(audio, sr)
    issues = grade(stats, word_count=0)
    assert_flags(issues, "CLIPPING", present=False)


# ── REPETITION ────────────────────────────────────────────────────────────────

def _test_repetition_detected():
    audio, sr = _make_repetition(segment_s=30.0, repeats=6)
    result = detect_repetition(audio, sr)
    assert result is not None, (
        "detect_repetition should find loop in repeated segment audio, got None"
    )
    assert result.startswith("REPETITION"), f"Expected REPETITION prefix, got: {result}"


def _test_no_repetition_on_good():
    audio, sr = _make_good(180.0)
    result = detect_repetition(audio, sr)
    assert result is None, (
        f"detect_repetition false-positive on good audio: {result}"
    )


def _test_repetition_skipped_short_audio():
    # Audio shorter than 2 * _REP_MIN_LAG_S (40s) should not be checked
    audio = _speech_noise(30.0)
    result = detect_repetition(audio, SR)
    assert result is None  # too short to run check


def _test_repetition_in_grade():
    # grade() must call detect_repetition when audio is passed
    audio, sr = _make_repetition(30.0, 6)
    stats = analyze_wav(audio, sr)
    issues_with = grade(stats, word_count=0, audio=audio, sr=sr)
    issues_without = grade(stats, word_count=0)  # no audio → no repetition check
    assert_flags(issues_with, "REPETITION")
    assert_flags(issues_without, "REPETITION", present=False)


# ── Silence checks ────────────────────────────────────────────────────────────

def _test_excess_silence():
    # 80% silence, 20% noise
    n = SR * 60
    audio = np.zeros(n, dtype=np.float32)
    audio[:n // 5] = _speech_noise(12.0)
    stats = analyze_wav(audio, SR)
    issues = grade(stats, word_count=0)
    assert_flags(issues, "EXCESS_SILENCE")


def _test_low_volume():
    audio = np.ones(SR * 10, dtype=np.float32) * 0.001  # RMS ≈ 0.001
    stats = analyze_wav(audio, SR)
    issues = grade(stats, word_count=0)
    assert_flags(issues, "LOW_VOLUME")


# ── check_wav integration ─────────────────────────────────────────────────────

def _test_check_wav_good(chapter1_wav: Path, chapter1_txt: Path):
    if not chapter1_wav.exists():
        print("    (skipped — chapter1 WAV not present)")
        return
    text = chapter1_txt.read_text() if chapter1_txt.exists() else ""
    all_issues, bad = check_wav(chapter1_wav, text)
    assert not bad, f"Chapter 1 (known good) flagged for regen: {bad}"


def _test_check_wav_empty_text_no_duration_flag(chapter1_wav: Path):
    if not chapter1_wav.exists():
        print("    (skipped — chapter1 WAV not present)")
        return
    # With empty text, duration checks must be skipped
    all_issues, bad = check_wav(chapter1_wav, "")
    duration_flags = [i for i in bad if i.startswith(("TOO_SHORT", "TOO_LONG"))]
    assert not duration_flags, f"Empty text still triggered duration flags: {duration_flags}"


# ── Stitch: comma-aware duration ─────────────────────────────────────────────

def _test_comma_bonus_raises_expected():
    # 14 words, 0 commas vs 2 commas — comma bonus must increase expected duration
    base = expected_unit_duration_seconds(14, 0)
    with_commas = expected_unit_duration_seconds(14, 2)
    assert with_commas > base, f"comma bonus not applied: {base:.2f} vs {with_commas:.2f}"
    assert abs(with_commas - base - 0.90) < 0.01, f"expected 0.90s bonus for 2 commas, got {with_commas - base:.2f}"


def _test_comma_sentence_no_retry():
    # 14 words, 2 commas → TTS ~5.5s must NOT trigger retry
    wc, nc = 14, 2
    audio_5s = np.zeros(int(5.5 * SR), dtype=np.float32)
    assert not should_retry_short_unit(audio_5s, SR, wc, nc), (
        "14-word 2-comma sentence at 5.5s must not trigger retry"
    )


def _test_slow_speech_no_false_retry():
    # 9-word sentence spoken 30% slower than expected (3.74s vs 2.88s expected)
    # was false-positive with old +0.55 threshold — must NOT retry now
    wc, nc = 9, 0
    expected = expected_unit_duration_seconds(wc, nc)  # 2.88s
    audio_slow = np.zeros(int((expected * 1.3) * SR), dtype=np.float32)
    assert not should_retry_short_unit(audio_slow, SR, wc, nc), (
        f"9-word sentence at 30% over expected must not trigger retry "
        f"(was false-positive with old threshold)"
    )


def _test_slow_speech_14w_no_false_retry():
    # 14-word sentence at 1.6× expected (6.77s) must NOT retry with new max(2× expected, expected+2.0)
    # Old formula (+2.0 absolute) had threshold 6.23s → 6.77s INCORRECTLY triggered retry
    # New formula: max(2×4.23=8.46, 4.23+2.0=6.23) = 8.46s → 6.77s safely below threshold
    wc, nc = 14, 0
    expected = expected_unit_duration_seconds(wc, nc)  # 4.23s
    audio_slow = np.zeros(int(expected * 1.6 * SR), dtype=np.float32)
    assert not should_retry_short_unit(audio_slow, SR, wc, nc), (
        f"14-word sentence at 1.6× expected ({expected * 1.6:.2f}s) must not trigger retry "
        f"(threshold should be max(2×, +2.0) = {max(expected*2.0, expected+2.0):.2f}s)"
    )


def _test_genuine_overflow_triggers_retry():
    # Genuinely anomalous unit (3× expected) must still retry — real EOS miss
    wc, nc = 9, 0
    expected = expected_unit_duration_seconds(wc, nc)  # 2.88s
    audio_overflow = np.zeros(int(expected * 3.5 * SR), dtype=np.float32)
    assert should_retry_short_unit(audio_overflow, SR, wc, nc), (
        "3.5× expected duration must still trigger retry (genuine EOS overflow)"
    )


def _test_trim_preserves_slow_speech():
    # 9-word sentence at 1.8× expected: trim cap (2.0×) must NOT cut it.
    # Old cap was 1.5×, which would incorrectly trim natural slow delivery at 1.8×.
    wc, nc = 9, 0
    expected = expected_unit_duration_seconds(wc, nc)  # 2.88s
    audio_slow = np.zeros(int(expected * 1.8 * SR), dtype=np.float32)
    trimmed, cut_ms = trim_excess_duration_for_unit(audio_slow, SR, wc, nc)
    assert len(trimmed) == len(audio_slow), (
        f"1.8× expected audio must not be trimmed (cap is 2.0×): {len(trimmed)/SR:.2f}s != {len(audio_slow)/SR:.2f}s"
    )


def _test_trim_catches_real_overflow():
    # Audio at 3× expected must be capped — genuine EOS overflow
    wc, nc = 9, 0
    expected = expected_unit_duration_seconds(wc, nc)  # 2.88s
    audio_overflow = np.zeros(int(expected * 3.0 * SR), dtype=np.float32)
    trimmed, cut_ms = trim_excess_duration_for_unit(audio_overflow, SR, wc, nc)
    assert len(trimmed) / SR < len(audio_overflow) / SR, (
        "3× expected overflow must be trimmed by trim_excess_duration_for_unit"
    )


def _test_trim_preserves_content_with_commas():
    # 14 words, 2 commas, 5.5s audio — trim must leave most audio intact
    wc, nc = 14, 2
    audio_5s = np.zeros(int(5.5 * SR), dtype=np.float32)
    trimmed, cut_ms = trim_excess_duration_for_unit(audio_5s, SR, wc, nc)
    assert len(trimmed) / SR >= 5.0, (
        f"trim cut too much for comma sentence: {len(trimmed)/SR:.2f}s < 5.0s"
    )


# ── REGEN_TRIGGERS completeness ───────────────────────────────────────────────

def _test_regen_triggers_coverage():
    # Every REGEN_TRIGGER must be producible and detected
    producible = {
        "TOO_SHORT": lambda: grade(analyze_wav(_speech_noise(2.0), SR), word_count=1000),
        "TOO_LONG":  lambda: grade(analyze_wav(_speech_noise(500.0), SR), word_count=10),
        "CLIPPING":  lambda: grade(analyze_wav(np.ones(SR * 5, dtype=np.float32), SR), word_count=0),
        "REPETITION": lambda: grade(
            analyze_wav(*_make_repetition(30.0, 6)), word_count=0,
            audio=_make_repetition(30.0, 6)[0], sr=SR
        ),
    }
    for trigger in REGEN_TRIGGERS:
        assert trigger in producible, f"REGEN_TRIGGER {trigger!r} has no test case — add one"
        issues = producible[trigger]()
        found = any(i.startswith(trigger) for i in issues)
        assert found, f"REGEN_TRIGGER {trigger!r} not detected by its dedicated test case: {issues}"


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("\n=== audio_quality.py test suite ===\n")
    ch1_wav = ROOT / "story_audio/a-regressors-tale-of-cultivation/chapter0001.wav"
    ch1_txt = ROOT / "story_data/polished/a-regressors-tale-of-cultivation/chapter0001.txt"

    print("count_words:")
    test("empty string returns 0",    _test_count_words_empty)
    test("normal text word count",    _test_count_words_normal)
    test("no old max(1,...) bug",     _test_count_words_no_false_one)

    print("\nDuration checks:")
    test("TOO_SHORT detected",        _test_too_short)
    test("TOO_LONG detected",         _test_too_long)
    test("good duration passes",      _test_duration_ok)
    test("wc=0 skips duration",       _test_wc_zero_skips_duration)

    print("\nCLIPPING:")
    test("clipping detected",         _test_clipping_detected)
    test("no clipping on good audio", _test_no_clipping_on_good)

    print("\nREPETITION:")
    test("loop detected",             _test_repetition_detected)
    test("no false-positive (good)",  _test_no_repetition_on_good)
    test("short audio skipped",       _test_repetition_skipped_short_audio)
    test("grade() calls detect when audio passed", _test_repetition_in_grade)

    print("\nSilence / volume:")
    test("excess silence detected",   _test_excess_silence)
    test("low volume detected",       _test_low_volume)

    print("\ncheck_wav integration (real chapter 1):")
    test("chapter 1 passes (known good)",
         lambda: _test_check_wav_good(ch1_wav, ch1_txt))
    test("empty text skips duration flags",
         lambda: _test_check_wav_empty_text_no_duration_flag(ch1_wav))

    print("\nNumber expansion (normalize_numbers_for_tts):")
    test("50 → năm mươi",
         lambda: _assert_eq(normalize_numbers_for_tts("50 năm trước"), "năm mươi năm trước"))
    test("30 → ba mươi",
         lambda: _assert_eq(normalize_numbers_for_tts("30 năm sau"), "ba mươi năm sau"))
    test("2024 → hai nghìn không trăm hai mươi bốn",
         lambda: _assert_in(normalize_numbers_for_tts("năm 2024"), "hai nghìn"))
    test("2001 → hai nghìn lẻ một",
         lambda: _assert_eq(_number_to_vietnamese(2001), "hai nghìn lẻ một"))
    test("2024 no 'lẻ' for two-digit remainder",
         lambda: _assert_not_in(_number_to_vietnamese(2024), " lẻ "))

    print("\nStitch: comma-aware duration (trim / retry):")
    test("comma bonus raises expected duration", _test_comma_bonus_raises_expected)
    test("comma sentence 5.5s no retry",           _test_comma_sentence_no_retry)
    test("slow speech (1.3×) no false retry",     _test_slow_speech_no_false_retry)
    test("slow speech 14w (1.6×) no false retry", _test_slow_speech_14w_no_false_retry)
    test("genuine overflow (3.5×) triggers retry",_test_genuine_overflow_triggers_retry)
    test("trim: 1.8× expected not cut (cap=2.0×)", _test_trim_preserves_slow_speech)
    test("trim: 3× overflow is capped",           _test_trim_catches_real_overflow)
    test("trim preserves content with commas",    _test_trim_preserves_content_with_commas)

    print("\nREGEN_TRIGGERS coverage:")
    test("every trigger is detectable", _test_regen_triggers_coverage)

    print(f"\n{'='*40}")
    total = _passed + _failed
    print(f"Results: {_passed}/{total} passed", end="")
    if _failed:
        print(f"  ({_failed} FAILED)")
        sys.exit(1)
    else:
        print("  ✓ all passed")


if __name__ == "__main__":
    main()
