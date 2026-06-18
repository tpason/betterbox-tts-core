from __future__ import annotations

import time
from pathlib import Path

import numpy as np

from scripts.story_pipeline.viterbox_audiobook_stitch import (
    AUTO_MAX_CHARS_PER_UNIT,
    AUTO_MIN_CHARS_PER_UNIT,
    count_hard_boundaries,
    count_words,
    edge_fade,
    expected_unit_duration_seconds,
    should_retry_short_unit,
    split_spoken_units,
    stitch_audio_segments,
    trim_edges,
    trim_excess_duration_for_unit,
    trim_trailing_artifact_for_unit,
)
from scripts.story_pipeline.vieneu_voice_profiles import resolve_vieneu_voice_profile


DEFAULT_VIENEU_VOICE = "Xuân Vĩnh"
DEFAULT_MAX_NEW_FRAMES = 300


def get_vieneu_sample_rate(tts) -> int:
    return int(getattr(tts, "sample_rate", getattr(tts, "sr", 48_000)))


def normalize_unit_for_vieneu(text: str) -> str:
    """Keep punctuation natural and let VieNeu/sea-g2p do punc_norm internally."""
    return " ".join(text.strip().split())


def resolve_vieneu_reference_kwargs(
    *,
    voice: str | None = None,
    reference_audio: str | None = None,
    reference_text: str | None = None,
    voice_profile: str | None = None,
) -> dict[str, str]:
    if reference_audio:
        kwargs = {"ref_audio": str(Path(reference_audio).resolve())}
        if reference_text:
            kwargs["ref_text"] = reference_text
        return kwargs
    if voice_profile:
        profile = resolve_vieneu_voice_profile(voice_profile)
        return {
            "ref_audio": str(profile.ref_audio_path.resolve()),
            "ref_text": profile.ref_text,
        }
    return {"voice": voice or DEFAULT_VIENEU_VOICE}


def generate_unit_audio_with_retry(
    tts,
    *,
    spoken: str,
    unit: str,
    word_count: int,
    voice: str | None,
    reference_audio: str | None,
    reference_text: str | None,
    voice_profile: str | None,
    emotion: str,
    temperature: float,
    top_k: int,
    top_p: float,
    max_new_frames: int,
    repetition_penalty: float,
    max_chars: int,
    apply_watermark: bool,
) -> tuple[np.ndarray, int, int]:
    sr = get_vieneu_sample_rate(tts)
    n_commas = unit.count(",")
    frame_attempts = (
        max_new_frames,
        max(120, round(max_new_frames * 0.82)),
        max(90, round(max_new_frames * 0.68)),
    ) if word_count <= 17 else (max_new_frames,)
    best_audio: np.ndarray | None = None
    best_frames = frame_attempts[0]
    ref_kwargs = resolve_vieneu_reference_kwargs(
        voice=voice,
        reference_audio=reference_audio,
        reference_text=reference_text,
        voice_profile=voice_profile,
    )

    for attempts, frames in enumerate(frame_attempts, start=1):
        audio_np = tts.infer(
            spoken,
            emotion=emotion,
            temperature=temperature,
            top_k=top_k,
            top_p=top_p,
            max_new_frames=frames,
            repetition_penalty=repetition_penalty,
            max_chars=max_chars,
            apply_watermark=apply_watermark,
            **ref_kwargs,
        )
        audio_np = np.asarray(audio_np, dtype=np.float32).squeeze()
        if best_audio is None or len(audio_np) < len(best_audio):
            best_audio = audio_np
            best_frames = frames
        if not should_retry_short_unit(audio_np, sr, word_count, n_commas):
            return audio_np, frames, attempts
        print(
            f"[retry] short unit overflow words={word_count} "
            f"audio={len(audio_np) / sr:.2f}s expected={expected_unit_duration_seconds(word_count):.2f}s "
            f"max_new_frames={frames}: {unit[:100]}"
        )

    assert best_audio is not None
    return best_audio, best_frames, len(frame_attempts)


