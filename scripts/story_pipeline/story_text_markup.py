from __future__ import annotations

import re
import unicodedata
from pathlib import Path


_DIGITS = {
    0: "không",
    1: "một",
    2: "hai",
    3: "ba",
    4: "bốn",
    5: "năm",
    6: "sáu",
    7: "bảy",
    8: "tám",
    9: "chín",
}


def _number_to_vietnamese(value: int) -> str:
    if value < 10:
        return _DIGITS[value]
    if value < 100:
        tens, ones = divmod(value, 10)
        prefix = "mười" if tens == 1 else f"{_DIGITS[tens]} mươi"
        if ones == 0:
            return prefix
        if ones == 1 and tens > 1:
            return f"{prefix} mốt"
        if ones == 5:
            return f"{prefix} lăm"
        return f"{prefix} {_DIGITS[ones]}"
    if value < 1000:
        hundreds, rest = divmod(value, 100)
        prefix = f"{_DIGITS[hundreds]} trăm"
        if rest == 0:
            return prefix
        if rest < 10:
            return f"{prefix} lẻ {_DIGITS[rest]}"
        return f"{prefix} {_number_to_vietnamese(rest)}"
    if value < 1_000_000:
        thousands, rest = divmod(value, 1000)
        prefix = f"{_number_to_vietnamese(thousands)} nghìn"
        if rest == 0:
            return prefix
        if rest < 10:
            return f"{prefix} lẻ {_DIGITS[rest]}"
        if rest < 100:
            return f"{prefix} không trăm {_number_to_vietnamese(rest)}"
        return f"{prefix} {_number_to_vietnamese(rest)}"
    if value < 1_000_000_000:
        millions, rest = divmod(value, 1_000_000)
        prefix = f"{_number_to_vietnamese(millions)} triệu"
        if rest == 0:
            return prefix
        return f"{prefix} {_number_to_vietnamese(rest)}"
    return str(value)


# VN uses dot as thousand separator, comma as decimal separator
_THOUSAND_SEP_RE = re.compile(r"\b(\d{1,3})(?:\.\d{3})+\b")


def _expand_thousand_sep(match: re.Match[str]) -> str:
    raw = match.group(0).replace(".", "")
    try:
        return _number_to_vietnamese(int(raw))
    except (ValueError, OverflowError):
        return match.group(0)


# Game/cultivation abbreviations common in translated novels
_STAT_ABBREVS: dict[str, str] = {
    r"\bHP\b": "HP",
    r"\bMP\b": "MP",
    r"\bEXP\b": "EXp",
    r"\bSTR\b": "sức mạnh",
    r"\bDEX\b": "sự khéo léo",
    r"\bAGI\b": "tốc độ",
    r"\bINT\b": "trí tuệ",
    r"\bATK\b": "tấn công",
    r"\bDEF\b": "phòng thủ",
}


def _replace_percent(match: re.Match[str]) -> str:
    value = int(match.group(1))
    return f"{_number_to_vietnamese(value)} phần trăm"


def _expand_plain_integer(match: re.Match[str]) -> str:
    try:
        n = int(match.group(0))
        if 0 <= n <= 999_999_999:
            return _number_to_vietnamese(n)
    except (ValueError, OverflowError):
        pass
    return match.group(0)


def normalize_reduplication_for_tts(text: str) -> str:
    """Normalize hyphenated reduplication: "té-té" → "té té", "lắc-lắc" → "lắc lắc".

    Vietnamese and Korean-origin translated text often expresses onomatopoeia and
    emphatic reduplication with a hyphen (e.g. "rào-rào", "vù-vù", "té-té").
    Viterbox tokenizes the hyphen as a punctuation token which creates a prosodic
    pause (like a comma) between the two syllables, making the reading sound choppy.
    Replacing with a space produces smooth delivery.
    """
    # Pattern: word-word where both halves are identical (full reduplication)
    # Also matches partial reduplication like "nhỏ-to", "xanh-đỏ" — hyphen → space
    # Only process hyphens WITHIN words (not leading/trailing), and both sides must
    # be 1-7 chars (Vietnamese syllable range).
    return re.sub(
        r"(?<=[A-Za-zÀ-ỹĐđ])-(?=[A-Za-zÀ-ỹĐđ])",
        " ",
        text,
    )


