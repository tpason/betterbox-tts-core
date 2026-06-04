from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = ROOT / "scripts" / "story_pipeline"
for path in (ROOT, SCRIPT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from genre_prompts import (
    GENRE_HE_THONG,
    GENRE_HUYEN_HUYEN,
    GENRE_TIEN_HIEP,
    GENRE_WESTERN_FANTASY,
    detect_genre,
    get_translate_genre_addendum,
    resolve_genre_from_context,
)


class GenrePromptsTest(unittest.TestCase):
    def test_korean_web_novel_defaults_to_western_fantasy(self) -> None:
        self.assertEqual(
            detect_genre("Korean Web Novel", raw_language="ko", source_code="naver_series"),
            GENRE_WESTERN_FANTASY,
        )

    def test_korean_or_naver_fantasy_is_not_chinese_huyen_huyen(self) -> None:
        self.assertEqual(
            detect_genre("Korean, Naver Series, Fantasy", raw_language="ko", source_code="naver_series"),
            GENRE_WESTERN_FANTASY,
        )

    def test_english_fantasy_is_western_fantasy(self) -> None:
        self.assertEqual(
            detect_genre("Fantasy, Academy, Action", raw_language="en", source_code="royalroad"),
            GENRE_WESTERN_FANTASY,
        )

    def test_chinese_fantasy_still_uses_huyen_huyen(self) -> None:
        self.assertEqual(
            detect_genre("Fantasy, Huyền huyễn", raw_language="zh", source_code="qidian"),
            GENRE_HUYEN_HUYEN,
        )

    def test_chinese_xianxia_keeps_tien_hiep_priority(self) -> None:
        self.assertEqual(
            detect_genre("Tiên hiệp, Hệ thống", raw_language="zh", source_code="qidian"),
            GENRE_TIEN_HIEP,
        )

    def test_system_addendum_distinguishes_chinese_from_litrpg(self) -> None:
        addendum = get_translate_genre_addendum(GENRE_HE_THONG)

        self.assertIn("Trung/tu luyện", addendum)
        self.assertIn("LitRPG/fantasy Hàn/Tây", addendum)

    def test_char_map_fills_missing_genre(self) -> None:
        char_map = "## Thể loại: Fantasy kiểu Korean light novel\n## Không dùng từ Hán Việt cổ phong"

        self.assertEqual(
            resolve_genre_from_context("", raw_language="vi", source_code="hako", char_map=char_map),
            GENRE_WESTERN_FANTASY,
        )

    def test_char_map_overrides_ambiguous_fantasy(self) -> None:
        char_map = "## Thể loại: Fantasy phương Tây\n### Enkrid\n- Ngôi thứ ba: anh ta"

        self.assertEqual(
            resolve_genre_from_context("Fantasy", raw_language="vi", source_code="hako", char_map=char_map),
            GENRE_WESTERN_FANTASY,
        )


if __name__ == "__main__":
    unittest.main()
