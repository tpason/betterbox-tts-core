#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any

import requests

from genre_prompts import (
    clean_source_noise,
    get_polish_genre_addendum,
    infer_genre_from_char_map,
    inject_genre_into_system,
    inject_char_map_into_system,
    filter_char_map_for_text,
    load_char_map,
    parse_aliases,
    apply_aliases,
)
from reader_content_format import format_polished_content as format_reader_polished_content
try:
    from check_translation_quality import (
        BLOCKING_QUALITY_ISSUES,
        check_polished_quality as _ext_check_quality,
        issue_to_repair_hint,
    )
    _QUALITY_RETRY_AVAILABLE = True
except ImportError:
    BLOCKING_QUALITY_ISSUES = frozenset()
    _QUALITY_RETRY_AVAILABLE = False
    def _ext_check_quality(*args, **kwargs): return []  # type: ignore[misc]
    def issue_to_repair_hint(issue: str) -> str: return issue  # type: ignore[misc]
from story_memory import (
    apply_story_memory_replacements,
    build_story_memory_prompt,
    find_story_memory_quality_issues,
    load_story_memory,
    story_memory_status,
)


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHAPTER_PATTERN = re.compile(r"chapter(\d+)\.txt$", re.IGNORECASE)

SYSTEM_PROMPT = """Bạn là biên tập viên truyện audio tiếng Việt, chuyên xử lý truyện dịch máy (tiên hiệp, huyền huyễn, hệ thống, fantasy phương Tây, Korean light novel, lãng mạn, v.v.).

Nhiệm vụ: chuyển văn bản dịch máy thành tiếng Việt tự nhiên, mượt mà — đọc như văn kể, văn tả của người Việt viết, không còn dấu vết dịch máy. Tập trung sửa ngôn ngữ và văn phong, không thêm hay bỏ nội dung.

Xử lý câu tối nghĩa (ưu tiên cao):
- Câu dịch máy tối nghĩa, lủng củng hoặc đọc không hiểu được: dựa vào ngữ cảnh trước và sau để suy ra nghĩa hợp lý nhất, rồi viết lại thành câu tiếng Việt rõ ràng.
- Không giữ nguyên câu tối nghĩa chỉ vì nó đã có trong bản dịch gốc — câu tối nghĩa trong bản dịch gốc không có nghĩa là phải giữ nguyên câu tối nghĩa trong bản biên tập.
- Cấu trúc câu dịch máy cứng nhắc (subject-verb-object dịch thẳng từ tiếng Trung/Hàn): viết lại theo cú pháp tiếng Việt tự nhiên, không copy cấu trúc câu nguồn.

Nguyên tắc bất di bất dịch:
- Chỉ trả về văn bản đã biên tập. Không giải thích, không nhận xét, không markdown, không tiêu đề.
- Tuyệt đối không thêm chú thích, ghi chú, nhận xét biên tập, lý do chỉnh sửa, hay dòng "phong cách chỉnh sửa", "đã sửa...", "phiên bản chỉnh sửa".
- Không tóm tắt, không rút gọn, không bỏ câu, không bỏ đoạn, không bỏ chi tiết.
- Không thêm tình tiết, sự kiện hay hành động mới nếu nguyên bản không có. Được phép dùng từ ngữ tiếng Việt phong phú, tự nhiên hơn miễn giữ đúng nghĩa gốc.
- Giữ đầy đủ thứ tự thông tin, lời thoại, hành động, tâm lý nhân vật và thông báo hệ thống.
- Sau khi xử lý bảng trạng thái hoặc thông báo hệ thống, tiếp tục biên tập đầy đủ phần truyện phía sau.

Văn phong — mục tiêu chính:
- Viết thành văn kể, văn tả tự nhiên: câu có nhịp điệu, rõ ý, dễ nghe khi TTS.
- Câu dịch máy cứng nhắc hoặc gượng: viết lại thành câu tiếng Việt tự nhiên giữ đúng nghĩa gốc.
- Tách câu dính liền (nhiều mệnh đề ghép lại) thành 2–3 câu ngắn dễ nghe.
- Gom câu quá vụn thành câu mạch lạc, miễn không mất ý.
- Xen kẽ câu dài tả cảnh và câu ngắn gọn trong cảnh hành động hoặc cảm xúc cao trào.
- Ưu tiên câu chủ động, động từ cụ thể mạnh hơn câu bị động khi không thay đổi nghĩa.
- Thêm từ nối, từ chuyển tiếp tự nhiên (bỗng nhiên, chợt, thế nhưng, bất giác, đột nhiên...) nếu câu gốc gượng, thiếu liên kết.
- Giữ sắc thái biểu cảm: cảm thán, hào hứng, căng thẳng, khinh thường — không làm phẳng cảm xúc.
- Dùng từ tượng hình, tượng thanh, tượng cảnh nếu phù hợp với nội dung gốc — đây là cách tiếng Việt diễn đạt tự nhiên, không cần nguyên bản phải dùng từ tương đương.
- Tăng sắc thái Hán Việt vừa phải nếu bối cảnh là tiên hiệp/huyền huyễn/kiếm hiệp; KHÔNG áp dụng cho fantasy phương Tây, Korean LN hoặc truyện hiện đại.
- Không hiện đại hóa quá mức từ thuộc bối cảnh tiên hiệp/huyền huyễn — chỉ áp dụng khi đây thực sự là thể loại đó.
- Giữ lời thoại trong dấu ngoặc kép nếu bản gốc là lời nói trực tiếp.
- Giữ khẩu khí nhân vật: "Mẹ kiếp!", "Lão tử không tin!", "Ngươi dám!"

Nhân vật và nhất quán giọng văn (bắt buộc):
- Nếu có character map (inject phía trên), TUÂN THỦ TUYỆT ĐỐI ngôi thứ ba, cách tự xưng, tính cách và giọng thoại từng nhân vật — đây là ưu tiên cao nhất.
- Không tự suy đoán đại từ/khẩu khí nếu character map đã chỉ định rõ.
- Giọng thoại mỗi nhân vật phải nhất quán trong toàn chương: nhân vật lạnh lùng không đột nhiên nói dài dòng/cảm thán; nhân vật kiệm lời không đột nhiên giải thích chi tiết.
- Nhân vật nam không có trong map: "hắn", "y", "gã", "lão" tùy tuổi và sắc thái.
- Nhân vật nữ không có trong map: "nàng", "cô", "bà" tùy tuổi — không dùng "hắn".
- Nhân vật trẻ (thiếu niên, trẻ nhỏ): "cậu", "nó" — không tự xưng "ông"/"bà"/"lão" sai ngữ cảnh.
- Giữ nhất quán đại từ cho từng nhân vật trong toàn chương.
- Lời thoại tự xưng giữ theo nguyên bản; không tự ý đổi khẩu khí nhân vật.

Xưng hô trong lời thoại — ngôi 1 và ngôi 2 (tiếng Việt đặc thù, bắt buộc áp dụng đúng):
Tiếng Việt xưng hô phụ thuộc vào QUAN HỆ + TUỔI + QUYỀN LỰC + CẢM XÚC của người nói. Không thể dịch cứng "I → tôi" hay "you → anh/bạn".

Ngôi thứ nhất (cách tự xưng theo ngữ cảnh):
- Trung tính / lịch sự: "tôi"
- Thân mật cùng lứa: "mình", "tớ"
- Tao (informal/giang hồ): CHỈ dùng khi người nói thực sự thân với người nghe, hoặc đang đối đầu kẻ thù cùng tầm; KHÔNG dùng với người lớn hơn/cấp trên
- Kiêu ngạo / quyền lực / phản diện mạnh: "ta", "lão tử", "bổn tọa" — KHÔNG dùng "tôi" khi nhân vật đang khiêu ngạo hoặc ra lệnh
- Khiêm tốn / cấp dưới / đệ tử: "tại hạ", "đệ tử", "tiểu nhân"
- Người nhỏ hơn nói với người lớn hơn: "em", "con", "cháu" tùy quan hệ — KHÔNG "tao"

Ngôi thứ hai (cách gọi người đối diện theo ngữ cảnh):
- Người nhỏ → người lớn hơn: "anh", "chị", "chú", "bác", "thầy", "ngài" — KHÔNG "mày", "ngươi", "mi"
- Người lớn → người nhỏ hơn: "cậu", "em", "con", "cháu", "nhóc"
- Ngang hàng / thân thiết: "cậu", "bạn", "anh/chị" tùy tuổi tương đối
- Kẻ thù / đối lập / coi thường: "ngươi", "mi" (tiên hiệp/huyền huyễn/kiếm hiệp/cổ phong); "mày" hoặc "tên kia" (fantasy phương Tây, Korean LN, giang hồ hiện đại) — TUYỆT ĐỐI KHÔNG gọi kẻ thù hay người lạ thù địch là "anh/bạn"
- Tôn kính / thần phục cấp trên: "ngài", "tiền bối", "đại nhân", "sư phụ", "lãnh chúa"
- Khinh thường / khiêu khích: "thằng kia", "con kia", "tiểu tử", "lão già", "ngươi"

Ba quy tắc cốt lõi không được vi phạm:
1. Kẻ thù / phản diện trong cảnh đối đầu: KHÔNG tự xưng "tôi" (dùng "ta"/"lão tử" cho cổ phong, "tao" cho hiện đại/western); KHÔNG gọi nhân vật chính là "anh/bạn/cậu" thân thiện — dùng "ngươi/mi" cho tiên hiệp/cổ phong, "mày/tên kia" cho western fantasy/hiện đại
2. Nhân vật nhỏ tuổi hơn gặp người lớn hơn: tự xưng "em/con/cháu", gọi người lớn là "anh/chú/bác" — KHÔNG "mày/tao" trừ khi hoàn cảnh là giang hồ thù địch ngang cấp hoặc đã được character map chỉ định rõ
3. Cùng một nhân vật xưng hô KHÁC NHAU tùy đối tượng: nói với đồng đội trẻ hơn là "cậu"; nói với thủ lĩnh là "ngài/tiền bối"; kẻ thù nói với họ là "ngươi" — không đồng nhất tất cả thành "anh/bạn"

Dấu câu và nhịp đọc:
- Dùng dấu chấm cho nhịp nghỉ rõ, dấu phẩy cho nhịp nghỉ nhẹ.
- Nếu câu gốc không chắc nghĩa, chỉnh dựa trên ngữ cảnh gần nhất; không bịa thêm.

Tên riêng và thuật ngữ:
- Chỉ dùng chữ Quốc ngữ. Không dùng chữ Hán, chữ Nôm, pinyin hoặc ký hiệu khó đọc.
- Nếu là truyện Trung/xianxia/tiên hiệp: giữ tên riêng, địa danh, cảnh giới, công pháp, pháp bảo theo âm Hán Việt đã ổn định; không dịch nghĩa tên Trung (小林 → Tiểu Lâm, không phải Nhỏ Rừng); không đổi thuật ngữ: linh căn, tu vi, cảnh giới, công pháp, pháp khí, pháp bảo, thần thông, khí vận.
- Nếu là truyện Hàn/Tây/fantasy phương Tây: giữ nguyên tên Tây theo character map; KHÔNG Hán Việt hóa tên phương Tây.

Số và thông tin hệ thống:
- Chuyển số và ký hiệu sang chữ khi đọc sẽ tự nhiên hơn: 1% → một phần trăm; 50% → năm mươi phần trăm; 20 năm → hai mươi năm.
- Phân số chỉ số như 11/65: viết "mười một trên sáu mươi lăm" hoặc diễn đạt tự nhiên theo ngữ cảnh.
- Dòng trạng thái, bảng thuộc tính: gom thành câu kể tự nhiên, giữ đủ từng mục; không để từng dòng rời rạc kiểu "Tên: X. Tuổi thọ: Y".
- Với thông tin hệ thống, có thể mở đầu bằng câu dẫn ngắn: "Thông tin nhân vật hiện ra trước mắt." hoặc "Một màn sáng hệ thống hiện lên."

Sửa lỗi dịch máy thường gặp:
- "Rót:" ghi chú có sẵn → "Chú thích:". Không tạo ghi chú mới về việc đã biên tập.
- "Trán," tiếng than → "Ách,"; trước thông báo hệ thống → "Đinh!".
- "Du hí" → "trò chơi".
- "Click" → "ấn", "nhấn", "chọn".
- "Thọ mệnh" → "tuổi thọ" hoặc "thọ nguyên" tùy văn cảnh.
- "Thuộc tính liệt biểu" → "bảng thuộc tính".
- "Đổ xúc xắc" / "lắc" → "tung xúc xắc", "lắc xúc xắc" tùy câu.
- "Xâu tạc thiên" → "nghịch thiên", "bá đạo", "kinh người" tùy ngữ cảnh.
- "Sau đó không có người" (chết/ngất/mất ý thức) → "rồi không còn biết gì nữa".
- "Vì tê liệt chính mình" (né đau khổ/sợ hãi) → "để khiến bản thân tạm quên đi nỗi sợ".
- Cụm dịch máy vô nghĩa: dựa vào ngữ cảnh gần nhất để sửa; nếu không chắc nghĩa, đánh dấu [nghi vấn: ...].

Glossary ưu tiên:
- 系统: hệ thống | 宿主: kí chủ | 修为: tu vi | 境界: cảnh giới | 寿命: thọ nguyên/tuổi thọ
- 炼气: Luyện Khí | 筑基: Trúc Cơ | 金丹: Kim Đan | 元婴: Nguyên Anh | 化神: Hóa Thần
- 炼虚: Luyện Hư | 合体: Hợp Thể | 大乘: Đại Thừa | 渡劫: Độ Kiếp
- 灵气: linh khí | 灵根: linh căn | 法宝: pháp bảo | 法器: pháp khí | 功法: công pháp
- 法术: pháp thuật | 神通: thần thông | 气运: khí vận | 先天气运: Tiên Thiên khí vận | 体魄: thể phách
- 商城: thương thành | 造化商城: Tạo Hóa thương thành | 唐三藏: Đường Tam Tạng | 观音: Quan Âm
"""

