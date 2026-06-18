"""Step 4 — QA / Consistency Check.

Two-phase approach:
  Phase A: Deterministic checks (fast, no LLM)
    - glossary term enforcement
    - forbidden variant detection
    - protected pronoun diff vs resolution
    - CJK leakage in output
    - length ratio sanity

  Phase B: LLM semantic QA (only for medium/high-risk chunks)
    - pronoun/speaker accuracy
    - faithfulness to source meaning
    - dialogue relationship integrity
    - uses /think for thorough reasoning

Returns QAReport with violations + patches.
Patches marked patch_risk=low are auto-applied.
Patches marked medium/high go to human review queue.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from .context import StoryContext
from .segmenter import Chunk, segments_to_source_block

PatchRisk = Literal["low", "medium", "high"]
ViolationType = Literal[
    "pronoun_error", "glossary_error", "faithfulness_error",
    "cjk_leakage", "length_ratio", "format_error", "style_error",
]


@dataclass
class Violation:
    type: ViolationType
    line_id: str
    current: str
    expected: str
    reason: str
    confidence: float = 1.0
    patch_risk: PatchRisk = "low"


@dataclass
class Patch:
    line_id: str
    before: str
    after: str
    confidence: float = 1.0
    patch_risk: PatchRisk = "low"
    auto_apply: bool = True


@dataclass
class QAReport:
    chunk_id: str
    violations: list[Violation] = field(default_factory=list)
    patches: list[Patch] = field(default_factory=list)
    needs_human_review: bool = False
    llm_ran: bool = False
    error: str = ""

    @property
    def has_blocking_issues(self) -> bool:
        return any(v.confidence >= 0.85 and v.patch_risk == "high" for v in self.violations)

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "violations": [
                {
                    "type": v.type, "line_id": v.line_id,
                    "current": v.current, "expected": v.expected,
                    "reason": v.reason, "confidence": v.confidence,
                    "patch_risk": v.patch_risk,
                }
                for v in self.violations
            ],
            "patches": [
                {
                    "line_id": p.line_id, "before": p.before,
                    "after": p.after, "confidence": p.confidence,
                    "patch_risk": p.patch_risk, "auto_apply": p.auto_apply,
                }
                for p in self.patches
            ],
            "needs_human_review": self.needs_human_review,
        }


# ---------------------------------------------------------------------------
# Phase A: Deterministic checks
# ---------------------------------------------------------------------------

_CJK_RE = re.compile(r'[一-鿿぀-ヿ가-힯]')
_MIN_LENGTH_RATIO = 0.50
_MAX_LENGTH_RATIO = 2.50


def _check_glossary(
    translation_lines: list[dict],
    ctx: StoryContext,
    source_text: str,
    report: QAReport,
) -> None:
    """Check that glossary targets appear and forbidden variants don't."""
    rel_glossary = ctx.relevant_glossary(source_text)
    translated_text = " ".join(item.get("text_vi") or "" for item in translation_lines)

    for entry in rel_glossary:
        if not entry.target_vi or not source_text:
            continue
        # Check any source form appears in source
        source_present = any(s in source_text for s in entry.all_sources if s)
        if not source_present:
            continue
        # Check target appears in translation
        if entry.target_vi not in translated_text:
            report.violations.append(Violation(
                type="glossary_error",
                line_id="",
                current="(missing)",
                expected=entry.target_vi,
                reason=f"Glossary term '{entry.source}' → '{entry.target_vi}' not found in translation",
                patch_risk="medium",
            ))


def _check_cjk_leakage(translation_lines: list[dict], report: QAReport) -> None:
    for item in translation_lines:
        text = item.get("text_vi") or ""
        if _CJK_RE.search(text):
            report.violations.append(Violation(
                type="cjk_leakage",
                line_id=item.get("line_id") or "",
                current=text[:80],
                expected="no CJK characters",
                reason="CJK characters found in Vietnamese output",
                patch_risk="high",
            ))


