from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = ROOT / "scripts" / "story_pipeline"
for path in (ROOT, SCRIPT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from translate_chapters_from_db import (  # noqa: E402
    build_metadata_translation_context,
    chapter_title_from_content,
)


class MetadataSyncTest(unittest.TestCase):
    def test_chapter_title_from_polished_first_line(self) -> None:
        text = "Chương 1: Khởi Đầu\n\nHắn bước vào rừng sâu."
        self.assertEqual(chapter_title_from_content(text), "Chương 1: Khởi Đầu")

    def test_chapter_title_rejects_prose_first_line(self) -> None:
        text = "Anh ta đi vào rừng. Rồi gặp một kẻ lạ."
        self.assertEqual(chapter_title_from_content(text), "")

    def test_metadata_context_includes_genre_from_title(self) -> None:
        ctx = build_metadata_translation_context(
            original_title="A Regressor's Tale of Cultivation",
            raw_language="en",
            source_code="wetriedtls",
        )
        self.assertIn("korean_cultivation", ctx)

    def test_metadata_context_includes_story_memory_glossary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mem_dir = Path(tmp)
            glossary = [{
                "source_terms": ["Test Term"],
                "canonical_vi": "Thuật Ngữ Thử",
                "priority": True,
            }]
            (mem_dir / "glossary.json").write_text(json.dumps(glossary), encoding="utf-8")
            ctx = build_metadata_translation_context(
                story_id="test-id",
                story_slug_value="test-slug",
                description="Test Term appears in this story",
                story_memory_dir=str(mem_dir),
                raw_language="en",
                source_code="wetriedtls",
            )
        self.assertIn("story_memory_excerpt", ctx)
        self.assertIn("Thuật Ngữ Thử", ctx)


if __name__ == "__main__":
    unittest.main()
