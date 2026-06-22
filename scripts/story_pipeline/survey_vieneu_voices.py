from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import wave
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.story_pipeline.vieneu_voice_profiles import (
    VieneuVoiceProfile,
    list_vieneu_voice_profiles,
)


@dataclass
class VoiceSurveyResult:
    key: str
    label: str
    speaker: str
    gender: str
    kind: str
    source: str
    score: float
    rank_reason: str
    sample_count: int = 0
    paired_text_count: int = 0
    total_duration_seconds: float = 0.0
    median_duration_seconds: float = 0.0
    median_rms_dbfs: float | None = None
    peak_dbfs: float | None = None
    clipping_ratio: float = 0.0
    reference_audio: str = ""
    preset_voice: str = ""


def _dbfs(value: float) -> float:
    if value <= 0:
        return -120.0
    return 20.0 * math.log10(value)


def _read_wav_metrics(path: Path) -> tuple[float, float, float, float]:
    with wave.open(path.as_posix(), "rb") as wav:
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        sample_rate = wav.getframerate()
        frames = wav.getnframes()
        raw = wav.readframes(frames)

    if frames <= 0 or not raw:
        return 0.0, -120.0, -120.0, 0.0
    if sample_width == 2:
        audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif sample_width == 4:
        audio = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    elif sample_width == 1:
        audio = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        return frames / sample_rate, -120.0, -120.0, 0.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    rms = float(np.sqrt(np.mean(np.square(audio)))) if audio.size else 0.0
    clipping = float(np.mean(np.abs(audio) >= 0.98)) if audio.size else 0.0
    return frames / sample_rate, _dbfs(rms), _dbfs(peak), clipping


def _score_preset(profile: VieneuVoiceProfile) -> tuple[float, str]:
    tags = set(profile.tags)
    text = f"{profile.label} {profile.notes}".lower()
    score = 72.0
    reasons = ["built-in speaker token ổn định hơn single WAV clone"]
    if profile.gender == "male":
        score += 8.0
        reasons.append("nam narrator hợp phần lớn tiên hiệp/system")
    if "calm" in tags or "điềm đạm" in text:
        score += 10.0
        reasons.append("điềm đạm, nghe lâu ít mệt")
    if "scholarly" in tags or "uyên bác" in text:
        score += 7.0
        reasons.append("uyên bác, hợp tu luyện/hệ thống")
    if "clear" in tags or "rõ ràng" in text:
        score += 5.0
        reasons.append("rõ chữ")
    if "strong" in tags:
        score += 2.0
        reasons.append("có lực cho cảnh chiến đấu")
    if "bright" in tags or "lively" in tags or "trẻ trung" in text or "vui tươi" in text:
        score -= 10.0
        reasons.append("tone sáng/vui dễ mệt khi nghe dài")
    if profile.gender == "female":
        score -= 3.0
        reasons.append("nữ narrator giữ làm lựa chọn phụ")
    return score, "; ".join(reasons)


def _profile_audio_dir(profile: VieneuVoiceProfile) -> Path | None:
    if profile.ref_audio:
        path = profile.ref_audio_path
        posix = path.as_posix()
        if "voice_bank/vieneu" in posix or "voice_bank/phoaudiobook" in posix:
            return path.parent
    return None


