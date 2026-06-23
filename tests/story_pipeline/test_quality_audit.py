"""Tests for quality_audit compose."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "story_pipeline"))

from quality_audit import _should_run_judge, audit_chapter_row  # noqa: E402


def test_judge_sample_deterministic():
    assert _should_run_judge(1, 5, seed="story-a") == _should_run_judge(1, 5, seed="story-a")
    assert _should_run_judge(2, 5, seed="story-a") in {True, False}


def test_audit_chapter_fails_hand_seal_row():
    row = {
        "chapter_id": "00000000-0000-0000-0000-000000000001",
        "chapter_number": 50,
        "story_id": "test",
        "source_code": "wetriedtls",
        "raw_language": "en",
        "raw_text_content": "I formed a hand seal. The hand seals were perfected.",
        "polished_text_content": (
            "Tôi tạo ra một thế kí. "
            "Những thế kí hoàn hảo qua việc thấu hiểu trước khi đột phá có thể được kích hoạt. "
            "Một đợt năng lượng bùng phát, thế kí được kích hoạt. "
            "Sau khi sử dụng thế kí, tôi cảm thấy đau đầu. " * 3
        ),
    }
    result = audit_chapter_row(
        row,
        genre="korean_cultivation",
        tiers=(0, 1),
        judge_sample=0,
    )
    assert not result.passed
    assert any("term_alignment" in b or "golden" in b or "thế" in b for b in result.blocking)
