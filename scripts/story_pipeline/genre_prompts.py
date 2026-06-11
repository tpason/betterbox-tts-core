#!/usr/bin/env python3
"""
Genre-aware prompt augmentation for translate and polish scripts.

Usage:
    from genre_prompts import detect_genre, get_polish_genre_addendum, get_translate_genre_addendum

    genre = detect_genre(story.get("category", ""))
    addendum = get_polish_genre_addendum(genre)
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

GENRE_TIEN_HIEP = "tien_hiep"
GENRE_HUYEN_HUYEN = "huyen_huyen"
GENRE_HE_THONG = "he_thong"
GENRE_KIEM_HIEP = "kiem_hiep"
GENRE_DO_THI = "do_thi"
GENRE_XUYEN_KHONG = "xuyen_khong"
GENRE_MAT_THE = "mat_the"
GENRE_VONG_DU = "vong_du"
GENRE_LANG_MAN = "lang_man"
GENRE_WESTERN_FANTASY = "western_fantasy"
GENRE_KOREAN_CULTIVATION = "korean_cultivation"

# Source codes known to produce Korean-language content.
_KO_SOURCE_CODES: frozenset[str] = frozenset({"naver", "naver_series", "kakao", "kakaopage"})

# English web-novel sources — default to western_fantasy when no genre signal.
_EN_SOURCE_CODES: frozenset[str] = frozenset({
    "royalroad", "wetriedtls", "skydemonorder", "lightnovelpub",
    "novelbin", "freewebnovel", "novelfire", "novelhub", "fanmtl",
})

# Explicit korean_cultivation signals — Korean cultivation/murim novels:
# Hán Việt cultivation terms + modern Korean LN narration. Checked BEFORE
# western_fantasy so "korean cultivation" is not swallowed by "korean novel".
_KOREAN_CULTIVATION_STRONG_KW: list[str] = [
    "korean cultivation",
    "korean xianxia",
    "korean murim",
    "tu tiên hàn",
    "tu tiên kiểu hàn",
    "murim",
    "võ lâm hàn",
]

# Explicit western_fantasy signals — checked before language heuristics.
_WESTERN_FANTASY_STRONG_KW: list[str] = [
    "western fantasy",
    "korean light novel",
    "korean web novel",
    "korean fantasy",
    "korean novel",
    "light novel",        # LN format = Japanese/Korean, not Chinese web novel
    "naver series",
    "trung cổ phương tây",
    "fantasy kiểu korean",
]

# Priority order: first match wins.
# western_fantasy is listed BEFORE huyen_huyen so "western fantasy" / "light novel"
# keywords match correctly without being swallowed by the generic "fantasy" keyword.
_GENRE_KEYWORDS: list[tuple[list[str], str]] = [
    (["tiên hiệp", "tu tiên", "tu chân", "tien hiep", "xianxia", "tiêu dao", "tiên ma"], GENRE_TIEN_HIEP),
    (["kiếm hiệp", "võ hiệp", "giang hồ", "wuxia", "kiem hiep", "cổ đại kiếm hiệp"], GENRE_KIEM_HIEP),
    # western_fantasy before huyen_huyen — specific terms must win
    (["western fantasy", "korean light novel", "korean web novel", "korean fantasy",
      "korean novel", "light novel", "naver series", "trung cổ phương tây",
      "fantasy kiểu korean"], GENRE_WESTERN_FANTASY),
    # "fantasy" alone → huyen_huyen for Chinese; language-bias handles Korean/English (see detect_genre)
    (["huyền huyễn", "huyen huyen", "xuanhuan", "dị thế giới", "dị giới", "fantasy"], GENRE_HUYEN_HUYEN),
    (["hệ thống", "he thong", "litrpg", "progression fantasy", "system"], GENRE_HE_THONG),
    (["võng du", "vong du", "game", "vrmmo", "mmo"], GENRE_VONG_DU),
    (["xuyên không", "xuyên nhanh", "trọng sinh", "isekai", "reincarnation", "rebirth"], GENRE_XUYEN_KHONG),
    (["mạt thế", "tận thế", "apocalypse", "zombie", "dị năng", "biến dị", "sinh tồn", "mat the"], GENRE_MAT_THE),
    (["đô thị", "do thi", "hiện đại", "đô thị huyền huyễn", "urban", "đô thị tu tiên"], GENRE_DO_THI),
    (["lãng mạn", "lang man", "ngôn tình", "romance", "ngôn tình hiện đại"], GENRE_LANG_MAN),
]


def detect_genre(category: str, raw_language: str = "", source_code: str = "") -> str:
    """Detect genre from category string with optional language/source awareness.

    Args:
        category:     Comma/semicolon-separated tag string from the story record.
        raw_language: 2-char language code ("ko", "zh", "en", "vi", …).
        source_code:  Pipeline source code, e.g. "naver", "hako", "qidian".

    Korean/English bias:
      - "fantasy" alone for Korean/English source → western_fantasy (not huyen_huyen).
      - Korean source with no recognised genre keyword → defaults to western_fantasy.
    Chinese bias:
      - Falls through to standard keyword list; "fantasy" → huyen_huyen.
    """
    normalized = (category or "").lower().strip()
    lang = (raw_language or "").lower()[:2]   # "ko", "zh", "en", …
    src = (source_code or "").lower()

    is_korean = lang == "ko" or src in _KO_SOURCE_CODES
    is_english = lang == "en" or src in _EN_SOURCE_CODES

    # Step 0: Explicit korean_cultivation keywords win regardless of language.
    for kw in _KOREAN_CULTIVATION_STRONG_KW:
        if kw in normalized:
            return GENRE_KOREAN_CULTIVATION

    # Step 1: Explicit western_fantasy keywords win regardless of language.
    for kw in _WESTERN_FANTASY_STRONG_KW:
        if kw in normalized:
            return GENRE_WESTERN_FANTASY

    # Step 2: Language/source-aware override for ambiguous keywords.
    if is_korean or is_english:
        # Cultivation/xianxia signals from Korean/English source → korean_cultivation
        # (Hán Việt terms, modern narration) instead of Chinese tien_hiep.
        if any(kw in normalized for kw in ("cultivation", "xianxia", "tu tiên", "tu chân", "tien hiep")):
            return GENRE_KOREAN_CULTIVATION
        # "fantasy", "web novel", "academy" for Korean/English → western_fantasy.
        if any(kw in normalized for kw in ("fantasy", "web novel", "academy", "magic")):
            return GENRE_WESTERN_FANTASY

    # Step 3: Standard keyword matching (first match wins).
    for keywords, genre in _GENRE_KEYWORDS:
        for kw in keywords:
            if kw in normalized:
                return genre

    # Step 4: Korean or English web-novel source with no specific genre signal → western_fantasy.
    if is_korean or is_english:
        return GENRE_WESTERN_FANTASY

    return ""


def resolve_genre_from_context(
    category: str = "",
    raw_language: str = "",
    source_code: str = "",
    char_map_file: str = "",
    char_map: str = "",
) -> str:
    """Resolve genre from DB metadata and optional character-map story rules.

    Metadata wins when it is specific. Character map fills missing metadata and can
    override ambiguous `fantasy` → `huyen_huyen` guesses for Western/Korean stories.
    """
    detected = detect_genre(category, raw_language=raw_language, source_code=source_code)
    map_text = char_map or load_char_map(char_map_file)
    inferred = infer_genre_from_char_map(map_text) if map_text else ""

    if inferred and (not detected or detected == GENRE_HUYEN_HUYEN):
        return inferred
    return detected or inferred


# ─── Polish prompt augmentation ────────────────────────────────────────────────
# Empty string → no augmentation (base prompt already handles that genre well).

_POLISH_GENRE_ADDENDUM: dict[str, str] = {
    GENRE_TIEN_HIEP: """\
