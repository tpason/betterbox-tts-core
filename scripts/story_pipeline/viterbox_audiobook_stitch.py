from __future__ import annotations

import re
import time

import numpy as np
import torch

from viterbox.tts_helper.tts_precision import config_token_for_precision


AUTO_MAX_CHARS_PER_UNIT = 300
AUTO_MIN_CHARS_PER_UNIT = 70

_SENTENCE_RE = re.compile(r'.+?(?:[.!?。！？…]+["\'”’)]*|$)(?=\s+|$)', re.DOTALL)
_HARD_END_RE = re.compile(r'[.!?。！？…]+["\'”’)]*$')
_QUOTE_START_RE = re.compile(r'^\s*["\'“‘]')
_QUOTE_END_RE = re.compile(r'["\'”’)]\s*$')
_QUOTE_CHARS_RE = re.compile(r'["“”‘’]')
_STABLE_PUNCT_TRANSLATION = str.maketrans({
    "?": ".",
    "!": ".",
    "？": ".",
    "！": ".",
    "…": ".",
    "。": ".",
})


def _split_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text.strip())
    if not text:
        return []
    return [match.group(0).strip() for match in _SENTENCE_RE.finditer(text) if match.group(0).strip()]


def _is_dialogue_or_exclamation(sentence: str) -> bool:
    stripped = sentence.strip()
    return bool(
        _QUOTE_START_RE.search(stripped)
        or _QUOTE_END_RE.search(stripped)
        or stripped.endswith(("!", "?", "！", "？"))
    )


def _soften_sentence_end(sentence: str) -> str:
    return _HARD_END_RE.sub("", sentence.strip()).strip(" ,;:，；：")


def _split_long_sentence(sentence: str, max_chars: int) -> list[str]:
    if len(sentence) <= max_chars:
        return [sentence]

    units: list[str] = []
    parts = re.split(r"(?<=[,;，；:：])\s+", sentence)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if len(part) <= max_chars:
            units.append(part)
            continue

        words = part.split()
        current: list[str] = []
        current_len = 0
        for word in words:
            word_len = len(word) + 1
            if current and current_len + word_len > max_chars:
                units.append(" ".join(current))
                current = [word]
                current_len = word_len
            else:
                current.append(word)
                current_len += word_len
        if current:
            units.append(" ".join(current))

    return units


def _semantic_pack_sentences(sentences: list[str], max_chars: int, min_chars: int) -> list[str]:
    """Pack sentences for audiobook TTS without hard stops inside a unit.

    The previous splitter merged short sentences by appending the next sentence
    unchanged. That created units such as "A. B. C.", so any T3/S3Gen tail
    artifact after the internal period became impossible to trim or fade. This
    packer keeps hard sentence boundaries as unit boundaries. It only merges a
    short narrative sentence with the next narrative sentence using a soft comma.
    """
    units: list[str] = []
    idx = 0

    while idx < len(sentences):
        sentence = sentences[idx].strip()
        if not sentence:
            idx += 1
            continue

        if len(sentence) > max_chars:
            units.extend(_split_long_sentence(sentence, max_chars))
            idx += 1
            continue

        can_soft_merge = (
            min_chars > 0
            and 15 < len(sentence) < min_chars   # ≤15 chars = sound-word / onomatopoeia → keep isolated
            and idx + 1 < len(sentences)
            and not _is_dialogue_or_exclamation(sentence)
        )
        if can_soft_merge:
            next_sentence = sentences[idx + 1].strip()
            candidate = f"{_soften_sentence_end(sentence)}, {next_sentence[:1].lower()}{next_sentence[1:]}"
            if (
                next_sentence
                and len(candidate) <= max_chars
                and len(next_sentence) <= max_chars
                and not _is_dialogue_or_exclamation(next_sentence)
            ):
                units.append(candidate)
                idx += 2
                continue

        units.append(sentence)
        idx += 1

    return units


def split_spoken_units(
    text: str,
    max_chars: int | None = None,
    min_chars: int | None = None,
) -> list[str]:
    """Split text into natural spoken units for Viterbox audiobook stitching.

    Sentence boundaries are treated as hard audio boundaries because Viterbox can
    leave an audible tail after internal "." pauses. Short narrative sentences
    may be merged softly with a comma, but dialogue/exclamation sentences stay
    isolated so their prosody remains intact.
    """
    max_chars = max_chars or AUTO_MAX_CHARS_PER_UNIT
    min_chars = AUTO_MIN_CHARS_PER_UNIT if min_chars is None else min_chars
    sentences = _split_sentences(text)
    return _semantic_pack_sentences(sentences, max_chars=max_chars, min_chars=min_chars)