USER_PROMPT_TEMPLATE = """Biên tập đoạn truyện sau thành văn phong truyện audio tiếng Việt tự nhiên, mượt mà — đọc như văn kể và văn tả, không còn dấu vết dịch máy.

Yêu cầu:
- Sửa ngôn ngữ và văn phong cho tự nhiên; giữ nguyên toàn bộ nội dung, thứ tự thông tin.
- Câu dịch máy cứng, gượng: viết lại thành câu tiếng Việt tự nhiên, đúng nghĩa gốc.
- Sửa đúng đại từ nhân xưng theo giới tính nhân vật — nếu có character map phía trên, TUÂN THỦ TUYỆT ĐỐI xưng hô và giọng thoại từng nhân vật trong map.
- Giữ nhất quán tính cách nhân vật: nhân vật lạnh lùng không đột nhiên nói dài dòng; nhân vật kiệm lời không đột nhiên giải thích nhiều.
- Tách câu dính liền thành câu ngắn dễ nghe; gom câu vụn vặt thành câu mạch lạc.
- Giữ sắc thái biểu cảm, cảm thán và khẩu khí nhân vật.
- Chuyển số, phần trăm, cấp độ sang chữ khi đọc mượt hơn.
- Gom bảng trạng thái và thông báo hệ thống thành câu kể tự nhiên, giữ đủ từng mục.
- Chỉ trả về nội dung truyện đã biên tập; không ghi chú, không giải thích, không dòng "Phong cách chỉnh sửa" hay "Ghi chú".

Đoạn cần biên tập:
{text}
"""

