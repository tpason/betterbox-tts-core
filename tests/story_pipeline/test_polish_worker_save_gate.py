"""polish_worker must route DB writes through save guard."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "story_pipeline"))

from polish_worker import QualityGateError, _save_chapter_to_db  # noqa: E402
from chapter_save_guard import SaveGateError  # noqa: E402

_GOOD_VI = (
    "Anh ta bước vào phòng. Cô ấy nhìn anh ta với ánh mắt lạnh lùng. "
    "Không khí trở nên căng thẳng khi họ đối mặt nhau. " * 10
)


def _job() -> dict:
    return {"id": "job-1", "chapter_id": "ch-1"}


def _args() -> argparse.Namespace:
    return argparse.Namespace(
        llm_judge="off",
        story_memory_dir="",
        ollama_url="http://127.0.0.1:11434",
    )


@patch("polish_worker.commit_guarded_chapter_outputs")
def test_save_chapter_delegates_to_guard(mock_commit):
    _save_chapter_to_db(
        _job(),
        _args(),
        polished_text=_GOOD_VI,
        gate_source_text="source",
        genre="western_fantasy",
    )
    mock_commit.assert_called_once()
    assert mock_commit.call_args[0][0] == "ch-1"


@patch("polish_worker.commit_guarded_chapter_outputs", side_effect=SaveGateError(["term_alignment:hand_seal"]))
def test_save_chapter_raises_quality_gate_on_block(mock_commit):
    with pytest.raises(QualityGateError, match="save_gate"):
        _save_chapter_to_db(
            _job(),
            _args(),
            polished_text="thế kí " + _GOOD_VI,
            gate_source_text="hand seal",
            genre="korean_cultivation",
        )
