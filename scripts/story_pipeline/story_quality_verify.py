#!/usr/bin/env python3
"""Story-wide QA helpers for the fleet quality pipeline."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from story_db.story_pipeline_db import repository as repo
from story_db.story_pipeline_db.db import connect

from story_quality_common import (
    pipeline_mode,
    quality_meta,
    resolve_genre,
    resolve_golden_profile,
    story_source_code,
    update_quality_meta,
)
from wetriedtls_verify import print_qa_report, run_qa_gate


def story_progress(story_id: str) -> dict[str, int]:
    return repo.get_story_chapter_progress(story_id)


def count_blocking_in_range(
    story: dict[str, Any],
    *,
    from_chapter: int,
    to_chapter: int,
    skip_llm_judge: bool = True,
    ollama_url: str = "http://127.0.0.1:11434",
    judge_model: str = "qwen3:14b",
) -> dict[str, Any]:
    """Lightweight QA summary without printing full report."""
    source_code = story_source_code(story)
    profile = resolve_golden_profile(story)
    result = run_qa_gate(
        story,
        source_code=source_code,
        from_chapter=from_chapter,
        to_chapter=to_chapter,
        profile=profile,
        llm_judge=not skip_llm_judge,
        ollama_url=ollama_url,
        judge_model=judge_model,
    )
    bad = result.get("qa_chapters") or []
    return {
        "passed": bool(result.get("passed")),
        "profile": profile,
        "genre": result.get("genre"),
        "blocking_chapters": len(bad),
        "chapters": bad,
        "result": result,
    }


def run_story_qa(
    story: dict[str, Any],
    *,
    from_chapter: int = 1,
    to_chapter: int = 0,
    skip_llm_judge: bool = False,
    ollama_url: str = "http://127.0.0.1:11434",
    judge_model: str = "qwen3:14b",
    json_out: str = "",
) -> dict[str, Any]:
    source_code = story_source_code(story)
    profile = resolve_golden_profile(story)
    result = run_qa_gate(
        story,
        source_code=source_code,
        from_chapter=from_chapter,
        to_chapter=to_chapter,
        profile=profile,
        llm_judge=not skip_llm_judge,
        ollama_url=ollama_url,
        judge_model=judge_model,
    )
    print_qa_report(result)
    if json_out:
        from pathlib import Path

        Path(json_out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    update_quality_meta(
        str(story["id"]),
        {
            "last_qa_at": datetime.now(timezone.utc).isoformat(),
            "last_qa_from": from_chapter,
            "last_qa_to": to_chapter,
            "last_qa_passed": bool(result.get("passed")),
            "last_qa_profile": profile,
            "mode": pipeline_mode(story),
        },
    )
    return result


def list_stories_for_pipeline(
    *,
    source_codes: list[str] | None = None,
    min_polished_chapters: int = 1,
    limit: int = 0,
    priority_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Stories with polished content, priority_ids first then rank_position."""
    query = """
        SELECT
            s.*,
            src.code AS source_code,
            COUNT(c.id) FILTER (
                WHERE c.is_polished AND c.polished_text_content IS NOT NULL
            )::int AS polished_count,
            COUNT(c.id)::int AS chapter_count,
            MAX(c.chapter_number) FILTER (
                WHERE c.is_polished AND c.polished_text_content IS NOT NULL
            )::int AS max_polished_chapter
        FROM stories s
        JOIN sources src ON src.id = s.source_id
        LEFT JOIN chapters c ON c.story_id = s.id
        WHERE s.is_active = TRUE
    """
    params: list[Any] = []
    if source_codes:
        query += " AND src.code = ANY(%s)"
        params.append(source_codes)
    query += """
        GROUP BY s.id, src.code
        HAVING COUNT(c.id) FILTER (
            WHERE c.is_polished AND c.polished_text_content IS NOT NULL
        ) >= %s
    """
    params.append(min_polished_chapters)
    query += " ORDER BY s.rank_position NULLS LAST, polished_count DESC, s.updated_at DESC"
    if limit > 0:
        query += " LIMIT %s"
        params.append(limit)

    with connect() as conn:
        rows = [dict(r) for r in conn.execute(query, params).fetchall()]

    if not priority_ids:
        return rows

    by_id = {str(r["id"]): r for r in rows}
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for sid in priority_ids:
        row = by_id.get(sid)
        if row:
            ordered.append(row)
            seen.add(sid)
    for row in rows:
        sid = str(row["id"])
        if sid not in seen:
            ordered.append(row)
    return ordered


def format_status_row(story: dict[str, Any]) -> str:
    sid = str(story["id"])
    meta = quality_meta(story)
    progress = story_progress(sid)
    polished = int(story.get("polished_count") or progress.get("polished_count") or 0)
    total = int(story.get("chapter_count") or progress.get("total_chapters") or 0)
    mode = pipeline_mode(story)
    src = story_source_code(story)
    qa_to = int(meta.get("last_batch_to") or meta.get("qa_passed_to_chapter") or 0)
    status = meta.get("status") or "pending"
    title = str(story.get("title") or story.get("display_title") or sid[:8])
    return (
        f"{title[:48]:<48} | {src:<16} | {mode:<20} | "
        f"polished {polished:>4}/{total:<4} | qa_to={qa_to:<4} | {status}"
    )
