"""Tests for pipeline orchestration without Ollama."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from scripts.story_pipeline.novel_translation import pipeline
from scripts.story_pipeline.novel_translation.context import StoryContext
from scripts.story_pipeline.novel_translation.pipeline import (
    ChunkResult,
    PipelineConfig,
    PipelineResult,
    translate_chapter,
)
from scripts.story_pipeline.novel_translation.qa import QAReport
from scripts.story_pipeline.novel_translation.qa import Violation


def test_pipeline_result_fails_when_final_quality_blocks():
    result = PipelineResult(
        story_id="s1",
        chapter_id="chapter_0001",
        polished_text="Đây là một đoạn tiếng Việt đủ dài để không bị rỗng.",
        final_quality_blocking=["large_en_block:1"],
    )
    assert result.success is False
    assert result.needs_review is True


def test_pipeline_result_fails_when_chunk_qa_blocks():
    report = QAReport(chunk_id="c001")
    report.violations.append(Violation(
        type="format_error",
        line_id="p001_n17",
        current="(missing)",
        expected="translated line",
        reason="Line_id present in source but missing from translation",
        confidence=0.95,
        patch_risk="high",
    ))
    result = PipelineResult(
        story_id="s1",
        chapter_id="chapter_0001",
        polished_text="Đây là bản dịch thử.",
        chunk_results=[ChunkResult(
            chunk_id="c001",
            translation={"lines": []},
            resolution={},
            qa_report=report,
            polished_lines=[],
            polish_warnings=[],
        )],
    )
    assert result.success is False
    assert result.needs_review is True


def test_translate_chapter_max_chunks_returns_partial_without_final_qa(monkeypatch):
    processed: list[str] = []

    def fake_load_story_context(**kwargs):
        return StoryContext(story_id="s1", slug="story", genre="western_fantasy")

    def fake_process_chunk(chunk, ctx, cfg, chapter_number):
        processed.append(chunk.chunk_id)
        return ChunkResult(
            chunk_id=chunk.chunk_id,
            translation={"lines": [{"line_id": chunk.segments[0].line_id, "text_vi": "Anh ta bước đi thật nhanh."}]},
            resolution={},
            qa_report=QAReport(chunk_id=chunk.chunk_id),
            polished_lines=[{"line_id": chunk.segments[0].line_id, "text_vi": "Anh ta bước đi thật nhanh."}],
            polish_warnings=[],
        )

    def fake_quality_check(*args, **kwargs):
        return [], []

    def fail_final_qa(*args, **kwargs):
        raise AssertionError("final QA must not run for partial debug runs")

    monkeypatch.setattr(pipeline, "load_story_context", fake_load_story_context)
    monkeypatch.setattr(pipeline, "_process_chunk", fake_process_chunk)
    monkeypatch.setattr(pipeline, "run_final_qa", fail_final_qa)

    import scripts.story_pipeline.check_translation_quality as quality
    monkeypatch.setattr(quality, "run_full_quality_check", fake_quality_check)

    source_text = "\n".join([
        "Enkrid walked forward.",
        "The sun was low.",
        "Krang appeared by the gate.",
        "Shinar raised her blade.",
    ])
    result = translate_chapter(
        source_text=source_text,
        story_id="s1",
        slug="story",
        genre="western_fantasy",
        chapter_number=1,
        cfg=PipelineConfig(max_chunks=1, chunk_size=40, skip_polish=True),
    )

    assert len(processed) == 1
    assert result.is_partial is True
    assert result.success is False
    assert result.polished_text