def synthesize_vieneu_audiobook(
    tts,
    text: str,
    *,
    voice: str | None,
    reference_audio: str | None,
    reference_text: str | None,
    voice_profile: str | None,
    emotion: str,
    temperature: float,
    top_k: int,
    top_p: float,
    max_new_frames: int,
    repetition_penalty: float,
    max_chars: int,
    apply_watermark: bool,
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
    sr = get_vieneu_sample_rate(tts)

    split_start = time.perf_counter()
    units = split_spoken_units(text, max_chars=max_chars_per_unit, min_chars=min_chars_per_unit)
    split_elapsed = time.perf_counter() - split_start
    resolved_max_chars = max_chars_per_unit or AUTO_MAX_CHARS_PER_UNIT
    resolved_min_chars = AUTO_MIN_CHARS_PER_UNIT if min_chars_per_unit is None else min_chars_per_unit
    if reference_audio:
        ref_label = f"ref_audio={reference_audio}"
    elif voice_profile:
        ref_label = f"voice_profile={voice_profile}"
    else:
        ref_label = f"voice={voice or DEFAULT_VIENEU_VOICE}"
    print(
        f"[VieNeu audiobook] {len(text)} chars -> {len(units)} spoken units "
        f"(auto_split max_chars={resolved_max_chars}, min_chars={resolved_min_chars}, sr={sr}, {ref_label})"
    )
    print(f"[timing] split={split_elapsed:.3f}s")

    generated: list[np.ndarray] = []
    for idx, unit in enumerate(units, start=1):
        unit_start = time.perf_counter()
        normalize_start = time.perf_counter()
        hard_boundaries = count_hard_boundaries(unit)
        word_count = count_words(unit)
        n_commas = unit.count(",")
        spoken = normalize_unit_for_vieneu(unit)
        normalize_elapsed = time.perf_counter() - normalize_start
        print(
            f"\n[{idx}/{len(units)}] unit chars={len(unit)} words={word_count} "
            f"hard_boundaries={hard_boundaries} commas={n_commas}: {unit[:140]}"
        )

        infer_start = time.perf_counter()
        audio_np, frames, attempts = generate_unit_audio_with_retry(
            tts,
            spoken=spoken,
            unit=unit,
            word_count=word_count,
            voice=voice,
            reference_audio=reference_audio,
            reference_text=reference_text,
            voice_profile=voice_profile,
            emotion=emotion,
            temperature=temperature,
            top_k=top_k,
            top_p=top_p,
            max_new_frames=max_new_frames,
            repetition_penalty=repetition_penalty,
            max_chars=max_chars,
            apply_watermark=apply_watermark,
        )
        infer_elapsed = time.perf_counter() - infer_start

        post_start = time.perf_counter()
        raw_samples = len(audio_np)
        audio_np = trim_edges(audio_np, threshold=trim_threshold, margin_ms=trim_margin_ms, sr=sr)
        edge_trim_ms = (raw_samples - len(audio_np)) / sr * 1000 if raw_samples > len(audio_np) else 0.0
        audio_np, tail_trim_ms = trim_trailing_artifact_for_unit(audio_np, sr=sr, word_count=word_count)
        audio_np, duration_trim_ms = trim_excess_duration_for_unit(audio_np, sr=sr, word_count=word_count, n_commas=n_commas)
        audio_np = edge_fade(audio_np, sr, fade_in_ms=edge_fade_in_ms, fade_out_ms=edge_fade_out_ms)
        post_elapsed = time.perf_counter() - post_start

        if len(audio_np) > 0:
            generated.append(audio_np)
        audio_seconds = len(audio_np) / sr if len(audio_np) else 0.0
        unit_elapsed = time.perf_counter() - unit_start
        rtf = audio_seconds / unit_elapsed if unit_elapsed > 0 else 0.0
        print(
            f"[timing] unit={idx}/{len(units)} normalize={normalize_elapsed:.3f}s "
            f"infer={infer_elapsed:.2f}s post={post_elapsed:.3f}s total={unit_elapsed:.2f}s "
            f"audio={audio_seconds:.2f}s rtf={rtf:.2f}x "
            f"edge_trim={edge_trim_ms:.0f}ms tail_trim={tail_trim_ms:.0f}ms "
            f"duration_trim={duration_trim_ms:.0f}ms max_new_frames={frames} attempts={attempts}"
        )

    stitch_start = time.perf_counter()
    result = stitch_audio_segments(
        generated,
        sr,
        sentence_pause_ms=sentence_pause_ms,
        crossfade_ms=crossfade_ms,
    )
    stitch_elapsed = time.perf_counter() - stitch_start
    total_elapsed = time.perf_counter() - total_start
    audio_total = len(result) / sr if len(result) else 0.0
    total_rtf = audio_total / total_elapsed if total_elapsed > 0 else 0.0
    print(
        f"\n[timing] stitch={stitch_elapsed:.3f}s total={total_elapsed:.2f}s "
        f"audio={audio_total:.2f}s rtf={total_rtf:.2f}x"
    )
    return result
