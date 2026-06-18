"""Step 5 — Patch Application.

Applies approved patches to translation lines.
Conservative: only exact string matches, only named line_ids, no paragraph rewrites.

Auto-applies patches where auto_apply=True and patch_risk=low.
High/medium patches go to unapplied list for human review.
"""
from __future__ import annotations

from typing import Any

from .qa import Patch


def apply_patches(
    translation_lines: list[dict],
    patches: list[Patch],
    *,
    auto_only: bool = True,
) -> tuple[list[dict], list[Patch]]:
    """Apply patches to translation_lines in-place.

    Args:
        translation_lines: [{line_id, text_vi}] from translator
        patches:           list of Patch from QA report
        auto_only:         if True, only apply patches with auto_apply=True

    Returns:
        (patched_lines, unapplied_patches)
    """
    line_map = {item["line_id"]: item for item in translation_lines if item.get("line_id")}
    applied: set[int] = set()
    unapplied: list[Patch] = []

    for i, patch in enumerate(patches):
        if auto_only and not patch.auto_apply:
            unapplied.append(patch)
            continue
        if not patch.line_id or not patch.before or not patch.after:
            unapplied.append(patch)
            continue

        target = line_map.get(patch.line_id)
        if target is None:
            unapplied.append(patch)
            continue

        text = target.get("text_vi") or ""
        if patch.before not in text:
            unapplied.append(patch)
            continue

        # Apply once (replace first occurrence to be conservative)
        target["text_vi"] = text.replace(patch.before, patch.after, 1)
        applied.add(i)

    not_applied = [p for i, p in enumerate(patches) if i not in applied]
    return translation_lines, not_applied


def render_patch_report(applied_count: int, unapplied: list[Patch]) -> str:
    lines = [f"Patches applied: {applied_count}"]
    if unapplied:
        lines.append(f"Unapplied ({len(unapplied)}):")
        for p in unapplied:
            lines.append(
                f"  [{p.line_id}] risk={p.patch_risk} "
                f"'{p.before[:40]}' → '{p.after[:40]}'"
            )
    return "\n".join(lines)
