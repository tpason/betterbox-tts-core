from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = ROOT / "scripts" / "story_pipeline"
for path in (ROOT, SCRIPT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

sys.modules.setdefault("requests", types.ModuleType("requests"))

from polish_chapter_texts_ollama import find_addressing_quality_issues as polish_issues
from translate_chapter_texts_ollama import find_addressing_quality_issues as translate_issues


class AddressingQualityTest(unittest.TestCase):
    def test_flags_polite_address_in_hostile_context(self) -> None:
        text = (
            'Tên cướp rút dao găm, xông lên chặn trước mặt Enkrid.\n\n'
            '"Anh là ai?" hắn hỏi, giọng đầy khiêu khích.'
        )

        self.assertTrue(polish_issues(text))
        self.assertTrue(translate_issues(text))

    def test_does_not_flag_non_hostile_innkeeper_polite_address(self) -> None:
        text = '"Anh thực sự định ăn ở đây sao?" chủ quán trọ hỏi, vẻ mặt lo lắng.'

        self.assertEqual(polish_issues(text), [])
        self.assertEqual(translate_issues(text), [])

    def test_flags_young_speaker_using_may_tao(self) -> None:
        text = 'Đứa trẻ nhìn người đàn ông lớn tuổi rồi gằn giọng: "Mày tránh ra cho tao."'

        self.assertTrue(polish_issues(text))
        self.assertTrue(translate_issues(text))


if __name__ == "__main__":
    unittest.main()
