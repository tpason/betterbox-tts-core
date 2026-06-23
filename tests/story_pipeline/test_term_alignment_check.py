"""Tests for term_alignment_check."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "story_pipeline"))

from term_alignment_check import check_term_alignment  # noqa: E402


def test_hand_seal_forbids_the_ki():
    source = "After making this short vow, I formed a hand seal."
    bad_vi = "Sau lời thề, tôi tạo ra một thế kí."
    issues = check_term_alignment(source, bad_vi, genre="korean_cultivation")
    assert any("hand_seal" in i for i in issues)


def test_hand_seal_allows_an_quyet():
    source = "I formed a hand seal. The hand seal was activated."
    good_vi = "Tôi kết một ấn quyết. Ấn quyết được kích hoạt."
    issues = check_term_alignment(source, good_vi, genre="korean_cultivation")
    assert not issues


def test_no_source_no_issues():
    assert check_term_alignment("", "thế kí trong văn bản.", genre="korean_cultivation") == []