# Variant khi có preceding context (chunk từ thứ 2 trở đi)
USER_PROMPT_WITH_CONTEXT_TEMPLATE = """Biên tập đoạn truyện sau thành văn phong truyện audio tiếng Việt tự nhiên, mượt mà — đọc như văn kể và văn tả, không còn dấu vết dịch máy.

Ngữ cảnh — phần kết của đoạn liền trước (CHỈ để tham khảo giọng văn và mạch truyện, KHÔNG biên tập lại):
---
{preceding_context}
---

Yêu cầu:
- Tiếp nối đúng giọng văn và mạch truyện từ đoạn trước.
- Sửa ngôn ngữ và văn phong cho tự nhiên; giữ nguyên toàn bộ nội dung, thứ tự thông tin.
- Câu dịch máy cứng, gượng: viết lại thành câu tiếng Việt tự nhiên, đúng nghĩa gốc.
- Sửa đúng đại từ nhân xưng — nếu có character map phía trên, TUÂN THỦ TUYỆT ĐỐI xưng hô và giọng thoại từng nhân vật trong map.
- Giữ nhất quán tính cách nhân vật xuyên suốt đoạn.
- Tách câu dính liền thành câu ngắn dễ nghe; gom câu vụn vặt thành câu mạch lạc.
- Giữ sắc thái biểu cảm, cảm thán và khẩu khí nhân vật.
- Chuyển số, phần trăm, cấp độ sang chữ khi đọc mượt hơn.
- Chỉ trả về nội dung truyện đã biên tập; không ghi chú, không giải thích.

Đoạn cần biên tập:
{text}
"""

FAST_SYSTEM_PROMPT = """Bạn là biên tập viên truyện audio tiếng Việt.
Biên tập truyện dịch máy thành văn kể tự nhiên, dễ nghe khi TTS; giữ đủ nội dung và đúng thứ tự.
Chỉ trả về văn bản đã biên tập, không giải thích, không markdown, không tiêu đề, không ghi chú.
Không tóm tắt, không bỏ câu, không thêm tình tiết.
Câu dịch máy cứng: viết lại tự nhiên, đúng nghĩa gốc.
Nếu có character map (inject phía trên): tuân thủ tuyệt đối ngôi thứ ba, cách tự xưng, cách gọi từng đối tượng, quan hệ và giọng thoại từng nhân vật.
Sửa đúng đại từ theo giới tính: nữ dùng nàng/cô, không dùng hắn; nhân vật trẻ không tự xưng ông/bà/lão sai ngữ cảnh.
Xưng hô tiếng Việt phải theo quan hệ + tuổi + quyền lực + cảm xúc, không dịch/giữ máy móc "tôi/anh/bạn".
Kẻ thù, phản diện, kẻ tấn công, người lạ thù địch: không gọi đối phương là "anh/bạn/cậu"; dùng "ngươi/mi" cho tiên hiệp/cổ phong, "mày/tên kia" cho western fantasy/Korean LN/hiện đại.
Nhân vật kiêu ngạo/đe dọa không tự xưng "tôi" nếu khẩu khí gốc hung hăng; dùng "ta/lão tử/bổn tọa" theo bối cảnh.
Người nhỏ tuổi nói với người lớn/cấp trên: tự xưng "em/con/cháu", gọi "anh/chị/chú/bác/thầy/ngài"; không dùng "mày/tao" trừ khi nguồn thể hiện thù địch rõ.
Cùng một nhân vật phải xưng hô khác nhau tùy người nghe: đồng đội, cấp trên, người lớn tuổi, trẻ nhỏ, người lạ và kẻ thù không dùng chung một cặp đại từ.
Giữ lời thoại, tên riêng, thuật ngữ tiên hiệp/hệ thống.
Chuyển số/ký hiệu sang cách đọc tự nhiên khi phù hợp."""

FAST_USER_PROMPT_TEMPLATE = """Biên tập đoạn truyện sau thành văn kể tự nhiên, mượt mà khi đọc audio tiếng Việt.
Giữ đủ nghĩa, đủ chi tiết, đúng thứ tự. Sửa câu cứng thành câu tiếng Việt tự nhiên.
Chỉ trả về nội dung truyện đã biên tập; không ghi chú, không giải thích.

{text}
"""

FAST_USER_PROMPT_WITH_CONTEXT_TEMPLATE = """Biên tập đoạn truyện sau thành văn kể tự nhiên, mượt mà khi đọc audio tiếng Việt.
Tiếp nối giọng văn từ đoạn trước (CHỈ tham khảo, không biên tập lại):
---
{preceding_context}
---
Giữ đủ nghĩa, đủ chi tiết, đúng thứ tự. Chỉ trả về nội dung đã biên tập; không ghi chú.

{text}
"""

# Dùng khi chunk bị quality check fail và cần retry với feedback cụ thể.
# {preceding_section} là optional — trống nếu không có preceding context.
QUALITY_REPAIR_USER_TEMPLATE = """Biên tập lại đoạn truyện sau — phiên bản trước còn lỗi cần sửa:

{repair_hints}
{preceding_section}
Yêu cầu:
- Sửa toàn bộ các lỗi nêu trên
- Toàn bộ nội dung bằng tiếng Việt tự nhiên, đọc như văn tác giả viết, không dấu vết dịch máy
- Không để sót ký tự tiếng Trung/Hàn/Anh chưa dịch
- Không lặp đoạn văn
- Tuân thủ char map và giọng văn từ ngữ cảnh trước nếu có
- Giữ đầy đủ nội dung gốc, đúng thứ tự
- Chỉ trả về nội dung đã biên tập; không ghi chú, không giải thích

Đoạn gốc cần biên tập:
{text}
"""


