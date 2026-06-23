#!/usr/bin/env python3
"""Tier-1 deterministic EN source ↔ VI output term alignment (no LLM).

Catches mistranslations like hand seal → thế kí when the English source clearly
uses the anchor term.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
GLOSSARY_DIR = ROOT / "story_data" / "seed_glossaries"

_CULTIVATION_GENRES = frozenset({
    "korean_cultivation",
    "korean_cultivation_regressor",
    "tien_hiep",
    "huyen_huyen",
    "kiem_hiep",
    "xuyen_khong",
    "mat_the",
    "he_thong",
})


@dataclass(frozen=True)
class TermAnchor:
    anchor_id: str
    source_re: re.Pattern[str]
    forbidden_vi: tuple[re.Pattern[str], ...]
    hint: str


_BUILTIN_ANCHORS: list[TermAnchor] = [
    TermAnchor(
        anchor_id="hand_seal",
        source_re=re.compile(r"\bhand seals?\b", re.IGNORECASE),
        forbidden_vi=(
            re.compile(r"\bthế k[íỉỷ]\b", re.IGNORECASE),
        ),
        hint="hand seal → ấn quyết / kết ấn (không phải thế kí/thế kỷ)",
    ),
    TermAnchor(
        anchor_id="spiritual_power",
        source_re=re.compile(r"\bspiritual power\b", re.IGNORECASE),
        forbidden_vi=(
            re.compile(r"\bnăng lượng tinh thần\b", re.IGNORECASE),
        ),
        hint="spiritual power → linh lực (không dịch word-for-word 'năng lượng tinh thần')",
    ),
]


def _genre_uses_cultivation_anchors(genre: str) -> bool:
    g = (genre or "").strip().lower()
    if not g:
        return True
    if g in _CULTIVATION_GENRES:
        return True
    return any(part in g for part in _CULTIVATION_GENRES)


def _anchors_from_glossary(genre: str) -> list[TermAnchor]:
    anchors: list[TermAnchor] = []
    if not GLOSSARY_DIR.is_dir():
        return anchors
    for path in sorted(GLOSSARY_DIR.glob("*.json")):
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if not entry.get("priority"):
                continue
            wrong = entry.get("wrong_translations") or []
            sources = entry.get("source_terms") or []
            canonical = str(entry.get("canonical_vi") or "").strip()
            if not wrong or not sources:
                continue
            src_pat = "|".join(re.escape(s) for s in sources if isinstance(s, str) and len(s) > 2)
            if not src_pat:
                continue
            forb = tuple(
                re.compile(rf"\b{re.escape(str(w).strip())}\b", re.IGNORECASE)
                for w in wrong
                if isinstance(w, str) and w.strip()
            )
            if not forb:
                continue
            anchor_id = re.sub(r"[^a-z0-9]+", "_", canonical.lower())[:40] or path.stem
            anchors.append(
                TermAnchor(
                    anchor_id=anchor_id,
                    source_re=re.compile(rf"\b(?:{src_pat})\b", re.IGNORECASE),
                    forbidden_vi=forb,
                    hint=f"dùng {canonical!r} theo glossary",
                )
            )
    return anchors


def check_term_alignment(
    source_text: str,
    polished_text: str,
    *,
    genre: str = "",
) -> list[str]:
    """Return blocking issue codes (empty = OK)."""
    if not (source_text or "").strip() or not (polished_text or "").strip():
        return []

    issues: list[str] = []
    anchors = list(_BUILTIN_ANCHORS)
    # ponytail: seed glossary anchors disabled until profile-scoped — too many false positives.

    seen: set[str] = set()
    for anchor in anchors:
        if not anchor.source_re.search(source_text):
            continue
        for pat in anchor.forbidden_vi:
            m = pat.search(polished_text)
            if m:
                code = f"term_alignment:{anchor.anchor_id}"
                if code not in seen:
                    seen.add(code)
                    issues.append(f"{code}:{m.group(0)!r}")
    return issues


def term_alignment_to_issues_dict(issues: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for issue in issues:
        base = issue.split(":", 1)[0]
        evidence = issue.split(":", 2)[-1] if issue.count(":") >= 2 else ""
        out.append({
            "code": issue,
            "severity": "blocking",
            "tier": 1,
            "evidence": evidence.strip("'\""),
        })
    return out