def normalize_numbers_for_tts(text: str) -> str:
    """Expand numeric patterns that TTS engines read poorly in Vietnamese text."""
    # Dot-separated thousands first: 1.500 → "một nghìn năm trăm"
    # Must run before plain integer expansion so "1.500" isn't split into "1" and "500"
    text = _THOUSAND_SEP_RE.sub(_expand_thousand_sep, text)
    # Common stat abbreviations in cultivation/LitRPG novels
    for pattern, replacement in _STAT_ABBREVS.items():
        text = re.sub(pattern, replacement, text)
    # Expand all remaining standalone integers: 50 → "năm mươi", 2024 → "hai nghìn..."
    # Percentages and thousand-sep numbers were already converted above.
    text = re.sub(r"\b\d+\b", _expand_plain_integer, text)
    return text


def normalize_story_markup(text: str) -> str:
    replacements = {
        "【": ". ",
        "】": ". ",
        "[": ". ",
        "]": ". ",
        "「": '"',
        "」": '"',
        "『": '"',
        "』": '"',
        "…": "...",
        "％": "%",
    }
    text = unicodedata.normalize("NFC", text)
    for src, dst in replacements.items():
        text = text.replace(src, dst)

    text = re.sub(r"\.{2,}", "...", text)
    text = re.sub(r"\b(\d{1,3})\s*%", _replace_percent, text)
    text = re.sub(r"\b(\d{1,3})\s+phần trăm\b", _replace_percent, text, flags=re.IGNORECASE)
    text = re.sub(r"\bDM\b", "đờ mờ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bdm\b", "đờ mờ", text)
    text = re.sub(r"[\u200b-\u200f\u2060-\u206f\ufeff]", "", text)
    return text.strip()


def _split_units(text: str) -> list[tuple[str, str]]:
    text = normalize_story_markup(text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    lines = [line.strip().strip('"').strip() for line in text.splitlines() if line.strip()]
    joined = ". ".join(lines)
    joined = re.sub(r"\s+", " ", joined).strip()
    if not joined:
        return []

    token_re = re.compile(r"(.+?)([.!?。！？;；:：]+|\.{2,}|…+|[,，、]+|$)")
    units: list[tuple[str, str]] = []
    for match in token_re.finditer(joined):
        content = match.group(1).strip(" ,.;:!?-—–")
        punct = match.group(2) or ""
        if not content:
            continue
        pause = "sentence" if re.search(r"[.!?。！？;；:：]|\.{2,}|…", punct) else "comma"
        units.append((content, pause))

    return units


def _split_long_unit(unit: str, max_clause_chars: int) -> list[str]:
    words = unit.split()
    chunks: list[str] = []
    chunk = ""
    for word in words:
        candidate = f"{chunk} {word}".strip() if chunk else word
        if len(candidate) > max_clause_chars and chunk:
            chunks.append(chunk)
            chunk = word
        else:
            chunk = candidate
    if chunk:
        chunks.append(chunk)
    return chunks


def pack_for_viterbox_tts(text: str, max_clause_chars: int = 160, comma_every_chars: int = 70) -> str:
    """Rewrite story text into fewer, smoother clauses for Viterbox.

    Viterbox internally turns punctuation into pause markers and generates each
    clause separately. Too many commas create many short generations and audible
    join/end artifacts. This packer removes most internal punctuation and emits
    clauses around `max_clause_chars` with period boundaries.
    """
    units = _split_units(text)
    if not units:
        return ""

    clauses: list[str] = []
    current = ""
    soft_len = 0
    for unit, pause in units:
        separator = " "
        if current and pause == "comma" and soft_len >= comma_every_chars:
            separator = ", "
            soft_len = 0

        candidate = f"{current} {unit}".strip() if current else unit
        if separator == ", " and current:
            candidate = f"{current}, {unit}".strip()

        if pause == "sentence":
            if len(candidate) <= max_clause_chars:
                clauses.append(candidate)
            else:
                if current:
                    clauses.append(current)
                clauses.extend(_split_long_unit(unit, max_clause_chars))
            current = ""
            soft_len = 0
            continue

        if len(candidate) <= max_clause_chars:
            current = candidate
            soft_len += len(unit)
        elif current:
            clauses.append(current)
            current = unit
            soft_len = len(unit)
        else:
            chunks = _split_long_unit(unit, max_clause_chars)
            clauses.extend(chunks[:-1])
            current = chunks[-1] if chunks else ""
            soft_len = len(current)

    if current:
        clauses.append(current)

    return ". ".join(clauses) + "."


def _get_clean_for_audiobook_tts():
    import importlib
    import sys
    _HERE = Path(__file__).resolve().parent
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))
    mod = importlib.import_module("polish_chapter_texts_ollama")
    return mod.clean_for_audiobook_tts


