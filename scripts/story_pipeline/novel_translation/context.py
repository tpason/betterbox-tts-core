"""Per-story context loader.

Wraps StoryMemory + genre_prompts char-map to produce a StoryContext object
that every pipeline pass reads from. All data is per-story — no global config.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CharacterProfile:
    """Loaded per-story character entry with pronoun policy."""
    key: str                           # normalized key (e.g. "li_ming")
    source_names: list[str]            # original CJK/romanized names
    name_vi: str                       # canonical Vietnamese name
    gender: str                        # male | female | unknown
    role: str                          # male_lead | female_lead | antagonist | ...
    narrator_reference: dict[str, str] = field(default_factory=dict)
    # {"neutral": "hắn", "respectful": "chàng", "derogatory": "gã"}
    speech_style: str = ""

    @property
    def all_surfaces(self) -> list[str]:
        return list({self.name_vi, *self.source_names, self.key})

    def narrator_pronoun(self, tone: str = "neutral") -> str:
        return self.narrator_reference.get(tone) or self.narrator_reference.get("neutral") or ""


@dataclass
class Relationship:
    """Directional pronoun policy for a speaker→addressee pair."""
    speaker: str          # character key
    addressee: str        # character key
    self_pronoun: str     # what speaker calls themselves
    you_pronoun: str      # what speaker calls addressee
    third_reference: str  # how narrator refers to addressee in this context
    tone: str = "neutral"


@dataclass
class GlossaryEntry:
    source: str
    target_vi: str
    term_type: str = ""   # sect | cultivation_level | skill | item | place | title
    aliases: list[str] = field(default_factory=list)
    do_not_translate: bool = False
    notes: str = ""
    priority: bool = False

    @property
    def all_sources(self) -> list[str]:
        return list({self.source, *self.aliases})


@dataclass
class StoryContext:
    """Complete per-story context for one pipeline run."""
    story_id: str
    slug: str
    genre: str
    style_profile: str = ""    # voice/tone description from story_bible
    char_map_raw: str = ""     # raw char_map text (legacy format, for genre_prompts compat)

    characters: list[CharacterProfile] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)
    glossary: list[GlossaryEntry] = field(default_factory=list)

    # chapter recaps for context injection
    recaps: dict[str, str] = field(default_factory=dict)

    def character_by_key(self, key: str) -> CharacterProfile | None:
        for c in self.characters:
            if c.key == key:
                return c
        return None

    def relationship(self, speaker: str, addressee: str) -> Relationship | None:
        for r in self.relationships:
            if r.speaker == speaker and r.addressee == addressee:
                return r
        return None

    def relevant_characters(self, text: str, max_items: int = 12) -> list[CharacterProfile]:
        """Return characters whose names/surfaces appear in text."""
        text_lower = text.lower()
        result: list[CharacterProfile] = []
        main: list[CharacterProfile] = []
        for c in self.characters:
            if "lead" in c.role or "main" in c.role:
                main.append(c)
            if any(s.lower() in text_lower for s in c.all_surfaces if s):
                result.append(c)
        merged: list[CharacterProfile] = []
        seen: set[str] = set()
        for c in [*result, *main]:
            if c.key not in seen:
                seen.add(c.key)
                merged.append(c)
        return merged[:max_items]

    def relevant_glossary(self, text: str, max_items: int = 20) -> list[GlossaryEntry]:
        """Return glossary entries whose sources appear in text."""
        text_lower = text.lower()
        result: list[GlossaryEntry] = []
        priority: list[GlossaryEntry] = []
        for entry in self.glossary:
            if entry.priority:
                priority.append(entry)
            if any(s.lower() in text_lower for s in entry.all_sources if s):
                result.append(entry)
        merged: list[GlossaryEntry] = []
        seen: set[str] = set()
        for entry in [*result, *priority]:
            k = entry.source
            if k not in seen:
                seen.add(k)
                merged.append(entry)
        return merged[:max_items]

    def relevant_relationships(self, active_character_keys: list[str]) -> list[Relationship]:
        """Return relationships where both parties are in active_character_keys."""
        keys = set(active_character_keys)
        return [
            r for r in self.relationships
            if r.speaker in keys or r.addressee in keys
        ]

    def format_characters_for_prompt(self, characters: list[CharacterProfile]) -> str:
        lines = []
        for c in characters:
            ref = "/".join(f"{k}:{v}" for k, v in c.narrator_reference.items() if v)
            line = f"- {c.name_vi} ({c.gender}, {c.role}): narrator_ref={ref or '?'}"
            if c.speech_style:
                line += f" | speech: {c.speech_style}"
            lines.append(line)
        return "\n".join(lines)

    def format_glossary_for_prompt(self, entries: list[GlossaryEntry]) -> str:
        lines = []
        for e in entries:
            aliases = f" (aliases: {', '.join(e.aliases)})" if e.aliases else ""
            note = f" — {e.notes}" if e.notes else ""
            lines.append(f"- {e.source}{aliases} → {e.target_vi}{note}")
        return "\n".join(lines)

    def format_relationships_for_prompt(self, rels: list[Relationship]) -> str:
        lines = []
        for r in rels:
            lines.append(
                f"- {r.speaker} → {r.addressee}: "
                f"self={r.self_pronoun}, you={r.you_pronoun}, "
                f"narrator_ref={r.third_reference}, tone={r.tone}"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _parse_gender_from_narration(narration: str, role: str) -> str:
    """Infer gender from third_person_narration / role text."""
    text = (narration + " " + role).lower()
    if any(w in text for w in ("nữ", "cô", "cô ta", "tiên nữ", "nàng", "bà", "female")):
        return "female"
    if any(w in text for w in ("nam", "anh", "anh ta", "hắn", "chàng", "ông", "gã", "male")):
        return "male"
    return "unknown"


def _narrator_reference_from_narration(narration: str, gender: str) -> dict[str, str]:
    """Extract narrator pronoun choices from third_person_narration text."""
    if not narration:
        defaults = {"male": "anh ta", "female": "cô ta"}
        return {"neutral": defaults.get(gender) or "họ"}

    # Pick the first-mentioned pronoun as neutral reference
    pronouns_male = ["anh ta", "anh", "hắn", "y", "gã", "cậu ta", "chàng"]
    pronouns_female = ["cô ta", "cô", "nàng", "bà", "ả", "cô ấy"]
    all_pronouns = (pronouns_male if gender != "female" else pronouns_female) + pronouns_female + pronouns_male

    result: dict[str, str] = {}
    for p in all_pronouns:
        if p in narration and "neutral" not in result:
            result["neutral"] = p
        elif p in narration and p != result.get("neutral") and "respectful" not in result:
            result["respectful"] = p

    if not result:
        result["neutral"] = "anh ta" if gender == "male" else ("cô ta" if gender == "female" else "họ")
    return result


def _load_characters_json(path: Path) -> tuple[list[CharacterProfile], list[Relationship]]:
    """Load characters.json (story_memory format — list of character objects)."""
    if not path.exists():
        return [], []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return [], []
    except Exception:
        return [], []

    characters: list[CharacterProfile] = []
    relationships: list[Relationship] = []

    for entry in data:
        key = entry.get("id") or ""
        if not key:
            continue
        name_vi = entry.get("canonical_name") or key

        narration = entry.get("third_person_narration") or ""
        role = entry.get("role") or ""
        gender = _parse_gender_from_narration(narration, role)
        narrator_ref = _narrator_reference_from_narration(narration, gender)

        # Build source_names from wrong_spellings + allowed_nicknames + canonical
        source_names: list[str] = [name_vi]
        source_names += entry.get("wrong_spellings") or []
        source_names += entry.get("allowed_nicknames") or []

        speech_parts = []
        if entry.get("self_address"):
            speech_parts.append(f"tự xưng: {entry['self_address']}")
        if entry.get("voice_style"):
            speech_parts.append(entry["voice_style"])

        characters.append(CharacterProfile(
            key=key,
            source_names=list(dict.fromkeys(s for s in source_names if s)),
            name_vi=name_vi,
            gender=gender,
            role=role,
            narrator_reference=narrator_ref,
            speech_style="; ".join(speech_parts),
        ))

        # Extract directional relationship rules from addressing_by_target.
        # Supports two schemas:
        #   dict: {"Krang_private": "tôi/anh tùy câu", ...}
        #   list: ["Speaker -> Target: tôi/cậu", ...]
        addressing = entry.get("addressing_by_target")
        if isinstance(addressing, dict):
            for target_context, rule_text in addressing.items():
                # "Krang_private" → addressee=krang, tone=private
                parts = target_context.split("_", 1)
                addressee_key = parts[0].lower() if parts else ""
                tone = parts[1] if len(parts) > 1 else "neutral"
                if not addressee_key or addressee_key in ("hostile", "subordinates", "civilians", "children"):
                    # Non-character addressing contexts — skip structural relationships
                    continue
                self_p = ""
                for p in ["tôi", "ta", "mình", "tại hạ", "anh"]:
                    if p in str(rule_text).lower() and not self_p:
                        self_p = p
                        break
                relationships.append(Relationship(
                    speaker=key,
                    addressee=addressee_key,
                    self_pronoun=self_p,
                    you_pronoun="",
                    third_reference=narrator_ref.get("neutral") or "",
                    tone=tone,
                ))
        elif isinstance(addressing, list):
            # List format: "Speaker -> Target: rule text" — parse loosely
            import re as _re
            for rule in addressing:
                if not isinstance(rule, str):
                    continue
                # Try "Speaker -> Target: ..." pattern
                m = _re.match(r"^([^->]+)\s*->\s*([^:]+):\s*(.+)$", rule)
                if not m:
                    continue
                speaker_raw, target_raw, rule_text = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
                # Only create relationship if speaker is self (current character)
                if key.lower() not in speaker_raw.lower() and speaker_raw.lower() not in (entry.get("canonical_name") or "").lower():
                    continue
                addressee_key = _re.sub(r"[^a-z0-9_]", "_", target_raw.lower())[:30]
                self_p = ""
                for p in ["tôi", "ta", "mình", "tại hạ", "anh"]:
                    if p in rule_text.lower() and not self_p:
                        self_p = p
                        break
                relationships.append(Relationship(
                    speaker=key,
                    addressee=addressee_key,
                    self_pronoun=self_p,
                    you_pronoun="",
                    third_reference=narrator_ref.get("neutral") or "",
                    tone="neutral",
                ))

    return characters, relationships


def _load_char_map_v2(path: Path) -> tuple[list[CharacterProfile], list[Relationship]]:
    """Load char-map v2 JSON (explicit format with narrator_reference + relationships dict)."""
    if not path.exists():
        return [], []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return [], []

    characters: list[CharacterProfile] = []
    for key, entry in (data.get("characters") or {}).items():
        characters.append(CharacterProfile(
            key=key,
            source_names=entry.get("source_names") or [],
            name_vi=entry.get("name_vi") or key,
            gender=entry.get("gender") or "unknown",
            role=entry.get("role") or "",
            narrator_reference=entry.get("narrator_reference") or {},
            speech_style=entry.get("speech_style") or "",
        ))

    relationships: list[Relationship] = []
    for rel in (data.get("relationships") or []):
        relationships.append(Relationship(
            speaker=rel.get("speaker") or "",
            addressee=rel.get("addressee") or "",
            self_pronoun=rel.get("self") or "",
            you_pronoun=rel.get("you") or "",
            third_reference=rel.get("third_person_reference") or "",
            tone=rel.get("tone") or "neutral",
        ))

    return characters, relationships


def _load_glossary_v2(path: Path) -> list[GlossaryEntry]:
    """Load glossary v2 JSON."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []

    entries: list[GlossaryEntry] = []
    items = data if isinstance(data, list) else (data.get("terms") or [])
    for item in items:
        entries.append(GlossaryEntry(
            source=item.get("source") or "",
            target_vi=item.get("target_vi") or item.get("canonical_vi") or item.get("vi") or "",
            term_type=item.get("type") or item.get("term_type") or "",
            aliases=item.get("aliases") or [],
            do_not_translate=bool(item.get("do_not_translate")),
            notes=item.get("notes") or "",
            priority=bool(item.get("priority")),
        ))
    return entries