def sanitize_unit_for_tts(text: str) -> str:
    text = _QUOTE_CHARS_RE.sub("", text)
    text = text.translate(_STABLE_PUNCT_TRANSLATION)
    text = re.sub(r"\.{2,}", ".", text)
    text = re.sub(r"\s+([.,])", r"\1", text)
    text = re.sub(r"([.,]){2,}", r"\1", text)
    text = re.sub(r"\s+", " ", text)
    text = text.strip()
    # Strip leading sentence-end punctuation — Viterbox vocalises a leading "."
    # as "ờ" (audible artifact). This can appear when a dialogue line originally
    # started with "..." and clean_for_audiobook_tts converted it to ". text".
    text = re.sub(r"^[.,;:\s]+", "", text)
    return text


def normalize_unit_for_viterbox(text: str, word_count: int | None = None) -> str:
    text = sanitize_unit_for_tts(text)
    return config_token_for_precision(
        text.casefold(),
        add_boundary_pause=not (word_count is not None and 2 <= word_count <= 4),
    )


def count_hard_boundaries(text: str) -> int:
    return len(re.findall(r"[.!?。！？…]+", text))


def count_words(text: str) -> int:
    return len(re.findall(r"\w+", text, flags=re.UNICODE))


def trim_edges(audio: np.ndarray, threshold: float = 0.006, margin_ms: int = 80, sr: int = 24000) -> np.ndarray:
    if audio is None or len(audio) == 0:
        return audio
    result = audio.astype(np.float32, copy=False)
    if result.ndim > 1:
        result = result.squeeze()
    indices = np.flatnonzero(np.abs(result) > threshold)
    if len(indices) == 0:
        return result
    margin = int(sr * margin_ms / 1000)
    start = max(0, int(indices[0]) - margin)
    end = min(len(result), int(indices[-1]) + margin)
    return result[start:end]