def chapter_number(path: Path) -> int:
    match = CHAPTER_PATTERN.match(path.name)
    return int(match.group(1)) if match else 0


def list_chapter_files(input_dir: Path) -> list[Path]:
    return sorted(
        [path for path in input_dir.glob("chapter*.txt") if CHAPTER_PATTERN.match(path.name)],
        key=chapter_number,
    )


def split_text(text: str, max_chars: int) -> list[str]:
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current = ""

    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= max_chars:
            current = candidate
            continue

        if current:
            chunks.append(current)
            current = ""

        if len(paragraph) <= max_chars:
            current = paragraph
            continue

        sentences = re.split(r"(?<=[.!?。！？…])\s+", paragraph)
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            candidate = f"{current} {sentence}".strip() if current else sentence
            if len(candidate) <= max_chars:
                current = candidate
                continue
            if current:
                chunks.append(current)
            current = sentence

    if current:
        chunks.append(current)
    return chunks


def clean_model_output(text: str) -> str:
    text = text.strip()
    # Strip Qwen3/DeepSeek thinking blocks nếu Ollama chưa lọc.
    text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"^```(?:text|markdown)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    text = re.sub(r"^\s*(?:Bản biên tập|Văn bản đã biên tập)\s*:\s*", "", text, flags=re.IGNORECASE)
    return text.strip()


def has_editorial_noise(text: str) -> bool:
    return re.search(
        r"(?im)^\s*(?:phong cách chỉnh sửa|phiên bản chỉnh sửa|ghi chú|note|nhận xét|"
        r"bản đã chỉnh|các thay đổi|đã chỉnh sửa)\b\s*:?",
        text,
    ) is not None


HOSTILE_CONTEXT_RE = re.compile(
    r"(?:kẻ tấn công|tên cướp|tên trộm|bang hội|phản diện|kẻ thù|thù địch|"
    r"giết|xông lên|rút kiếm|dao găm|đe dọa|phục kích|attacker|thief|gang|"
    r"enemy|hostile|ambush|kill him|kill her|rushed|dagger)",
    re.IGNORECASE,
)
POLITE_HOSTILE_DIALOGUE_RE = re.compile(
    r'"(?:\s*(?:Này,?\s*)?(?:Anh|anh|Bạn|bạn|Cậu|cậu)\b[^"\n]{0,100}[?!.]?|'
    r'[^"\n]{1,24},\s*(?:anh|bạn|cậu)\b[^"\n]{0,100}[?!.]?)"'
)
RUDE_TO_ELDER_RE = re.compile(
    r"(?:đứa trẻ|cậu bé|cô bé|thiếu niên|nhân viên trẻ|child|boy|girl|young staff)"
    r"[\s\S]{0,180}"
    r'"[^"\n]{0,80}\b(?:mày|tao)\b[^"\n]{0,100}"',
    re.IGNORECASE,
)


def find_addressing_quality_issues(text: str) -> list[str]:
    """Best-effort warnings for Vietnamese address choices that are often wrong."""
    issues: list[str] = []
    if not text:
        return issues
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    for idx, paragraph in enumerate(paragraphs, start=1):
        nearby = "\n".join(paragraphs[max(0, idx - 2): min(len(paragraphs), idx + 1)])
        if HOSTILE_CONTEXT_RE.search(nearby):
            for match in POLITE_HOSTILE_DIALOGUE_RE.finditer(paragraph):
                issues.append(
                    f"paragraph {idx}: hostile context may use polite/friendly address: {match.group(0)[:140]}"
                )
        if RUDE_TO_ELDER_RE.search(paragraph):
            issues.append(f"paragraph {idx}: young speaker may be using mày/tao toward an older/respected listener")
    return issues


def warn_addressing_quality(output_text: str, label: str) -> None:
    issues = find_addressing_quality_issues(output_text)
    for issue in issues[:8]:
        print(f"[QUALITY WARN] {label}: {issue}")
    if len(issues) > 8:
        print(f"[QUALITY WARN] {label}: {len(issues) - 8} more possible addressing issue(s)")


def output_too_short(source_text: str, output_text: str, min_ratio: float) -> bool:
    if min_ratio <= 0:
        return False
    source_len = len(re.sub(r"\s+", "", source_text or ""))
    output_len = len(re.sub(r"\s+", "", output_text or ""))
    if source_len < 500:
        return False
    return output_len < source_len * min_ratio


def build_messages(
    text: str,
    prompt_profile: str,
    genre: str = "",
    char_map: str = "",
    preceding_context: str = "",
    story_memory_context: str = "",
    repair_hints: str = "",
) -> list[dict[str, str]]:
    addendum = get_polish_genre_addendum(genre)
    focused_char_map = filter_char_map_for_text(
        char_map,
        f"{preceding_context}\n\n{text}".strip(),
    ) if char_map else ""
    if prompt_profile == "fast":
        system = inject_genre_into_system(FAST_SYSTEM_PROMPT, addendum)
        system = inject_char_map_into_system(system, focused_char_map)
        if story_memory_context:
            system += (
                "\n\n"
                "══════ STORY MEMORY / ROLE BIBLE / GLOSSARY (ƯU TIÊN CAO) ══════\n"
                "Các quy tắc sau đặc thù cho truyện, nhân vật, role đại chúng, biệt danh và thuật ngữ. "
                "Chúng ghi đè quy tắc chung nếu mâu thuẫn:\n"
                f"{story_memory_context}"
            )
        if repair_hints:
            preceding_section = (
                f"\nNgữ cảnh đoạn trước (CHỈ tham khảo giọng văn, không biên tập lại):\n"
                f"---\n{preceding_context}\n---\n"
                if preceding_context else ""
            )
            user_content = QUALITY_REPAIR_USER_TEMPLATE.format(
                repair_hints=repair_hints, preceding_section=preceding_section, text=text
            )
        elif preceding_context:
            user_content = FAST_USER_PROMPT_WITH_CONTEXT_TEMPLATE.format(
                preceding_context=preceding_context, text=text
            )
        else:
            user_content = FAST_USER_PROMPT_TEMPLATE.format(text=text)
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ]
    system = inject_genre_into_system(SYSTEM_PROMPT, addendum)
    system = inject_char_map_into_system(system, focused_char_map)
    if story_memory_context:
        system += (
            "\n\n"
            "══════ STORY MEMORY / ROLE BIBLE / GLOSSARY (ƯU TIÊN CAO) ══════\n"
            "Các quy tắc sau đặc thù cho truyện, nhân vật, role đại chúng, biệt danh và thuật ngữ. "
            "Chúng ghi đè quy tắc chung nếu mâu thuẫn:\n"
            f"{story_memory_context}"
        )
    if repair_hints:
        preceding_section = (
            f"\nNgữ cảnh đoạn trước (CHỈ tham khảo giọng văn, không biên tập lại):\n"
            f"---\n{preceding_context}\n---\n"
            if preceding_context else ""
        )
        user_content = QUALITY_REPAIR_USER_TEMPLATE.format(
            repair_hints=repair_hints, preceding_section=preceding_section, text=text
        )
    elif preceding_context:
        user_content = USER_PROMPT_WITH_CONTEXT_TEMPLATE.format(
            preceding_context=preceding_context, text=text
        )
    else:
        user_content = USER_PROMPT_TEMPLATE.format(text=text)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]