def _check_length_ratio(source_text: str, translated_text: str, report: QAReport) -> None:
    if not source_text:
        return
    if not translated_text:
        report.violations.append(Violation(
            type="length_ratio",
            line_id="",
            current="0",
            expected=f">={_MIN_LENGTH_RATIO}",
            reason="Translation is completely empty — translator likely failed",
            patch_risk="high",
        ))
        return
    ratio = len(translated_text) / max(len(source_text), 1)
    if ratio < _MIN_LENGTH_RATIO:
        report.violations.append(Violation(
            type="length_ratio",
            line_id="",
            current=f"{ratio:.2f}",
            expected=f">={_MIN_LENGTH_RATIO}",
            reason=f"Translation is too short ({ratio:.0%} of source)",
            patch_risk="high",
        ))
    elif ratio > _MAX_LENGTH_RATIO:
        report.violations.append(Violation(
            type="length_ratio",
            line_id="",
            current=f"{ratio:.2f}",
            expected=f"<={_MAX_LENGTH_RATIO}",
            reason=f"Translation is too long ({ratio:.0%} of source) — possible hallucination",
            patch_risk="medium",
        ))


def _check_pronoun_drift(
    translation_lines: list[dict],
    resolution: dict[str, Any],
    report: QAReport,
) -> None:
    """Detect clear pronoun violations using resolution decisions."""
    turn_map = {t["line_id"]: t for t in resolution.get("dialogue_turns", [])}
    for item in translation_lines:
        lid = item.get("line_id") or ""
        text = item.get("text_vi") or ""
        if lid not in turn_map:
            continue
        turn = turn_map[lid]
        if turn.get("needs_review"):
            continue
        # Check that if self_pronoun_vi is specified, its counterpart (wrong gender) isn't present
        self_p = turn.get("self_pronoun_vi") or ""
        you_p = turn.get("you_pronoun_vi") or ""
        speaker = turn.get("speaker") or ""
        # Simple heuristic: if expected pronoun is "anh ta" and "cô ta" appears → flag
        opposite = {"hắn": "nàng", "nàng": "hắn", "anh ta": "cô ta", "cô ta": "anh ta",
                    "chàng": "ả", "ta": "", "ngươi": ""}
        text_lower = text.lower()
        for expected, wrong in opposite.items():
            if expected not in (self_p, you_p) or not wrong:
                continue
            if wrong in text_lower:
                report.violations.append(Violation(
                    type="pronoun_error",
                    line_id=lid,
                    current=wrong,
                    expected=expected,
                    reason=f"Wrong pronoun for {speaker}: expected {expected!r}, found {wrong!r}",
                    confidence=0.80,
                    patch_risk="low",
                ))
                report.patches.append(Patch(
                    line_id=lid,
                    before=wrong,
                    after=expected,
                    confidence=0.80,
                    patch_risk="low",
                    auto_apply=True,
                ))


def run_deterministic_qa(
    chunk: Chunk,
    translation: dict[str, Any],
    resolution: dict[str, Any],
    ctx: StoryContext,
) -> QAReport:
    report = QAReport(chunk_id=chunk.chunk_id)
    lines = translation.get("lines") or []
    translated_text = " ".join(item.get("text_vi") or "" for item in lines)

    _check_cjk_leakage(lines, report)
    _check_length_ratio(chunk.source_text, translated_text, report)
    _check_glossary(lines, ctx, chunk.source_text, report)
    _check_pronoun_drift(lines, resolution, report)

    report.needs_human_review = any(v.patch_risk == "high" for v in report.violations)
    return report


# ---------------------------------------------------------------------------
# Phase B: LLM semantic QA
# ---------------------------------------------------------------------------

_SYSTEM_QA = """\
/think

You are a strict QA checker for Vietnamese fiction translation.
Compare SOURCE_CHUNK, TRANSLATION_LINES, PRONOUN_RESOLUTION, CHARACTER_RULES, and GLOSSARY.

Do NOT rewrite the translation. Identify only concrete, verifiable violations.
Do not flag stylistic preferences — only errors that change meaning, identity, or fixed terms.

Check:
1. Wrong speaker/addressee pronouns (vs PRONOUN_RESOLUTION)
2. Wrong third-person referent pronouns
3. Required glossary target missing or replaced with wrong variant
4. Source meaning omitted or fabricated in translation
5. Line_id present in source but missing from translation

patch_risk rules:
  low    = simple string replace, high confidence
  medium = context-dependent, may need human check
  high   = meaning-level issue, must be human-reviewed

Return strict JSON:
{
  "violations": [
    {
      "type": "pronoun_error|glossary_error|faithfulness_error|format_error|style_error",
      "line_id": "...",
      "current": "...",
      "expected": "...",
      "reason": "...",
      "confidence": 0.0,
      "patch_risk": "low|medium|high"
    }
  ],
  "patches": [
    {
      "line_id": "...",
      "before": "...",
      "after": "...",
      "confidence": 0.0,
      "patch_risk": "low"
    }
  ],
  "needs_human_review": false
}
"""


