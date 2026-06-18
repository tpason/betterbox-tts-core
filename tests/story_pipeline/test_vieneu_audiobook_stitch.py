from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from scripts.story_pipeline.vieneu_audiobook_stitch import (
    DEFAULT_VIENEU_VOICE,
    generate_unit_audio_with_retry,
    get_vieneu_sample_rate,
    resolve_vieneu_reference_kwargs,
)
from scripts.story_pipeline.vieneu_voice_profiles import (
    DEFAULT_VIENEU_VOICE_PROFILE,
    list_vieneu_voice_profiles,
)


class FakeVieNeu:
    sample_rate = 48_000

    def __init__(self, outputs: list[np.ndarray] | None = None):
        self.outputs = outputs or [np.zeros(self.sample_rate, dtype=np.float32)]
        self.calls: list[dict] = []

    def infer(self, text: str, **kwargs):
        self.calls.append({"text": text, **kwargs})
        idx = min(len(self.calls) - 1, len(self.outputs) - 1)
        return self.outputs[idx]


def test_get_vieneu_sample_rate_prefers_sample_rate_attr():
    assert get_vieneu_sample_rate(FakeVieNeu()) == 48_000


def test_resolve_reference_audio_overrides_builtin_voice():
    kwargs = resolve_vieneu_reference_kwargs(
        voice="Xuân Vĩnh",
        reference_audio="wavs/ref.wav",
        reference_text="tham chiếu",
    )

    assert "ref_audio" in kwargs
    assert kwargs["ref_audio"].endswith("wavs/ref.wav")
    assert kwargs["ref_text"] == "tham chiếu"
    assert "voice" not in kwargs


def test_resolve_builtin_voice_when_no_reference_audio():
    assert resolve_vieneu_reference_kwargs(voice="Xuân Vĩnh", reference_audio=None) == {"voice": "Xuân Vĩnh"}


def test_default_vieneu_voice_profile_resolves_to_local_reference_audio():
    kwargs = resolve_vieneu_reference_kwargs(voice_profile=DEFAULT_VIENEU_VOICE_PROFILE)

    assert "ref_audio" in kwargs
    assert kwargs["ref_audio"].endswith("wavs/vieneu_capybara1812_0048.wav")
    assert Path(kwargs["ref_audio"]).exists()
    assert kwargs["ref_text"]


def test_vieneu_voice_profiles_are_unique_and_license_tracked():
    profiles = list_vieneu_voice_profiles()

    assert len({profile.key for profile in profiles}) == len(profiles)
    assert all(profile.source == "pnnbao-ump/VieNeu-TTS-140h" for profile in profiles)
    assert all(profile.license == "Apache-2.0" for profile in profiles)


def test_generate_unit_audio_retries_short_overflow_with_lower_max_new_frames():
    tts = FakeVieNeu(
        outputs=[
            np.ones(48_000 * 4, dtype=np.float32),
            np.ones(48_000, dtype=np.float32),
        ]
    )

    audio, frames, attempts = generate_unit_audio_with_retry(
        tts,
        spoken="xin chào",
        unit="xin chào.",
        word_count=2,
        voice="Xuân Vĩnh",
        reference_audio=None,
        reference_text=None,
        voice_profile=None,
        emotion="natural",
        temperature=0.8,
        top_k=25,
        top_p=0.95,
        max_new_frames=300,
        repetition_penalty=1.2,
        max_chars=256,
        apply_watermark=False,
    )

    assert len(audio) == 48_000
    assert frames == 246
    assert attempts == 2
    assert [call["max_new_frames"] for call in tts.calls] == [300, 246]
    assert all(call["voice"] == "Xuân Vĩnh" for call in tts.calls)


def test_default_vieneu_voice_exists_in_installed_assets():
    import vieneu

    voices_path = Path(vieneu.__file__).parent / "assets" / "voices_v3_turbo.json"
    voices = json.loads(voices_path.read_text(encoding="utf-8"))["presets"]

    assert DEFAULT_VIENEU_VOICE in voices