def clean_for_audiobook_tts(text: str) -> str:
    """
    Pre/post-clean cho audiobook/TTS.

    Mục tiêu:
    - Xóa separator như ---o0o---, ***, ===.
    - Xóa trailing spaces / Markdown hard line-break.
    - Chuẩn hóa quote thoại từ '...' sang "...".
    - Chuẩn hóa dấu câu lặp, dấu ba chấm.
    - Tách âm báo như Keng!, Đinh! thành dòng riêng để TTS đỡ rè/artifact.
    """

    if not text:
        return ""

    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Xóa ký tự vô hình.
    text = re.sub(r"[\ufeff\u200b\u200c\u200d\u2060]", "", text)

    # Xóa trailing spaces từng dòng, gồm Markdown hard line-break "  ".
    text = "\n".join(line.rstrip() for line in text.split("\n"))

    # Chuẩn hóa quote cong/lạ.
    quote_map = {
        """: '"',
        """: '"',
        "„": '"',
        "‟": '"',
        "«": '"',
        "»": '"',
        "＂": '"',
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
        "`": "'",
        "´": "'",
    }
    for old, new in quote_map.items():
        text = text.replace(old, new)

    # Xóa dòng separator đứng riêng.
    separator_line_re = re.compile(
        r"""^\s*(?:
            [-=_~*]{3,}
            |
            [-=_~*]*\s*[oO0]\s*[oO0]\s*[oO0]\s*[-=_~*]*
            |
            [•●◆◇★☆]{2,}
            |
            [—–-]{3,}
        )\s*$""",
        re.VERBOSE | re.IGNORECASE,
    )

    lines = []
    for line in text.split("\n"):
        if separator_line_re.match(line):
            lines.append("")
        else:
            lines.append(line)

    text = "\n".join(lines)

    # Xóa separator nằm giữa dòng.
    inline_separator_patterns = [
        r"\s*-{2,}\s*[oO0]\s*[oO0]\s*[oO0]\s*-{2,}\s*",
        r"\s*\*{3,}\s*",
        r"\s*={3,}\s*",
        r"\s*_{3,}\s*",
        r"\s*~{3,}\s*",
        r"\s*[—–-]{4,}\s*",
    ]
    for pattern in inline_separator_patterns:
        text = re.sub(pattern, "\n\n", text, flags=re.IGNORECASE)

    # Xóa URL / rác web.
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"www\.\S+", "", text)

    # Xóa bullet / ký hiệu trang trí.
    text = re.sub(r"[•●◆◇★☆]+", "", text)

    # Xóa emoji.
    text = re.sub(
        r"[\U0001F300-\U0001FAFF\U00002700-\U000027BF]",
        "",
        text,
        flags=re.UNICODE,
    )

    # Bỏ ngoặc rỗng.
    text = re.sub(r"【\s*】", "", text)
    text = re.sub(r"\[\s*\]", "", text)
    text = re.sub(r"\(\s*\)", "", text)
    text = re.sub(r"<\s*>", "", text)

    # Bỏ vỏ ngoặc hệ thống, giữ nội dung bên trong.
    # Ví dụ: 【 Kí chủ: Lâm Thần 】 -> Kí chủ: Lâm Thần
    text = re.sub(r"【\s*([^】\n]{1,160})\s*】", r"\1", text)
    text = re.sub(r"［\s*([^］\n]{1,160})\s*］", r"\1", text)

    # Đổi thoại quote đơn thành quote kép.
    # Ví dụ: 'Lại nữa à?' -> "Lại nữa à?"
    text = re.sub(
        r"(?m)(^|[\s\n])'([^'\n]{1,220}[.!?])'(?=[$\s\n,.!?])",
        r'\1"\2"',
        text,
    )

    # Ví dụ: hắn nói: 'Lại nữa à?' -> hắn nói: "Lại nữa à?"
    text = re.sub(
        r"(:\s*)'([^'\n]{1,220}[.!?])'",
        r'\1"\2"',
        text,
    )

    # Chuẩn hóa dấu ba chấm.
    text = text.replace("…", "...")

    # Ellipsis trước chữ hoa đầu câu (nhịp ngắt giữa hành động) -> dấu chấm.
    # Dùng [A-ZĐ] thay vì [À-Ỹ] vì range Unicode bao gồm cả lowercase Vietnamese.
    # Phải đứng TRƯỚC rule hesitation vì Đ cũng khớp \w trong Unicode.
    # Ví dụ: "Hắn bước tới... Đột nhiên hắn dừng lại." -> "Hắn bước tới. Đột nhiên hắn dừng lại."
    text = re.sub(r"\s*\.\.\.\s*(?=[A-ZĐ])", ". ", text)

    # Hesitation do dự / lời thoại ngập ngừng (word...lowercase_word) -> dấu phẩy.
    # Ví dụ: "Nhóc... bao nhiêu tuổi?" -> "Nhóc, bao nhiêu tuổi?"
    text = re.sub(r"(?<=\w)\s*\.\.\.\s*(?=\w)", ", ", text, flags=re.UNICODE)

    # Ellipsis cuối câu / trước quote -> dấu chấm.
    text = re.sub(r"\s*\.\.\.\s*(?=[\"'\n]|$)", ". ", text)

    # Ellipsis còn lại -> dấu phẩy.
    text = re.sub(r"\s*\.\.\.\s*", ", ", text)

    # Giảm dấu câu lặp.
    text = re.sub(r"!{2,}", "!", text)
    text = re.sub(r"\?{2,}", "?", text)
    text = re.sub(r"([!?]){2,}", r"\1", text)

    # Xóa dấu phẩy giữa các âm tiết ngắn lặp lại: "Ừ, ừ" → "Ừ ừ", "Ha, ha" → "Ha ha".
    # Lý do: TTS đọc dấu phẩy như khoảng dừng rõ ràng, tạo prosody giật cục cho grunt/laugh/sob.
    # Pattern: 1-4 ký tự (kể dấu thanh) lặp lại 2+ lần, cách nhau bằng ", ".
    # Giới hạn 1-4 ký tự để tránh nhầm với "Không, không!" (5 ký tự — emphatic negation cần giữ).
    def _dedup_short_syllable(m: re.Match) -> str:
        syllable = m.group(1)  # giữ nguyên case của lần xuất hiện đầu tiên
        count = m.group(0).count(",") + 1
        return " ".join([syllable] + [syllable.lower()] * (count - 1))

    text = re.sub(
        r"\b(\w{1,4})(?:,\s+\1)+(?=\b|[.!?,])",
        _dedup_short_syllable,
        text,
        flags=re.IGNORECASE | re.UNICODE,
    )

    # Dấu câu Trung nếu còn sót.
    text = text.replace("。", ".")
    text = text.replace("，", ",")
    text = text.replace("！", "!")
    text = text.replace("？", "?")
    text = text.replace("：", ":")
    text = text.replace("；", ";")

    # Xóa space trước dấu câu.
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)

    # Thêm space sau dấu câu nếu bị dính chữ.
    text = re.sub(r"([!?;:])(?=\S)", r"\1 ", text)
    text = re.sub(r"([,.])(?=[A-Za-zÀ-ỹ])", r"\1 ", text)

    # Thay thế âm thanh tiếng Anh bằng từ tương đương tiếng Việt (chỉ khi theo sau bởi !).
    # Giới hạn ở context "!" để tránh nhầm với tên nhân vật hoặc từ bình thường.
    _EN_SOUND_MAP = {
        "Swish": "Xoẹt",
        "Swoosh": "Vù",
        "Whoosh": "Vù",
        "Bang": "Đùng",
        "Boom": "Ầm",
        "Crash": "Rầm",
        "Thud": "Bụp",
        "Thump": "Bịch",
        "Crack": "Rắc",
        "Snap": "Rắc",
        "Slash": "Xoẹt",
        "Smash": "Rầm",
        "Clang": "Keng",
        "Clank": "Keng",
        "Zap": "Vút",
        "Pop": "Bốp",
        "Splat": "Bịch",
    }
    for en_word, vi_word in _EN_SOUND_MAP.items():
        text = re.sub(
            rf"\b{re.escape(en_word)}\b",
            vi_word,
            text,
            flags=re.IGNORECASE,
        )

    # Tách âm báo cho TTS.
    sound_words = [
        "Keng",
        "Đinh",
        "Tinh",
        "Ting",
        "Ầm",
        "Rầm",
        "Vù",
        "Xoẹt",
        "Két",
        "Bốp",
        "Chát",
    ]
    sound_pattern = "|".join(re.escape(w) for w in sound_words)

    # Đầu dòng: Keng! Tiếng động... -> Keng!\nTiếng động...
    text = re.sub(
        rf"(?m)(^|\n)({sound_pattern})!\s+([A-ZÀ-ỸĐ])",
        r"\1\2!\n\3",
        text,
    )

    # Giữa đoạn: ... Keng! Tiếng động... -> ...\nKeng!\nTiếng động...
    text = re.sub(
        rf"([.!?])\s+({sound_pattern})!\s+([A-ZÀ-ỸĐ])",
        r"\1\n\2!\n\3",
        text,
    )

    # Riêng thông báo hệ thống.
    text = re.sub(
        r"(?m)(^|\n)(Đinh|Tinh|Ting)!\s+(Nhiệm vụ|Hệ thống|Kí chủ|Thông tin|Bảng|Giao diện)",
        r"\1\2!\n\3",
        text,
        flags=re.IGNORECASE,
    )

    # Trim từng dòng.
    lines = [line.strip() for line in text.split("\n")]

    # Strip artifact ". " đầu dòng thoại do ellipsis normalization tạo ra.
    # Ví dụ: '"... Dù chuyện"' → sau ellipsis rule → '". Dù chuyện"' → fix → '"Dù chuyện"'
    # Tương tự cho dòng narrative bắt đầu bằng dấu câu trơ.
    _LEADING_SENT_PUNCT_RE = re.compile(r'^"([.\s]+)')
    cleaned_lines = []
    for line in lines:
        # Dialogue: '". text"' -> '"text"'
        line = _LEADING_SENT_PUNCT_RE.sub('"', line)
        # Narrative: '. text' hoặc '. . text' ở đầu dòng
        line = re.sub(r'^[.\s]+(?=[^\s.])', '', line)
        cleaned_lines.append(line)
    lines = cleaned_lines

    # Giữ tối đa một dòng trống liên tiếp.
    final_lines = []
    blank_count = 0

    for line in lines:
        if not line:
            blank_count += 1
            if blank_count <= 1:
                final_lines.append("")
        else:
            blank_count = 0
            final_lines.append(line)

    return "\n".join(final_lines).strip()


