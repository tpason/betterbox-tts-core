"""Step 6 — Constrained Polish Pass.

Improves readability and prose flow AFTER correctness is stable.
Hard constraints — cannot change:
  - pronouns / address terms in protected_pronoun_decisions
  - names and glossary terms (protected_tokens)
  - meaning, event order, speaker/addressee relationships
  - line_id → text_vi mapping structure

Uses /no_think for clean prose output.
Returns [{line_id, text_vi}] with optional polish_notes.
Post-polish validation checks protected tokens haven't changed.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .context import StoryContext
from .segmenter import Chunk

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SYSTEM_POLISHER = """\
/no_think

You are a Vietnamese fiction editor. Lightly polish the TRANSLATION_LINES.

Hard constraints (violations will be caught by post-polish QA):
1. Do NOT change any pronouns or address terms listed in PROTECTED_PRONOUNS.
2. Do NOT change names or any term in PROTECTED_TOKENS.
3. Do NOT change meaning, add information, or remove events.
4. Do NOT change speaker/addressee relationships.
5. Do NOT reorder paragraphs or merge/split line_ids.
6. Preserve line_id → text_vi structure in output.
7. Keep the genre style: {genre_note}

Polish only:
- Awkward phrasing or unnatural word order
- Repetitive sentence structure
- Rhythm and readability
- Dialogue naturalness (while respecting address terms)

Output strict JSON:
{{
  "chunk_id": "...",
  "lines": [
    {{"line_id": "...", "text_vi": "..."}}
  ],
  "polish_notes": []
}}
"""


def _build_protected_tokens(ctx: StoryContext, chunk: Chunk) -> tuple[list[str], list[str]]:
    """Return (protected_names, protected_pronouns) for this chunk."""
    rel_glossary = ctx.relevant_glossary(chunk.source_text)
    rel_chars = ctx.relevant_characters(chunk.source_text)

    names: list[str] = []
    for entry in rel_glossary:
        if entry.target_vi:
            names.append(entry.target_vi)
    for char in rel_chars:
        if char.name_vi:
            names.append(char.name_vi)

    # All pronoun variants that appear in char data
    pronouns: list[str] = []
    vn_pronouns = {
        "ta", "tôi", "mình", "tại hạ",
        "hắn", "nàng", "y", "lão", "gã", "ả",
        "anh ta", "cô ta", "anh ấy", "cô ấy",
        "chàng", "ngươi", "người", "tên",
    }
    for char in rel_chars:
        pronouns.extend(char.narrator_reference.values())
    pronouns.extend(vn_pronouns)
    pronouns = list(set(p for p in pronouns if p))

    return names, pronouns


def _genre_note(genre: str) -> str:
    notes = {
        "tien_hiep": "cổ phong trang nghiêm, Hán Việt khi cần",
        "huyen_huyen": "Hán Việt vừa phải, không quá cổ",
        "western_fantasy": "hiện đại, không dùng hắn/nàng/lão cổ phong",
        "mat_the": "câu ngắn, căng thẳng",
        "do_thi": "đô thị, không Hán Việt cổ phong",
        "lang_man": "mềm mại, cảm xúc",
    }
    return notes.get(genre) or "giữ phong cách gốc"


def _build_polisher_prompt(
    chunk: Chunk,
    translation_lines: list[dict],
    ctx: StoryContext,
) -> list[dict[str, str]]:
    names, pronouns = _build_protected_tokens(ctx, chunk)

    system = _SYSTEM_POLISHER.format(genre_note=_genre_note(ctx.genre))
    lines_json = json.dumps(translation_lines, ensure_ascii=False, indent=2)

    user_content = "\n".join([
        f"GENRE: {ctx.genre}",
        f"PROTECTED_TOKENS: {', '.join(names[:30])}",
        f"PROTECTED_PRONOUNS: {', '.join(pronouns[:20])}",
        f"\nTRANSLATION_LINES:\n{lines_json}",
    ])

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]


# ---------------------------------------------------------------------------
# Post-polish validation
# ---------------------------------------------------------------------------

def _validate_protected_tokens(
    before: list[dict],
    after: list[dict],
    protected: list[str],
) -> list[str]:
    """Return list of tokens that were present before but missing after."""
    before_text = " ".join(item.get("text_vi") or "" for item in before)
    after_text = " ".join(item.get("text_vi") or "" for item in after)
    removed = [t for t in protected if t in before_text and t not in after_text]
    return removed


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def run_polisher(
    chunk: Chunk,
    translation_lines: list[dict],
    ctx: StoryContext,
    ollama_url: str,
    model: str = "qwen3:14b",
    timeout: int = 300,
    num_ctx: int = 32768,
    keep_alive: str = "10m",
) -> tuple[list[dict], list[str]]:
    """Polish translation_lines.

    Returns:
        (polished_lines, warnings)
        warnings: list of protected tokens that changed (post-polish validation failures)
    """
    import requests

    names, pronouns = _build_protected_tokens(ctx, chunk)
    all_protected = list(set(names + pronouns))
    messages = _build_polisher_prompt(chunk, translation_lines, ctx)

    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": 0.25,
            "top_p": 0.90,
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
        raw = _strip_think(resp.json().get("message", {}).get("content", ""))
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start == -1 or end == 0:
            raise ValueError("no JSON in polisher output")
        data = json.loads(raw[start:end])
        polished_lines = data.get("lines") or translation_lines

        warnings = _validate_protected_tokens(translation_lines, polished_lines, all_protected)
        if warnings:
            # Revert lines where token was removed
            line_map_before = {item["line_id"]: item for item in translation_lines}
            for item in polished_lines:
                lid = item.get("line_id") or ""
                if lid in line_map_before:
                    before_text = line_map_before[lid].get("text_vi") or ""
                    after_text = item.get("text_vi") or ""
                    # If this line lost a protected token, revert it
                    if any(t in before_text and t not in after_text for t in all_protected):
                        item["text_vi"] = before_text

        return polished_lines, warnings

    except Exception as exc:
        return translation_lines, [f"polisher error: {exc}"]
