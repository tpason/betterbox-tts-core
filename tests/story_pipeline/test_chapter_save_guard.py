"""Tests for chapter_save_guard — DB write must not happen when quality fails."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "story_pipeline"))

from chapter_save_guard import SaveGateError, commit_guarded_chapter_outputs, verify_chapter_save_allowed  # noqa: E402
from novel_translation.pipeline import PipelineResult  # noqa: E402

_GOOD_VI = (
    "Anh ta bước vào phòng. Cô ấy nhìn anh ta với ánh mắt lạnh lùng. "
    "Không khí trở nên căng thẳng khi họ đối mặt nhau. "
    "Một tiếng động vang lên từ hành lang bên ngoài. " * 8
)


def _args(llm_judge: str = "warn") -> argparse.Namespace:
    return argparse.Namespace(
        llm_judge=llm_judge,
        ollama_url="http://127.0.0.1:11434",
        translate_model="qwen3:14b",
        judge_model="qwen3:14b",
        story_memory_dir="",
    )


def test_blocks_hand_seal_the_ki():
    source = "I formed a hand seal. The hand seals were perfected."
    bad_vi = (
        "Tôi tạo ra một thế kí. Những thế kí hoàn hảo qua việc thấu hiểu. "
        "Một đợt năng lượng bùng phát, thế kí được kích hoạt. " * 5
    )
    with pytest.raises(SaveGateError) as exc:
        verify_chapter_save_allowed(
            bad_vi,
            source_text=source,
            genre="korean_cultivation",
            args=_args(),
        )
    assert any("hand_seal" in b or "term" in b.lower() or "thế" in b for b in exc.value.blocking)


def test_judge_error_blocks_when_novel_engine():
    source = "He walked into the room and looked around carefully."
    with patch("chapter_save_guard.judge_chapter_quality") as mock_judge:
        mock_judge.return_value = MagicMock(error="timeout", issues=[], warnings=[])
        with pytest.raises(SaveGateError) as exc:
            verify_chapter_save_allowed(
                _GOOD_VI,
                source_text=source,
                genre="western_fantasy",
                args=_args(llm_judge="warn"),
                novel_engine=True,
            )
    assert any("judge_error" in b for b in exc.value.blocking)


def test_judge_error_warn_only_when_warn_mode():
    source = "He walked into the room and looked around carefully."
    with patch("chapter_save_guard.judge_chapter_quality") as mock_judge:
        mock_judge.return_value = MagicMock(error="timeout", issues=[], warnings=[])
        blocking, warnings = verify_chapter_save_allowed(
            _GOOD_VI,
            source_text=source,
            genre="western_fantasy",
            args=_args(llm_judge="warn"),
            novel_engine=False,
        )
    assert not blocking
    assert any("judge_error" in w for w in warnings)


def test_pipeline_partial_blocks():
    pr = PipelineResult(
        story_id="s",
        chapter_id="c",
        polished_text=_GOOD_VI,
        is_partial=True,
    )
    with pytest.raises(SaveGateError) as exc:
        verify_chapter_save_allowed(
            _GOOD_VI,
            pipeline_result=pr,
            novel_engine=True,
            translated_text=_GOOD_VI,
            args=_args(),
        )
    assert "pipeline_partial" in exc.value.blocking


@patch("story_db.story_pipeline_db.repository.update_chapter_text_outputs")
@patch("chapter_save_guard.judge_chapter_quality")
def test_commit_skips_db_on_block(mock_judge, mock_update):
    mock_judge.return_value = MagicMock(error="", issues=[], warnings=[])
    source = "I formed a hand seal."
    bad_vi = "Tôi tạo thế kí. " + _GOOD_VI
    with pytest.raises(SaveGateError):
        commit_guarded_chapter_outputs(
            "chapter-uuid",
            polished_text=bad_vi,
            verify_kwargs={
                "source_text": source,
                "genre": "korean_cultivation",
                "args": _args(),
            },
        )
    mock_update.assert_not_called()


@patch("story_db.story_pipeline_db.repository.update_chapter_text_outputs")
@patch("chapter_save_guard.judge_chapter_quality")
def test_commit_writes_db_when_pass(mock_judge, mock_update):
    mock_judge.return_value = MagicMock(error="", issues=[], warnings=[])
    source = "He walked into the room."
    with patch("chapter_save_guard.check_term_alignment", return_value=[]):
        commit_guarded_chapter_outputs(
            "chapter-uuid",
            polished_text=_GOOD_VI,
            translated_text=_GOOD_VI,
            verify_kwargs={
                "source_text": source,
                "genre": "western_fantasy",
                "args": _args(llm_judge="off"),
            },
        )
    mock_update.assert_called_once()
    assert mock_update.call_args.kwargs.get("quality_status") == "pending_audit"