Thể loại: Tiên hiệp / Tu tiên / Xianxia.
- Văn phong cổ phong có khí thế; từ Hán Việt trang trọng nhưng không nặng nề, không khó nghe.
- Cảnh tu luyện và đột phá: câu dài tả khí tức, ánh sáng, cảm giác linh lực; kết bằng câu ngắn mạnh mẽ để nhấn cao trào.
- Cảnh chiến đấu: câu ngắn, nhịp nhanh, động từ mạnh (vung, chém, xé, bùng, vỡ...); tượng thanh cho âm thanh chiến đấu.
- Cảnh tả phong cảnh: có chiều sâu, chi tiết về màu sắc, ánh sáng, khí tức; không liệt kê khô khan.
- Cảnh nội tâm: giữ sắc thái suy tư, quyết tâm hoặc phân vân — không làm phẳng cảm xúc.\
""",

    GENRE_HUYEN_HUYEN: """\
Thể loại: Huyền huyễn / Fantasy đa thế giới.
- Linh hoạt hơn tiên hiệp với tên kỹ năng và vật phẩm ngoại lai; có thể giữ nguyên tên tiếng Anh nếu phổ biến.
- Giữ thuật ngữ cảnh giới, kỹ năng, phe phái nhất quán trong cùng chương.
- Sắc thái Hán Việt vừa phải; không quá cổ phong như tiên hiệp thuần túy.\
""",

    GENRE_HE_THONG: """\
Thể loại: Hệ thống / System fiction.
- Bảng thuộc tính và thông báo hệ thống: ưu tiên chuyển thành câu kể mạch lạc, giữ đủ từng mục.
- Chỉ số hệ thống đọc bằng tiếng Việt: EXP/kinh nghiệm, HP/máu hoặc thể lực, Level/cấp độ, Skill/kỹ năng.
- MP đọc là "linh lực" nếu bối cảnh Trung/tu luyện; đọc là "mana" hoặc "năng lượng" nếu là LitRPG, fantasy phương Tây hoặc Korean fantasy.
- Chỉ dùng sắc thái Hán Việt/cổ phong khi hệ thống thuộc truyện Trung/tu luyện; không ép Hán Việt cho truyện văn phòng, học viện, LitRPG hoặc fantasy Hàn/Tây.
- Thông báo âm thanh (Đinh!, Tinh!) tách dòng riêng, sau đó là nội dung thông báo.
- Giọng văn hệ thống ngắn gọn, khách quan; giọng nhân vật tự nhiên, sinh động.\
""",

    GENRE_KIEM_HIEP: """\
Thể loại: Kiếm hiệp / Võ hiệp.
- Giảm Hán Việt nặng so với tiên hiệp; câu văn lưu loát, dễ nghe.
- Chiêu thức, bí kíp, nội công giữ âm Hán Việt nhưng không quá khó nghe.
- Danh xưng võ lâm, bang phái, môn phái theo âm Hán Việt chuẩn.
- Ngôi kể: "hắn", "lão", "y" phù hợp; xưng hô giữ đúng quan hệ và bối cảnh giang hồ.\
""",

    GENRE_DO_THI: """\
Thể loại: Đô thị / Hiện đại. Các quy tắc sau ghi đè quy tắc chung khi mâu thuẫn:
- Không dùng từ cổ phong hoặc Hán Việt không cần thiết.
- Xưng hô hiện đại: "anh/chị/em/họ/cô ấy/anh ấy" thay vì "hắn/nàng/y/lão".
- Văn phong tự nhiên như văn xuôi hiện đại tiếng Việt; câu không nặng nề.
- Tên thương hiệu, địa danh, tổ chức hiện đại giữ nguyên hoặc phiên âm thông dụng.\
""",

    GENRE_XUYEN_KHONG: """\
Thể loại: Xuyên không / Trọng sinh.
- Điều chỉnh văn phong theo bối cảnh: cổ đại dùng Hán Việt và xưng hô cổ, hiện đại dùng văn phong bình thường.
- Nhân vật chính mang tư duy hiện đại; giữ giọng điệu thông minh, linh hoạt, đôi khi hóm hỉnh.
- Không cứng nhắc áp một giọng văn duy nhất — theo sát bối cảnh từng cảnh.\
""",

    GENRE_MAT_THE: """\
Thể loại: Mạt thế / Tận thế / Sinh tồn. Các quy tắc sau ghi đè quy tắc chung khi mâu thuẫn:
- Câu ngắn, khẩn trương, trực tiếp trong cảnh hành động và nguy hiểm.
- Không cần sắc thái Hán Việt; ngôn ngữ gần tiếng Việt thông thường, sắc bén.
- Từ ngữ về chiến đấu, sinh tồn, biến thể phải rõ nghĩa, không mơ hồ.
- Không làm mềm hoặc nhẹ hóa ngôn ngữ khi mô tả cảnh bạo lực hay sinh tử.\
""",

    GENRE_VONG_DU: """\