def prepare_text_for_tts(text: str) -> str:
    """Full pre-TTS text pipeline: audiobook cleanup → markup normalize → number expand.

    Call this once on the raw polished chapter text before passing to any Viterbox
    synthesis function. Covers the gap where audio workers skip clean_for_audiobook_tts.
    """
    clean_for_audiobook_tts = _get_clean_for_audiobook_tts()
    text = clean_for_audiobook_tts(text)
    text = normalize_story_markup(text)
    text = normalize_reduplication_for_tts(text)
    text = normalize_numbers_for_tts(text)
    return text


# ── Text quality evaluation ────────────────────────────────────────────────────

_EN_WORD_RE = re.compile(r"\b([A-Za-z]{5,})\b")  # ≥5 pure-ASCII chars (short words are likely Vietnamese)
# Common loanwords that Viterbox handles fine
_VI_ENGLISH_LOOKALIKE = frozenset({
    "OK", "ok", "boss", "team", "app", "web", "game", "fan", "cafe",
    "HP", "MP", "ATK", "DEF", "EXP", "STR", "AGI", "INT", "DEX",
    "online", "offline", "level",
})

_LONG_WORD_RE = re.compile(r"\b\w{15,}\b")
_DIGIT_RE = re.compile(r"\b\d+\b")


def evaluate_text_for_tts(text: str) -> list[dict]:
    """Scan polished chapter text for patterns that degrade TTS audio quality.

    Returns a list of issue dicts:
        {"type": str, "severity": "warn"|"info", "line": int, "text": str, "detail": str}

    Use this BEFORE generate to identify problematic segments. The function does
    NOT modify the text — use prepare_text_for_tts() for auto-fixes.
    """
    issues: list[dict] = []

    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue

        # 1. Leading period in dialogue (clean_for_audiobook_tts should fix, but verify)
        if re.match(r'^"\.\s', stripped):
            issues.append({
                "type": "LEADING_DOT_DIALOGUE",
                "severity": "warn",
                "line": lineno,
                "text": stripped[:80],
                "detail": 'Dialogue starts with ". " — TTS generates "ờ" artifact',
            })

        # 2. Hyphenated reduplication — TTS reads hyphen as pause
        hyph = re.findall(r"[A-Za-zÀ-ỹĐđ]+-[A-Za-zÀ-ỹĐđ]+", stripped)
        if hyph:
            issues.append({
                "type": "HYPHEN_IN_WORD",
                "severity": "info",
                "line": lineno,
                "text": stripped[:80],
                "detail": f"Hyphenated words → choppy delivery: {hyph[:3]}",
            })

        # 3. Short repeated syllable with comma — TTS creates awkward pause
        m = re.search(r"\b([A-ZÀ-ỸĐa-zà-ỹđ]{1,6}),\s+\1\b", stripped)
        if m:
            issues.append({
                "type": "REPEATED_SYLLABLE_COMMA",
                "severity": "info",
                "line": lineno,
                "text": stripped[:80],
                "detail": f'Comma between repeated syllable "{m.group(0)}" → robotic pause',
            })

        # 4. Unexpanded numbers — TTS reads in English
        digits = _DIGIT_RE.findall(stripped)
        if digits:
            issues.append({
                "type": "UNEXPANDED_NUMBER",
                "severity": "warn",
                "line": lineno,
                "text": stripped[:80],
                "detail": f"Plain number(s) not expanded: {digits[:5]}",
            })

        # 5. Non-onomatopoeia English words
        en_words = [
            w for w in _EN_WORD_RE.findall(stripped)
            if w not in _VI_ENGLISH_LOOKALIKE and not w.isupper()
        ]
        if en_words:
            issues.append({
                "type": "ENGLISH_WORD",
                "severity": "info",
                "line": lineno,
                "text": stripped[:80],
                "detail": f"English words TTS may mispronounce: {en_words[:5]}",
            })

        # 6. Very long lines (>250 chars) — likely to create >17 word units
        if len(stripped) > 250:
            wc = len(stripped.split())
            issues.append({
                "type": "LONG_LINE",
                "severity": "info",
                "line": lineno,
                "text": stripped[:80] + "…",
                "detail": f"Line {len(stripped)} chars / ~{wc} words — may cause EOS miss",
            })

    return issues
