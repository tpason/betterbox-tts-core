from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = ROOT / "scripts" / "story_pipeline"
for path in (ROOT, SCRIPT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from reader_content_format import format_polished_content
from reformat_polished_chapter_content import update_chapter_content


def chapter_job(title: str = "Chương 09 - Mỗi ngày mỗi khác") -> dict:
    return {"chapter_title": title, "payload": {"chapter_title": title}}


class FormatPolishedContentTest(unittest.TestCase):
    def test_removes_title_separator_and_tail_editorial_notes(self) -> None:
        content = """Chương 09 - Mỗi ngày mỗi khác

(+)

Lệnh chuẩn bị chiến đấu đã được ban ra, nhưng không có cuộc họp bàn chiến thuật chi tiết nào cả.

Tất cả những gì họ được nghe chỉ là: sẵn sàng.

"Mày có đưa tao sợi chỉ nào đâu."

Những thay đổi:
- Cải thiện văn phong và dấu câu.
- Giữ nguyên tên riêng.
"""

        self.assertEqual(
            format_polished_content(content, chapter_job()),
            'Lệnh chuẩn bị chiến đấu đã được ban ra, nhưng không có cuộc họp bàn chiến thuật chi tiết nào cả. '
            'Tất cả những gì họ được nghe chỉ là: sẵn sàng.\n\n'
            '"Mày có đưa tao sợi chỉ nào đâu."',
        )

    def test_removes_generic_chapter_heading_without_known_title(self) -> None:
        content = """Chapter 12: A Bad Morning

He opened his eyes.

The room was silent.
"""

        self.assertEqual(
            format_polished_content(content, {}),
            "He opened his eyes. The room was silent.",
        )

    def test_does_not_remove_character_name_that_contains_chuong(self) -> None:
        content = """Mặc Chương Yêu.

"Mỗi đầu Mặc Chương Yêu, bất quá ba mươi xúc tu," Tô Tỉnh nhướng mày.
"""

        self.assertEqual(
            format_polished_content(content, {}),
            'Mặc Chương Yêu.\n\n"Mỗi đầu Mặc Chương Yêu, bất quá ba mươi xúc tu," Tô Tỉnh nhướng mày.',
        )

    def test_keeps_story_note_in_body_but_removes_editorial_tail_note(self) -> None:
        content = """Hắn mở quyển sách cũ.

Ghi chú:

Dòng chữ này vốn đã có trong cổ tịch.

Đã chỉnh sửa:
- Đã sửa dấu câu.
"""

        self.assertEqual(
            format_polished_content(content, {}),
            "Hắn mở quyển sách cũ. Ghi chú: Dòng chữ này vốn đã có trong cổ tịch.",
        )

    def test_preserves_system_stats_and_sound_breaks(self) -> None:
        content = """Đinh!

Thông tin nhân vật hiện ra trước mắt.

Tên: Hàn Tuyệt. Tuổi thọ: mười một trên sáu mươi lăm.
"""

        self.assertEqual(
            format_polished_content(content, {}),
            "Đinh!\n\nThông tin nhân vật hiện ra trước mắt. Tên: Hàn Tuyệt. Tuổi thọ: mười một trên sáu mươi lăm.",
        )

    def test_splits_long_paragraph_on_sentence_boundaries(self) -> None:
        sentences = [f"Câu thứ {index} có nội dung đủ dài để kiểm tra tách đoạn." for index in range(1, 20)]
        result = format_polished_content(" ".join(sentences), {})

        self.assertGreater(len(result.split("\n\n")), 1)
        self.assertNotIn("  ", result)
        self.assertTrue(result.endswith("."))

    def test_is_idempotent_for_clean_content(self) -> None:
        content = 'Hắn mở mắt. Căn phòng vẫn im lặng.\n\n"Ngươi đến rồi?"'

        first = format_polished_content(content, {})
        second = format_polished_content(first, {})

        self.assertEqual(second, first)


class UpdateChapterContentTest(unittest.TestCase):
    def test_update_clears_reader_cache_when_columns_exist(self) -> None:
        conn = FakeConnection()

        update_chapter_content(conn, {"id": "chapter-1"}, "clean\n", clear_reader_cache=True)

        sql, params = conn.calls[0]
        self.assertIn("reader_formatted_text_content = NULL", sql)
        self.assertIn("reader_formatted_source_hash = NULL", sql)
        self.assertEqual(params, ("clean\n", "chapter-1"))

    def test_update_does_not_reference_reader_cache_when_columns_missing(self) -> None:
        conn = FakeConnection()

        update_chapter_content(conn, {"id": "chapter-1"}, "clean\n", clear_reader_cache=False)

        sql, params = conn.calls[0]
        self.assertNotIn("reader_formatted_text_content", sql)
        self.assertEqual(params, ("clean\n", "chapter-1"))


class FakeConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def execute(self, sql: str, params: object = None) -> None:
        self.calls.append((sql, params))


if __name__ == "__main__":
    unittest.main()
