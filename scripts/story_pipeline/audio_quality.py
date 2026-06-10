"""Signal-level audio quality analysis for TTS-generated WAV files.

Shared module — imported by audio_worker_viterbox.py (inline retry)
and audio_quality_gate.py (batch analysis + regen).

Checks (in order of reliability):
  RELIABLE (auto-regen triggers):
    TOO_LONG    — duration > 2.2× expected: EOS miss / repetition loop
    TOO_SHORT   — duration < 0.18s/word: generation truncated early
    CLIPPING    — >0.1% samples near clip: hard distortion artifact
    REPETITION  — same audio segment repeats with gap >20s: TTS loop artifact

  REVIEW-ONLY (not auto-regen — false-positive risk too high):
    LOW_VOLUME / HIGH_VOLUME — RMS out of range
    EXCESS_SILENCE           — >40% silence frames (natural at 420ms pauses)
    LEADING_SILENCE / TRAILING_SILENCE
    DARK_SPECTRUM / BRIGHT_SPECTRUM — spectral centroid out of range

Calibrated on chapter 1 of A Regressor's Tale (2026-06-09):
  4672 words, 1490s → 0.319 s/word, 30.3% silence, 1689Hz centroid, RMS 0.133
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import soundfile as sf

# ── Cadence constants calibrated for Viterbox + Vietnamese ───────────────────
SECS_PER_WORD     = 0.32   # generous narrative pace (slow delivery)
SECS_PER_WORD_MIN = 0.18   # suspiciously fast → likely truncated
DURATION_MAX_MULT = 2.2    # × expected → EOS miss / repeat artifact

# ── Signal thresholds ─────────────────────────────────────────────────────────
SILENCE_THRESHOLD  = 0.003
CLIP_THRESHOLD     = 0.98
CLIP_RATIO_WARN    = 0.001  # >0.1% clipped
SILENCE_WARN_RATIO = 0.40   # 420ms sentence pauses → 30-35% silence at normal pace
RMS_LOW_WARN       = 0.03
RMS_HIGH_WARN      = 0.35
LEADING_MS_WARN    = 300
TRAILING_MS_WARN   = 500
CENTROID_LOW_HZ    = 800
CENTROID_HIGH_HZ   = 5500

# ── Repetition detection ──────────────────────────────────────────────────────
# Uses FFT autocorrelation of RMS envelope — fast (O(n log n)) and speaker-agnostic.
# A periodic TTS loop produces a strong autocorrelation peak at the loop period.
# Calibrated on chapter 1 (good): peak=0.034. Typical loop: >0.40.
_REP_ENVELOPE_MS   = 50     # RMS frame size for envelope
_REP_MIN_LAG_S     = 20.0   # minimum loop period to detect (shorter = likely normal sentence repetition)
_REP_MAX_LAG_S     = 180.0  # maximum loop period (longer → chapter probably just TOO_LONG)
_REP_CORR_THRESH   = 0.35   # autocorrelation peak above this = periodic repetition detected

# Issues that trigger automatic re-generation (others are human-review only)
REGEN_TRIGGERS: frozenset[str] = frozenset({"TOO_LONG", "TOO_SHORT", "CLIPPING", "REPETITION"})


# ── Core audio helpers ────────────────────────────────────────────────────────

def load_wav(path: Path) -> tuple[np.ndarray, int]:
    audio, sr = sf.read(str(path), dtype="float32", always_2d=False)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    return audio, sr


def count_words(text: str) -> int:
    """Return word count. Returns 0 for empty/whitespace text (callers skip duration checks)."""
    if not text or not text.strip():
        return 0
    return len(re.findall(r"\w+", text, flags=re.UNICODE))


# ── Repetition detection ──────────────────────────────────────────────────────

def detect_repetition(audio: np.ndarray, sr: int) -> str | None:
    """Detect periodic TTS loop artifact via RMS envelope autocorrelation.

    A stuck TTS model generates the same token sequence repeatedly → periodic audio.
    The RMS envelope autocorrelation will show a strong peak at the loop period.

    Calibrated: good chapter peaks at ~0.034; genuine loops expected >0.40.
    O(n log n) via FFT — fast even for long chapters.
    """
    duration = len(audio) / sr
    if duration < _REP_MIN_LAG_S * 2:
        return None

    frame = max(1, int(sr * _REP_ENVELOPE_MS / 1000))
    n_frames = len(audio) // frame
    frames_mat = audio[:n_frames * frame].reshape(n_frames, frame)
    envelope = np.sqrt(np.mean(frames_mat ** 2, axis=1)).astype(np.float32)

    envelope -= envelope.mean()
    std = envelope.std()
    if std < 1e-9:
        return None
    envelope /= std

    # FFT-based autocorrelation
    fft = np.fft.rfft(envelope, n=2 * n_frames)
    acorr = np.fft.irfft(fft * fft.conj())[:n_frames]
    acorr /= max(float(acorr[0]), 1e-9)

    min_lag = max(1, int(_REP_MIN_LAG_S * 1000 / _REP_ENVELOPE_MS))
    max_lag = min(int(_REP_MAX_LAG_S * 1000 / _REP_ENVELOPE_MS), n_frames // 2)
    if min_lag >= max_lag:
        return None

    peak_idx = int(np.argmax(acorr[min_lag:max_lag]))
    peak_val = float(acorr[min_lag + peak_idx])
    peak_lag_s = (min_lag + peak_idx) * _REP_ENVELOPE_MS / 1000

    if peak_val >= _REP_CORR_THRESH:
        return f"REPETITION:autocorr={peak_val:.3f}@{peak_lag_s:.0f}s-period"

    return None


# ── Main analysis ─────────────────────────────────────────────────────────────

def analyze_wav(audio: np.ndarray, sr: int) -> dict:
    duration = len(audio) / sr
    rms = float(np.sqrt(np.mean(audio ** 2)))
    peak = float(np.max(np.abs(audio)))
    clip_ratio = float(np.mean(np.abs(audio) > CLIP_THRESHOLD))

    frame = max(1, int(sr * 30 / 1000))
    usable = (len(audio) // frame) * frame
    if usable > 0:
        frames = audio[:usable].reshape(-1, frame)
        frame_rms = np.sqrt(np.mean(frames ** 2, axis=1))
        voiced = np.flatnonzero(frame_rms >= SILENCE_THRESHOLD)
        silence_ratio = float(np.mean(frame_rms < SILENCE_THRESHOLD))
        leading_ms  = int(voiced[0]) * 30 if len(voiced) else int(duration * 1000)
        trailing_ms = (len(frame_rms) - 1 - int(voiced[-1])) * 30 if len(voiced) else 0
    else:
        silence_ratio = leading_ms = trailing_ms = 0

    # Sample centroid at 3 positions (start / middle / end) and average
    sample_len = min(len(audio), sr * 5)
    positions = [0, max(0, len(audio) // 2 - sample_len // 2), max(0, len(audio) - sample_len)]
    centroids = []
    for pos in positions:
        chunk = audio[pos:pos + sample_len]
        fft  = np.abs(np.fft.rfft(chunk))
        freq = np.fft.rfftfreq(len(chunk), d=1.0 / sr)
        if fft.sum() > 0:
            centroids.append(float(np.sum(freq * fft) / np.sum(fft)))
    centroid = float(np.mean(centroids)) if centroids else 0.0

    return {
        "duration_s": duration,
        "rms": rms,
        "peak": peak,
        "clip_ratio": clip_ratio,
        "silence_ratio": silence_ratio,
        "leading_ms": leading_ms,
        "trailing_ms": trailing_ms,
        "centroid_hz": centroid,
    }


def grade(stats: dict, word_count: int, audio: np.ndarray | None = None, sr: int = 0) -> list[str]:
    """Return list of issue strings. Prefix matches REGEN_TRIGGERS for auto-regen.

    Pass audio + sr to enable repetition detection (recommended).
    Pass word_count=0 to skip duration checks (when text is unavailable).
    """
    issues: list[str] = []
    d = stats["duration_s"]

    if word_count > 0:
        wc = word_count
        expected_min = wc * SECS_PER_WORD_MIN
        expected_max = wc * SECS_PER_WORD * DURATION_MAX_MULT
        if d < expected_min:
            issues.append(f"TOO_SHORT:{d:.0f}s<{expected_min:.0f}s({wc}w)")
        elif d > expected_max:
            issues.append(f"TOO_LONG:{d:.0f}s>{expected_max:.0f}s({wc}w)")

    if stats["clip_ratio"] > CLIP_RATIO_WARN:
        issues.append(f"CLIPPING:{stats['clip_ratio']*100:.2f}%")

    # Repetition detection (independent of word count)
    if audio is not None and sr > 0 and d > _REP_MIN_LAG_S * 2:
        rep = detect_repetition(audio, sr)
        if rep:
            issues.append(rep)

    if stats["rms"] < RMS_LOW_WARN:
        issues.append(f"LOW_VOLUME:rms={stats['rms']:.4f}")
    elif stats["rms"] > RMS_HIGH_WARN:
        issues.append(f"HIGH_VOLUME:rms={stats['rms']:.4f}")

    if stats["silence_ratio"] > SILENCE_WARN_RATIO:
        issues.append(f"EXCESS_SILENCE:{stats['silence_ratio']*100:.1f}%")
    if stats["leading_ms"] > LEADING_MS_WARN:
        issues.append(f"LEADING_SILENCE:{stats['leading_ms']:.0f}ms")
    if stats["trailing_ms"] > TRAILING_MS_WARN:
        issues.append(f"TRAILING_SILENCE:{stats['trailing_ms']:.0f}ms")

    sc = stats["centroid_hz"]
    if sc < CENTROID_LOW_HZ:
        issues.append(f"DARK_SPECTRUM:{sc:.0f}Hz")
    elif sc > CENTROID_HIGH_HZ:
        issues.append(f"BRIGHT_SPECTRUM:{sc:.0f}Hz")

    return issues


def regen_issues(issues: list[str]) -> list[str]:
    return [i for i in issues if any(i.startswith(t) for t in REGEN_TRIGGERS)]


def check_wav(wav_path: Path, text: str) -> tuple[list[str], list[str]]:
    """Full quality check: load WAV, analyze + repetition-detect, grade against text.

    Returns (all_issues, regen_only_issues).
    Pass empty text to skip duration checks (repetition detection still runs).
    """
    audio, sr = load_wav(wav_path)
    stats = analyze_wav(audio, sr)
    wc = count_words(text) if text and text.strip() else 0
    all_iss = grade(stats, wc, audio=audio, sr=sr)
    return all_iss, regen_issues(all_iss)


def fmt_duration(s: float) -> str:
    return f"{int(s//60)}m{s%60:04.1f}s"
