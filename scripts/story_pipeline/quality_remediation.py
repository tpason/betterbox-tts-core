#!/usr/bin/env python3
"""Map quality issues → repolish | retranslate and enqueue atomic repairs."""
from __future__ import annotations

import os
from typing import Any

from story_db.story_pipeline_db import repository as repo

from check_translation_quality import issue_to_repair_hint

MAX_REPAIR_ATTEMPTS = int(os.environ.get("QUALITY_MAX_REPAIR_ATTEMPTS", "3"))

# Issues that only need VI polish pass on existing translation.
_REPOLISH_PREFIXES = (
    "repeated_content",
    "wrong_pronoun",
    "forbidden_term",
    "structure_drift",
    "judge:unnatural",
    "judge:word_for_word",
    "register drift",
    "dialogue register",
    "format drift",
)

# Issues that need full re-translate from source.
_RETRANSLATE_PREFIXES = (
    "term_alignment:",
    "cjk_not_translated",
    "large_en_block",
    "not_vietnamese",
    "length_ratio_low",
    "truncated_output",
    "output_too_short",
    "no_polished_text",
    "judge:mistranslation",
    "judge:omission",
    "untranslated_slang",
)


def route_repair_action(issues: list[str]) -> str:
    """Return 'retranslate' or 'repolish' based on blocking issues."""
    for issue in issues:
        base = issue.split(":")[0]
        if base == "judge":
            sub = issue.split(":", 1)[1] if ":" in issue else ""
            if sub in {"mistranslation", "omission"}:
                return "retranslate"
            if sub in {"unnatural", "word_for_word", "wrong_pronoun"}:
                return "repolish"
        if any(issue.startswith(p) or base == p.rstrip(":") for p in _RETRANSLATE_PREFIXES):
            return "retranslate"
    return "repolish"


def build_repair_hints(issues: list[str]) -> str:
    lines: list[str] = []
    seen: set[str] = set()
    for issue in issues:
        hint = issue_to_repair_hint(issue)
        if hint not in seen:
            seen.add(hint)
            lines.append(f"- {hint}")
    return "\n".join(lines)


def request_chapter_repair(
    chapter_id: str,
    issues: list[str],
    *,
    action: str = "",
    dry_run: bool = False,
    force_running: bool = False,
) -> dict[str, Any]:
    """Enqueue repolish or retranslate for one chapter. Atomic job reset."""
    action = action or route_repair_action(issues)
    hints = build_repair_hints(issues)
    return repo.request_quality_repair(
        chapter_id,
        action,
        repair_hints=hints,
        dry_run=dry_run,
        force_running=force_running,
        max_attempts=MAX_REPAIR_ATTEMPTS,
    )


def request_force_repair_range(
    chapter_ids: list[str],
    action: str,
    *,
    dry_run: bool = False,
    force_running: bool = False,
) -> list[dict[str, Any]]:
    """Force repolish or retranslate for chapters (no issue-based routing)."""
    out: list[dict[str, Any]] = []
    for chapter_id in chapter_ids:
        out.append(
            request_chapter_repair(
                chapter_id,
                [],
                action=action,
                dry_run=dry_run,
                force_running=force_running,
            )
        )
    return out