def trim_trailing_artifact(
    audio: np.ndarray,
    sr: int,
    *,
    absolute_threshold: float = 0.0045,
    relative_threshold: float = 0.035,
    frame_ms: int = 30,
    margin_ms: int = 130,
    min_trim_ms: int = 180,
) -> tuple[np.ndarray, float]:
    """Trim generated tail rasp while preserving final syllable release.

    Some Viterbox generations leave a low-level buzzing tail after the spoken
    sentence. A simple sample threshold can miss it because the tail may stay
    above the silence threshold for hundreds of milliseconds. RMS windows give a
    more stable speech endpoint estimate; the margin keeps final Vietnamese
    consonant/vowel releases from being cut too tightly.
    """
    if audio is None or len(audio) == 0:
        return audio, 0.0

    result = audio.astype(np.float32, copy=False)
    if result.ndim > 1:
        result = result.squeeze()
    if len(result) == 0:
        return result, 0.0

    frame = max(1, int(sr * frame_ms / 1000))
    if len(result) < frame * 4:
        return result, 0.0

    usable_len = (len(result) // frame) * frame
    frames = result[:usable_len].reshape(-1, frame)
    rms = np.sqrt(np.mean(frames * frames, axis=1))
    if len(rms) == 0:
        return result, 0.0

    peak_rms = float(np.max(rms))
    if peak_rms <= 0:
        return result, 0.0

    threshold = max(absolute_threshold, peak_rms * relative_threshold)
    voiced = np.flatnonzero(rms > threshold)
    if len(voiced) == 0:
        return result, 0.0

    margin = int(sr * margin_ms / 1000)
    min_trim = int(sr * min_trim_ms / 1000)
    end = min(len(result), int((voiced[-1] + 1) * frame) + margin)

    if len(result) - end < min_trim:
        return result, 0.0
    trimmed_ms = (len(result) - end) / sr * 1000
    return result[:end], trimmed_ms


def trim_trailing_artifact_for_unit(audio: np.ndarray, sr: int, word_count: int) -> tuple[np.ndarray, float]:
    # Short/medium units: tighter threshold to catch buzz after !-sentences
    if word_count <= 17:
        return trim_trailing_artifact(
            audio,
            sr,
            absolute_threshold=0.006,
            relative_threshold=0.08,
            frame_ms=30,
            margin_ms=100,
            min_trim_ms=120,
        )
    # Long units (>17 words): same aggressive params — they are even more likely to
    # accumulate trailing noise since T3 must sustain attention over more tokens.
    # Lower absolute_threshold (0.0045 vs 0.006) because long-sentence artifacts are
    # typically quieter residual noise rather than full-amplitude buzz.
    return trim_trailing_artifact(
        audio,
        sr,
        absolute_threshold=0.0045,
        relative_threshold=0.035,
        frame_ms=30,
        margin_ms=100,
        min_trim_ms=120,
    )


def trim_excess_duration_for_unit(audio: np.ndarray, sr: int, word_count: int, n_commas: int = 0) -> tuple[np.ndarray, float]:
    """Cap suspiciously long short-unit audio.

    When T3 misses EOS, the generated audio may continue as a rasp or repeat a
    trailing phrase. RMS-based endpoint detection can treat that artifact as
    voiced. For short and medium units, a conservative duration cap catches the
    obvious overflow while leaving long narrative units untouched.

    Cap is set at 2.0× expected to allow slow emotional/narrative delivery
    (which can run 30-60% over the baseline estimate) without hard-cutting real speech.
    Genuine EOS overflow generates 3-10× expected and is still caught well within this cap.
    """
    if audio is None or len(audio) == 0 or word_count <= 0 or word_count > 17:
        return audio, 0.0

    max_seconds = expected_unit_duration_seconds(word_count, n_commas) * 2.0

    max_samples = int(sr * max_seconds)
    min_trim_samples = int(sr * 0.08)
    if len(audio) - max_samples < min_trim_samples:
        return audio, 0.0

    trimmed_ms = (len(audio) - max_samples) / sr * 1000
    return audio[:max_samples], trimmed_ms


def expected_unit_duration_seconds(word_count: int, n_commas: int = 0) -> float:
    """Expected maximum duration for a unit. n_commas accounts for TTS pause at each comma."""
    if word_count <= 0:
        return 0.0
    comma_bonus = n_commas * 0.45  # each comma adds ~0.45s pause in Viterbox output
    if word_count <= 4:
        return max(0.70, word_count * 0.25 + 0.40) + comma_bonus
    if word_count <= 17:
        return max(0.85, word_count * 0.27 + 0.45) + comma_bonus
    return word_count * 0.34 + 0.70 + comma_bonus


def should_retry_short_unit(audio: np.ndarray, sr: int, word_count: int, n_commas: int = 0) -> bool:
    if audio is None or len(audio) == 0 or word_count <= 0 or word_count > 17:
        return False
    expected = expected_unit_duration_seconds(word_count, n_commas)
    duration = len(audio) / sr
    # Only retry when audio is clearly anomalous — genuine EOS miss produces audio
    # 2-3× the expected length.  The old +0.55s margin was far too tight: emotional
    # dialogue or slow narration can easily run 30-60% over the "expected" duration
    # without any overflow, and the retry (scale=0.82) then *truncates* real words.
    # Use max(2× expected, expected+2.0) so both short units (absolute margin matters)
    # and long units (percentage margin matters) are handled correctly.
    threshold = max(expected * 2.0, expected + 2.0)
    return duration > threshold


def generate_unit_audio_with_retry(
    model,
    *,
    spoken: str,
    unit: str,
    word_count: int,
    language: str,
    cfg_weight: float,
    temperature: float,
    top_p: float,
    repetition_penalty: float,
    speed: float,
    pitch_shift: float,
) -> tuple[np.ndarray, float, int]:
    n_commas = unit.count(",")
    scales = (1.0, 0.82, 0.68) if word_count <= 17 else (1.0,)
    best_audio: np.ndarray | None = None
    best_scale = scales[0]
    attempts = 0

    for scale in scales:
        attempts += 1
        audio_np = model._generate_single(
            text=spoken,
            language=language,
            cfg_weight=cfg_weight,
            temperature=temperature,
            top_p=top_p,
            repetition_penalty=repetition_penalty,
            speed=speed,
            pitch_shift=pitch_shift,
            max_new_tokens_scale=scale,
        )
        if best_audio is None or len(audio_np) < len(best_audio):
            best_audio = audio_np
            best_scale = scale
        if not should_retry_short_unit(audio_np, model.sr, word_count, n_commas):
            return audio_np, scale, attempts
        print(
            f"[retry] short unit overflow words={word_count} "
            f"audio={len(audio_np) / model.sr:.2f}s expected={expected_unit_duration_seconds(word_count):.2f}s "
            f"scale={scale:.2f}: {unit[:100]}"
        )

    assert best_audio is not None
    return best_audio, best_scale, attempts


def edge_fade(audio: np.ndarray, sr: int, fade_in_ms: int = 4, fade_out_ms: int = 18) -> np.ndarray:
    if audio is None or len(audio) == 0:
        return audio
    result = audio.astype(np.float32, copy=True)
    result = result - float(np.mean(result))
    max_fade = max(1, len(result) // 4)
    fade_in = min(int(sr * fade_in_ms / 1000), max_fade)
    fade_out = min(int(sr * fade_out_ms / 1000), max_fade)
    if fade_in > 1:
        result[:fade_in] *= np.linspace(0.0, 1.0, fade_in, dtype=np.float32)
    if fade_out > 1:
        result[-fade_out:] *= np.linspace(1.0, 0.0, fade_out, dtype=np.float32)
    return result


def append_with_crossfade(left: np.ndarray, right: np.ndarray, crossfade_samples: int) -> np.ndarray:
    if len(left) == 0:
        return right
    if len(right) == 0:
        return left
    n = min(crossfade_samples, len(left), len(right))
    if n <= 1:
        return np.concatenate([left, right])
    fade_out = np.linspace(1.0, 0.0, n, dtype=np.float32)
    fade_in = np.linspace(0.0, 1.0, n, dtype=np.float32)
    overlap = left[-n:] * fade_out + right[:n] * fade_in
    return np.concatenate([left[:-n], overlap, right[n:]])


def stitch_audio_segments(
    segments: list[np.ndarray],
    sr: int,
    sentence_pause_ms: int = 500,
    crossfade_ms: int = 50,
) -> np.ndarray:
    if not segments:
        return np.zeros(0, dtype=np.float32)

    pause = np.zeros(int(sr * sentence_pause_ms / 1000), dtype=np.float32)
    crossfade_samples = int(sr * crossfade_ms / 1000)

    result = np.zeros(0, dtype=np.float32)
    for idx, segment in enumerate(segments):
        piece = segment.astype(np.float32, copy=False)
        if idx:
            piece = np.concatenate([pause, piece])
            if crossfade_samples > 0:
                result = append_with_crossfade(result, piece, crossfade_samples)
            else:
                result = np.concatenate([result, piece])
        else:
            result = piece
    return result


def synthesize_viterbox_audiobook(
    model,
    text: str,
    *,
    language: str,
    reference_audio: str,
    exaggeration: float,
    cfg_weight: float,
    temperature: float,
    top_p: float,
    repetition_penalty: float,
    speed: float,
    pitch_shift: float,
    max_chars_per_unit: int | None,
    min_chars_per_unit: int | None,
    sentence_pause_ms: int,
    crossfade_ms: int,
    trim_threshold: float,
    trim_margin_ms: int,
    edge_fade_in_ms: int,
    edge_fade_out_ms: int,
) -> np.ndarray:
    total_start = time.perf_counter()

    prepare_start = time.perf_counter()
    model.prepare_conditionals(reference_audio, exaggeration)
    if model.conds is not None and hasattr(model.conds.t3, "emotion_adv"):
        model.conds.t3.emotion_adv = exaggeration * torch.ones(1, 1, 1).to(model.device)
    prepare_elapsed = time.perf_counter() - prepare_start

    split_start = time.perf_counter()
    units = split_spoken_units(text, max_chars=max_chars_per_unit, min_chars=min_chars_per_unit)
    split_elapsed = time.perf_counter() - split_start
    resolved_max_chars = max_chars_per_unit or AUTO_MAX_CHARS_PER_UNIT
    resolved_min_chars = AUTO_MIN_CHARS_PER_UNIT if min_chars_per_unit is None else min_chars_per_unit
    print(
        f"[Viterbox audiobook] {len(text)} chars -> {len(units)} spoken units "
        f"(auto_split max_chars={resolved_max_chars}, min_chars={resolved_min_chars})"
    )
    print(f"[timing] prepare_conditionals={prepare_elapsed:.2f}s split={split_elapsed:.3f}s")

    generated: list[np.ndarray] = []
    for idx, unit in enumerate(units, start=1):
        unit_start = time.perf_counter()
        normalize_start = time.perf_counter()
        hard_boundaries = count_hard_boundaries(unit)
        word_count = count_words(unit)
        n_commas = unit.count(",")
        spoken = normalize_unit_for_viterbox(unit, word_count=word_count)
        token_count = model.tokenizer.text_to_tokens(spoken, language_id=language).shape[-1]
        normalize_elapsed = time.perf_counter() - normalize_start
        print(
            f"\n[{idx}/{len(units)}] unit chars={len(unit)} words={word_count} tokens={token_count} "
            f"hard_boundaries={hard_boundaries} commas={n_commas}: {unit[:140]}"
        )
        infer_start = time.perf_counter()
        audio_np, token_scale, attempts = generate_unit_audio_with_retry(
            model,
            spoken=spoken,
            unit=unit,
            word_count=word_count,
            language=language,
            cfg_weight=cfg_weight,
            temperature=temperature,
            top_p=top_p,
            repetition_penalty=repetition_penalty,
            speed=speed,
            pitch_shift=pitch_shift,
        )
        infer_elapsed = time.perf_counter() - infer_start
        post_start = time.perf_counter()
        raw_samples = len(audio_np)
        audio_np = trim_edges(audio_np, threshold=trim_threshold, margin_ms=trim_margin_ms, sr=model.sr)
        edge_trim_ms = (raw_samples - len(audio_np)) / model.sr * 1000 if raw_samples > len(audio_np) else 0.0
        audio_np, tail_trim_ms = trim_trailing_artifact_for_unit(audio_np, sr=model.sr, word_count=word_count)
        audio_np, duration_trim_ms = trim_excess_duration_for_unit(audio_np, sr=model.sr, word_count=word_count, n_commas=n_commas)
        # Trailing buzz ("rè") at sentence ends:
        #   - Viterbox TTS generates a tonal artifact after sentence-ending "."
        #   - This artifact is loud enough (~0.03-0.05 RMS) to be counted as "voiced"
        #     by RMS-based trim → trim_trailing_artifact reports tail_trim=0ms always
        #   - A 100ms fade reliably masks buzzes up to ~100ms without cutting real speech
        #   - Short units (2-4 words) use 120ms: buzz is more prominent relative to clip length
        if 2 <= word_count <= 4:
            effective_fade_out_ms = max(edge_fade_out_ms, 120)
        else:
            effective_fade_out_ms = max(edge_fade_out_ms, 100)
        audio_np = edge_fade(audio_np, model.sr, fade_in_ms=edge_fade_in_ms, fade_out_ms=effective_fade_out_ms)
        post_elapsed = time.perf_counter() - post_start
        if len(audio_np) > 0:
            generated.append(audio_np)
        audio_seconds = len(audio_np) / model.sr if len(audio_np) else 0.0
        unit_elapsed = time.perf_counter() - unit_start
        rtf = audio_seconds / unit_elapsed if unit_elapsed > 0 else 0.0
        print(
            f"[timing] unit={idx}/{len(units)} normalize={normalize_elapsed:.3f}s "
            f"infer={infer_elapsed:.2f}s post={post_elapsed:.3f}s total={unit_elapsed:.2f}s "
            f"audio={audio_seconds:.2f}s rtf={rtf:.2f}x "
            f"edge_trim={edge_trim_ms:.0f}ms tail_trim={tail_trim_ms:.0f}ms "
            f"duration_trim={duration_trim_ms:.0f}ms token_scale={token_scale:.2f} attempts={attempts}"
        )

    stitch_start = time.perf_counter()
    result = stitch_audio_segments(
        generated,
        model.sr,
        sentence_pause_ms=sentence_pause_ms,
        crossfade_ms=crossfade_ms,
    )
    stitch_elapsed = time.perf_counter() - stitch_start
    total_elapsed = time.perf_counter() - total_start
    audio_total = len(result) / model.sr if len(result) else 0.0
    total_rtf = audio_total / total_elapsed if total_elapsed > 0 else 0.0
    print(
        f"\n[timing] stitch={stitch_elapsed:.3f}s total={total_elapsed:.2f}s "
        f"audio={audio_total:.2f}s rtf={total_rtf:.2f}x"
    )
    return result
