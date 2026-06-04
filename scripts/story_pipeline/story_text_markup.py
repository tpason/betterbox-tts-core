from __future__ import annotations

import re
import unicodedata


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
    return str(value)


def _replace_percent(match: re.Match[str]) -> str:
    value = int(match.group(1))
    return f"{_number_to_vietnamese(value)} phần trăm"


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
