"""Tests for quality_remediation router."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "story_pipeline"))

from quality_remediation import route_repair_action  # noqa: E402


def test_term_alignment_routes_retranslate():
    assert route_repair_action(["term_alignment:hand_seal:'thế kí'"]) == "retranslate"


def test_repeated_content_routes_repolish():
    assert route_repair_action(["repeated_content"]) == "repolish"


def test_judge_mistranslation_routes_retranslate():
    assert route_repair_action(["judge:mistranslation"]) == "retranslate"


def test_judge_unnatural_routes_repolish():
    assert route_repair_action(["judge:unnatural"]) == "repolish"
