from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_VIENEU_VOICE_PROFILE = "preset_trong_huu"


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
    # Built-in VieNeu preset voice name (e.g. "Bình An"). Mutually exclusive
    # with ref_audio — when set, voice cloning is skipped and the preset token
    # is used directly. Takes precedence over ref_audio in resolve helpers.
    preset_voice: str = ""

    @property
    def ref_audio_path(self) -> Path:
        path = Path(self.ref_audio)
        return path if path.is_absolute() else PROJECT_ROOT / path

    @property
    def is_preset(self) -> bool:
        return bool(self.preset_voice)


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
    # ── Dolly voice bank — xianxia candidates ───────────────────────────────
    "dolly_serene_elder": VieneuVoiceProfile(
        key="dolly_serene_elder",
        label="Trưởng bối an tĩnh",
        speaker="dolly_serene_elder",
        gender="male",
        ref_audio="wavs/dolly_serene_elder.wav",
        ref_text="",
        source="dolly-voice-bank",
        license="unknown",
        tags=("xianxia", "male", "elder", "calm", "narrator"),
        notes="Serene elder — rất phù hợp tiên hiệp, giọng trầm an tĩnh.",
    ),
    "dolly_steadfast_narrator": VieneuVoiceProfile(
        key="dolly_steadfast_narrator",
        label="Người kể chuyện kiên định",
        speaker="dolly_steadfast_narrator",
        gender="male",
        ref_audio="wavs/dolly_steadfast_narrator.wav",
        ref_text="",
        source="dolly-voice-bank",
        license="unknown",
        tags=("xianxia", "male", "narrator", "steady"),
        notes="Steadfast narrator — giọng ổn định, phù hợp long-form audiobook.",
    ),
    "dolly_wise_scholar": VieneuVoiceProfile(
        key="dolly_wise_scholar",
        label="Học giả thông thái",
        speaker="dolly_wise_scholar",
        gender="male",
        ref_audio="wavs/dolly_wise_scholar.wav",
        ref_text="",
        source="dolly-voice-bank",
        license="unknown",
        tags=("xianxia", "male", "wise", "scholar", "narrator"),
        notes="Wise scholar — tone trí tuệ, hợp xianxia/tiên hiệp.",
    ),
    "dolly_thoughtful_mentor": VieneuVoiceProfile(
        key="dolly_thoughtful_mentor",
        label="Sư phụ suy nghĩ sâu",
        speaker="dolly_thoughtful_mentor",
        gender="male",
        ref_audio="wavs/dolly_thoughtful_mentor.wav",
        ref_text="",
        source="dolly-voice-bank",
        license="unknown",
        tags=("xianxia", "male", "mentor", "thoughtful"),
        notes="Thoughtful mentor — hợp cảnh giảng dạy, tu luyện.",
    ),
    "dolly_deep_voiced_gentleman": VieneuVoiceProfile(
        key="dolly_deep_voiced_gentleman",
        label="Quý ông giọng trầm",
        speaker="dolly_deep_voiced_gentleman",
        gender="male",
        ref_audio="wavs/dolly_deep_voiced_gentleman.wav",
        ref_text="",
        source="dolly-voice-bank",
        license="unknown",
        tags=("xianxia", "male", "deep", "powerful"),
        notes="Deep voiced gentleman — giọng trầm quyền lực, hợp nhân vật mạnh.",
    ),
    "dolly_mysterious_girl": VieneuVoiceProfile(
        key="dolly_mysterious_girl",
        label="Thiếu nữ huyền bí",
        speaker="dolly_mysterious_girl",
        gender="female",
        ref_audio="wavs/dolly_mysterious_girl.wav",
        ref_text="",
        source="dolly-voice-bank",
        license="unknown",
        tags=("xianxia", "female", "mysterious", "narrator"),
        notes="Mysterious girl — hợp truyện có nữ chính hoặc không khí huyền bí.",
    ),
    "dolly_captivating_storyteller": VieneuVoiceProfile(
        key="dolly_captivating_storyteller",
        label="Người kể chuyện cuốn hút",
        speaker="dolly_captivating_storyteller",
        gender="female",
        ref_audio="wavs/dolly_captivating_storyteller.wav",
        ref_text="",
        source="dolly-voice-bank",
        license="unknown",
        tags=("xianxia", "female", "storyteller", "engaging"),
        notes="Captivating storyteller — giọng kể chuyện cuốn hút, nữ.",
    ),
    # ── Dolly — male narrator candidates (xianxia, based on ref text analysis) ─
    "dolly_steady_man": VieneuVoiceProfile(
        key="dolly_steady_man",
        label="Vũ tướng kiên định",
        speaker="dolly_steady_man",
        gender="male",
        ref_audio="wavs/dolly_steady_man.wav",
        ref_text="Khương Thượng nhờ có võ công cao cường lại rất dũng mãnh, nên đã mở con đường máu thoát ra khỏi vòng vây đông đến mười vạn quân.",
        source="dolly-voice-bank",
        license="unknown",
        tags=("xianxia", "male", "warrior", "steady", "narrator"),
        notes="Ref text xianxia thuần — võ công/mở đường máu. Ứng cử viên hàng đầu cho default.",
    ),
    "dolly_reliable_man": VieneuVoiceProfile(
        key="dolly_reliable_man",
        label="Đáng tin cậy, trọng nghĩa",
        speaker="dolly_reliable_man",
        gender="male",
        ref_audio="wavs/dolly_reliable_man.wav",
        ref_text="Khương Thượng là một bậc thần thánh, thậm chí còn đặt ra câu chuyện ông đã thay trời để phong thần.",
        source="dolly-voice-bank",
        license="unknown",
        tags=("xianxia", "male", "reliable", "solemn", "narrator"),
        notes="Ref text veneration/thần thánh — solemn reverent tone, hợp cảnh kể về nhân vật huyền thoại.",
    ),
    "dolly_humorous_elder": VieneuVoiceProfile(
        key="dolly_humorous_elder",
        label="Lão trưởng bối hài hước",
        speaker="dolly_humorous_elder",
        gender="male",
        ref_audio="wavs/dolly_humorous_elder.wav",
        ref_text="Phượng hoàng là vua trong bách điểu, cảnh bách điểu triều bái vừa rồi là một cảnh tượng hiếm có.",
        source="dolly-voice-bank",
        license="unknown",
        tags=("xianxia", "male", "elder", "humor", "narrator"),
        notes="Ref text bách điểu mythology — elder xianxia vibe với chút ấm áp.",
    ),
    "dolly_calm_leader": VieneuVoiceProfile(
        key="dolly_calm_leader",
        label="Lãnh đạo trầm tĩnh",
        speaker="dolly_calm_leader",
        gender="male",
        ref_audio="wavs/dolly_calm_leader.wav",
        ref_text="Cứ đến ngày Tết, dòng dõi của họ vẫn không quên cử người đến nhà thờ ông thắp hương kỷ niệm.",
        source="dolly-voice-bank",
        license="unknown",
        tags=("xianxia", "male", "calm", "leader", "narrator"),
        notes="Ceremonial/veneration tone — calm, measured, hợp long-form narrator.",
    ),
    "dolly_confident_man": VieneuVoiceProfile(
        key="dolly_confident_man",
        label="Nam giới tự tin",
        speaker="dolly_confident_man",
        gender="male",
        ref_audio="wavs/dolly_confident_man.wav",
        ref_text="Thưa thúc phụ, tôi được gặp chú ở tại Triều Ca này, thật hết sức vui mừng.",
        source="dolly-voice-bank",
        license="unknown",
        tags=("xianxia", "male", "confident", "clear", "narrator"),
        notes="Clear, grounded confident voice — Khương Tử Nha ref. Lighter than default.",
    ),
    "dolly_thoughtful_man": VieneuVoiceProfile(
        key="dolly_thoughtful_man",
        label="Người kể chuyện suy tư",
        speaker="dolly_thoughtful_man",
        gender="male",
        ref_audio="wavs/dolly_thoughtful_man.wav",
        ref_text="Cáo ôi! Nhà tao nghèo lắm chỉ có con gà dành cho ngày giỗ cha.",
        source="dolly-voice-bank",
        license="unknown",
        tags=("xianxia", "male", "thoughtful", "storyteller", "narrator"),
        notes="Fable/parable narration — measured storytelling pacing.",
    ),
    "dolly_male_narrator": VieneuVoiceProfile(
        key="dolly_male_narrator",
        label="Nam người kể chuyện",
        speaker="dolly_male_narrator",
        gender="male",
        ref_audio="wavs/dolly_male_narrator.wav",
        ref_text="",
        source="dolly-voice-bank",
        license="unknown",
        tags=("male", "narrator", "clear"),
        notes="Explicit narrator profile — designed for long-form narration.",
    ),
    "dolly_narrator": VieneuVoiceProfile(
        key="dolly_narrator",
        label="Người kể chuyện trung tính",
        speaker="dolly_narrator",
        gender="male",
        ref_audio="wavs/dolly_narrator.wav",
        ref_text="",
        source="dolly-voice-bank",
        license="unknown",
        tags=("male", "narrator", "neutral"),
        notes="Neutral narrator — clear diction, no strong character accent.",
    ),
    "dolly_distinguished_gentleman": VieneuVoiceProfile(
        key="dolly_distinguished_gentleman",
        label="Quý ông phong thái",
        speaker="dolly_distinguished_gentleman",
        gender="male",
        ref_audio="wavs/dolly_distinguished_gentleman.wav",
        ref_text="Khương Tử Nha hết sức vui mừng, bèn xuống tận thôn quê mua hai mươi con heo sống, đi bất kể ngày đêm.",
        source="dolly-voice-bank",
        license="unknown",
        tags=("xianxia", "male", "dignified", "narrator"),
        notes="Dignified tone — Khương Tử Nha ceremonial ref. Hợp nhân vật tu sĩ cao cấp.",
    ),
    # ── Dolly — female narrator candidates ──────────────────────────────────────
    "dolly_decisive_queen": VieneuVoiceProfile(
        key="dolly_decisive_queen",
        label="Nữ hoàng quả quyết",
        speaker="dolly_decisive_queen",
        gender="female",
        ref_audio="wavs/dolly_decisive_queen.wav",
        ref_text="Nghi biểu phải trang nghiêm đường hoàng, bình tĩnh không hấp tấp, khiến mọi người có cảm giác nhà vua cao siêu như bầu trời, thâm sâu như đáy biển.",
        source="dolly-voice-bank",
        license="unknown",
        tags=("xianxia", "female", "decisive", "imperial", "narrator"),
        notes="Imperial measured female — ref text hoàng đế/trời/biển. Hợp Korean LN female narrator.",
    ),
    "dolly_sage_woman": VieneuVoiceProfile(
        key="dolly_sage_woman",
        label="Nữ hiền nhân",
        speaker="dolly_sage_woman",
        gender="female",
        ref_audio="wavs/dolly_sage_woman.wav",
        ref_text="",
        source="dolly-voice-bank",
        license="unknown",
        tags=("xianxia", "female", "wise", "elder", "narrator"),
        notes="Wise elder female — hợp nhân vật nữ tu sĩ hoặc narrator nữ xianxia.",
    ),
    # ── PhoAudiobook (thivux/phoaudiobook) — sachnoiviet.net narration ─────────
    "phoaudiobook_lu_thu": VieneuVoiceProfile(
        key="phoaudiobook_lu_thu",
        label="PhoAudiobook Lữ Thứ — kể chuyện hắn/nàng",
        speaker="Lữ_Thứ",
        gender="male",
        ref_audio="voice_bank/phoaudiobook/lữ_thứ/vieneu_lữ_thứ_0001.wav",
        ref_text=(
            "như những lần hắn gặp trước ở trại quy nhơn, đôi mắt nhìn hắn thương xót "
            "và đau đớn như chính nàng đang bị hành hạ. khuôn mặt ấy đã chết hẳn nụ cười."
        ),
        source="thivux/phoaudiobook",
        license="unknown",
        tags=("xianxia", "male", "narrator", "phoaudiobook", "clone"),
        notes=(
            "Top PhoAudiobook survey pick: avg_quality 9.19, 370 narrative hits. "
            "Full bank: voice_bank/phoaudiobook/lữ_thứ/ (~25 min, 131 clips)."
        ),
    ),
    "phoaudiobook_le_quyen": VieneuVoiceProfile(
        key="phoaudiobook_le_quyen",
        label="PhoAudiobook Lệ Quyên — cung đình trang nghiêm",
        speaker="Lệ_Quyên",
        gender="female",
        ref_audio="voice_bank/phoaudiobook/lệ_quyên/vieneu_lệ_quyên_0000.wav",
        ref_text=(
            "đã lâu bệ hạ không đến tây cung, khiến cho thần thiếp vô cùng lo lắng. "
            "chẳng hay việc quân quốc đại sự bận rộn lắm sao?"
        ),
        source="thivux/phoaudiobook",
        license="unknown",
        tags=("xianxia", "female", "imperial", "narrator", "phoaudiobook", "clone"),
        notes=(
            "Highest avg_quality (9.38) in PhoAudiobook survey; cung đình/bệ hạ prose. "
            "Export with: download_vieneu_voice_bank.py --dataset phoaudiobook --speaker Lệ_Quyên"
        ),
    ),
    "phoaudiobook_bach_diep": VieneuVoiceProfile(
        key="phoaudiobook_bach_diep",
        label="PhoAudiobook Bách Diệp — kể chuyện căng thẳng",
        speaker="Bách_Diệp",
        gender="male",
        ref_audio="voice_bank/phoaudiobook/bách_diệp/vieneu_bách_diệp_0000.wav",
        ref_text="máy quay đều rõ ràng. hơn nữa; hắn muốn giết ai; muốn giết lúc nào đều có thể đạt thành mục tiêu.",
        source="thivux/phoaudiobook",
        license="unknown",
        tags=("xianxia", "male", "thriller", "narrator", "phoaudiobook", "clone"),
        notes="Large usable bank (~207 min); strong hắn-in-prose hits. Export on demand.",
    ),
    # ── VieNeu-TTS-140h voice bank — unregistered speakers ──────────────────────
    "vieneu_capybara1812_0027": VieneuVoiceProfile(
        key="vieneu_capybara1812_0027",
        label="Capybara 0027 — năng động",
        speaker="capybara1812_0027",
        gender="male",
        ref_audio="voice_bank/vieneu/capybara1812_0027/vieneu_capybara1812_0027_0000.wav",
        ref_text="Khi hỏi những điều này, bạn hãy tự hỏi chính mình và tự đánh giá mức độ tự tin, nhiệt huyết, mục tiêu xác định hay mục tiêu chính trong cuộc đời bạn.",
        source="pnnbao-ump/VieNeu-TTS-140h",
        license="Apache-2.0",
        tags=("male", "energetic", "motivational", "narrator"),
        notes="Motivational/self-help speaker — lighter than 0048, possibly less breathy.",
    ),
    "vieneu_capybara1812_1003": VieneuVoiceProfile(
        key="vieneu_capybara1812_1003",
        label="Capybara 1003 — tường thuật",
        speaker="capybara1812_1003",
        gender="male",
        ref_audio="voice_bank/vieneu/capybara1812_1003/vieneu_capybara1812_1003_0000.wav",
        ref_text="Lão Grunewald có thể nhìn từ sân tennis tới sân gôn, hồ tắm, và bảy cái villa lấp lánh như cung điện mới xây.",
        source="pnnbao-ump/VieNeu-TTS-140h",
        license="Apache-2.0",
        tags=("male", "narrative", "sophisticated", "narrator"),
        notes="Crime fiction narrative text — sophisticated, lighter than capybara_0048.",
    ),
    "vieneu_capybara1812_1017": VieneuVoiceProfile(
        key="vieneu_capybara1812_1017",
        label="Capybara 1017 — kể chuyện Nam Bộ",
        speaker="capybara1812_1017",
        gender="male",
        ref_audio="voice_bank/vieneu/capybara1812_1017/vieneu_capybara1812_1017_0000.wav",
        ref_text="Phía ngoài, một bên có lót một bộ ván dầu nhỏ để nằm chơi, còn một bên để bàn lóc nhóc.",
        source="pnnbao-ump/VieNeu-TTS-140h",
        license="Apache-2.0",
        tags=("male", "southern-vn", "warm", "narrator"),
        notes="Southern Vietnamese storytelling warmth — folksy, organic tone.",
    ),
    "vieneu_jellyfish1010_0028": VieneuVoiceProfile(
        key="vieneu_jellyfish1010_0028",
        label="Jellyfish 0028 — rõ ràng",
        speaker="jellyfish1010_0028",
        gender="male",
        ref_audio="voice_bank/vieneu/jellyfish1010_0028/vieneu_jellyfish1010_0028_0000.wav",
        ref_text="Theo Charles C. Manz, tác giả cuốn sách về lãnh đạo bản thân trong thời đại biến động.",
        source="pnnbao-ump/VieNeu-TTS-140h",
        license="Apache-2.0",
        tags=("male", "clear", "journalism", "narrator"),
        notes="Biography/journalism speaker — very clear diction, neutral tone.",
    ),
    "vieneu_alloy1512_1005": VieneuVoiceProfile(
        key="vieneu_alloy1512_1005",
        label="Alloy 1005 — nữ sinh động",
        speaker="alloy1512_1005",
        gender="female",
        ref_audio="voice_bank/vieneu/alloy1512_1005/vieneu_alloy1512_1005_0000.wav",
        ref_text="Mọi người xúm đen xúm đỏ vào đỡ bác, vài người gọi bác là mẹ ơi, vài người khóc sụt sùi.",
        source="pnnbao-ump/VieNeu-TTS-140h",
        license="Apache-2.0",
        tags=("female", "lively", "storyteller", "narrator"),
        notes="Female lively narrator from 202-sample speaker set — larger pool than alloy_1002.",
    ),
    # ── VieNeu-TTS v3 Turbo built-in preset voices (no WAV needed) ─────────────
    # These use dedicated speaker tokens trained into the model.
    # Recommended for main narrator — more consistent than single-WAV cloning.
    "preset_binh_an": VieneuVoiceProfile(
        key="preset_binh_an",
        label="Bình An — điềm đạm (preset)",
        speaker="Bình An",
        gender="male",
        ref_audio="",
        ref_text="",
        preset_voice="Bình An",
        source="VieNeu-TTS-v3-Turbo built-in",
        license="VieNeu",
        tags=("xianxia", "male", "calm", "narrator", "preset"),
        notes="Built-in preset — điềm đạm; dự phòng nếu Trọng Hữu quá trầm.",
    ),
    "preset_trong_huu": VieneuVoiceProfile(
        key="preset_trong_huu",
        label="Trọng Hữu — uyên bác (preset)",
        speaker="Trọng Hữu",
        gender="male",
        ref_audio="",
        ref_text="",
        preset_voice="Trọng Hữu",
        source="VieNeu-TTS-v3-Turbo built-in",
        license="VieNeu",
        tags=("xianxia", "male", "scholarly", "narrator", "preset"),
        notes="Built-in preset — uyên bác, hợp tiên hiệp/tu luyện long-form. BetterBox default narrator.",
    ),
    "preset_thai_son": VieneuVoiceProfile(
        key="preset_thai_son",
        label="Thái Sơn — chắc khỏe (preset)",
        speaker="Thái Sơn",
        gender="male",
        ref_audio="",
        ref_text="",
        preset_voice="Thái Sơn",
        source="VieNeu-TTS-v3-Turbo built-in",
        license="VieNeu",
        tags=("xianxia", "male", "strong", "warrior", "preset"),
        notes="Built-in preset — chắc khỏe, hợp cảnh chiến đấu/nhân vật mạnh.",
    ),
    "preset_gia_bao": VieneuVoiceProfile(
        key="preset_gia_bao",
        label="Gia Bảo — mượt mà (preset)",
        speaker="Gia Bảo",
        gender="male",
        ref_audio="",
        ref_text="",
        preset_voice="Gia Bảo",
        source="VieNeu-TTS-v3-Turbo built-in",
        license="VieNeu",
        tags=("male", "smooth", "narrator", "preset"),
        notes="Built-in preset — giọng nam mượt, candidate nếu Bình An quá trầm.",
    ),
    "preset_duc_tri": VieneuVoiceProfile(
        key="preset_duc_tri",
        label="Đức Trí — rõ ràng (preset)",
        speaker="Đức Trí",
        gender="male",
        ref_audio="",
        ref_text="",
        preset_voice="Đức Trí",
        source="VieNeu-TTS-v3-Turbo built-in",
        license="VieNeu",
        tags=("male", "clear", "narrator", "preset"),
        notes="Built-in preset — rõ ràng, dự phòng narrator.",
    ),
    "preset_xuan_vinh": VieneuVoiceProfile(
        key="preset_xuan_vinh",
        label="Xuân Vĩnh — vui tươi (preset)",
        speaker="Xuân Vĩnh",
        gender="male",
        ref_audio="",
        ref_text="",
        preset_voice="Xuân Vĩnh",
        source="VieNeu-TTS-v3-Turbo built-in",
        license="VieNeu",
        tags=("male", "bright", "lively", "preset"),
        notes="Built-in preset — vui tươi, hợp content ngắn hơn long-form tiên hiệp.",
    ),
    "preset_ngoc_lan": VieneuVoiceProfile(
        key="preset_ngoc_lan",
        label="Ngọc Lan — dịu dàng (preset)",
        speaker="Ngọc Lan",
        gender="female",
        ref_audio="",
        ref_text="",
        preset_voice="Ngọc Lan",
        source="VieNeu-TTS-v3-Turbo built-in",
        license="VieNeu",
        tags=("xianxia", "female", "gentle", "narrator", "preset"),
        notes="Built-in preset — dịu dàng, hợp truyện romance/nữ nhân vật chính.",
    ),
    "preset_my_duyen": VieneuVoiceProfile(
        key="preset_my_duyen",
        label="Mỹ Duyên — mượt mà (preset)",
        speaker="Mỹ Duyên",
        gender="female",
        ref_audio="",
        ref_text="",
        preset_voice="Mỹ Duyên",
        source="VieNeu-TTS-v3-Turbo built-in",
        license="VieNeu",
        tags=("female", "smooth", "narrator", "preset"),
        notes="Built-in preset — nữ mượt, dùng khi cần narrator nữ mềm hơn.",
    ),
    "preset_truc_ly": VieneuVoiceProfile(
        key="preset_truc_ly",
        label="Trúc Ly — trẻ trung (preset)",
        speaker="Trúc Ly",
        gender="female",
        ref_audio="",
        ref_text="",
        preset_voice="Trúc Ly",
        source="VieNeu-TTS-v3-Turbo built-in",
        license="VieNeu",
        tags=("female", "young", "bright", "preset"),
        notes="Built-in preset — trẻ trung, ít phù hợp hơn cho narrator nghe lâu.",
    ),
    "preset_ngoc_linh": VieneuVoiceProfile(
        key="preset_ngoc_linh",
        label="Ngọc Linh — tươi sáng (preset)",
        speaker="Ngọc Linh",
        gender="female",
        ref_audio="",
        ref_text="",
        preset_voice="Ngọc Linh",
        source="VieNeu-TTS-v3-Turbo built-in",
        license="VieNeu",
        tags=("female", "bright", "preset"),
        notes="Built-in default của VieNeu package — sáng, hợp demo/content ngắn.",
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
    if profile.is_preset:
        # Preset profiles use a built-in model token — no WAV file needed.
        return profile
    if not profile.ref_audio_path.exists():
        raise FileNotFoundError(
            f"VieNeu voice profile {key!r} reference audio is missing: {profile.ref_audio_path}. "
            "Export/copy the voice bank locally before using this profile."
        )
    return profile


def resolve_vieneu_reference_kwargs(key: str) -> dict:
    """Return kwargs for tts.synthesize(): either {'voice': preset} or {'ref_audio': path, 'ref_text': text}."""
    profile = resolve_vieneu_voice_profile(key)
    if profile.is_preset:
        return {"voice": profile.preset_voice}
    kwargs: dict = {"ref_audio": profile.ref_audio_path.as_posix()}
    if profile.ref_text:
        kwargs["ref_text"] = profile.ref_text
    return kwargs