Thể loại: Võng du / Game VRMMO.
- Bảng chỉ số và thông báo hệ thống game: HP → máu, MP → mana/năng lượng, EXP → kinh nghiệm, Level → cấp, Skill → kỹ năng, Quest → nhiệm vụ.
- Tên kỹ năng/skill: giữ nguyên tên gốc nếu phổ biến, hoặc dịch + ghi tên gốc trong ngoặc lần đầu xuất hiện.
- Thông báo hệ thống game giữ giọng khô khan, ngắn gọn; không làm mềm quá.
- Xen lẫn ngôn ngữ game (interface) và ngôn ngữ nhân vật tự nhiên.\
""",

    GENRE_LANG_MAN: """\
Thể loại: Lãng mạn / Ngôn tình / Romance. Các quy tắc sau ghi đè quy tắc chung khi mâu thuẫn:
- Không dùng "hắn" — thay bằng tên nhân vật hoặc "anh/anh ấy/cô ấy" phù hợp ngữ cảnh.
- Văn phong nhẹ nhàng, giàu cảm xúc, tinh tế; tránh từ cứng nhắc hoặc nặng nề.
- Đối thoại mềm mại, chú ý sắc thái cảm xúc; không rút gọn cảnh tình cảm hay nội tâm nhân vật.
- Giữ sự lãng mạn và tinh tế của nguyên bản; tránh dịch quá literal.\
""",

    GENRE_WESTERN_FANTASY: """\
Thể loại: Fantasy phương Tây / Korean light novel.
- Không dùng văn phong tiên hiệp, cổ phong hoặc Hán Việt nặng nếu nguyên tác không có.
- Văn kể ngôi 3: "anh", "anh ta", "cô", "cô ta", "cậu" — tránh "hắn", "nàng", "lão", "y" trừ khi character map chỉ định. ĐÂY LÀ QUY TẮC CHO VĂN KỂ, không áp vào lời thoại.
- Lời thoại thù địch — kẻ tấn công, băng hội, kẻ lạ mặt hung hãn, người chuẩn bị rút vũ khí: KHÔNG dùng "anh/bạn/cậu" khi gọi người đối diện — dùng "mày", "tên kia", "thằng kia", lược đại từ, hoặc gọi thẳng vai trò. Tự xưng bằng "ta" (kiêu ngạo) hoặc "tao" (thô lỗ). Tuyệt đối không dùng "ngươi/mi" (đó là khẩu khí tiên hiệp, không phù hợp).
- Đối thoại nhân vật lính/hiệp sĩ: tự nhiên, ngắn, không thêm khẩu khí tu tiên như "bổn tọa", "lão tử".
- Giữ tên riêng, địa danh, tổ chức, vật phẩm theo character map; không Hán Việt hóa tên Tây.
- Cảnh chiến đấu: câu ngắn, động từ rõ; cảnh nội tâm: giọng quan sát, phân tích, không hoa mỹ.\
""",

    GENRE_KOREAN_CULTIVATION: """\
Thể loại: Tu tiên Hàn Quốc (Korean cultivation) — thuật ngữ tu luyện Hán Việt + văn kể hiện đại Korean LN. Các quy tắc sau ghi đè quy tắc chung khi mâu thuẫn:
- THUẬT NGỮ TU LUYỆN BẮT BUỘC dùng âm Hán Việt chuẩn của truyện tu tiên: cảnh giới, công pháp, pháp bảo, tông môn, bí kíp. KHÔNG dịch nghĩa từng chữ ("Tam Hoa Tụ Đỉnh" — KHÔNG phải "ba hoa hội tụ đỉnh cao"; "Ngũ Khí Triều Nguyên" — KHÔNG phải "năm năng lực hợp nhất về nguồn").
- Tên bí kíp/sách/công pháp: Hán Việt trang trọng, có thể đặt trong 《 》("Siêu Việt Tu Chân Lục" — KHÔNG phải "Kỷ Lục Vượt Qua Tu Chân").
- "cultivator" = "tu sĩ" (KHÔNG phải "tu luyện giả"); giữ nhất quán: linh khí, linh căn, tu vi, cảnh giới, đan dược, kiếm ý, đạo tâm.
- VĂN KỂ ngôi 3 vẫn hiện đại kiểu Korean LN: "anh ta", "cô ta", "cậu", tên nhân vật — tránh "hắn/nàng/lão/y" trừ khi character map chỉ định. Ngôi 1: "tôi".
- Lời thoại thù địch: "mày", "tên kia", "thằng kia" hoặc lược đại từ; tự xưng "ta" (kiêu ngạo) hoặc "tao" (thô lỗ). KHÔNG dùng "ngươi/mi" trừ khi nhân vật cổ phong/tiền bối tu tiên thực sự nói giọng cổ.
- Tên người Hàn giữ phiên âm Hàn (Seo Eun-Hyun, Kim Young-hoon) — KHÔNG Hán Việt hóa tên người.
- Cảnh tu luyện/đột phá: câu dài tả khí tức, linh khí, cảm giác; kết câu ngắn mạnh để nhấn cao trào. Cảnh chiến đấu: câu ngắn, động từ mạnh.\
""",
}


# ─── Translate prompt augmentation ─────────────────────────────────────────────

_TRANSLATE_GENRE_ADDENDUM: dict[str, str] = {
    GENRE_TIEN_HIEP: """\
Thể loại: Tiên hiệp / Tu tiên / Xianxia.
- Dùng âm Hán Việt chuẩn cho cảnh giới tu luyện: 炼气(期) → Luyện Khí (kỳ), 筑基 → Trúc Cơ, 金丹 → Kim Đan, 元婴 → Nguyên Anh, 化神 → Hóa Thần, 炼虚 → Luyện Hư, 合体 → Hợp Thể, 大乘 → Đại Thừa, 渡劫 → Độ Kiếp, 散仙 → Tán Tiên, 地仙 → Địa Tiên, 天仙 → Thiên Tiên.
- Tên tông môn, phái, địa danh: âm Hán Việt. Ví dụ: 天剑宗 → Thiên Kiếm Tông, 青云门 → Thanh Vân Môn.
- Công pháp, pháp bảo, pháp khí, thần thông giữ âm Hán Việt khi có thể đọc được.
- Giữ sắc thái trang nghiêm, cổ phong. Ngôi kể thứ ba: "hắn", "nàng", "lão", "y", "gã".
- Không hiện đại hóa từ thuộc bối cảnh tu tiên. Không dùng "anh ấy", "cô ấy" thay thế.\
""",

    GENRE_HUYEN_HUYEN: """\
