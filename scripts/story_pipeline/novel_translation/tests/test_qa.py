"""Tests for QA deterministic checks — no Ollama required."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from scripts.story_pipeline.novel_translation.context import (
    CharacterProfile,
    GlossaryEntry,
    StoryContext,
)
from scripts.story_pipeline.novel_translation.qa import (
    QAReport,
    _check_cjk_leakage,
    _check_glossary,
    _check_length_ratio,
    _check_pronoun_drift,
    run_deterministic_qa,
)
from scripts.story_pipeline.novel_translation.segmenter import Chunk, Segment


def _make_chunk(texts=None):
    segs = [Segment(f"p001_n{i:02d}", t, "narration") for i, t in enumerate(texts or ["Hello."])]
    return Chunk(chunk_id="test_c001", chapter_id="chapter_0001", segments=segs)


def _make_ctx(glossary=None):
    ctx = StoryContext(story_id="s1", slug="s", genre="western_fantasy")
    ctx.glossary = glossary or []
    return ctx


def test_check_cjk_leakage_clean():
    report = QAReport(chunk_id="c001")
    _check_cjk_leakage([{"line_id": "p001_n01", "text_vi": "Anh ta bước đi."}], report)
    assert len(report.violations) == 0


def test_check_cjk_leakage_detected():
    report = QAReport(chunk_id="c001")
    _check_cjk_leakage([{"line_id": "p001_n01", "text_vi": "Anh ta 走了."}], report)
    assert any(v.type == "cjk_leakage" for v in report.violations)


def test_check_length_ratio_ok():
    report = QAReport(chunk_id="c001")
    _check_length_ratio("A" * 100, "B" * 80, report)
    assert len(report.violations) == 0


def test_check_length_ratio_too_short():
    report = QAReport(chunk_id="c001")
    _check_length_ratio("A" * 100, "B" * 20, report)
    assert any(v.type == "length_ratio" for v in report.violations)


def test_check_length_ratio_empty():
    report = QAReport(chunk_id="c001")
    _check_length_ratio("A" * 100, "", report)
    assert any(v.type == "length_ratio" for v in report.violations)
    assert any("empty" in v.reason.lower() for v in report.violations)


def test_check_glossary_term_present():
    ctx = _make_ctx(glossary=[
        GlossaryEntry(source="Border Guard", target_vi="Đội Bảo Vệ Biên Giới"),
    ])
    report = QAReport(chunk_id="c001")
    lines = [{"line_id": "p001_n01", "text_vi": "Đội Bảo Vệ Biên Giới đã đến."}]
    _check_glossary(lines, ctx, "Border Guard arrived.", report)
    assert len(report.violations) == 0


def test_check_glossary_term_missing():
    ctx = _make_ctx(glossary=[
        GlossaryEntry(source="Border Guard", target_vi="Đội Bảo Vệ Biên Giới"),
    ])
    report = QAReport(chunk_id="c001")
    lines = [{"line_id": "p001_n01", "text_vi": "Lính biên giới đã đến."}]
    _check_glossary(lines, ctx, "Border Guard arrived.", report)
    assert any(v.type == "glossary_error" for v in report.violations)


def test_check_pronoun_drift_wrong_gender():
    report = QAReport(chunk_id="c001")
    resolution = {
        "dialogue_turns": [{
            "line_id": "p001_d01",
            "speaker": "enkrid",
            "addressee": "shinar",
            "self_pronoun_vi": "tôi",
            "you_pronoun_vi": "cô",
            "confidence": 0.95,
            "needs_review": False,
        }]
    }
    # nàng is wrong when expected you_pronoun is "cô" — but the heuristic checks
    # self_pronoun_vi against opposite-gender pronouns
    # Let's test hắn → nàng swap for a male character
    resolution2 = {
        "dialogue_turns": [{
            "line_id": "p001_n01",
            "speaker": "enkrid",
            "addressee": "",
            "self_pronoun_vi": "hắn",  # expected: hắn
            "you_pronoun_vi": "",
            "confidence": 0.90,
            "needs_review": False,
        }]
    }
    lines = [{"line_id": "p001_n01", "text_vi": "Nàng bước đi thật nhanh."}]
    _check_pronoun_drift(lines, resolution2, report)
    # The heuristic checks if "nàng" appears when "hắn" is expected
    assert any(v.type == "pronoun_error" for v in report.violations)


def test_run_deterministic_qa_empty_translation_blocks():
    chunk = _make_chunk(["Enkrid walked forward. He saw three enemies."])
    translation = {
        "chunk_id": "test_c001",
        "lines": [{"line_id": "p001_n00", "text_vi": ""}],
    }
    ctx = _make_ctx()
    report = run_deterministic_qa(chunk, translation, {}, ctx)
    assert any(v.type == "length_ratio" for v in report.violations)
    assert any("empty" in v.reason.lower() for v in report.violations)
