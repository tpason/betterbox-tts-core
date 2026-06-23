"""Tests for novel_translation_adapter routing."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "story_pipeline"))

from novel_translation_adapter import resolve_translation_engine  # noqa: E402


def _args(engine: str = "auto") -> argparse.Namespace:
    return argparse.Namespace(translation_engine=engine)


def test_resolve_auto_en_novel():
    assert resolve_translation_engine(_args("auto"), "en") == "novel"


def test_resolve_auto_zh_legacy():
    assert resolve_translation_engine(_args("auto"), "zh") == "legacy"


def test_resolve_explicit_legacy():
    assert resolve_translation_engine(_args("legacy"), "en") == "legacy"


def test_resolve_explicit_novel():
    assert resolve_translation_engine(_args("novel"), "zh") == "novel"
