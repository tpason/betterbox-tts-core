#!/usr/bin/env python3
"""Bridge polish_worker jobs → novel_translation.translate_chapter()."""
from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

from story_db.story_pipeline_db import repository as repo

from novel_translation.pipeline import PipelineConfig, PipelineResult, translate_chapter

ROOT = Path(__file__).resolve().parents[2]
STORY_MEMORY_ROOT = ROOT / "story_data" / "story_memory"


def resolve_translation_engine(args: argparse.Namespace, raw_language: str) -> str:
    import os

    engine = (
        str(getattr(args, "translation_engine", "") or "").strip()
        or os.environ.get("POLISH_TRANSLATION_ENGINE", "auto")
        or "auto"
    ).lower()
    if engine == "auto":
        return "novel" if raw_language == "en" else "legacy"
    return engine


def find_memory_dir(story_id: str, slug: str, story_title: str = "") -> Path | None:
    slug_part = re.sub(r"[^a-z0-9]+", "-", (story_title or slug or "").lower()).strip("-")[:40]
    candidates = [
        STORY_MEMORY_ROOT / f"{story_id}-{slug_part}",
        STORY_MEMORY_ROOT / story_id,
    ]
    if STORY_MEMORY_ROOT.exists():
        for d in STORY_MEMORY_ROOT.iterdir():
            if d.is_dir() and (d.name.startswith(story_id) or (slug_part and slug_part in d.name)):
                candidates.insert(0, d)
    for c in candidates:
        if c.exists():
            return c
    return None


def fetch_context_tail(story_id: str, chapter_number: int, *, max_chars: int = 600) -> str:
    if chapter_number <= 1:
        return ""
    row = repo.get_chapter_by_story_number(story_id, chapter_number - 1)
    if not row:
        return ""
    raw = (row.get("raw_text_content") or "").strip()
    return raw[-max_chars:] if len(raw) > max_chars else raw


def build_pipeline_config(args: argparse.Namespace, *, source_language: str) -> PipelineConfig:
    model = str(getattr(args, "translate_model", "") or getattr(args, "vi_model", "") or "qwen3:14b")
    return PipelineConfig(
        ollama_url=str(getattr(args, "ollama_url", "http://127.0.0.1:11434")),
        model=model,
        chunk_size=int(getattr(args, "novel_chunk_size", 1800) or 1800),
        source_language=source_language,
        skip_final_qa=bool(getattr(args, "novel_skip_final_qa", False)),
        skip_polish=bool(getattr(args, "novel_skip_polish", False)),
        repair_hints=str(getattr(args, "chapter_repair_hints", "") or "").strip(),
    )


def run_novel_translation_for_job(
    job: dict[str, Any],
    args: argparse.Namespace,
    *,
    source_text: str,
    genre: str,
    slug: str,
    char_map_raw: str = "",
    chapter_number: int,
) -> PipelineResult:
    story_id = str(job.get("story_id") or "")
    memory_dir = find_memory_dir(story_id, slug, story_title=str(job.get("story_title") or ""))
    context_tail = fetch_context_tail(story_id, chapter_number)
    raw_language = str((job.get("payload") or {}).get("raw_language") or "en").lower()
    cfg = build_pipeline_config(args, source_language=raw_language)
    return translate_chapter(
        source_text=source_text,
        story_id=story_id,
        slug=slug,
        genre=genre,
        chapter_number=chapter_number,
        memory_dir=memory_dir,
        char_map_raw=char_map_raw,
        cfg=cfg,
        context_tail=context_tail,
    )


def load_char_map_raw(story_id: str) -> str:
    story = repo.get_story_by_id(story_id)
    meta = story.get("metadata") or {}
    return str(meta.get("char_map_content") or "")
