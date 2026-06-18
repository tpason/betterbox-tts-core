"""Novel Translation Pipeline — Orchestrator.

Chains all 7 passes for one chapter:
  1. Segmenter       → Chunk list with stable IDs + risk scores
  2. Resolver        → pronoun/speaker JSON (high/medium risk only)
  3. Translator      → [{line_id, text_vi}]
  4. QA (det + LLM)  → violations + patches
  5. Patcher         → apply low-risk patches
  6. Polisher        → constrained style polish
  7. FinalQA         → holistic chapter-level review

Per-story context (genre, char-map v2, glossary v2, recaps) is loaded once
per chapter run from StoryContext — no global config.

Usage:
    from scripts.story_pipeline.novel_translation.pipeline import translate_chapter
    result = translate_chapter(
        source_text=raw_text,
        story_id="21180",
        slug="21180-vinh-thoai-hiep-si",
        genre="western_fantasy",
        chapter_number=543,
        memory_dir="story_data/story_memory/21180-vinh-thoai-hiep-si",
        ollama_url="http://127.0.0.1:11434",
        model="qwen3:14b",
    )
    print(result.polished_text)
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .context import StoryContext, load_story_context
from .final_qa import FinalQAReport, run_final_qa
from .patcher import apply_patches, render_patch_report
from .polisher import run_polisher
from .qa import (
    QAReport,
    merge_qa_reports,
    run_deterministic_qa,
    run_llm_qa,
)
from .resolver import apply_resolution_to_chunk, run_resolver
from .segmenter import Chunk, reconstruct_text, segment_text
from .translator import extract_translated_text, run_translator

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_DEFAULT_CHUNK_SIZE = 1800        # chars per chunk
_DEFAULT_RESOLVER_RISKS = {"medium", "high"}   # risks that trigger resolver
_DEFAULT_LLM_QA_RISKS = {"high"}               # risks that trigger LLM QA


@dataclass
class PipelineConfig:
    ollama_url: str = "http://127.0.0.1:11434"
    model: str = "qwen3:14b"
    timeout_resolver: int = 120
    timeout_translator: int = 300
    timeout_qa: int = 180
    timeout_polisher: int = 300
    timeout_final_qa: int = 300
    num_ctx_resolver: int = 8192
    num_ctx_translator: int = 32768
    num_ctx_qa: int = 16384
    num_ctx_polisher: int = 32768
    num_ctx_final_qa: int = 32768
    keep_alive: str = "10m"
    chunk_size: int = _DEFAULT_CHUNK_SIZE
    resolver_risks: set[str] = field(default_factory=lambda: set(_DEFAULT_RESOLVER_RISKS))
    llm_qa_risks: set[str] = field(default_factory=lambda: set(_DEFAULT_LLM_QA_RISKS))
    skip_final_qa: bool = False
    skip_polish: bool = False
    max_chunks: int = 0
    source_language: str = ""


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class ChunkResult:
    chunk_id: str
    translation: dict[str, Any]
    resolution: dict[str, Any]
    qa_report: QAReport
    polished_lines: list[dict]
    polish_warnings: list[str]
    elapsed_s: float = 0.0


@dataclass
class PipelineResult:
    story_id: str
    chapter_id: str
    polished_text: str
    chunk_results: list[ChunkResult] = field(default_factory=list)
    final_qa: FinalQAReport | None = None
    final_quality_blocking: list[str] = field(default_factory=list)
    final_quality_warnings: list[str] = field(default_factory=list)
    total_elapsed_s: float = 0.0
    error: str = ""
    is_partial: bool = False

    @property
    def success(self) -> bool:
        if self.error:
            return False
        if self.is_partial:
            return False
        if self.final_quality_blocking:
            return False
        if any(cr.qa_report.has_blocking_issues for cr in self.chunk_results):
            return False
        if self.final_qa and self.final_qa.verdict == "fail":
            return False
        return bool(self.polished_text)

    @property
    def needs_review(self) -> bool:
        if self.final_quality_blocking:
            return True
        if any(cr.qa_report.has_blocking_issues for cr in self.chunk_results):
            return True
        if self.final_qa and self.final_qa.has_major:
            return True
        return any(cr.qa_report.needs_human_review for cr in self.chunk_results)


# ---------------------------------------------------------------------------
# Per-chunk processing
# ---------------------------------------------------------------------------

def _process_chunk(
    chunk: Chunk,
    ctx: StoryContext,
    cfg: PipelineConfig,
    chapter_number: int,
) -> ChunkResult:
    t0 = time.monotonic()
    resolution: dict[str, Any] = {
        "chunk_id": chunk.chunk_id,
        "scene_pov": "unknown",
        "active_characters": chunk.active_characters,
        "dialogue_turns": [],
        "third_person_refs": [],
        "ambiguous_refs": [],
    }

    # Step 2: Resolver (only for medium/high-risk chunks)
    if chunk.risk in cfg.resolver_risks:
        log.debug("[RESOLVER] chunk=%s risk=%s", chunk.chunk_id, chunk.risk)
        resolution = run_resolver(
            chunk=chunk,
            ctx=ctx,
            ollama_url=cfg.ollama_url,
            model=cfg.model,
            current_chapter=chapter_number,
            timeout=cfg.timeout_resolver,
            num_ctx=cfg.num_ctx_resolver,
            keep_alive=cfg.keep_alive,
        )
        chunk = apply_resolution_to_chunk(chunk, resolution)
    else:
        log.debug("[RESOLVER] chunk=%s risk=%s → skipped (low-risk)", chunk.chunk_id, chunk.risk)

    # Step 3: Translator
    log.debug("[TRANSLATOR] chunk=%s", chunk.chunk_id)
    translation = run_translator(
        chunk=chunk,
        ctx=ctx,
        resolution=resolution,
        ollama_url=cfg.ollama_url,
        model=cfg.model,
        current_chapter=chapter_number,
        timeout=cfg.timeout_translator,
        num_ctx=cfg.num_ctx_translator,
        keep_alive=cfg.keep_alive,
    )

    lines = translation.get("lines") or []

    # Step 4: QA — deterministic always, LLM for high-risk
    det_report = run_deterministic_qa(chunk, translation, resolution, ctx)
    if chunk.risk in cfg.llm_qa_risks:
        log.debug("[QA_LLM] chunk=%s", chunk.chunk_id)
        llm_report = run_llm_qa(
            chunk=chunk,
            translation=translation,
            resolution=resolution,
            ctx=ctx,
            ollama_url=cfg.ollama_url,
            model=cfg.model,
            timeout=cfg.timeout_qa,
            num_ctx=cfg.num_ctx_qa,
            keep_alive=cfg.keep_alive,
        )
        qa_report = merge_qa_reports(det_report, llm_report)
    else:
        qa_report = det_report

    # Step 5: Patcher
    auto_patches = [p for p in qa_report.patches if p.auto_apply]
    lines, unapplied = apply_patches(lines, auto_patches, auto_only=True)
    if unapplied:
        log.debug(
            "[PATCHER] chunk=%s: %d patches unapplied → human review",
            chunk.chunk_id, len(unapplied),
        )

    # Step 6: Polisher
    polish_warnings: list[str] = []
    if not cfg.skip_polish:
        log.debug("[POLISH] chunk=%s", chunk.chunk_id)
        lines, polish_warnings = run_polisher(
            chunk=chunk,
            translation_lines=lines,
            ctx=ctx,
            ollama_url=cfg.ollama_url,
            model=cfg.model,
            timeout=cfg.timeout_polisher,
            num_ctx=cfg.num_ctx_polisher,
            keep_alive=cfg.keep_alive,
        )
        if polish_warnings:
            log.warning("[POLISH WARN] chunk=%s: %s", chunk.chunk_id, polish_warnings)

    return ChunkResult(
        chunk_id=chunk.chunk_id,
        translation=translation,
        resolution=resolution,
        qa_report=qa_report,
        polished_lines=lines,
        polish_warnings=polish_warnings,
        elapsed_s=time.monotonic() - t0,
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def translate_chapter(
    source_text: str,
    story_id: str,
    slug: str,
    genre: str,
    chapter_number: int,
    *,
    memory_dir: str | Path | None = None,
    char_map_raw: str = "",
    cfg: PipelineConfig | None = None,
    context_tail: str = "",
) -> PipelineResult:
    """Translate one chapter through the full multi-pass pipeline.

    Args:
        source_text:    raw source chapter text (Chinese/Korean/English)
        story_id:       DB story ID
        slug:           story slug
        genre:          detected genre
        chapter_number: chapter number (for recaps + chapter ID)
        memory_dir:     path to story_memory/<story> directory
        char_map_raw:   raw char_map text (legacy fallback)
        cfg:            pipeline config (uses defaults if None)
        context_tail:   last ~600 chars from previous chapter for pronoun continuity

    Returns:
        PipelineResult with polished_text and diagnostics
    """
    if cfg is None:
        cfg = PipelineConfig()

    t_start = time.monotonic()
    chapter_id = f"chapter_{chapter_number:04d}"

    # Load per-story context
    ctx = load_story_context(
        story_id=story_id,
        slug=slug,
        genre=genre,
        memory_dir=memory_dir,
        char_map_raw=char_map_raw,
    )

    # Build surface→key map so Chunk.active_characters stores keys (not display names).
    # StoryContext.relevant_relationships() matches on keys — without this, relationship
    # rules never reach the resolver prompt.
    surface_to_key: dict[str, str] = {}
    for c in ctx.characters:
        for surface in c.all_surfaces:
            if surface:
                surface_to_key[surface] = c.key

    # Step 1: Segment
    chunks: list[Chunk] = segment_text(
        text=source_text,
        chapter_id=chapter_id,
        character_surface_to_key=surface_to_key,
        context_tail=context_tail,
        max_chars=cfg.chunk_size,
    )
    log.info(
        "[PIPELINE] story=%s ch=%d → %d chunks (risks: %s)",
        slug, chapter_number, len(chunks),
        {c.risk for c in chunks},
    )
    chunks_to_process = chunks
    is_partial = False
    if cfg.max_chunks > 0 and cfg.max_chunks < len(chunks):
        chunks_to_process = chunks[:cfg.max_chunks]
        is_partial = True
        log.warning(
            "[PIPELINE] partial run: processing first %d/%d chunks",
            len(chunks_to_process), len(chunks),
        )

    # Process each chunk
    chunk_results: list[ChunkResult] = []
    pipeline_error = ""
    try:
        for chunk in chunks_to_process:
            cr = _process_chunk(chunk, ctx, cfg, chapter_number)
            chunk_results.append(cr)

            # Detect translator failure (empty output)
            has_translator_error = "_translator_error" in cr.translation
            all_empty = all(
                not (item.get("text_vi") or "").strip()
                for item in (cr.translation.get("lines") or [])
            )
            if has_translator_error or all_empty:
                pipeline_error = (
                    cr.translation.get("_translator_error")
                    or f"chunk {chunk.chunk_id} produced no translated text"
                )
                log.error("[PIPELINE] blocking failure: %s", pipeline_error)
                return PipelineResult(
                    story_id=story_id,
                    chapter_id=chapter_id,
                    polished_text="",
                    chunk_results=chunk_results,
                    total_elapsed_s=time.monotonic() - t_start,
                    error=pipeline_error,
                    is_partial=is_partial,
                )

            log.info(
                "[CHUNK] %s risk=%s qa_violations=%d elapsed=%.1fs",
                chunk.chunk_id, chunk.risk,
                len(cr.qa_report.violations), cr.elapsed_s,
            )
    except Exception as exc:
        return PipelineResult(
            story_id=story_id,
            chapter_id=chapter_id,
            polished_text="",
            chunk_results=chunk_results,
            total_elapsed_s=time.monotonic() - t_start,
            error=str(exc),
            is_partial=is_partial,
        )

    # Assemble full chapter text
    all_lines: list[dict] = []
    for cr in chunk_results:
        all_lines.extend(cr.polished_lines)
    polished_text = reconstruct_text(all_lines)

    final_quality_blocking: list[str] = []
    final_quality_warnings: list[str] = []
    if polished_text:
        from scripts.story_pipeline.check_translation_quality import run_full_quality_check

        source_for_quality = "\n\n".join(chunk.source_text for chunk in chunks_to_process)
        final_quality_blocking, final_quality_warnings = run_full_quality_check(
            polished_text,
            genre=ctx.genre,
            story_id=story_id,
            slug=slug,
            story_memory_dir=str(memory_dir or ""),
            source_text=source_for_quality,
            source_language=cfg.source_language,
            log=log.warning,
        )
        if final_quality_blocking:
            log.error("[FINAL_QUALITY] blocking=%s", final_quality_blocking)
        if final_quality_warnings:
            log.warning("[FINAL_QUALITY] warnings=%s", final_quality_warnings)

    # Step 7: Final holistic QA
    final_qa: FinalQAReport | None = None
    if is_partial:
        log.warning("[FINAL_QA] skipped for partial run")
    elif not cfg.skip_final_qa and polished_text:
        log.info("[FINAL_QA] ch=%d running holistic review…", chapter_number)
        final_qa = run_final_qa(
            chapter_text=polished_text,
            ctx=ctx,
            chapter_number=chapter_number,
            ollama_url=cfg.ollama_url,
            model=cfg.model,
            timeout=cfg.timeout_final_qa,
            num_ctx=cfg.num_ctx_final_qa,
            keep_alive=cfg.keep_alive,
        )
        log.info(
            "[FINAL_QA] verdict=%s violations=%d",
            final_qa.verdict, len(final_qa.violations),
        )

    total = time.monotonic() - t_start
    log.info("[PIPELINE] ch=%d done in %.1fs", chapter_number, total)

    return PipelineResult(
        story_id=story_id,
        chapter_id=chapter_id,
        polished_text=polished_text,
        chunk_results=chunk_results,
        final_qa=final_qa,
        final_quality_blocking=final_quality_blocking,
        final_quality_warnings=final_quality_warnings,
        total_elapsed_s=total,
        is_partial=is_partial,
    )
