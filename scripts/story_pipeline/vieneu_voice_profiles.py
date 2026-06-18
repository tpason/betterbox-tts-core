from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_VIENEU_VOICE_PROFILE = "xianxia_story_male"


@dataclass(frozen=True)
class VieneuVoiceProfile:
    key: str
    label: str
    speaker: str
    gender: str
    ref_audio: str
    ref_text: str
    source: str
    license: str
    tags: tuple[str, ...]
    notes: str = ""

    @property
    def ref_audio_path(self) -> Path:
        path = Path(self.ref_audio)
        return path if path.is_absolute() else PROJECT_ROOT / path


VIENEU_VOICE_PROFILES: dict[str, VieneuVoiceProfile] = {
    "xianxia_spirit_male": VieneuVoiceProfile(
        key="xianxia_spirit_male",
        label="Tiên hiệp nam - trầm tĩnh",
        speaker="jellyfish1010_0006",
        gender="male",
        ref_audio="wavs/vieneu_jellyfish1010_0006.wav",
        ref_text=(
            "Khi thời gian trần thế kết thúc, linh hồn của bạn phải nguyên vẹn "
            "mới có thể sống sót qua hành trình."
        ),
        source="pnnbao-ump/VieNeu-TTS-140h",
        license="Apache-2.0",
        tags=("xianxia", "male", "calm", "narrator", "spiritual"),
        notes="Primary BetterBox audiobook voice candidate for cultivation/fantasy narration.",
    ),
    "xianxia_story_male": VieneuVoiceProfile(
        key="xianxia_story_male",
        label="Tiên hiệp nam - kể chuyện",
        speaker="capybara1812_0048",
        gender="male",
        ref_audio="wavs/vieneu_capybara1812_0048.wav",
        ref_text=(
            "Bố tôi mỉm cười và nói, tôi dẫn nó đến gặp ông, Intep à, "
            "vì ông là người duy nhất còn sống trong số..."
        ),
        source="pnnbao-ump/VieNeu-TTS-140h",
        license="Apache-2.0",
        tags=("xianxia", "male", "story", "narrator"),
        notes="Longer exported speaker set; good second candidate for chapter narration.",
    ),
    "xianxia_elder_male": VieneuVoiceProfile(
        key="xianxia_elder_male",
        label="Tiên hiệp nam - trưởng bối",
        speaker="capybara1812_0076",
        gender="male",
        ref_audio="wavs/vieneu_capybara1812_0076.wav",
        ref_text=(
            "Tôi được thừa hưởng những đặc điểm tốt nhất của người Hà Lan, "
            "đức tin sự tiết kiệm, lối sống thực tế."
        ),
        source="pnnbao-ump/VieNeu-TTS-140h",
        license="Apache-2.0",
        tags=("xianxia", "male", "elder", "steady"),
        notes="Candidate for older/steadier male narration tests.",
    ),
    "xianxia_formal_male": VieneuVoiceProfile(
        key="xianxia_formal_male",
        label="Tiên hiệp nam - trang trọng",
        speaker="jellyfish1010_0039",
        gender="male",
        ref_audio="wavs/vieneu_jellyfish1010_0039.wav",
        ref_text=(
            "Không những thế, xuất thân của tôi chứng minh rằng doanh nhân "
            "có thể khởi đầu từ bất cứ đâu."
        ),
        source="pnnbao-ump/VieNeu-TTS-140h",
        license="Apache-2.0",
        tags=("male", "formal", "narrator"),
        notes="More formal tone; useful for comparison against fantasy candidates.",
    ),
    "xianxia_female_narrator": VieneuVoiceProfile(
        key="xianxia_female_narrator",
        label="Tiên hiệp nữ - kể chuyện",
        speaker="alloy1512_1002",
        gender="female",
        ref_audio="wavs/vieneu_alloy1512_1002.wav",
        ref_text=(
            "Tôi thầm nghĩ, ôi, mới ngày đầu tiên thôi, còn những 9 tháng nữa, "
            "mỗi tháng còn phải làm bài kiểm tra."
        ),
        source="pnnbao-ump/VieNeu-TTS-140h",
        license="Apache-2.0",
        tags=("female", "narrator", "story"),
        notes="Female narrator candidate from a large exported speaker set.",
    ),
}


def list_vieneu_voice_profiles() -> list[VieneuVoiceProfile]:
    return list(VIENEU_VOICE_PROFILES.values())


def get_vieneu_voice_profile(key: str | None) -> VieneuVoiceProfile | None:
    if not key:
        return None
    return VIENEU_VOICE_PROFILES.get(key)


def resolve_vieneu_voice_profile(key: str) -> VieneuVoiceProfile:
    profile = get_vieneu_voice_profile(key)
    if profile is None:
        known = ", ".join(sorted(VIENEU_VOICE_PROFILES))
        raise ValueError(f"Unknown VieNeu voice profile: {key!r}. Known profiles: {known}")
    if not profile.ref_audio_path.exists():
        raise FileNotFoundError(
            f"VieNeu voice profile {key!r} reference audio is missing: {profile.ref_audio_path}. "
            "Export/copy the voice bank locally before using this profile."
        )
    return profile
