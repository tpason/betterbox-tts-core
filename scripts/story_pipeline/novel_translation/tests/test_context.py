"""Tests for context loader — no Ollama required."""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from scripts.story_pipeline.novel_translation.context import (
    CharacterProfile,
    GlossaryEntry,
    Relationship,
    StoryContext,
    _load_characters_json,
    _load_glossary_v2,
    _parse_gender_from_narration,
    load_story_context,
)


def test_parse_gender_male():
    assert _parse_gender_from_narration("anh ta, Enkrid", "hiệp sĩ") == "male"


def test_parse_gender_female():
    assert _parse_gender_from_narration("cô ta, Shinar", "tiên tộc") == "female"


def test_parse_gender_unknown():
    assert _parse_gender_from_narration("", "") == "unknown"


def test_load_characters_json_basic():
    data = [
        {
            "id": "enkrid",
            "canonical_name": "Enkrid",
            "wrong_spellings": ["Encrid"],
            "role": "nhân vật chính, hiệp sĩ",
            "third_person_narration": "anh, anh ta, Enkrid",
            "self_address": "tôi",
            "voice_style": "điềm tĩnh, ngắn gọn",
            "addressing_by_target": {
                "krang_private": "tôi/anh tùy câu",
            },
        },
        {
            "id": "shinar",
            "canonical_name": "Shinar",
            "role": "tiên nữ đồng đội",
            "third_person_narration": "cô, cô ta, Shinar",
        },
    ]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(data, f)
        path = Path(f.name)

    chars, rels = _load_characters_json(path)
    path.unlink()

    assert len(chars) == 2
    enkrid = next(c for c in chars if c.key == "enkrid")
    assert enkrid.gender == "male"
    assert enkrid.name_vi == "Enkrid"
    assert "Encrid" in enkrid.source_names
    assert enkrid.narrator_reference.get("neutral") == "anh ta"

    shinar = next(c for c in chars if c.key == "shinar")
    assert shinar.gender == "female"
    assert shinar.narrator_reference.get("neutral") == "cô ta"

    # Relationships from addressing_by_target
    assert len(rels) >= 1
    rel = next((r for r in rels if r.speaker == "enkrid"), None)
    assert rel is not None
    assert rel.self_pronoun == "tôi"


def test_load_glossary_v2_list():
    data = [
        {"source": "captain", "target_vi": "đội trưởng", "type": "title", "priority": True},
        {"source": "Border Guard", "target_vi": "Đội Bảo Vệ Biên Giới"},
    ]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(data, f)
        path = Path(f.name)

    entries = _load_glossary_v2(path)
    path.unlink()

    assert len(entries) == 2
    captain = next(e for e in entries if e.source == "captain")
    assert captain.target_vi == "đội trưởng"
    assert captain.priority is True


def test_load_story_context_from_memory_dir():
    chars = [
        {
            "id": "hero",
            "canonical_name": "Hero",
            "third_person_narration": "anh ta",
            "role": "main",
        }
    ]
    glossary = [{"source": "sword", "target_vi": "kiếm"}]

    with tempfile.TemporaryDirectory() as tmpdir:
        mem = Path(tmpdir)
        (mem / "characters.json").write_text(json.dumps(chars), encoding="utf-8")
        (mem / "glossary.json").write_text(json.dumps(glossary), encoding="utf-8")
        (mem / "style_guide.txt").write_text("Keep it short.", encoding="utf-8")

        ctx = load_story_context(
            story_id="s001",
            slug="test-story",
            genre="western_fantasy",
            memory_dir=mem,
        )

    assert len(ctx.characters) == 1
    assert ctx.characters[0].key == "hero"
    assert len(ctx.glossary) == 1
    assert ctx.glossary[0].target_vi == "kiếm"
    assert "short" in ctx.style_profile


def test_story_context_relevant_characters():
    ctx = StoryContext(story_id="s1", slug="s", genre="western_fantasy")
    ctx.characters = [
        CharacterProfile(key="enkrid", source_names=["Enkrid", "Encrid"], name_vi="Enkrid", gender="male", role="main lead"),
        CharacterProfile(key="shinar", source_names=["Shinar"], name_vi="Shinar", gender="female", role="support"),
    ]
    rel = ctx.relevant_characters("Enkrid nodded at Shinar.")
    keys = [c.key for c in rel]
    assert "enkrid" in keys
    assert "shinar" in keys


def test_story_context_surface_to_key_via_all_surfaces():
    ctx = StoryContext(story_id="s1", slug="s", genre="western_fantasy")
    ctx.characters = [
        CharacterProfile(key="enkrid", source_names=["Enkrid", "Encrid"], name_vi="Enkrid", gender="male", role=""),
    ]
    # all_surfaces includes name_vi + source_names + key
    surfaces = ctx.characters[0].all_surfaces
    assert "Enkrid" in surfaces
    assert "Encrid" in surfaces
    assert "enkrid" in surfaces


def test_load_characters_json_list_addressing():
    """Regression: addressing_by_target as list (A Regressor format) must not crash."""
    data = [
        {
            "id": "seo_eun_hyun",
            "canonical_name": "Seo Eun-hyun",
            "third_person_narration": "anh ta",
            "role": "main",
            "addressing_by_target": [
                "Seo Eun-hyun -> modern colleagues near transmigration: tôi/cậu",
                "Hostile strangers -> Seo Eun-hyun: ngươi/tên kia",
            ],
        }
    ]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(data, f)
        path = Path(f.name)

    chars, rels = _load_characters_json(path)
    path.unlink()

    assert len(chars) == 1
    assert chars[0].key == "seo_eun_hyun"
    # Should not crash; may produce 0 or more relationships
    assert isinstance(rels, list)


def test_story_context_relevant_relationships():
    ctx = StoryContext(story_id="s1", slug="s", genre="western_fantasy")
    ctx.relationships = [
        Relationship(speaker="enkrid", addressee="krang", self_pronoun="tôi", you_pronoun="anh", third_reference="anh ta"),
        Relationship(speaker="shinar", addressee="enkrid", self_pronoun="tôi", you_pronoun="anh", third_reference="anh ta"),
    ]
    rels = ctx.relevant_relationships(["enkrid", "krang"])
    assert len(rels) == 2  # both have enkrid or krang
    rels2 = ctx.relevant_relationships(["shinar"])
    assert len(rels2) == 1