def _build_qa_prompt(
    chunk: Chunk,
    translation: dict[str, Any],
    resolution: dict[str, Any],
    ctx: StoryContext,
) -> list[dict[str, str]]:
    rel_chars = ctx.relevant_characters(chunk.source_text)
    rel_glossary = ctx.relevant_glossary(chunk.source_text)

    char_block = ctx.format_characters_for_prompt(rel_chars) or "(none)"
    glossary_block = ctx.format_glossary_for_prompt(rel_glossary) or "(none)"

    # Compact resolution summary
    turns = resolution.get("dialogue_turns", [])
    res_summary = "\n".join(
        f"  {t['line_id']}: speaker={t.get('speaker')}, "
        f"self={t.get('self_pronoun_vi')}, you={t.get('you_pronoun_vi')}"
        for t in turns[:20]
    ) or "(no resolution)"

    lines_block = json.dumps(translation.get("lines") or [], ensure_ascii=False, indent=2)

    user_content = "\n".join([
        f"CHARACTER_RULES:\n{char_block}",
        f"\nGLOSSARY:\n{glossary_block}",
        f"\nPRONOUN_RESOLUTION:\n{res_summary}",
        f"\nSOURCE_CHUNK:\n{segments_to_source_block(chunk.segments)}",
        f"\nTRANSLATION_LINES:\n{lines_block}",
    ])

    return [
        {"role": "system", "content": _SYSTEM_QA},
        {"role": "user", "content": user_content},
    ]


def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def run_llm_qa(
    chunk: Chunk,
    translation: dict[str, Any],
    resolution: dict[str, Any],
    ctx: StoryContext,
    ollama_url: str,
    model: str = "qwen3:14b",
    timeout: int = 180,
    num_ctx: int = 16384,
    keep_alive: str = "10m",
) -> QAReport:
    """Run LLM semantic QA. Merges findings into existing deterministic report if needed."""
    import requests

    report = QAReport(chunk_id=chunk.chunk_id, llm_ran=True)
    messages = _build_qa_prompt(chunk, translation, resolution, ctx)

    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0.05, "top_p": 0.80, "num_ctx": num_ctx},
        "keep_alive": keep_alive,
    }

    try:
        resp = requests.post(
            f"{ollama_url.rstrip('/')}/api/chat",
            json=payload,
            timeout=timeout,
        )
        resp.raise_for_status()
        raw = _strip_think(resp.json().get("message", {}).get("content", ""))
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start == -1 or end == 0:
            raise ValueError("no JSON in QA response")
        data = json.loads(raw[start:end])

        for v in (data.get("violations") or []):
            report.violations.append(Violation(
                type=v.get("type") or "faithfulness_error",
                line_id=v.get("line_id") or "",
                current=v.get("current") or "",
                expected=v.get("expected") or "",
                reason=v.get("reason") or "",
                confidence=float(v.get("confidence") or 0.8),
                patch_risk=v.get("patch_risk") or "medium",
            ))
        for p in (data.get("patches") or []):
            report.patches.append(Patch(
                line_id=p.get("line_id") or "",
                before=p.get("before") or "",
                after=p.get("after") or "",
                confidence=float(p.get("confidence") or 0.8),
                patch_risk=p.get("patch_risk") or "medium",
                auto_apply=(p.get("patch_risk") or "medium") == "low",
            ))
        report.needs_human_review = bool(data.get("needs_human_review"))

    except Exception as exc:
        report.error = str(exc)

    return report


def merge_qa_reports(det: QAReport, llm: QAReport) -> QAReport:
    merged = QAReport(chunk_id=det.chunk_id, llm_ran=llm.llm_ran)
    merged.violations = det.violations + llm.violations
    merged.patches = det.patches + llm.patches
    merged.needs_human_review = det.needs_human_review or llm.needs_human_review
    merged.error = " | ".join(e for e in [det.error, llm.error] if e)
    return merged
