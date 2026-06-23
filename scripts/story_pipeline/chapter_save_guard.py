#!/usr/bin/env python3
"""Single gate before writing translate/polish text to PostgreSQL.

Nothing reaches `chapters.*_text_content` without passing this module when
workers/CLI use commit_guarded_chapter_outputs().
"""
from __future__ import annotations

import argparse
from typing import TYPE_CHECKING, Any

from llm_quality_judge import judge_chapter_quality
from term_alignment_check import check_term_alignment

if TYPE_CHECKING:
    from novel_translation.pipeline import PipelineResult


class SaveGateError(Exception):
    """Raised when chapter text must not be persisted."""

    def __init__(self, blocking: list[str]):
        self.blocking = blocking
        super().__init__(", ".join(blocking))


def _effective_judge_mode(args: argparse.Namespace, *, novel_engine: bool) -> str:
    mode = str(getattr(args, "llm_judge", "warn") or "warn").lower()
    if novel_engine:
        return "block"
    return mode


def verify_chapter_save_allowed(
    polished_text: str,
    *,
    source_text: str = "",
    genre: str = "",
    story_id: str = "",
    slug: str = "",
    char_map: str = "",
    source_language: str = "",
    story_memory_dir: str = "",
    pipeline_result: PipelineResult | None = None,
    args: argparse.Namespace | None = None,
    novel_engine: bool = False,
    translated_text: str = "",
) -> tuple[list[str], list[str]]:
    """Return (blocking, warnings). Raises SaveGateError if blocking non-empty."""
    from check_translation_quality import is_probably_vietnamese, run_full_quality_check

    args = args or argparse.Namespace()
    blocking: list[str] = []
    warnings: list[str] = []

    text = (polished_text or "").strip()
    if len(text) < 100:
        blocking.append("output_too_short")
    elif not is_probably_vietnamese(text):
        blocking.append("not_vietnamese")

    if pipeline_result is not None:
        if pipeline_result.is_partial:
            blocking.append("pipeline_partial")
        if pipeline_result.error:
            blocking.append(f"pipeline_error:{pipeline_result.error[:120]}")
        if not pipeline_result.success:
            blocking.append("pipeline_not_success")
        if novel_engine:
            tr = (translated_text or getattr(pipeline_result, "translated_text", "") or "").strip()
            if len(tr) < 100:
                blocking.append("translated_text_too_short")

    if blocking:
        raise SaveGateError(blocking)

    b0, w0 = run_full_quality_check(
        polished_text,
        genre=genre,
        char_map=char_map,
        story_id=story_id,
        slug=slug,
        story_memory_dir=story_memory_dir,
        source_text=source_text,
        source_language=source_language,
    )
    blocking.extend(b0)
    warnings.extend(w0)

    if source_text:
        blocking.extend(check_term_alignment(source_text, polished_text, genre=genre))

    judge_mode = _effective_judge_mode(args, novel_engine=novel_engine)
    if judge_mode != "off" and source_text:
        judge_model = (
            str(getattr(args, "judge_model", "") or "").strip()
            or str(getattr(args, "translate_model", "") or "").strip()
            or "qwen3:14b"
        )
        judge = judge_chapter_quality(
            source_text,
            polished_text,
            genre=genre,
            ollama_url=str(getattr(args, "ollama_url", "http://127.0.0.1:11434")),
            model=judge_model,
            seed=story_id or slug,
        )
        if judge.error:
            err_code = f"judge_error:{judge.error}"
            if judge_mode == "block" or novel_engine:
                blocking.append(err_code)
            else:
                warnings.append(err_code)
        for issue in judge.issues:
            if judge_mode == "block" or novel_engine:
                blocking.append(issue)
            else:
                warnings.append(issue)
        warnings.extend(judge.warnings)

    if blocking:
        raise SaveGateError(blocking)
    return blocking, warnings


def commit_guarded_chapter_outputs(
    chapter_id: str,
    *,
    polished_text: str,
    translated_text: str | None = None,
    polished_text_path: str | None = None,
    translated_text_path: str | None = None,
    clear_audio: bool = False,
    verify_kwargs: dict[str, Any],
) -> None:
    """Verify quality then persist chapter text columns + pending_audit status."""
    from story_db.story_pipeline_db import repository as repo

    verify_chapter_save_allowed(polished_text, **verify_kwargs)
    repo.update_chapter_text_outputs(
        chapter_id,
        polished_text_path=polished_text_path,
        translated_text_path=translated_text_path,
        polished_text_content=polished_text,
        translated_text_content=translated_text,
        clear_audio=clear_audio,
        quality_status="pending_audit",
    )