def load_story_context(
    story_id: str,
    slug: str,
    genre: str,
    memory_dir: str | Path | None = None,
    char_map_raw: str = "",
) -> StoryContext:
    """Load per-story context from story_memory directory.

    Supports both legacy (char_map raw text) and new v2 JSON formats.
    V2 files take precedence when present.

    Args:
        story_id:     DB story ID
        slug:         story slug (e.g. "21180-vinh-thoai-hiep-si")
        genre:        detected genre (e.g. "western_fantasy")
        memory_dir:   path to story_memory/<story-slug>/ directory
        char_map_raw: raw char_map text from DB (legacy fallback)
    """
    ctx = StoryContext(
        story_id=story_id,
        slug=slug,
        genre=genre,
        char_map_raw=char_map_raw,
    )

    if memory_dir is None:
        return ctx

    mem_path = Path(memory_dir)
    if not mem_path.exists():
        return ctx

    # Load story bible / style guide
    for fname in ("story_bible.txt", "story_bible.md", "style_guide.txt"):
        p = mem_path / fname
        if p.exists():
            ctx.style_profile = p.read_text(encoding="utf-8").strip()
            break

    # Load characters: prefer char_map_v2.json (explicit), fall back to characters.json (story_memory format)
    v2_char = mem_path / "char_map_v2.json"
    chars_json = mem_path / "characters.json"
    if v2_char.exists():
        ctx.characters, ctx.relationships = _load_char_map_v2(v2_char)
    elif chars_json.exists():
        ctx.characters, ctx.relationships = _load_characters_json(chars_json)

    # Load glossary v2 if present, fall back to glossary.json
    for gname in ("glossary_v2.json", "glossary.json"):
        gpath = mem_path / gname
        if gpath.exists():
            ctx.glossary = _load_glossary_v2(gpath)
            break

    # Load recaps
    recaps_path = mem_path / "recaps.json"
    if recaps_path.exists():
        try:
            data = json.loads(recaps_path.read_text(encoding="utf-8"))
            ctx.recaps = {str(k): str(v.get("recap") or v) for k, v in data.items() if v}
        except Exception:
            pass

    return ctx


def build_recap_context(ctx: StoryContext, current_chapter: int, max_prev: int = 3) -> str:
    """Return last N chapter recaps as a context block."""
    if not ctx.recaps:
        return ""
    chapters = sorted(int(k) for k in ctx.recaps if k.isdigit())
    prev = [c for c in chapters if c < current_chapter][-max_prev:]
    if not prev:
        return ""
    lines = []
    for c in prev:
        recap = ctx.recaps.get(str(c), "")
        if recap:
            lines.append(f"Chương {c}: {recap}")
    return "\n".join(lines)