def _score_audio_profile(profile: VieneuVoiceProfile) -> VoiceSurveyResult:
    audio_dir = _profile_audio_dir(profile)
    wavs = sorted(audio_dir.glob("*.wav")) if audio_dir and audio_dir.exists() else []
    if not wavs and profile.ref_audio:
        ref = profile.ref_audio_path
        wavs = [ref] if ref.exists() else []

    durations: list[float] = []
    rms_values: list[float] = []
    peak_values: list[float] = []
    clipping_values: list[float] = []
    for wav_path in wavs:
        duration, rms_db, peak_db, clipping = _read_wav_metrics(wav_path)
        if duration <= 0:
            continue
        durations.append(duration)
        rms_values.append(rms_db)
        peak_values.append(peak_db)
        clipping_values.append(clipping)

    paired_text_count = 0
    if audio_dir and audio_dir.exists():
        paired_text_count = len(list(audio_dir.glob("*.txt")))
    elif profile.ref_text:
        paired_text_count = 1

    total_duration = sum(durations)
    median_duration = statistics.median(durations) if durations else 0.0
    median_rms = statistics.median(rms_values) if rms_values else None
    peak_db = max(peak_values) if peak_values else None
    clipping_ratio = max(clipping_values) if clipping_values else 0.0

    tags = set(profile.tags)
    score = 35.0
    reasons: list[str] = []
    if profile.gender == "male":
        score += 8.0
        reasons.append("nam narrator")
    if "xianxia" in tags:
        score += 7.0
        reasons.append("đã tag xianxia")
    if "narrator" in tags or "story" in tags or "storyteller" in tags:
        score += 8.0
        reasons.append("tag narrator/story")
    score += min(14.0, len(durations) / 12.0)
    if len(durations) >= 50:
        reasons.append(f"nhiều sample ({len(durations)})")
    score += min(12.0, total_duration / 120.0)
    if total_duration >= 600:
        reasons.append(f"tổng audio {total_duration / 60:.1f} phút")
    if 3.0 <= median_duration <= 12.0:
        score += 6.0
        reasons.append("duration mẫu nằm trong vùng voice-clone tốt")
    elif median_duration < 2.0:
        score -= 7.0
        reasons.append("mẫu quá ngắn")
    if median_rms is not None:
        if -30.0 <= median_rms <= -15.0:
            score += 6.0
            reasons.append(f"loudness vừa phải ({median_rms:.1f} dBFS)")
        elif median_rms > -12.0:
            score -= 8.0
            reasons.append(f"rms hơi gắt ({median_rms:.1f} dBFS)")
    if clipping_ratio > 0.001:
        score -= 12.0
        reasons.append("có dấu hiệu clipping")
    if "bright" in tags or "lively" in tags or "energetic" in tags:
        score -= 3.0
        reasons.append("tone sáng/năng động, dễ mệt hơn khi nghe dài")

    return VoiceSurveyResult(
        key=profile.key,
        label=profile.label,
        speaker=profile.speaker,
        gender=profile.gender,
        kind="clone",
        source=profile.source,
        score=round(score, 2),
        rank_reason="; ".join(reasons) or "audio profile",
        sample_count=len(durations),
        paired_text_count=paired_text_count,
        total_duration_seconds=round(total_duration, 2),
        median_duration_seconds=round(median_duration, 2),
        median_rms_dbfs=round(median_rms, 2) if median_rms is not None else None,
        peak_dbfs=round(peak_db, 2) if peak_db is not None else None,
        clipping_ratio=round(clipping_ratio, 6),
        reference_audio=profile.ref_audio,
    )


def survey_profiles() -> list[VoiceSurveyResult]:
    results: list[VoiceSurveyResult] = []
    for profile in list_vieneu_voice_profiles():
        if profile.is_preset:
            score, reason = _score_preset(profile)
            results.append(
                VoiceSurveyResult(
                    key=profile.key,
                    label=profile.label,
                    speaker=profile.speaker,
                    gender=profile.gender,
                    kind="preset",
                    source=profile.source,
                    score=round(score, 2),
                    rank_reason=reason,
                    preset_voice=profile.preset_voice,
                )
            )
        else:
            results.append(_score_audio_profile(profile))
    return sorted(results, key=lambda item: item.score, reverse=True)


def write_markdown(results: list[VoiceSurveyResult], path: Path, top: int) -> None:
    lines = [
        "# VieNeu Voice Survey",
        "",
        f"Recommended default: `{results[0].key}` ({results[0].label})",
        "",
        "| Rank | Key | Kind | Gender | Score | Reason |",
        "|---:|---|---|---|---:|---|",
    ]
    for idx, item in enumerate(results[:top], start=1):
        reason = item.rank_reason.replace("|", "/")
        lines.append(f"| {idx} | `{item.key}` | {item.kind} | {item.gender} | {item.score:.2f} | {reason} |")
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- Preset voices use VieNeu v3 Turbo built-in speaker tokens, so they are expected to be more stable than single reference WAV cloning.",
            "- Clone voices are scored from local WAV count, total duration, median duration, RMS, peak/clipping, and narrator/xianxia tags.",
            "- This is an automatic shortlist. Final acceptance should still include a listening pass on 2-3 generated samples.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Survey VieNeu preset and local voice-bank voices for BetterBox audiobook use.")
    parser.add_argument("--output-dir", default="/tmp/betterbox-vieneu-voice-survey")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--json-name", default="vieneu_voice_survey.json")
    parser.add_argument("--markdown-name", default="vieneu_voice_survey.md")
    args = parser.parse_args()

    results = survey_profiles()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / args.json_name
    markdown_path = output_dir / args.markdown_name
    json_path.write_text(
        json.dumps([asdict(item) for item in results], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_markdown(results, markdown_path, args.top)

    print(f"Recommended default: {results[0].key} ({results[0].label}) score={results[0].score:.2f}")
    print(f"JSON: {json_path}")
    print(f"Markdown: {markdown_path}")
    print()
    for idx, item in enumerate(results[: args.top], start=1):
        print(f"{idx:02d}. {item.key:28s} {item.kind:6s} {item.gender:6s} {item.score:6.2f}  {item.rank_reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