Thể loại: Huyền huyễn / Xuanhuan / Fantasy.
- Giữ thuật ngữ cảnh giới, kỹ năng, vật phẩm nhất quán trong cùng chương.
- Tên kỹ năng/vật phẩm tiếng Anh phổ biến có thể giữ nguyên hoặc phiên âm + ghi tên gốc.
- Sắc thái Hán Việt vừa phải; không cứng nhắc như tiên hiệp thuần.
- Ngôi kể: "hắn", "nàng", "y" trong văn kể thứ ba.\
""",

    GENRE_HE_THONG: """\
Thể loại: Hệ thống / System fiction.
- Thông báo hệ thống trong 【】 hoặc [] dịch rõ ràng, ngắn gọn; giữ cấu trúc UI.
- Bảng thuộc tính dịch đủ từng mục; ưu tiên chuyển thành câu kể cho TTS.
- Chỉ số hệ thống: EXP/经验 → kinh nghiệm, HP/血量 → máu hoặc thể lực, Level/等级 → cấp độ, Skill/技能 → kỹ năng.
- MP → "linh lực" nếu bối cảnh Trung/tu luyện; MP → "mana" hoặc "năng lượng" nếu là LitRPG/fantasy Hàn/Tây.
- Hán Việt chỉ áp dụng cho hệ thống Trung/tu luyện. Với truyện văn phòng Hàn, học viện, LitRPG hoặc western fantasy, giữ tên người/địa danh/tổ chức theo source/character map và dùng văn hiện đại/fantasy tự nhiên.
- Âm hiệu hệ thống (叮!/당!) dịch thành "Đinh!" hoặc "Tinh!".
- Giọng văn hệ thống khô khan, khách quan; giọng nhân vật sinh động, tự nhiên.\
""",

    GENRE_KIEM_HIEP: """\
Thể loại: Kiếm hiệp / Võ hiệp / Wuxia.
- Văn phong kiếm hiệp truyền thống; giữ danh xưng võ lâm, bang phái theo âm Hán Việt.
- Ít Hán Việt nặng hơn tiên hiệp; câu lưu loát, dễ nghe.
- Chiêu thức, bí kíp, nội công, khinh công dịch theo âm Hán Việt.
- Bối cảnh thường là cổ đại Trung Hoa; giữ xưng hô và lễ nghi phù hợp.\
""",

    GENRE_DO_THI: """\
Thể loại: Đô thị / Hiện đại.
- Văn phong hiện đại, tự nhiên. Không dùng từ Hán Việt cổ không cần thiết.
- Tên địa danh, thương hiệu, tổ chức giữ nguyên hoặc phiên âm thông dụng.
- Xưng hô hiện đại: "anh/chị/em/họ" thay vì "hắn/nàng/y".
- Nếu có yếu tố huyền huyễn đô thị: giữ thuật ngữ năng lực theo phong cách hiện đại, không cổ phong.\
""",

    GENRE_XUYEN_KHONG: """\
Thể loại: Xuyên không / Trọng sinh / Isekai.
- Bối cảnh có thể xen lẫn hiện đại và cổ đại; điều chỉnh văn phong theo từng cảnh.
- Nhân vật chính mang tư duy hiện đại; giữ giọng điệu tự nhiên, thông minh, đôi khi hóm hỉnh.
- Cổ đại: dùng xưng hô và văn phong phù hợp. Hiện đại: văn phong bình thường.\
""",

    GENRE_MAT_THE: """\
Thể loại: Mạt thế / Tận thế / Apocalypse.
- Văn phong khẩn trương, mạnh mẽ, trực tiếp. Câu ngắn trong cảnh hành động căng thẳng.
- Từ về sinh tồn, chiến đấu, biến thể, dị năng cần rõ nghĩa, không mơ hồ.
- Không cần sắc thái Hán Việt; ngôn ngữ gần tiếng Việt thông thường, sắc bén.
- Xưng hô linh hoạt: "hắn" trong văn kể thứ ba, tên nhân vật trong cảnh hành động gấp.\
""",

    GENRE_VONG_DU: """\
Thể loại: Võng du / Game / VRMMO.
- UI game dịch rõ: HP → máu, MP → mana/năng lượng, EXP → kinh nghiệm, Level → cấp, Skill → kỹ năng, Quest → nhiệm vụ, Boss → trùm.
- Tên kỹ năng tiếng Anh phổ biến có thể giữ nguyên hoặc dịch + ghi tên gốc trong ngoặc lần đầu.
- Thông báo hệ thống game: ngắn gọn, khô khan, rõ nghĩa; bảng chỉ số dịch đủ từng dòng.
- Xen lẫn ngôn ngữ thế giới game và ngôn ngữ nhân vật đời thường.\
""",

    GENRE_LANG_MAN: """\
Thể loại: Lãng mạn / Ngôn tình / Romance.
- Văn phong nhẹ nhàng, giàu cảm xúc, tinh tế. Đối thoại mềm mại, chú ý sắc thái tình cảm.
- Không dịch cứng literal; ưu tiên giữ cảm xúc và sắc thái của nguyên bản.
- Xưng hô theo quan hệ nhân vật: "anh/em", "anh ấy/cô ấy", hoặc tên nhân vật.
- Không rút gọn cảnh mô tả cảm xúc, nội tâm, hay đối thoại tình cảm.\
""",

    GENRE_WESTERN_FANTASY: """\