def validate_tts_text(text: str) -> list[str]:
    issues: list[str] = []

    if re.search(r"(?m)^\s*---o0o---\s*$", text, flags=re.IGNORECASE):
        issues.append("Còn separator ---o0o---")

    if re.search(r"[ \t]+$", text, flags=re.MULTILINE):
        issues.append("Còn trailing spaces cuối dòng")

    if "..." in text or "…" in text:
        issues.append("Còn dấu ba chấm")

    if re.search(r"(?m)^'[^'\n]+[.!?]'$", text):
        issues.append("Còn thoại dùng quote đơn")

    if re.search(r"\*{3,}|={3,}|_{3,}|~{3,}", text):
        issues.append("Còn separator dạng ***, ===, ___ hoặc ~~~")

    return issues


def call_ollama(
    base_url: str,
    model: str,
    text: str,
    temperature: float,
    num_ctx: int,
    timeout: int,
    retries: int,
    keep_alive: str,
    prompt_profile: str = "full",
    genre: str = "",
    char_map: str = "",
    preceding_context: str = "",
    story_memory_context: str = "",
    session: requests.Session | None = None,
    no_think: bool = True,
    repair_hints: str = "",
) -> str:
    url = base_url.rstrip("/") + "/api/chat"
    messages = build_messages(text, prompt_profile, genre, char_map, preceding_context, story_memory_context, repair_hints)
    # Disable Qwen3 thinking mode for polish: rewriting doesn't need deep reasoning,
    # and thinking tokens eat into context window leaving less room for content.
    if no_think and "qwen3" in model.lower():
        for msg in messages:
            if msg["role"] == "user":
                msg["content"] = "/no_think\n\n" + msg["content"]
                break
    payload: dict[str, Any] = {
        "model": model,
        "stream": False,
        "messages": messages,
        "options": {
            "temperature": temperature,
            "num_ctx": num_ctx,
        },
        "keep_alive": keep_alive,
    }

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            client = session or requests
            response = client.post(url, json=payload, timeout=timeout)
            if response.status_code >= 400:
                raise RuntimeError(
                    f"HTTP {response.status_code} from Ollama {url}: {response.text[:1000]}"
                )
            data = response.json()
            content = data.get("message", {}).get("content", "")
            if not content.strip():
                raise ValueError(f"Ollama trả về rỗng: {json.dumps(data, ensure_ascii=False)[:500]}")
            return clean_model_output(content)
        except Exception as exc:
            last_error = exc
            print(f"[WARN] Ollama error attempt {attempt}/{retries}: {exc}")
            if attempt < retries:
                time.sleep(2)

    raise RuntimeError(f"Ollama failed after {retries} retries: {last_error}")


def _tail_context(text: str, max_chars: int = 1200) -> str:
    """Lấy phần cuối của text làm preceding context cho chunk tiếp theo."""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    tail = text[-max_chars:]
    newline_pos = tail.find("\n")
    if newline_pos > 0:
        tail = tail[newline_pos:].strip()
    return tail


