"""Step 2 — Pronoun / Context Resolution Pass.

Runs only for medium/high-risk chunks. Returns structured JSON with:
  - scene_pov
  - dialogue_turns[speaker, addressee, self_pronoun_vi, you_pronoun_vi, tone, confidence]
  - third_person_refs[source_ref → referent → vi_pronoun, confidence]
  - ambiguous_refs[candidates, chosen, confidence, needs_review]

Low-confidence refs (< CONFIDENCE_REVIEW_THRESHOLD) are flagged needs_review=True
and should not be silently applied without QA approval.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .context import StoryContext, build_recap_context
from .segmenter import Chunk, segments_to_source_block

CONFIDENCE_REVIEW_THRESHOLD = 0.70

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SYSTEM_RESOLVER = """\
/think

You are a Vietnamese fiction pronoun and dialogue resolver.

Your job is to analyze the SOURCE_CHUNK and produce ONLY a JSON object.
Do not translate. Do not write prose. Output strict JSON only.

Use STORY_CONTEXT, CHARACTER_RULES, RELATIONSHIP_RULES, and RECAP to resolve:
1. Scene POV (whose perspective narrates)
2. Speaker and addressee for every dialogue line
3. Correct Vietnamese pronoun for each third-person reference
4. Flag ambiguous cases with confidence < {threshold}

Pronoun defaults when no relationship rule applies:
- Male character, narration: hắn (neutral), chàng (respectful), gã (derogatory)
- Female character, narration: nàng (neutral), cô (modern/neutral), ả (derogatory)
- Self (cổ phong/xianxia): ta | tôi | tại hạ depending on genre
- You (cổ phong/xianxia): ngươi (equal/hostile) | người (respectful) | nàng/chàng (intimate)

For genre=western_fantasy (Korean LN): avoid hắn/nàng/lão/y; use anh ta/cô ta/anh ấy/cô ấy.

CONFIDENCE GUIDE:
  0.90+ = explicit dialogue tag or relationship rule
  0.75  = strong contextual inference
  0.60  = plausible guess, multiple candidates possible
  <0.60 = mark needs_review=true

OUTPUT FORMAT (strict JSON, no other text):
{{
  "chunk_id": "...",
  "scene_pov": "...",
  "active_characters": ["..."],
  "dialogue_turns": [
    {{
      "line_id": "...",
      "speaker": "...",
      "addressee": "...",
      "self_pronoun_vi": "...",
      "you_pronoun_vi": "...",
      "tone": "...",
      "confidence": 0.0,
      "evidence_line_ids": ["..."],
      "needs_review": false
    }}
  ],
  "third_person_refs": [
    {{
      "line_id": "...",
      "source_ref": "...",
      "referent": "...",
      "vi_pronoun": "...",
      "confidence": 0.0,
      "needs_review": false
    }}
  ],
  "ambiguous_refs": [
    {{
      "line_id": "...",
      "source_text": "...",
      "candidates": ["..."],
      "chosen": "...",
      "confidence": 0.0,
      "needs_review": true
    }}
  ]
}}
""".format(threshold=CONFIDENCE_REVIEW_THRESHOLD)


def _build_resolver_prompt(
    chunk: Chunk,
    ctx: StoryContext,
    current_chapter: int = 0,
) -> list[dict[str, str]]:
    active_chars = chunk.active_characters or [c.name_vi for c in ctx.characters[:8]]
    relevant_chars = ctx.relevant_characters(chunk.source_text)
    relevant_rels = ctx.relevant_relationships(active_chars)
    recap = build_recap_context(ctx, current_chapter)

    char_block = ctx.format_characters_for_prompt(relevant_chars) or "(no character data)"
    rel_block = ctx.format_relationships_for_prompt(relevant_rels) or "(no relationship rules)"

    user_parts = [
        f"STORY_CONTEXT:\n  genre={ctx.genre}, story_id={ctx.story_id}",
        f"  style: {ctx.style_profile[:300]}" if ctx.style_profile else "",
        f"\nRECAP (recent chapters):\n{recap}" if recap else "",
        f"\nCHARACTER_RULES:\n{char_block}",
        f"\nRELATIONSHIP_RULES:\n{rel_block}",
        f"\nSOURCE_CHUNK (chunk_id={chunk.chunk_id}):\n{segments_to_source_block(chunk.segments)}",
        "\nPrevious context tail:\n" + chunk.context_tail if chunk.context_tail else "",
    ]
    user_content = "\n".join(p for p in user_parts if p)

    return [
        {"role": "system", "content": _SYSTEM_RESOLVER},
        {"role": "user", "content": user_content},
    ]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _extract_json(text: str) -> dict[str, Any]:
    text = _strip_think(text)
    # Find outermost JSON object
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError("No JSON object found in resolver output")
    return json.loads(text[start:end])


def run_resolver(
    chunk: Chunk,
    ctx: StoryContext,
    ollama_url: str,
    model: str = "qwen3:14b",
    current_chapter: int = 0,
    timeout: int = 120,
    num_ctx: int = 8192,
    keep_alive: str = "10m",
) -> dict[str, Any]:
    """Run pronoun/context resolution for one chunk.

    Returns the parsed resolution JSON. On failure returns a minimal safe dict
    (all lists empty, scene_pov="unknown") so downstream passes still run.
    """
    import requests  # lazy import — not always needed

    messages = _build_resolver_prompt(chunk, ctx, current_chapter)

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
        raw = resp.json().get("message", {}).get("content", "")
        result = _extract_json(raw)
        result.setdefault("chunk_id", chunk.chunk_id)
        # flag all low-confidence refs
        for turn in result.get("dialogue_turns", []):
            if turn.get("confidence", 1.0) < CONFIDENCE_REVIEW_THRESHOLD:
                turn["needs_review"] = True
        for ref in result.get("third_person_refs", []):
            if ref.get("confidence", 1.0) < CONFIDENCE_REVIEW_THRESHOLD:
                ref["needs_review"] = True
        return result

    except Exception as exc:
        return {
            "chunk_id": chunk.chunk_id,
            "scene_pov": "unknown",
            "active_characters": chunk.active_characters,
            "dialogue_turns": [],
            "third_person_refs": [],
            "ambiguous_refs": [],
            "_resolver_error": str(exc),
        }


def apply_resolution_to_chunk(chunk: Chunk, resolution: dict[str, Any]) -> Chunk:
    """Back-fill resolver decisions onto Segment objects in-place."""
    turn_map = {t["line_id"]: t for t in resolution.get("dialogue_turns", [])}
    ref_map = {r["line_id"]: r for r in resolution.get("third_person_refs", [])}

    for seg in chunk.segments:
        if seg.line_id in turn_map:
            t = turn_map[seg.line_id]
            seg.speaker = t.get("speaker") or ""
            seg.addressee = t.get("addressee") or ""
            seg.self_pronoun_vi = t.get("self_pronoun_vi") or ""
            seg.you_pronoun_vi = t.get("you_pronoun_vi") or ""
        if seg.line_id in ref_map:
            seg.third_person_refs = [ref_map[seg.line_id]]

    return chunk
