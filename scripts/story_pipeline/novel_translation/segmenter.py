"""Segmenter — split chapter text into segments with stable IDs and risk scores.

Each segment carries:
  - line_id:   immutable reference used by all downstream passes (p001, p001_d01, ...)
  - text:      raw source text of the segment
  - kind:      dialogue | narration | inner_thought | system | poem | title
  - risk:      low | medium | high — controls whether resolver runs

Risk scoring:
  low    — single active character implied, pure narration, few pronoun cues
  medium — two characters OR tagged dialogue present
  high   — multiple characters, same-gender ambiguity, implicit speaker changes,
            emotional/status-sensitive scenes (romance, hostility, ceremony)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

SegmentKind = Literal["dialogue", "narration", "inner_thought", "system", "poem", "title"]
RiskLevel = Literal["low", "medium", "high"]


@dataclass
class Segment:
    line_id: str
    text: str
    kind: SegmentKind
    risk: RiskLevel = "low"
    # populated by resolver
    speaker: str = ""
    addressee: str = ""
    self_pronoun_vi: str = ""
    you_pronoun_vi: str = ""
    third_person_refs: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "line_id": self.line_id,
            "text": self.text,
            "kind": self.kind,
            "risk": self.risk,
            "speaker": self.speaker,
            "addressee": self.addressee,
            "self_pronoun_vi": self.self_pronoun_vi,
            "you_pronoun_vi": self.you_pronoun_vi,
            "third_person_refs": self.third_person_refs,
        }


@dataclass
class Chunk:
    """One chunk = one LLM call unit (usually a chapter or sub-chapter slice)."""
    chunk_id: str
    chapter_id: str
    segments: list[Segment] = field(default_factory=list)
    risk: RiskLevel = "low"
    active_characters: list[str] = field(default_factory=list)
    context_tail: str = ""  # last ~600 chars of previous chunk

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "chapter_id": self.chapter_id,
            "risk": self.risk,
            "active_characters": self.active_characters,
            "context_tail": self.context_tail,
            "segments": [s.to_dict() for s in self.segments],
        }

    @property
    def source_text(self) -> str:
        return "\n".join(s.text for s in self.segments)

    @property
    def segment_count(self) -> int:
        return len(self.segments)

    @property
    def dialogue_count(self) -> int:
        return sum(1 for s in self.segments if s.kind == "dialogue")


# ---------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------

# Vietnamese/Chinese quotation marks and common dialogue openers
_DIALOGUE_RE = re.compile(
    r'^(?:'
    r'[""«»「」『』【】]'      # opening quote
    r'|[-–—]\s*\w'             # em-dash dialogue (Vietnamese style)
    r')',
    re.UNICODE,
)

# Inner thought markers: italics-like, or explicit cues
_INNER_THOUGHT_RE = re.compile(
    r'^(?:'
    r'\*[^*]+\*'               # *italics*
    r'|（[^）]{1,80}）'        # full-width parens (jp/cn)
    r'|\([^)]{1,80}\)'         # half-width parens
    r')',
    re.UNICODE,
)

# System/status box: common in LitRPG / he_thong
_SYSTEM_RE = re.compile(
    r'^\[(?:System|Thông báo|Hệ thống|Status|Quest|EXP|HP|MP|Skill)',
    re.IGNORECASE | re.UNICODE,
)

# Poem/verse: short lines with consistent ending punctuation or explicit markers
_POEM_RE = re.compile(
    r'(?:^|\n)(?:[-–—]{3,}|[*]{3,}|={3,})',
)

# Separator lines that should be dropped
_SEPARATOR_RE = re.compile(
    r'^\s*(?:[*\-=_~]{3,}|…{3,}|\.{3,})\s*$'
)

# High-risk cue words in source text
_HIGH_RISK_SOURCE = re.compile(
    r'他|她|其|此人|那人|彼|众人|她们|他们'                    # ambiguous CJK pronouns
    r'|그|그녀|그들|그가|그녀가',                               # Korean pronouns
    re.UNICODE,
)

# High-risk cue words in Vietnamese output context / character names matching
_HIGH_RISK_SCENE = re.compile(
    r'(?:hôn|ôm|nắm tay|nhìn chằm chằm|thách thức|quỳ|bái|kính|tôn)',
    re.IGNORECASE | re.UNICODE,
)

# CJK character detector (indicates source language)
_CJK_RE = re.compile(r'[一-鿿぀-ヿ가-힯]')


# ---------------------------------------------------------------------------
# Core segmentation
# ---------------------------------------------------------------------------

def _classify_line(line: str) -> SegmentKind:
    stripped = line.strip()
    if not stripped:
        return "narration"
    if _SYSTEM_RE.match(stripped):
        return "system"
    if _INNER_THOUGHT_RE.match(stripped):
        return "inner_thought"
    if _DIALOGUE_RE.match(stripped):
        return "dialogue"
    return "narration"


def _score_risk(segments: list[Segment], active_characters: list[str]) -> RiskLevel:
    dialogue_count = sum(1 for s in segments if s.kind == "dialogue")
    n_chars = len(active_characters)
    source_text = " ".join(s.text for s in segments)
    has_ambiguous_pronouns = bool(_HIGH_RISK_SOURCE.search(source_text))
    has_sensitive_scene = bool(_HIGH_RISK_SCENE.search(source_text))

    if (
        dialogue_count >= 3
        or n_chars >= 3
        or (n_chars >= 2 and has_ambiguous_pronouns)
        or has_sensitive_scene
    ):
        return "high"
    if dialogue_count >= 1 or n_chars >= 2:
        return "medium"
    return "low"


def _find_active_character_keys(
    text: str,
    surface_to_key: dict[str, str],
) -> list[str]:
    """Return character keys (not surfaces) whose names appear in text.

    Uses a surface→key map so Chunk.active_characters always holds keys,
    which is what StoryContext.relevant_relationships() expects.
    """
    text_lower = text.lower()
    found_keys: list[str] = []
    seen: set[str] = set()
    for surface, key in surface_to_key.items():
        if surface.lower() in text_lower and key not in seen:
            found_keys.append(key)
            seen.add(key)
    return found_keys


def segment_text(
    text: str,
    chapter_id: str,
    chunk_index: int = 0,
    character_surface_to_key: dict[str, str] | None = None,
    context_tail: str = "",
    max_chars: int = 2000,
) -> list[Chunk]:
    """Split chapter text into chunks of ~max_chars, each with segmented lines.

    Args:
        text:                      raw source text
        chapter_id:                e.g. "chapter_0042"
        chunk_index:               starting chunk counter (for stable IDs across splits)
        character_surface_to_key:  mapping surface/name → character key.
                                   Chunk.active_characters will contain keys, not surfaces,
                                   so StoryContext.relevant_relationships() works correctly.
        context_tail:              last ~600 chars from previous chunk for pronoun continuity
        max_chars:                 soft ceiling per chunk

    Returns:
        list of Chunk objects
    """
    character_surface_to_key = character_surface_to_key or {}
    lines = text.splitlines()

    # Build raw segments, skipping pure separators
    raw_segments: list[tuple[str, SegmentKind]] = []
    for line in lines:
        if _SEPARATOR_RE.match(line):
            continue
        stripped = line.strip()
        if not stripped:
            continue
        raw_segments.append((stripped, _classify_line(stripped)))

    # Group into chunks by char budget
    chunks: list[Chunk] = []
    current_lines: list[tuple[str, SegmentKind]] = []
    current_chars = 0

    def _flush(lines_buf: list[tuple[str, SegmentKind]], tail: str) -> Chunk:
        nonlocal chunk_index
        chunk_index += 1
        chunk_id = f"{chapter_id}_c{chunk_index:03d}"
        segments: list[Segment] = []
        # assign IDs within chunk
        d_idx = n_idx = t_idx = 0
        for i, (ln, kind) in enumerate(lines_buf):
            if kind == "dialogue":
                d_idx += 1
                lid = f"p{chunk_index:03d}_d{d_idx:02d}"
            elif kind in ("inner_thought",):
                t_idx += 1
                lid = f"p{chunk_index:03d}_t{t_idx:02d}"
            else:
                n_idx += 1
                lid = f"p{chunk_index:03d}_n{n_idx:02d}"
            segments.append(Segment(line_id=lid, text=ln, kind=kind))

        combined = " ".join(ln for ln, _ in lines_buf)
        active = _find_active_character_keys(combined, character_surface_to_key)
        risk = _score_risk(segments, active)
        for seg in segments:
            seg.risk = risk

        return Chunk(
            chunk_id=chunk_id,
            chapter_id=chapter_id,
            segments=segments,
            risk=risk,
            active_characters=active,
            context_tail=tail,
        )

    prev_tail = context_tail
    for line_text, kind in raw_segments:
        current_lines.append((line_text, kind))
        current_chars += len(line_text)
        if current_chars >= max_chars:
            chunk = _flush(current_lines, prev_tail)
            prev_tail = current_lines[-1][0][-600:] if current_lines else ""
            chunks.append(chunk)
            current_lines = []
            current_chars = 0

    if current_lines:
        chunks.append(_flush(current_lines, prev_tail))

    return chunks


def segments_to_source_block(segments: list[Segment]) -> str:
    """Format segments for injection into prompts with line_id markers."""
    lines = []
    for seg in segments:
        lines.append(f"[{seg.line_id}] {seg.text}")
    return "\n".join(lines)


def reconstruct_text(translated_lines: list[dict]) -> str:
    """Render [{line_id, text_vi}] list back to plain paragraph text."""
    paragraphs = [item["text_vi"] for item in translated_lines if item.get("text_vi")]
    return "\n\n".join(p.strip() for p in paragraphs if p.strip())
