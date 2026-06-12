from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = ROOT / "scripts" / "story_pipeline"
for path in (ROOT, SCRIPT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from story_memory import (  # noqa: E402
    apply_seed_glossary_replacements,
    apply_story_memory_replacements,
    find_story_memory_quality_issues,
    load_story_memory,
)

MEMORY_DIR = (
    ROOT
    / "story_data"
    / "story_memory"
    / "1a1af87a-e85e-476f-87b7-1aeac2dadb1d-a-regressors-tale-of-cultivation"
)


class RegressorsTaleMemoryTest(unittest.TestCase):
    def test_story_glossary_normalizes_known_literal_drift(self) -> None:
        memory = load_story_memory(story_memory_dir=str(MEMORY_DIR))
        memory = apply_seed_glossary_replacements(memory, "korean_cultivation")

        text = (
            "Đường Thăng Thiên mở ra trước mắt. Rễ thần linh của hắn rung động, "
            "rồi lưỡi kiếm mềm hóa thành một luồng sáng."
        )

        normalized = apply_story_memory_replacements(text, memory)

        self.assertIn("Thăng Thiên Lộ", normalized)
        self.assertIn("Linh Căn", normalized)
        self.assertIn("Kiếm Ti", normalized)
        self.assertNotIn("Đường Thăng Thiên", normalized)
        self.assertNotIn("Rễ thần linh", normalized)
        self.assertNotIn("lưỡi kiếm mềm", normalized)

    def test_korean_cultivation_dialogue_warns_on_ban(self) -> None:
        memory = load_story_memory(story_memory_dir=str(MEMORY_DIR))
        text = 'Seo Eun-hyun nhìn đồng nghiệp rồi hỏi: "Bạn còn nhớ chuyện ở công ty không?"'

        issues = find_story_memory_quality_issues(text, memory, genre="korean_cultivation")

        self.assertTrue(any("lời thoại có `bạn`" in issue for issue in issues))

    def test_modern_office_scene_warns_on_archaic_address(self) -> None:
        memory = load_story_memory(story_memory_dir=str(MEMORY_DIR))
        text = 'Trong văn phòng công ty, trưởng phòng cau mày nói: "Ngươi đang làm gì vậy?"'

        issues = find_story_memory_quality_issues(text, memory, genre="korean_cultivation")

        self.assertTrue(any("cảnh hiện đại/công sở" in issue for issue in issues))


if __name__ == "__main__":
    unittest.main()
