from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = ROOT / "scripts" / "story_pipeline"
for path in (ROOT, SCRIPT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from extract_term_glossary import (  # noqa: E402
    mine_chunk_candidate_terms,
    resolve_chunk_glossary_supplement,
    should_chunk_glossary_supplement,
)
from genre_prompts import GENRE_KOREAN_CULTIVATION, get_translate_genre_addendum  # noqa: E402
from story_memory import StoryMemory  # noqa: E402


class ChunkGlossaryTest(unittest.TestCase):
    def test_should_supplement_for_korean_cultivation(self) -> None:
        self.assertTrue(should_chunk_glossary_supplement("korean_cultivation"))
        self.assertTrue(should_chunk_glossary_supplement("korean_cultivation,trong_sinh"))
        self.assertFalse(should_chunk_glossary_supplement("western_fantasy"))

    def test_mine_chunk_finds_technique_suffix(self) -> None:
        text = 'He activated the Void Slash Technique against the elder.'
        found = mine_chunk_candidate_terms(text)
        terms = {c["term"] for c in found}
        self.assertTrue(any("Void Slash" in t or "Technique" in t for t in terms))

    def test_resolve_skips_without_ollama(self) -> None:
        memory = StoryMemory(glossary=[{
            "source_terms": ["Qi Refining"],
            "canonical_vi": "Luyện Khí",
        }])
        items, updated = resolve_chunk_glossary_supplement(
            "Qi Refining stage nine.",
            memory=memory,
            genre=GENRE_KOREAN_CULTIVATION,
            ollama_url="",
        )
        self.assertEqual(items, [])
        self.assertIs(updated, memory)

    @patch("extract_term_glossary.call_ollama_glossary")
    def test_resolve_calls_llm_for_unknown_term(self, mock_glossary) -> None:
        mock_glossary.return_value = [{
            "source_terms": ["Crimson Lotus Seal"],
            "canonical_vi": "Hồng Liên Ấn",
            "priority": True,
        }]
        memory = StoryMemory()
        items, updated = resolve_chunk_glossary_supplement(
            "He formed the Crimson Lotus Seal with both hands.",
            memory=memory,
            genre=GENRE_KOREAN_CULTIVATION,
            ollama_url="http://127.0.0.1:11434",
            persist=False,
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(updated.glossary[0]["canonical_vi"], "Hồng Liên Ấn")
        mock_glossary.assert_called_once()

    def test_multi_genre_addendum_includes_cultivation_and_fallback(self) -> None:
        addendum = get_translate_genre_addendum("korean_cultivation,trong_sinh")
        self.assertIn("Luyện Khí", addendum)
        self.assertIn("Ascension Path", addendum)
        self.assertIn("tu sĩ", addendum)


if __name__ == "__main__":
    unittest.main()