Thể loại: Fantasy phương Tây / Korean light novel.
- Không dùng văn phong tiên hiệp hoặc Hán Việt cổ phong nếu nguyên tác không có.
- Tên Tây, địa danh, tổ chức, danh hiệu giữ nhất quán theo character map.
- Văn kể ngôi 3: "anh", "anh ta", "cô", "cô ta", "cậu" — tránh "hắn/nàng/lão/y" trừ khi map yêu cầu. ĐÂY LÀ QUY TẮC CHO VĂN KỂ, không áp vào lời thoại.
- Lời thoại thù địch — kẻ tấn công, băng hội, kẻ lạ mặt hung hãn, kẻ khiêu khích: KHÔNG dùng "anh/bạn/cậu" khi gọi người đối diện — dùng "mày", "tên kia", "thằng kia", lược đại từ. Tự xưng bằng "ta" hoặc "tao". KHÔNG "ngươi/mi" (đó là khẩu khí tiên hiệp).
- Giọng nhân vật lính/hiệp sĩ: ngắn, chắc, ít cảm thán; không thêm khẩu khí tu tiên.
- Ưu tiên văn xuôi fantasy trung cổ phương Tây tự nhiên, dễ nghe khi đọc audio.\
""",

    GENRE_KOREAN_CULTIVATION: """\
Thể loại: Tu tiên Hàn Quốc (Korean cultivation) — thuật ngữ tu luyện Hán Việt + văn kể hiện đại Korean LN.
- THUẬT NGỮ TU LUYỆN dùng âm Hán Việt chuẩn, KHÔNG dịch nghĩa từng chữ từ tiếng Anh. Bảng chuyển bắt buộc:
  Qi Refining (Nth Star/Level) → Luyện Khí tầng N | Foundation Establishment / Qi Building → Trúc Cơ
  Core Formation / Golden Core → Kết Đan / Kim Đan | Nascent Soul → Nguyên Anh | Soul Transformation → Hóa Thần
  Three Flowers Gathered at the Peak / Three Flowers Converging → Tam Hoa Tụ Đỉnh
  Five Energies Returning to Origin / Five Qi Returning to Origin → Ngũ Khí Triều Nguyên
  cultivator → tu sĩ | cultivation → tu luyện/tu vi | spiritual energy/qi → linh khí | spirit root → linh căn
  Sword Intent → Kiếm Ý | Dao Heart → Đạo Tâm | tribulation → thiên kiếp/độ kiếp | elixir/pill → đan dược
  sect → tông môn/môn phái | technique/art → công pháp/võ công | artifact → pháp bảo/pháp khí
