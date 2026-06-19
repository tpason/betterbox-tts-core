"""Shared helpers for story-wide quality verify + repolish pipeline."""
from __future__ import annotations

from typing import Any

from genre_prompts import find_char_map_file, resolve_genre_from_context

# Sources that use EN raw → translate + polish via job queue.
_EN_TRANSLATE_SOURCES = frozenset({
    "wetriedtls", "royalroad", "lightnovelpub", "skydemonorder", "jadescrolls",
    "novelbin", "freewebnovel", "novelhub",
})

# VI (or already-VI) stories: polish raw_text_content in-process.
_VI_POLISH_SOURCES = frozenset({
    "hako", "truyenfull_today", "truyenyy", "wattpad_vn", "docln",
    "truyenchuhay", "truyenhoangdung", "sttruyen",
})


def story_slug(story: dict[str, Any]) -> str:
    meta = story.get("metadata") or {}
    return str(meta.get("slug") or story.get("source_story_id") or "")


def story_source_code(story: dict[str, Any]) -> str:
    return str(story.get("source_code") or (story.get("metadata") or {}).get("source") or "")


def story_language(story: dict[str, Any]) -> str:
    return str(story.get("language") or "").lower()


def pipeline_mode(story: dict[str, Any]) -> str:
    """vi_polish | en_translate_polish"""
    lang = story_language(story)
    src = story_source_code(story)
    if lang in {"en", "zh", "cn", "ko", "kr", "ja"} or src in _EN_TRANSLATE_SOURCES:
        return "en_translate_polish"
    return "vi_polish"


def resolve_genre(story: dict[str, Any]) -> str:
    meta = story.get("metadata") or {}
    sid = str(story.get("id") or "")
    slug = story_slug(story)
    char_map_file = find_char_map_file(story_id=sid, slug=slug)
    return resolve_genre_from_context(
        str(story.get("category") or meta.get("genre") or ""),
        raw_language=story_language(story),
        source_code=story_source_code(story),
        char_map_file=char_map_file,
        title=str(story.get("original_title") or story.get("title") or ""),
        description=str(meta.get("source_description") or story.get("description") or ""),
    )


def resolve_golden_profile(story: dict[str, Any], genre: str = "") -> str:
    genre = genre or resolve_genre(story)
    title = str(story.get("original_title") or story.get("title") or "").lower()
    if "trong_sinh" in genre or "regress" in title:
        return "korean_cultivation_regressor"
    if any(g in genre for g in ("korean_cultivation", "tien_hiep", "huyen_huyen", "kiem_hiep")):
        return "korean_cultivation"
    if genre in {"western_fantasy", "do_thi", "lang_man"} or "western_fantasy" in genre:
        return "western_fantasy"
    if story_language(story) == "vi" and pipeline_mode(story) == "vi_polish":
        return "vietnamese_default"
    return "generic"


def quality_meta(story: dict[str, Any]) -> dict[str, Any]:
    return dict((story.get("metadata") or {}).get("quality_pipeline") or {})


def update_quality_meta(story_id: str, patch: dict[str, Any]) -> None:
    from story_db.story_pipeline_db import repository as repo

    story = repo.get_story_by_id(story_id)
    meta = story.get("metadata") or {}
    current = dict(meta.get("quality_pipeline") or {})
    current.update(patch)
    repo.update_story_metadata(story_id, {"quality_pipeline": current})
