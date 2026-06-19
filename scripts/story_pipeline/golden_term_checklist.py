#!/usr/bin/env python3
"""Golden terminology checklist for cultivation stories (Phase 4 verify gate).

Checks polished/translated text for known bad patterns and optional encouraged terms.
Used by wetriedtls_verify.py before TTS / audio enqueue.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GoldenRule:
    pattern: re.Pattern[str]
    reason: str
    severity: str = "blocking"  # blocking | warning


@dataclass
class GoldenFinding:
    chapter: str
    kind: str
    detail: str
    severity: str
    matched: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "chapter": self.chapter,
            "kind": self.kind,
            "detail": self.detail,
            "severity": self.severity,
            "matched": self.matched,
        }


# Profile: A Regressor's Tale / korean_cultivation + trong_sinh
_KOREAN_CULTIVATION_REGRESSOR_RULES: list[GoldenRule] = [
    GoldenRule(
        re.compile(r"\bhồi phục\b", re.IGNORECASE),
        "regressor/cultivation — dùng 'Hồi Quy' / 'hồi quy', không 'hồi phục' (dễ nhầm healing)",
    ),
    GoldenRule(
        re.compile(r"\bTrọng Sinh\b"),
        "regressor — dùng 'Hồi Quy' / 'Người Hồi Quy', không 'Trọng Sinh' (reincarnation ≠ regression)",
    ),
    GoldenRule(
        re.compile(r"\btrọng sinh\b"),
        "regressor — dùng 'hồi quy', không 'trọng sinh'",
    ),
    GoldenRule(
        re.compile(r"\bRegressor(?:'s)?\b"),
        "EN chưa dịch — dùng 'Người Hồi Quy' / 'Hồi Quy Giả'",
    ),
    GoldenRule(
        re.compile(r"\bRegression\b"),
        "EN chưa dịch — dùng 'Hồi Quy'",
    ),
    GoldenRule(
        re.compile(r"\bcultivation\b", re.IGNORECASE),
        "EN chưa dịch — dùng 'tu luyện' / 'tu tiên'",
    ),
    GoldenRule(
        re.compile(r"\bQi\b"),
        "EN chưa dịch — dùng 'linh khí' / 'linh lực'",
    ),
    GoldenRule(
        re.compile(r"\bCultivator\b"),
        "EN chưa dịch — dùng 'tu sĩ'",
    ),
]

_KOREAN_CULTIVATION_REGRESSOR_ENCOURAGED = [
    "Hồi Quy",
    "hồi quy",
    "Luyện Khí",
    "tu sĩ",
    "linh lực",
    "Linh Căn",
]

_PROFILES: dict[str, dict[str, Any]] = {
    "korean_cultivation_regressor": {
        "rules": _KOREAN_CULTIVATION_REGRESSOR_RULES,
        "encouraged_any": _KOREAN_CULTIVATION_REGRESSOR_ENCOURAGED,
    },
    "korean_cultivation": {
        "rules": _KOREAN_CULTIVATION_REGRESSOR_RULES,
        "encouraged_any": ["Luyện Khí", "tu sĩ", "linh lực", "Linh Căn"],
    },
    "western_fantasy": {
        "rules": [
            GoldenRule(
                re.compile(r"\b(hắn|Hắn|nàng|Nàng)\b"),
                "western_fantasy — văn kể dùng anh ta/cô ta, không hắn/nàng",
            ),
            GoldenRule(
                re.compile(r"\b(ngươi|Ngươi|mi)\b"),
                "western_fantasy — không xưng hô cổ phong tiên hiệp",
            ),
            GoldenRule(
                re.compile(r"\bEncrid\b"),
                "tên sai — dùng Enkrid (char-map alias)",
            ),
            GoldenRule(
                re.compile(r"\bEnkrido\b"),
                "tên sai — dùng Enkrid",
            ),
        ],
        "encouraged_any": ["Enkrid", "anh ta", "cô ta"],
    },
    "vietnamese_default": {
        "rules": [],
        "encouraged_any": [],
    },
    "generic": {
        "rules": [],
        "encouraged_any": [],
    },
}


def list_profiles() -> list[str]:
    return sorted(_PROFILES)


def run_golden_checklist(
    chapters: dict[str, str],
    *,
    profile: str = "korean_cultivation_regressor",
    check_encouraged: bool = True,
) -> list[GoldenFinding]:
    """Scan chapter texts against golden rules. Returns findings (blocking + warnings)."""
    spec = _PROFILES.get(profile) or _PROFILES.get("generic") or _PROFILES["korean_cultivation_regressor"]
    rules: list[GoldenRule] = spec["rules"]
    encouraged: list[str] = spec.get("encouraged_any") or []

    findings: list[GoldenFinding] = []
    combined = "\n".join(chapters.values())

    for chapter_name, text in sorted(chapters.items()):
        if not (text or "").strip():
            continue
        for rule in rules:
            for match in rule.pattern.finditer(text):
                findings.append(
                    GoldenFinding(
                        chapter=chapter_name,
                        kind="forbidden",
                        detail=rule.reason,
                        severity=rule.severity,
                        matched=match.group(0),
                    )
                )

    if check_encouraged and encouraged and combined.strip():
        if not any(term in combined for term in encouraged):
            findings.append(
                GoldenFinding(
                    chapter="(all)",
                    kind="missing_encouraged",
                    detail=f"Không thấy thuật ngữ cultivation mong đợi: {', '.join(encouraged[:5])}…",
                    severity="warning",
                )
            )

    return findings


def summarize_golden_findings(findings: list[GoldenFinding]) -> dict[str, int]:
    blocking = sum(1 for f in findings if f.severity == "blocking")
    warnings = sum(1 for f in findings if f.severity == "warning")
    return {"blocking": blocking, "warnings": warnings, "total": len(findings)}


def gate_passed(findings: list[GoldenFinding]) -> bool:
    return not any(f.severity == "blocking" for f in findings)
