"""Step 7 — Final Holistic QA.

Unlike per-chunk QA which checks correctness locally, the final QA reads the
complete translated+polished chapter as a whole and catches cross-chunk issues:

  - Pronoun drift across scene boundaries
  - Character voice inconsistency (same character uses different speech style)
  - Glossary term used correctly in chunk 1 but wrong in chunk 5
  - Tone drift between dialogue and narration
  - Any remaining gross errors that passed per-chunk QA

Uses /think for thorough holistic reasoning.
Returns a FinalQAReport with overall verdict + any remaining violations.

This is the "safety net" — it does NOT need to re-check every detail,
only things that require reading the chapter end-to-end.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .context import StoryContext

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

@dataclass
class FinalViolation:
    type: str
    location: str   # "ch001_c002_p003_d01" or "chapter-level"
    description: str
    severity: str   # "critical" | "major" | "minor"
    suggestion: str = ""


@dataclass
class FinalQAReport:
    story_id: str
    chapter_id: str
    verdict: str = "pass"          # "pass" | "warn" | "fail"
    violations: list[FinalViolation] = field(default_factory=list)
    overall_notes: str = ""
    error: str = ""

    @property
    def has_critical(self) -> bool:
        return any(v.severity == "critical" for v in self.violations)

    @property
    def has_major(self) -> bool:
        return any(v.severity == "major" for v in self.violations)

    def to_dict(self) -> dict:
        return {
            "story_id": self.story_id,
            "chapter_id": self.chapter_id,
            "verdict": self.verdict,
            "violations": [
                {
                    "type": v.type,
                    "location": v.location,
                    "description": v.description,
                    "severity": v.severity,
                    "suggestion": v.suggestion,
                }
                for v in self.violations
            ],
            "overall_notes": self.overall_notes,
        }


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SYSTEM_FINAL_QA = """\
/think

You are a senior Vietnamese fiction editor performing a final chapter review.

You have already seen per-chunk QA. This is a HOLISTIC read of the full translated chapter.
Focus ONLY on cross-chunk issues that cannot be caught per-chunk:

1. PRONOUN CONSISTENCY — does the same character use consistent pronouns throughout?
2. CHARACTER VOICE — does each character maintain their speech style across scenes?
3. GLOSSARY STABILITY — are sect names, skills, places used consistently end-to-end?
4. TONE DRIFT — does the narrative register shift unexpectedly?
5. CROSS-SCENE CONTINUITY — do events/references across chunk boundaries make sense?
6. GROSS ERRORS — anything clearly wrong that slipped through per-chunk checks?

Do NOT re-flag issues that are inherently local (single-line typos, punctuation).
Only flag CHAPTER-LEVEL patterns.

severity guide:
  critical = wrong character identity, completely wrong meaning
  major    = consistent pronoun error across multiple scenes, important term drift
  minor    = style inconsistency, borderline modernization

Output strict JSON:
{
  "verdict": "pass|warn|fail",
  "violations": [
    {
      "type": "pronoun_drift|voice_drift|glossary_drift|tone_drift|continuity|gross_error",
      "location": "p003_d01 to p007_d02",
      "description": "...",
      "severity": "critical|major|minor",
      "suggestion": "..."
    }
  ],
  "overall_notes": "..."
}

verdict rules:
  pass = 0 critical/major issues
  warn = 1+ minor issues only
  fail = any critical or major issue
"""


def _build_final_qa_prompt(
    chapter_text: str,
    ctx: StoryContext,
    chapter_number: int,
) -> list[dict[str, str]]:
    rel_chars = ctx.relevant_characters(chapter_text)
    rel_glossary = ctx.relevant_glossary(chapter_text)

    char_block = ctx.format_characters_for_prompt(rel_chars) or "(none)"
    glossary_block = ctx.format_glossary_for_prompt(rel_glossary) or "(none)"

    # Bound chapter text to avoid token overflow
    text_for_review = chapter_text
    if len(text_for_review) > 6000:
        text_for_review = (
            text_for_review[:3000]
            + "\n\n[... middle of chapter ...]\n\n"
            + text_for_review[-3000:]
        )

    user_content = "\n".join([
        f"GENRE: {ctx.genre}",
        f"CHAPTER: {chapter_number}",
        f"\nCHARACTER_RULES:\n{char_block}",
        f"\nGLOSSARY:\n{glossary_block}",
        f"\nFULL_TRANSLATED_CHAPTER:\n{text_for_review}",
    ])

    return [
        {"role": "system", "content": _SYSTEM_FINAL_QA},
        {"role": "user", "content": user_content},
    ]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def run_final_qa(
    chapter_text: str,
    ctx: StoryContext,
    chapter_number: int,
    ollama_url: str,
    model: str = "qwen3:14b",
    timeout: int = 300,
    num_ctx: int = 32768,
    keep_alive: str = "10m",
) -> FinalQAReport:
    """Run final holistic QA on the assembled chapter.

    This runs AFTER polisher and uses the complete polished text,
    not chunk-by-chunk output.
    """
    import requests

    report = FinalQAReport(
        story_id=ctx.story_id,
        chapter_id=f"chapter_{chapter_number:04d}",
    )

    messages = _build_final_qa_prompt(chapter_text, ctx, chapter_number)
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": 0.1,
            "top_p": 0.85,
            "num_ctx": num_ctx,
        },
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
            raise ValueError("no JSON in final QA response")

        data = json.loads(raw[start:end])
        report.verdict = data.get("verdict") or "pass"
        report.overall_notes = data.get("overall_notes") or ""

        for v in (data.get("violations") or []):
            report.violations.append(FinalViolation(
                type=v.get("type") or "gross_error",
                location=v.get("location") or "chapter-level",
                description=v.get("description") or "",
                severity=v.get("severity") or "minor",
                suggestion=v.get("suggestion") or "",
            ))

    except Exception as exc:
        report.error = str(exc)
        report.verdict = "warn"

    return report
