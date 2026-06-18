"""Tests for segmenter — no Ollama required."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from scripts.story_pipeline.novel_translation.segmenter import (
    Chunk,
    Segment,
    _classify_line,
    _find_active_character_keys,
    _score_risk,
    segment_text,
    segments_to_source_block,
    reconstruct_text,
)


def test_classify_line_dialogue():
    assert _classify_line('"Hello there"') == "dialogue"
    assert _classify_line('— Ngươi là ai?') == "dialogue"
    assert _classify_line('"Enkrid said something"') == "dialogue"


def test_classify_line_narration():
    assert _classify_line("Enkrid walked forward.") == "narration"
    assert _classify_line("The sun was rising.") == "narration"


def test_classify_line_inner_thought():
    assert _classify_line("*He thought carefully.*") == "inner_thought"


def test_find_active_character_keys():
    surface_to_key = {
        "Enkrid": "enkrid",
        "Krang": "krang",
        "Shinar": "shinar",
        "Encrid": "enkrid",  # alias
    }
    result = _find_active_character_keys("Enkrid talked to Krang.", surface_to_key)
    assert "enkrid" in result
    assert "krang" in result
    assert "shinar" not in result


def test_find_active_character_keys_deduplicates():
    surface_to_key = {"Enkrid": "enkrid", "Encrid": "enkrid"}
    result = _find_active_character_keys("Enkrid aka Encrid", surface_to_key)
    assert result.count("enkrid") == 1


def test_score_risk_low():
    segs = [Segment("p001_n01", "The sun rose.", "narration")]
    assert _score_risk(segs, []) == "low"


def test_score_risk_medium_dialogue():
    segs = [
        Segment("p001_n01", "Enkrid nodded.", "narration"),
        Segment("p001_d01", '"Yes," he said.', "dialogue"),
    ]
    assert _score_risk(segs, ["enkrid"]) == "medium"


def test_score_risk_high_many_chars():
    segs = [Segment("p001_n01", "Three people entered.", "narration")]
    assert _score_risk(segs, ["enkrid", "krang", "shinar"]) == "high"


def test_segment_text_basic():
    text = """Enkrid walked forward.
"Who are you?" he asked.
The stranger said nothing.
Shinar appeared from the shadows.
"Leave," she said coldly.
"""
    surface_to_key = {"Enkrid": "enkrid", "Shinar": "shinar"}
    chunks = segment_text(text, "chapter_0001", character_surface_to_key=surface_to_key)
    assert len(chunks) >= 1
    chunk = chunks[0]
    assert isinstance(chunk, Chunk)
    # All segments have line_ids
    for seg in chunk.segments:
        assert seg.line_id
        assert seg.kind in ("dialogue", "narration", "inner_thought", "system")
    # Active characters are keys, not surfaces
    assert "enkrid" in chunk.active_characters or "shinar" in chunk.active_characters
    # No surface strings in active_characters
    assert "Enkrid" not in chunk.active_characters
    assert "Shinar" not in chunk.active_characters


def test_segment_text_chunk_splitting():
    # Generate text larger than max_chars
    lines = [f"Line {i}: " + "word " * 20 for i in range(30)]
    text = "\n".join(lines)
    chunks = segment_text(text, "chapter_0002", max_chars=300)
    assert len(chunks) > 1
    # Each chunk has a unique chunk_id
    ids = [c.chunk_id for c in chunks]
    assert len(ids) == len(set(ids))


def test_segment_text_stable_ids():
    text = "Enkrid attacked.\n\"Fight!\" he said.\nThe enemy fell."
    chunks = segment_text(text, "chapter_0003")
    all_ids = [seg.line_id for c in chunks for seg in c.segments]
    assert len(all_ids) == len(set(all_ids)), "line_ids must be unique"


def test_segments_to_source_block():
    segs = [
        Segment("p001_n01", "He walked.", "narration"),
        Segment("p001_d01", '"Hello"', "dialogue"),
    ]
    block = segments_to_source_block(segs)
    assert "[p001_n01] He walked." in block
    assert "[p001_d01]" in block


def test_reconstruct_text():
    lines = [
        {"line_id": "p001_n01", "text_vi": "Anh ta bước đi."},
        {"line_id": "p001_d01", "text_vi": '"Xin chào."'},
    ]
    text = reconstruct_text(lines)
    assert "Anh ta bước đi." in text
    assert "Xin chào." in text
