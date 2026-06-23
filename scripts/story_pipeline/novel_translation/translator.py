"""Step 3 — Translation Draft Pass.

Consumes:
  - source chunk (with line IDs)
  - resolution JSON from resolver
  - matched glossary
  - matched character rules
  - previous context summary / recap

Uses /no_think for clean prose output.
Returns [{line_id, text_vi}] list preserving segment structure.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .context import StoryContext, GlossaryEntry, CharacterProfile, build_recap_context
from .segmenter import Chunk, segments_to_source_block

# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

_SYSTEM_TRANSLATOR = """\
/no_think

You are a professional Vietnamese fiction translator.
Translate SOURCE_CHUNK into Vietnamese.

Hard rules:
1. Follow PRONOUN_RESOLUTION exactly for every named line_id.
   Do NOT re-derive speaker/addressee — use what is given.
2. Use GLOSSARY target values exactly. Never invent alternative translations for glossary terms.
3. Preserve the literary style described in STYLE_PROFILE.
4. Do not modernize pronouns or address terms unless STYLE_PROFILE says so.
5. Preserve paragraph order and line_id mapping.
6. Do not add notes, headings, explanations, or markdown.
7. Keep cổ phong/xianxia register when genre requires it.
   For genre=western_fantasy: avoid hắn/nàng/lão/y unless specified in PRONOUN_RESOLUTION.

Output strict JSON only:
{
  "chunk_id": "...",
  "lines": [
    {"line_id": "p001_d01", "text_vi": "..."}
  ],
  "translator_flags": []
}

If you cannot apply a pronoun decision exactly, add an entry to translator_flags:
  {"line_id": "...", "issue": "brief reason"}
"""


def _format_pronoun_block(resolution: dict[str, Any]) -> str:
    """Format resolver output into a compact prompt block."""
    turns = resolution.get("dialogue_turns", [])
    refs = resolution.get("third_person_refs", [])
    if not turns and not refs:
        return "(no pronoun resolution — apply genre defaults)"

    lines = []
    for t in turns:
        lid = t.get("line_id", "?")
        sp = t.get("speaker") or "?"
        ad = t.get("addressee") or "?"
        self_p = t.get("self_pronoun_vi") or "?"
        you_p = t.get("you_pronoun_vi") or "?"
        flag = " [REVIEW]" if t.get("needs_review") else ""
        lines.append(f"  {lid}: speaker={sp}, addressee={ad}, self={self_p}, you={you_p}{flag}")
    for r in refs:
        lid = r.get("line_id", "?")
        ref = r.get("source_ref") or "?"
        referent = r.get("referent") or "?"
        vi = r.get("vi_pronoun") or "?"
        flag = " [REVIEW]" if r.get("needs_review") else ""
        lines.append(f"  {lid}: {ref}→{referent} ({vi}){flag}")
    return "\n".join(lines)


def _build_translator_prompt(
    chunk: Chunk,
    ctx: StoryContext,
    resolution: dict[str, Any],
    current_chapter: int = 0,
    repair_hints: str = "",
) -> list[dict[str, str]]:
    rel_chars = ctx.relevant_characters(chunk.source_text)
    rel_glossary = ctx.relevant_glossary(chunk.source_text)
    recap = build_recap_context(ctx, current_chapter)

    char_block = ctx.format_characters_for_prompt(rel_chars) or "(no character data)"
    glossary_block = ctx.format_glossary_for_prompt(rel_glossary) or "(no glossary terms)"
    pronoun_block = _format_pronoun_block(resolution)

    user_parts = [
        f"GENRE: {ctx.genre}",
        f"STYLE_PROFILE: {ctx.style_profile[:400]}" if ctx.style_profile else "",
        f"\nRECAP:\n{recap}" if recap else "",
        f"\nCHARACTER_RULES:\n{char_block}",
        f"\nGLOSSARY:\n{glossary_block}",
        f"\nPRONOUN_RESOLUTION:\n{pronoun_block}",
        "\nPrevious context:\n" + chunk.context_tail if chunk.context_tail else "",
    ]
    hints = (repair_hints or "").strip()
    if hints:
        user_parts.append(f"\nREPAIR_HINTS (fix these from prior failed attempt):\n{hints[:2000]}")
    user_parts.append(
        f"\nSOURCE_CHUNK (chunk_id={chunk.chunk_id}):\n{segments_to_source_block(chunk.segments)}",
    )
    user_content = "\n".join(p for p in user_parts if p)

    return [
        {"role": "system", "content": _SYSTEM_TRANSLATOR},
        {"role": "user", "content": user_content},
    ]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _extract_json(text: str) -> dict[str, Any]:
    text = _strip_think(text)
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError("No JSON object found in translator output")
    return json.loads(text[start:end])


def run_translator(
    chunk: Chunk,
    ctx: StoryContext,
    resolution: dict[str, Any],
    ollama_url: str,
    model: str = "qwen3:14b",
    current_chapter: int = 0,
    timeout: int = 300,
    num_ctx: int = 32768,
    keep_alive: str = "10m",
    repair_hints: str = "",
) -> dict[str, Any]:
    """Run translation draft for one chunk.

    Returns dict with "lines": [{line_id, text_vi}] and "translator_flags".
    On error returns fallback with empty translations and error recorded.
    """
    import requests

    messages = _build_translator_prompt(
        chunk, ctx, resolution, current_chapter, repair_hints=repair_hints,
    )

    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": 0.15,
            "top_p": 0.85,
            "repeat_penalty": 1.05,
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
        raw = resp.json().get("message", {}).get("content", "")
        result = _extract_json(raw)
        result.setdefault("chunk_id", chunk.chunk_id)
        result.setdefault("lines", [])
        result.setdefault("translator_flags", [])
        return result

    except Exception as exc:
        return {
            "chunk_id": chunk.chunk_id,
            "lines": [{"line_id": s.line_id, "text_vi": ""} for s in chunk.segments],
            "translator_flags": [{"issue": str(exc)}],
            "_translator_error": str(exc),
        }


def extract_translated_text(translation: dict[str, Any]) -> str:
    """Flatten lines to plain text for downstream passes."""
    lines = translation.get("lines") or []
    return "\n\n".join(
        item["text_vi"].strip()
        for item in lines
        if item.get("text_vi") and item["text_vi"].strip()
    )