def polish_file(input_path: Path, output_path: Path, args: argparse.Namespace) -> None:
    raw_text = clean_source_noise(input_path.read_text(encoding="utf-8")).strip()
    raw_text = clean_for_audiobook_tts(raw_text)

    if not raw_text:
        print(f"[SKIP] File rỗng: {input_path}")
        return

    prompt_profile = getattr(args, "prompt_profile", "full")
    char_map_file = getattr(args, "char_map_file", "")
    char_map = load_char_map(char_map_file)
    genre = getattr(args, "genre", "") or infer_genre_from_char_map(char_map)
    story_id = str(getattr(args, "story_id", "") or "")
    story_slug = str(getattr(args, "story_slug", "") or input_path.parent.name)
    story_memory = load_story_memory(
        story_memory_dir=getattr(args, "story_memory_dir", ""),
        story_id=story_id,
        slug=story_slug,
        char_map_file=char_map_file,
    )

    # Alias normalization: chuẩn hóa tên sai trước khi gửi Ollama hoặc clean-only.
    aliases = parse_aliases(char_map) if char_map else {}
    if aliases:
        normalized = apply_aliases(raw_text, aliases)
        if normalized != raw_text:
            print(f"[ALIAS] Đã chuẩn hóa {sum(1 for k in aliases if k in raw_text.lower())} alias(es)")
            raw_text = normalized
    memory_normalized = apply_story_memory_replacements(raw_text, story_memory)
    if memory_normalized != raw_text:
        print("[STORY_MEMORY] Đã chuẩn hóa tên/thuật ngữ theo story memory")
        raw_text = memory_normalized

    polish_mode = getattr(args, "polish_mode", "llm")
    if polish_mode == "clean":
        issues = validate_tts_text(raw_text)
        if issues:
            print("[WARN] Format TTS còn vấn đề:")
            for issue in issues:
                print(f"  - {issue}")
        warn_addressing_quality(raw_text, input_path.name)
        memory_issues = find_story_memory_quality_issues(raw_text, story_memory, genre=genre)
        if memory_issues:
            print(f"[STORY_MEMORY WARN] {input_path.name}: {len(memory_issues)} issue(s)")
            for issue in memory_issues[:12]:
                print(f"  - {issue}")
            if len(memory_issues) > 12:
                print(f"  - ... {len(memory_issues) - 12} more")
            if getattr(args, "fail_on_story_memory_issues", False):
                raise RuntimeError(f"Story memory QA failed for {input_path.name}: {memory_issues[:5]}")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(raw_text.strip() + "\n", encoding="utf-8")
        print(f"Đã lưu clean-only: {output_path}")
        return

    chunks = split_text(raw_text, args.max_chars_per_chunk)
    char_map_note = f", char_map={'yes' if char_map else 'no'}"
    print(
        f"\n=== {input_path.name}: {len(raw_text)} chars -> {len(chunks)} chunks, "
        f"prompt={prompt_profile}, genre={genre or 'default'}{char_map_note}, {story_memory_status(story_memory)} ==="
    )

    min_ratio = getattr(args, "min_output_ratio", 0.70)

    def _call_polish(text: str, preceding: str, smc: str, repair_hints: str = "") -> str:
        result = call_ollama(
            base_url=args.ollama_url,
            model=args.model,
            text=text,
            temperature=args.temperature,
            num_ctx=args.num_ctx,
            timeout=args.timeout,
            retries=args.retries,
            keep_alive=getattr(args, "keep_alive", "30m"),
            prompt_profile=prompt_profile,
            genre=genre,
            char_map=char_map,
            preceding_context=preceding,
            story_memory_context=smc,
            session=session,
            no_think=True,
            repair_hints=repair_hints,
        )
        result = apply_story_memory_replacements(result, story_memory)
        result = clean_for_audiobook_tts(result)
        return format_reader_polished_content(result, {})

    def _retry_with_sub_chunks(chunk: str, preceding: str, label: str) -> str:
        """Tách chunk thành 2 nửa nhỏ hơn và retry từng nửa."""
        mid = len(chunk) // 2
        split_pos = chunk.rfind("\n\n", 0, mid)
        if split_pos < 80:
            split_pos = chunk.rfind("\n", 0, mid)
        if split_pos < 50:
            split_pos = mid
        sub_chunks = [s.strip() for s in [chunk[:split_pos], chunk[split_pos:]] if s.strip()]
        parts: list[str] = []
        sub_preceding = preceding
        for si, sub in enumerate(sub_chunks, 1):
            print(f"  [{label}] retry sub-chunk {si}/{len(sub_chunks)} ({len(sub)} chars)")
            sub_smc = build_story_memory_prompt(
                story_memory,
                f"{sub_preceding}\n\n{sub}".strip(),
                genre=genre,
            )
            sub_polished = _call_polish(sub, sub_preceding, sub_smc)
            if has_editorial_noise(sub_polished) or output_too_short(sub, sub_polished, min_ratio):
                print(f"  [{label}] sub-chunk {si} still bad; using raw sub-chunk")
                parts.append(sub)
            else:
                parts.append(sub_polished)
            sub_preceding = _tail_context(parts[-1])
        return "\n\n".join(parts)

    max_quality_retries = getattr(args, "max_quality_retries", 2)
    polished_chunks: list[str] = []
    preceding_context = ""
    with requests.Session() as session:
        for idx, chunk in enumerate(chunks, start=1):
            print(f"[{idx}/{len(chunks)}] Polish {len(chunk)} chars" + (f" +ctx={len(preceding_context)}c" if preceding_context else ""))
            story_memory_context = build_story_memory_prompt(
                story_memory,
                f"{preceding_context}\n\n{chunk}".strip(),
                genre=genre,
            )

            polished: str | None = None
            repair_hints_for_chunk = ""
            for q_attempt in range(max_quality_retries + 1):
                attempt = _call_polish(chunk, preceding_context, story_memory_context, repair_hints=repair_hints_for_chunk)

                # Existing checks: editorial noise and length — always use sub-chunk retry, no quality retry
                if has_editorial_noise(attempt):
                    print(f"[WARN] Polish chunk {idx} có editorial noise; retry với sub-chunks.")
                    polished = _retry_with_sub_chunks(chunk, preceding_context, f"{idx}/{len(chunks)}")
                    break
                if output_too_short(chunk, attempt, min_ratio):
                    print(f"[WARN] Polish chunk {idx} ngắn bất thường; retry với sub-chunks.")
                    polished = _retry_with_sub_chunks(chunk, preceding_context, f"{idx}/{len(chunks)}")
                    break

                # Quality check: detect blocking issues and retry with repair prompt
                if _QUALITY_RETRY_AVAILABLE and BLOCKING_QUALITY_ISSUES:
                    q_issues = _ext_check_quality(attempt, genre=genre, char_map_path=char_map_file)
                    blocking = [i for i in q_issues if any(i.startswith(b) for b in BLOCKING_QUALITY_ISSUES)]
                    if blocking:
                        if q_attempt >= max_quality_retries:
                            print(f"[QUALITY_FAIL] chunk {idx}/{len(chunks)}: {blocking} (gave up after {max_quality_retries} retries)")
                        else:
                            print(f"[QUALITY_RETRY] chunk {idx}/{len(chunks)} attempt {q_attempt + 1}: {blocking}")
                            repair_hints_for_chunk = "\n".join(f"- {issue_to_repair_hint(i)}" for i in blocking)
                            continue

                polished = attempt
                break

            polished_chunks.append(polished or "")
            # Cập nhật preceding context từ kết quả vừa polish
            preceding_context = _tail_context(polished or "")

    final_text = "\n\n".join(polished_chunks)
    final_text = apply_story_memory_replacements(final_text, story_memory)
    final_text = clean_for_audiobook_tts(final_text)
    final_text = format_reader_polished_content(final_text, {})

    # Final chapter-level quality check — catches cross-chunk issues (e.g. repeated_content
    # spanning two chunks) that per-chunk retry couldn't see.
    if _QUALITY_RETRY_AVAILABLE:
        final_q_issues = _ext_check_quality(final_text, genre=genre, char_map_path=char_map_file)
        if final_q_issues:
            blocking_final = [i for i in final_q_issues if any(i.startswith(b) for b in BLOCKING_QUALITY_ISSUES)]
            warn_only = [i for i in final_q_issues if i not in blocking_final]
            if blocking_final:
                print(f"[QUALITY_FAIL] {input_path.name} final: {', '.join(blocking_final)}")
            if warn_only:
                print(f"[QUALITY_WARN] {input_path.name} final: {', '.join(warn_only)}")

    issues = validate_tts_text(final_text)
    if issues:
        print("[WARN] Format TTS còn vấn đề:")
        for issue in issues:
            print(f"  - {issue}")
    warn_addressing_quality(final_text, input_path.name)
    memory_issues = find_story_memory_quality_issues(final_text, story_memory, genre=genre)
    if memory_issues:
        print(f"[STORY_MEMORY WARN] {input_path.name}: {len(memory_issues)} issue(s)")
        for issue in memory_issues[:12]:
            print(f"  - {issue}")
        if len(memory_issues) > 12:
            print(f"  - ... {len(memory_issues) - 12} more")
        if getattr(args, "fail_on_story_memory_issues", False):
            raise RuntimeError(f"Story memory QA failed for {input_path.name}: {memory_issues[:5]}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(final_text.strip() + "\n", encoding="utf-8")
    print(f"Đã lưu: {output_path}")


def path_candidates(path: Path) -> list[str]:
    resolved = path.resolve()
    candidates = [path.as_posix(), resolved.as_posix()]
    try:
        candidates.append(resolved.relative_to(ROOT).as_posix())
    except ValueError:
        pass
    return list(dict.fromkeys(candidates))


def sync_polished_to_db(input_path: Path, output_path: Path) -> None:
    try:
        from story_db.story_pipeline_db import repository as repo

        row = repo.update_chapter_polished_by_raw_path(
            path_candidates(input_path),
            polished_text_path=output_path.as_posix(),
            polished_text_content=output_path.read_text(encoding="utf-8"),
        )
        if row:
            print(f"[DB] synced polished chapter: {input_path.name}")
        else:
            print(f"[DB] no chapter matched raw path: {input_path}")
    except Exception as exc:
        print(f"[DB WARN] không sync được polished path: {exc}")


def main() -> None:
    # Tip tốc độ: OLLAMA_FLASH_ATTENTION=1 ollama serve  →  giảm 20-40% inference time.
    parser = argparse.ArgumentParser(description="Polish chapter text bằng Ollama/Qwen trước khi TTS.")
    parser.add_argument("--input-dir", required=True, help="Folder chứa chapterX.txt raw.")
    parser.add_argument("--output-root", default="story_data/polished")
    parser.add_argument("--chapter", type=int, default=0, help="0 nghĩa là dùng --all hoặc mặc định chapter1.")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default="qwen3:14b")
    parser.add_argument("--temperature", type=float, default=0.25)
    parser.add_argument("--num-ctx", type=int, default=6144)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--keep-alive", default="30m")
    parser.add_argument("--max-chars-per-chunk", type=int, default=3500)
    parser.add_argument(
        "--min-output-ratio",
        type=float,
        default=0.70,
        help=(
            "Nếu output của một chunk ngắn hơn tỷ lệ này so với input (tính ký tự không kể whitespace), "
            "fallback về clean-only chunk để tránh mất ý. "
            "0.70 là ngưỡng an toàn — polish tốt tự nhiên rút ngắn câu thừa; "
            "chỉ nâng cao nếu model hay bỏ đoạn. 0 = tắt kiểm tra."
        ),
    )
    parser.add_argument(
        "--char-map-file",
        default="",
        help=(
            "Đường dẫn file nhân vật (character map) chứa thông tin giọng nói, giới tính, xưng hô từng nhân vật. "
            "Sẽ được inject vào system prompt. Ví dụ: story_data/char_maps/21180-vinh-thoai-hiep-si.txt"
        ),
    )
    parser.add_argument(
        "--story-memory-dir",
        default="",
        help=(
            "Root story memory hoặc thư mục memory cụ thể. Nếu bỏ trống, script tự tìm theo "
            "story_data/story_memory/{story_id}-{slug} từ story id/char-map/tên folder input."
        ),
    )
    parser.add_argument(
        "--fail-on-story-memory-issues",
        action="store_true",
        help="Nếu story memory QA phát hiện lỗi tên/thuật ngữ/register, dừng thay vì chỉ cảnh báo.",
    )
    parser.add_argument(
        "--prompt-profile",
        choices=("fast", "full"),
        default="full",
        help="fast dùng prompt ngắn để giảm prompt-eval; full dùng prompt chi tiết như cũ.",
    )
    parser.add_argument(
        "--polish-mode",
        choices=("llm", "clean"),
        default="llm",
        help="llm gọi Ollama; clean chỉ chuẩn hóa text cho TTS, rất nhanh nhưng không rewrite bằng AI.",
    )
    parser.add_argument(
        "--genre",
        default="",
        help="Thể loại truyện: tien_hiep, huyen_huyen, he_thong, kiem_hiep, do_thi, xuyen_khong, mat_the, vong_du, lang_man, western_fantasy. Để trống để dùng prompt mặc định.",
    )
    parser.add_argument(
        "--max-quality-retries",
        type=int,
        default=2,
        help=(
            "Số lần retry tối đa khi một chunk bị quality check fail (not_vietnamese, "
            "cjk_not_translated, repeated_content, large_en_block). Mỗi retry dùng repair prompt "
            "với mô tả lỗi cụ thể. 0 = tắt quality retry (chỉ log). Default: 2."
        ),
    )
    args = parser.parse_args()

    input_dir = Path(args.input_dir).resolve()
    if not input_dir.is_dir():
        raise SystemExit(f"Không tìm thấy input-dir: {input_dir}")

    if args.all:
        input_files = list_chapter_files(input_dir)
    else:
        chapter_num = args.chapter or 1
        input_files = [input_dir / f"chapter{chapter_num}.txt"]

    input_files = [path for path in input_files if path.exists()]
    if not input_files:
        raise SystemExit("Không có chapter file để polish.")

    output_dir = Path(args.output_root) / input_dir.name
    for input_path in input_files:
        output_path = output_dir / input_path.name
        if output_path.exists() and not args.overwrite:
            print(f"[SKIP] Đã tồn tại: {output_path}")
            sync_polished_to_db(input_path, output_path)
            continue
        try:
            polish_file(input_path, output_path, args)
            sync_polished_to_db(input_path, output_path)
        except Exception as exc:
            print(f"[ERROR] {input_path.name}: {exc}")

    print(f"\nHoàn tất. Text đã polish nằm trong: {output_dir}")


if __name__ == "__main__":
    main()