- Tên bí kíp/sách/công pháp/tông môn: dịch Hán Việt trang trọng theo nghĩa ("Transcendent Cultivation Record" → "Siêu Việt Tu Chân Lục"; "Absolute Martial Sect" → "Tuyệt Võ Môn") — KHÔNG dịch word-by-word thành cụm thuần Việt lủng củng.
- Thuật ngữ đã dùng ở đoạn trước/character map: giữ nguyên y hệt, không đổi cách gọi giữa các đoạn.
- VĂN KỂ ngôi 3 hiện đại kiểu Korean LN: "anh ta", "cô ta", "cậu", tên nhân vật — tránh "hắn/nàng/lão/y" trừ khi character map chỉ định. Ngôi 1 (truyện kể ngôi nhất): "tôi".
- Lời thoại thù địch: "mày", "tên kia" hoặc lược đại từ; tự xưng "ta"/"tao". CHỈ dùng "ngươi/mi" cho nhân vật cổ phong/tu sĩ tiền bối thực sự nói giọng cổ.
- Tên người Hàn giữ phiên âm Hàn (Seo Eun-Hyun, Kim Young-hoon) — KHÔNG Hán Việt hóa tên người, KHÔNG dịch nghĩa tên người.\
""",
}

# English-language addenda for translategemma (English-prompt model).
_TRANSLATE_GENRE_ADDENDUM_EN: dict[str, str] = {
    GENRE_TIEN_HIEP: (
        "Genre: Xianxia / Cultivation fiction. "
        "Use Sino-Vietnamese (Hán Việt) romanization for cultivation realms, techniques, artifacts. "
        "Realms: 炼气 → Luyện Khí, 筑基 → Trúc Cơ, 金丹 → Kim Đan, 元婴 → Nguyên Anh, 化神 → Hóa Thần, 炼虚 → Luyện Hư, 合体 → Hợp Thể, 大乘 → Đại Thừa, 渡劫 → Độ Kiếp. "
        "Maintain archaic, formal tone. Use 'hắn'/'nàng'/'lão' for third-person pronouns."
    ),
    GENRE_HUYEN_HUYEN: (
        "Genre: Xuanhuan / Fantasy (Chinese source). Moderate Sino-Vietnamese tone. "
        "Keep proper nouns, place names, sect names, and skill/item names in Sino-Vietnamese (Hán Việt) romanization where possible. "
        "May use English names if widely known."
    ),
    GENRE_HE_THONG: (
        "Genre: System fiction / LitRPG. "
        "Translate system notifications clearly and concisely. "
        "Stats: EXP → kinh nghiệm, HP → máu/thể lực, Level → cấp độ, Skill → kỹ năng. "
        "Translate MP as 'linh lực' only for Chinese/cultivation context; use 'mana' or 'năng lượng' for Western/Korean LitRPG or fantasy. "
        "Use Sino-Vietnamese only for Chinese/cultivation system fiction; do not force Han-Viet for Korean office, academy, LitRPG, or Western fantasy stories. "
        "System UI should sound official; character dialogue should sound natural."
    ),
    GENRE_KIEM_HIEP: (
        "Genre: Wuxia / Martial arts fiction (Chinese source). "
        "Keep proper nouns, place names, sect names, and martial techniques in Sino-Vietnamese (Hán Việt) romanization. "
        "Less archaic tone than xianxia."
    ),
    GENRE_DO_THI: (
        "Genre: Urban / Modern fiction. "
        "Use modern, natural Vietnamese. No archaic Sino-Vietnamese terms. "
        "Third-person pronouns: 'anh ấy'/'cô ấy' instead of 'hắn'/'nàng'."
    ),
    GENRE_XUYEN_KHONG: (
        "Genre: Isekai / Time-travel / Reincarnation. "
        "Adapt tone to context: archaic setting → formal Vietnamese; modern setting → natural Vietnamese."
    ),
    GENRE_MAT_THE: (
        "Genre: Post-apocalyptic / Survival. "
        "Tense, direct, punchy prose. Short sentences in action scenes. "
        "No archaic Sino-Vietnamese. Clear, unambiguous vocabulary."
    ),
    GENRE_VONG_DU: (
        "Genre: Game / VRMMO. "
        "Translate UI: HP → máu, MP → mana, EXP → kinh nghiệm, Level → cấp, Skill → kỹ năng, Quest → nhiệm vụ. "
        "Keep system messages terse. Skill names may remain in English if common."
    ),
    GENRE_LANG_MAN: (
        "Genre: Romance / Ngôn tình. "
        "Soft, emotional, nuanced language. Prioritize conveying feelings over literal accuracy. "
        "Use 'anh ấy'/'cô ấy' or character names as third-person pronouns."
    ),
    GENRE_WESTERN_FANTASY: (
        "Genre: Western fantasy / Korean light novel. "
        "Do not use xianxia-style archaic Vietnamese or heavy Sino-Vietnamese unless required. "
        "NARRATION (third-person) pronouns: 'anh', 'anh ta', 'cô', 'cô ta', 'cậu' — never 'hắn/nàng/lão'. "
        "These narration pronouns do NOT apply to dialogue. "
        "HOSTILE DIALOGUE (attacker, gang member, enemy, threat, hostile stranger): "
        "second-person address must use 'mày', 'tên kia', 'thằng kia', or drop the pronoun — NEVER 'anh/bạn/cậu'; "
        "first-person self-address uses 'ta' (arrogant) or 'tao' (street/rough) — NEVER 'tôi'; "
        "do NOT use 'ngươi/mi' (those are xianxia register, wrong for Western setting). "
        "Keep Western names, places, organizations, and titles consistent with the character map."
    ),
    GENRE_KOREAN_CULTIVATION: (
        "Genre: Korean cultivation novel — Sino-Vietnamese (Hán Việt) cultivation terminology with modern Korean LN narration. "
        "Cultivation realms, techniques, sects, artifacts MUST use standard Hán Việt terms, never literal word-by-word Vietnamese: "
        "Qi Refining → Luyện Khí, Foundation Establishment → Trúc Cơ, Core Formation → Kết Đan, Nascent Soul → Nguyên Anh, "
        "Three Flowers Gathered at the Peak → Tam Hoa Tụ Đỉnh, Five Energies Returning to Origin → Ngũ Khí Triều Nguyên, "
        "cultivator → tu sĩ, spiritual energy → linh khí, Sword Intent → Kiếm Ý, sect → tông môn, tribulation → thiên kiếp. "
        "Book/technique titles translate into formal Hán Việt by meaning (Transcendent Cultivation Record → Siêu Việt Tu Chân Lục). "
        "NARRATION pronouns stay modern: 'anh ta', 'cô ta', 'cậu', or character names — never 'hắn/nàng/lão/y' unless the character map says so. "
        "Korean person names keep Korean romanization (Seo Eun-Hyun); never sinicize person names. "
        "Hostile dialogue uses 'mày/tên kia' and 'ta/tao'; reserve 'ngươi/mi' for genuinely archaic-voiced cultivators."
    ),
}


_GENRE_HEADER_LINES: dict[str, str] = {
    GENRE_WESTERN_FANTASY: "## Thể loại: Fantasy kiểu Korean light novel / Fantasy phương Tây — tên Tây, không cổ phong Hán Việt, không ngươi/mi",
    GENRE_TIEN_HIEP: "## Thể loại: Tiên hiệp / Tu tiên / Xianxia",
    GENRE_HUYEN_HUYEN: "## Thể loại: Huyền huyễn / Fantasy đa thế giới",
    GENRE_HE_THONG: "## Thể loại: Hệ thống / System fiction / LitRPG",
    GENRE_KIEM_HIEP: "## Thể loại: Kiếm hiệp / Võ hiệp / Wuxia — giang hồ, bang phái",
    GENRE_DO_THI: "## Thể loại: Đô thị / Hiện đại — không cổ phong",
    GENRE_XUYEN_KHONG: "## Thể loại: Xuyên không / Trọng sinh / Isekai",
    GENRE_MAT_THE: "## Thể loại: Mạt thế / Tận thế / Apocalypse",
    GENRE_VONG_DU: "## Thể loại: Võng du / VRMMO",
    GENRE_LANG_MAN: "## Thể loại: Lãng mạn / Ngôn tình / Romance",
    GENRE_KOREAN_CULTIVATION: "## Thể loại: Tu tiên Hàn Quốc (korean cultivation) — thuật ngữ tu luyện Hán Việt, văn kể hiện đại, tên người Hàn",
}


def genre_header_line(genre: str) -> str:
    """Trả về dòng ## Thể loại: ... để inject vào đầu char_map mới tạo.

    Dùng các từ khóa mà infer_genre_from_char_map() có thể detect lại,
    đảm bảo genre được nhận ra khi không có DB category.
    """
    return _GENRE_HEADER_LINES.get(genre, "")


def get_polish_genre_addendum(genre: str) -> str:
    """Return genre-specific text to append to polish system prompt. Empty → no change."""
    return _POLISH_GENRE_ADDENDUM.get(genre, "").strip()


def get_translate_genre_addendum(genre: str, *, for_english_model: bool = False) -> str:
    """Return genre-specific text to append to translate system prompt."""
    if for_english_model:
        return _TRANSLATE_GENRE_ADDENDUM_EN.get(genre, "").strip()
    return _TRANSLATE_GENRE_ADDENDUM.get(genre, "").strip()


def infer_genre_from_char_map(char_map: str) -> str:
    """Infer a genre override from character-map prose."""
    normalized = (char_map or "").lower()
    # korean_cultivation BEFORE western_fantasy — its header also mentions Korean/hiện đại.
    if any(
        marker in normalized
        for marker in (
            "korean cultivation",
            "tu tiên hàn quốc",
            "tu tiên hàn",
            "tu tiên kiểu hàn",
            "korean murim",
        )
    ):
        return GENRE_KOREAN_CULTIVATION
    if any(
        marker in normalized
        for marker in (
            "western fantasy",
            "korean light novel",
            "fantasy kiểu korean",
            "trung cổ phương tây",
            "tên tây",
            "fantasy phương tây",
            "knight",
            "hiệp sĩ",
        )
    ):
        return GENRE_WESTERN_FANTASY
    if any(m in normalized for m in ("tiên hiệp", "tu tiên", "xianxia", "luyện khí", "kim đan")):
        return GENRE_TIEN_HIEP
    if any(m in normalized for m in ("hệ thống", "system", "litrpg", "[exp]", "[hp]", "exp:")):
        return GENRE_HE_THONG
    if any(m in normalized for m in ("kiếm hiệp", "wuxia", "giang hồ", "võ lâm")):
        return GENRE_KIEM_HIEP
    if any(m in normalized for m in ("đô thị", "hiện đại", "urban")):
        return GENRE_DO_THI
    return ""


SOURCE_NOISE_PATTERNS = [
    r"(?is)\s*www\.\s*ko-fi\.\s*com/\S+.*$",
    r"(?is)\s*ko-fi\.\s*com/\S+.*$",
    r"(?is)\s*\[CÁC GÓI ĐÓNG GÓP\].*$",
    r"(?is)\s*Máy chủ Discord\s*:.*$",
    r"(?is)\s*Discord\s*:?\s*\.?\s*gg/\S+.*$",
    r"(?im)^\s*(?:Translator|Editor|Proofreader|Support us|Join our Discord|Read ahead|Donate|Patreon|Ko-fi)\b.*$",
]


def clean_source_noise(text: str) -> str:
    """Remove crawler/source promo footers before translate/polish."""
    if not text:
        return ""
    cleaned = text
    for pattern in SOURCE_NOISE_PATTERNS:
        cleaned = re.sub(pattern, "", cleaned)
    return cleaned.strip()


_ROOT = Path(__file__).resolve().parents[2]


def _resolve_path(p: "Path") -> "Path":
    return p if p.is_absolute() else _ROOT / p


def find_char_map_file(story_id: str = "", slug: str = "") -> str:
    """
    Auto-tìm char map file theo convention:
      story_data/char_maps/{story_id}-{slug}.txt  (ưu tiên nhất)
      story_data/char_maps/{story_id}.txt
      story_data/char_maps/{slug}.txt
    Nếu không tìm thấy file, thử lấy path từ DB metadata (char_map_path).
    Trả về path tuyệt đối nếu tìm thấy, chuỗi rỗng nếu không.
    """
    base = _ROOT / "story_data" / "char_maps"
    candidates: list[Path] = []
    if story_id and slug:
        candidates.append(base / f"{story_id}-{slug}.txt")
    if story_id:
        candidates.append(base / f"{story_id}.txt")
    if slug:
        candidates.append(base / f"{slug}.txt")
    for p in candidates:
        if p.exists():
            return str(p)

    # DB fallback: check metadata.char_map_path
    if story_id:
        db_path = _char_map_path_from_db_metadata(story_id)
        if db_path:
            return db_path

    return ""


def _char_map_path_from_db_metadata(story_id: str) -> str:
    """
    Lấy char_map_content từ DB, ghi ra /tmp để dùng như file.
    Trả về path nếu có content, chuỗi rỗng nếu không.
    """
    if not story_id:
        return ""
    try:
        from story_db.story_pipeline_db import repository as repo
        story = repo.get_story_by_id(story_id)
        if not story:
            return ""
        meta = story.get("metadata") or {}
        # Thử file path trong metadata trước
        db_path_str = meta.get("char_map_path", "")
        if db_path_str:
            db_path = _resolve_path(Path(db_path_str))
            if db_path.exists():
                return str(db_path)
        # Fallback: dùng content từ metadata
        content = meta.get("char_map_content", "")
        if not content:
            return ""
        tmp_path = Path(f"/tmp/betterbox_char_map_{story_id}.txt")
        tmp_path.write_text(content, encoding="utf-8")
        return str(tmp_path)
    except Exception:
        return ""


def load_char_map(char_map_file: str | None, story_id: str = "") -> str:
    """Load character map text from file, with DB fallback when story_id provided."""
    if char_map_file:
        p = _resolve_path(Path(char_map_file))
        if p.exists():
            return p.read_text(encoding="utf-8").strip()

    # DB fallback
    if story_id:
        try:
            from story_db.story_pipeline_db import repository as repo
            story = repo.get_story_by_id(story_id)
            content = (story.get("metadata") or {}).get("char_map_content", "") if story else ""
            if content:
                return content.strip()
        except Exception:
            pass

    return ""


def parse_aliases(char_map: str) -> dict[str, str]:
    """
    Parse [ALIASES] section trong char_map.
    Format: wrong_name = correct_name  (mỗi dòng)
    Trả về dict {wrong_lower: correct} để dùng khi normalize text.
    """
    aliases: dict[str, str] = {}
    in_aliases = False
    for line in char_map.splitlines():
        stripped = line.strip()
        if stripped.lower() == "[aliases]":
            in_aliases = True
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            in_aliases = False
            continue
        if in_aliases and "=" in stripped and not stripped.startswith("#"):
            wrong, _, correct = stripped.partition("=")
            aliases[wrong.strip().lower()] = correct.strip()
    return aliases


def apply_aliases(text: str, aliases: dict[str, str]) -> str:
    """Thay thế các tên sai/biến thể trong text bằng tên chuẩn."""
    if not aliases:
        return text
    import re as _re
    for wrong_lower, correct in sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True):
        wrong = wrong_lower.strip()
        correct = correct.strip()
        if not wrong or not correct or wrong.lower() == correct.lower():
            continue
        # Unicode-ish name boundary: avoid replacing inside longer words/names.
        pattern = _re.compile(
            rf"(?<![A-Za-zÀ-ỹ0-9_]){_re.escape(wrong)}(?![A-Za-zÀ-ỹ0-9_])",
            _re.IGNORECASE,
        )
        def _replace(m: "_re.Match") -> str:
            # Giữ hoa/thường của ký tự đầu nếu match bắt đầu bằng chữ hoa
            return correct[0].upper() + correct[1:] if m.group()[0].isupper() else correct
        text = pattern.sub(_replace, text)
    return text


_METADATA_ONLY_RE = re.compile(
    r"^##\s*(Truyện\s*:|Cập nhật|Updated\s*:|Story\s*ID\s*:)",
    re.IGNORECASE,
)
_MULTI_VOICE_RE = re.compile(
    r"^##\s*(Quy tắc văn phong|Giọng văn|Phong cách|Tone|Voice|Narrative|Style notes)",
    re.IGNORECASE,
)


def _extract_story_voice_section(char_map: str) -> str:
    """Extract all story-level rule/voice content from char map.

    Collects TWO areas:
    1. Single-line ``## rule`` headers at the top of the file — e.g.
       ``## Thể loại: Fantasy kiểu Korean``  or  ``## Không dùng từ Hán Việt…``
       (pure metadata lines like ``## Truyện:`` and ``## Cập nhật:`` are skipped).
    2. The multi-line ``## Quy tắc văn phong`` (or Voice/Narrative) section that
       typically appears after the ``###`` character entries.
    """
    lines = char_map.splitlines()
    top_rules: list[str] = []    # ## rule lines before first ### entry
    voice_section: list[str] = []  # ## Quy tắc văn phong + its content

    in_aliases = False
    past_first_char = False
    in_multi_voice = False

    for line in lines:
        stripped = line.strip()

        # ── [ALIASES] block: skip entirely ─────────────────────────────────
        if stripped.lower() == "[aliases]":
            in_aliases = True
            continue
        if in_aliases:
            if stripped == "---":
                in_aliases = False
            continue

        # ── First ### character entry: switch to post-char phase ────────────
        if stripped.startswith("### "):
            past_first_char = True
            in_multi_voice = False
            continue

        if not past_first_char:
            # ── Pre-char phase: collect non-metadata ## lines ─────────────
            if stripped.startswith("## ") and not _METADATA_ONLY_RE.match(stripped):
                top_rules.append(line)
        else:
            # ── Post-char phase: collect multi-line voice section ──────────
            if stripped.startswith("## "):
                in_multi_voice = bool(_MULTI_VOICE_RE.match(stripped))
            if in_multi_voice:
                voice_section.append(line)

    parts = []
    if top_rules:
        parts.append("\n".join(top_rules))
    if voice_section:
        parts.append("\n".join(voice_section).strip())
    return "\n\n".join(parts).strip()


def _extract_char_entries(char_map: str) -> str:
    """Extract only the ### character entries from char map (skip story-level sections)."""
    lines = char_map.splitlines()
    result: list[str] = []
    in_char_block = False
    voice_re = re.compile(
        r"^##\s*(Quy tắc văn phong|Giọng văn|Phong cách|Tone|Voice|Narrative)",
        re.IGNORECASE,
    )
    for line in lines:
        stripped = line.strip()
        # Skip story-voice ## sections
        if voice_re.match(stripped):
            in_char_block = False
            continue
        # ### char section starts a character entry
        if stripped.startswith("### "):
            in_char_block = True
        if in_char_block:
            result.append(line)
    return "\n".join(result).strip()


def _split_char_entries(char_entries: str) -> list[str]:
    entries: list[str] = []
    current: list[str] = []
    for line in char_entries.splitlines():
        if line.strip().startswith("### ") and current:
            entries.append("\n".join(current).strip())
            current = [line]
        elif line.strip().startswith("### "):
            current = [line]
        elif current:
            current.append(line)
    if current:
        entries.append("\n".join(current).strip())
    return [entry for entry in entries if entry]


def _entry_surfaces(entry: str) -> list[str]:
    surfaces: list[str] = []
    lines = entry.splitlines()
    if lines and lines[0].strip().startswith("### "):
        surfaces.append(lines[0].strip()[4:].strip())
    for line in lines:
        stripped = line.strip()
        lower = stripped.lower()
        if lower.startswith("- tên khác:"):
            value = stripped.split(":", 1)[-1]
            surfaces.extend(part.strip() for part in value.split(",") if part.strip())
        elif lower.startswith("- danh hiệu/chức vụ:"):
            value = stripped.split(":", 1)[-1].strip()
            if value:
                surfaces.append(value)
    return [surface for surface in dict.fromkeys(surfaces) if surface]


def _entry_is_priority(entry: str) -> bool:
    lower = entry.casefold()
    return (
        "nhân vật chính" in lower
        or "main character" in lower
        or "priority" in lower
        or "vai trò: chính" in lower
    )


def filter_char_map_for_text(char_map: str, text: str, max_entries: int = 20) -> str:
    """Keep story-level rules and only character entries relevant to the current chunk."""
    if not char_map:
        return ""
    story_voice = _extract_story_voice_section(char_map)
    char_entries = _split_char_entries(_extract_char_entries(char_map))
    if not char_entries:
        return char_map

    text_key = (text or "").casefold()
    selected: list[str] = []
    priority: list[str] = []
    for entry in char_entries:
        if _entry_is_priority(entry):
            priority.append(entry)
        surfaces = [surface.casefold() for surface in _entry_surfaces(entry)]
        if any(surface and surface in text_key for surface in surfaces):
            selected.append(entry)

    merged: list[str] = []
    seen: set[str] = set()
    for entry in [*selected, *priority]:
        key = entry.splitlines()[0].casefold() if entry.splitlines() else entry.casefold()
        if key not in seen:
            seen.add(key)
            merged.append(entry)
        if len(merged) >= max_entries:
            break

    if not merged:
        merged = priority[: max(1, min(4, max_entries))]
    if not merged:
        merged = char_entries[: min(4, max_entries)]

    parts: list[str] = []
    if story_voice:
        parts.append(story_voice)
    if merged:
        parts.append("\n\n".join(merged))
    return "\n\n---\n\n".join(part for part in parts if part.strip()).strip()


def inject_char_map_into_system(base_system: str, char_map: str) -> str:
    """Inject character map into system prompt with separate story-voice and per-character blocks."""
    if not char_map:
        return base_system

    story_voice = _extract_story_voice_section(char_map)
    char_entries = _extract_char_entries(char_map)

    result = base_system

    if story_voice:
        result += (
            "\n\n"
            "══════ GIỌNG VĂN VÀ PHONG CÁCH TRUYỆN NÀY (BẮT BUỘC TUÂN THỦ) ══════\n"
            "Các quy tắc sau đặc thù cho truyện này — ghi đè mọi quy tắc thể loại chung:\n"
            f"{story_voice}"
        )

    char_block = char_entries or char_map
    result += (
        "\n\n"
        "══════ NHÂN VẬT — XƯNG HÔ — GIỌNG NÓI (ƯU TIÊN TUYỆT ĐỐI) ══════\n"
        "Quy tắc sau ĐẶC THÙ cho từng nhân vật — ghi đè mọi quy tắc chung nếu mâu thuẫn:\n"
        f"{char_block}"
    )
    return result


def inject_genre_into_system(base_system: str, addendum: str) -> str:
    """Append genre addendum to a system prompt. No-op if addendum is empty."""
    if not addendum:
        return base_system
    return f"{base_system}\n\nQuy tắc thể loại (ghi đè quy tắc chung nếu mâu thuẫn):\n{addendum}"
