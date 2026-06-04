#!/usr/bin/env python3
"""
Two-pass character map builder — nhanh hơn extract_char_map.py cho story dài.

Pass 1 (local, không cần LLM):
  - Đọc toàn bộ chapter text từ DB.
  - Dùng regex/heuristics tìm candidate proper names.
  - Track: số lần xuất hiện, first_seen_chapter, last_seen_chapter, context snippets.
  - Filter theo min-frequency.

Pass 2 (LLM, batches ~20 candidates mỗi lần):
  - Gửi compact candidate list + evidence snippets đến Ollama.
  - Nhận diện đây có phải tên nhân vật không, và extract thông tin nhân vật.
  - Merge vào char map hiện có mà không xóa manual edits.

So với extract_char_map.py (gọi LLM mỗi chapter):
  - Story 1000 chapters → Pass 1 đọc tất cả local, Pass 2 chỉ ~10-15 LLM calls.
  - Evidence từ toàn bộ story, không phụ thuộc vào sampling may rủi.

Use cases:
  # Build char map lần đầu từ toàn bộ story
  python scripts/story_pipeline/build_char_map_from_story.py \\
    --story-title "Vĩnh Thoái Hiệp Sĩ"

  # Scan chapter 500-600 để tìm nhân vật mới chưa có trong map
  python scripts/story_pipeline/build_char_map_from_story.py \\
    --story-title "Vĩnh Thoái Hiệp Sĩ" \\
    --from-chapter 500 --to-chapter 600 --append-only

  # Chỉ chạy Pass 1 để xem candidates (không gọi LLM)
  python scripts/story_pipeline/build_char_map_from_story.py \\
    --story-title "Vĩnh Thoái Hiệp Sĩ" --pass1-only
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from story_db.story_pipeline_db import repository as repo
from story_db.story_pipeline_db.db import connect
from genre_prompts import find_char_map_file

# ── Constants ──────────────────────────────────────────────────────────────────

DEFAULT_MIN_FREQUENCY = 3
DEFAULT_BATCH_SIZE = 20
CONTEXT_WINDOW = 250
MAX_SNIPPETS_PER_CANDIDATE = 3

# Từ thường xuất hiện viết hoa nhưng KHÔNG phải tên nhân vật
_EXCLUDE_WORDS: frozenset[str] = frozenset({
    # Vietnamese common words that can be capitalized
    "Không", "Được", "Này", "Đây", "Khi", "Nếu", "Và", "Nhưng", "Vì", "Cũng",
    "Rồi", "Thì", "Còn", "Với", "Cho", "Từ", "Theo", "Trên", "Dưới", "Sau",
    "Trước", "Trong", "Ngoài", "Đã", "Sẽ", "Đang", "Vẫn", "Chỉ", "Mới",
    "Lại", "Ra", "Vào", "Lên", "Xuống", "Đi", "Đến", "Về", "Một", "Hai",
    "Người", "Hắn", "Cô", "Anh", "Chị", "Họ", "Nàng", "Gã", "Lão",
    "Ngài", "Ngươi", "Bổn", "Tiểu", "Đại", "Sư",
    # Common English words appearing capitalized
    "The", "And", "But", "For", "With", "That", "This", "From", "Into",
    "After", "Before", "During", "While", "Since", "When", "Where",
    "What", "Who", "How", "Not", "Can", "Will", "Just", "Even",
    "Chapter", "Arc", "Part", "Book", "Volume", "Side", "Story",
    "Yes", "No", "Well", "Now", "Then", "Here", "There",
    # Role/title words
    "Knight", "Guard", "Captain", "Lord", "Lady", "King", "Queen",
    "Rank", "Class", "Level", "Grade", "Order", "Guild",
})


# ── DB helpers ─────────────────────────────────────────────────────────────────

def fetch_chapters(
    story_title: str = "",
    story_id: str = "",
    from_chapter: int = 0,
    to_chapter: int = 0,
    use_polished: bool = True,
) -> list[dict[str, Any]]:
    query = """
        SELECT
            c.id AS chapter_id,
            c.chapter_number,
            c.polished_text_content,
            c.polished_text_path,
            c.translated_text_content,
            c.translated_text_path,
            s.id AS story_id,
            s.title AS story_title,
            s.metadata AS story_metadata,
            s.source_url AS story_url
        FROM chapters c
        JOIN stories s ON s.id = c.story_id
        WHERE s.is_active = TRUE
          AND c.is_downloaded = TRUE
    """
    params: dict[str, Any] = {}

    if story_id:
        query += " AND s.id = %(story_id)s"
        params["story_id"] = story_id
    if story_title:
        query += " AND (s.title ILIKE %(story_title)s OR s.display_title ILIKE %(story_title)s)"
        params["story_title"] = f"%{story_title}%"
    if from_chapter:
        query += " AND c.chapter_number >= %(from_chapter)s"
        params["from_chapter"] = from_chapter
    if to_chapter:
        query += " AND c.chapter_number <= %(to_chapter)s"
        params["to_chapter"] = to_chapter

    if use_polished:
        query += " AND (c.polished_text_content IS NOT NULL OR c.polished_text_path IS NOT NULL)"
    else:
        query += " AND (c.translated_text_content IS NOT NULL OR c.translated_text_path IS NOT NULL)"

    query += " ORDER BY c.chapter_number"

    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]


def get_chapter_text(row: dict[str, Any], use_polished: bool = True) -> str:
    if use_polished:
        content = row.get("polished_text_content") or ""
        path_str = row.get("polished_text_path") or ""
    else:
        content = row.get("translated_text_content") or ""
        path_str = row.get("translated_text_path") or ""

    if content and len(content) > 100:
        return content

    if path_str:
        p = Path(path_str)
        if not p.is_absolute():
            p = ROOT / p
        if p.exists():
            return p.read_text(encoding="utf-8")
    return ""


def story_slug_from_row(row: dict[str, Any]) -> str:
    from urllib.parse import urlparse
    metadata = row.get("story_metadata") or {}
    if isinstance(metadata, dict) and metadata.get("slug"):
        slug = str(metadata["slug"])
    else:
        parsed = urlparse(str(row.get("story_url") or ""))
        slug = parsed.path.rstrip("/").rsplit("/", 1)[-1] or str(row.get("story_title") or "story")
    slug = re.sub(r"\s+", "-", slug.strip().lower())
    slug = re.sub(r"[^a-z0-9À-ỹ-]+", "", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug or "story"


# ── Pass 1: Local name extraction ──────────────────────────────────────────────

# Tên phương Tây: từ bắt đầu bằng chữ hoa, 3+ ký tự, chỉ chứa chữ cái
_WESTERN_NAME_RE = re.compile(r'\b([A-Z][a-zA-Z]{2,})\b')

# Tên xuất hiện sau dấu hiệu dialogue attribution (tiếng Việt)
_DIALOGUE_SPEAKER_RE = re.compile(
    r'(?:"[^"]{3,80}[.!?,]"\s*[,—–-]\s*([A-Z][a-zA-Z]{2,})\s+(?:nói|hỏi|đáp|la|thì thầm|cười|quát))'
    r'|(?:\b([A-Z][a-zA-Z]{2,})\s+(?:nói|hỏi|đáp|la|thì thầm|cười|quát)\s*[":,])',
    re.UNICODE,
)


class CandidateInfo:
    __slots__ = ("count", "first_ch", "last_ch", "contexts")

    def __init__(self) -> None:
        self.count = 0
        self.first_ch: int = 999_999
        self.last_ch: int = 0
        self.contexts: list[str] = []


def _get_context_snippet(text: str, name: str) -> str:
    pos = text.find(name)
    if pos == -1:
        return ""
    start = max(0, pos - CONTEXT_WINDOW)
    end = min(len(text), pos + len(name) + CONTEXT_WINDOW)
    snippet = text[start:end].strip()
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return f"{prefix}{snippet}{suffix}"


def pass1_scan(
    rows: list[dict[str, Any]],
    use_polished: bool = True,
    min_frequency: int = DEFAULT_MIN_FREQUENCY,
) -> dict[str, CandidateInfo]:
    candidates: dict[str, CandidateInfo] = {}

    print(f"[PASS1] Scanning {len(rows)} chapters locally...")
    for row in rows:
        ch_num = int(row["chapter_number"])
        text = get_chapter_text(row, use_polished=use_polished)
        if not text or len(text) < 100:
            continue

        names_found: set[str] = set()

        for m in _WESTERN_NAME_RE.finditer(text):
            name = m.group(1)
            if name not in _EXCLUDE_WORDS and len(name) >= 3:
                names_found.add(name)

        for m in _DIALOGUE_SPEAKER_RE.finditer(text):
            name = m.group(1) or m.group(2) or ""
            if name and name not in _EXCLUDE_WORDS and len(name) >= 3:
                names_found.add(name)

        for name in names_found:
            if name not in candidates:
                candidates[name] = CandidateInfo()
            info = candidates[name]
            info.count += text.count(name)
            info.first_ch = min(info.first_ch, ch_num)
            info.last_ch = max(info.last_ch, ch_num)
            if len(info.contexts) < MAX_SNIPPETS_PER_CANDIDATE:
                snippet = _get_context_snippet(text, name)
                if snippet and snippet not in info.contexts:
                    info.contexts.append(snippet)

    filtered = {
        name: info
        for name, info in candidates.items()
        if info.count >= min_frequency
    }
    print(
        f"[PASS1] {len(candidates)} raw candidates, "
        f"{len(filtered)} with freq >= {min_frequency}"
    )
    return filtered


# ── Pass 2: LLM batch summarization ───────────────────────────────────────────

_BATCH_SYSTEM = """\
Bạn là chuyên gia phân tích nhân vật trong truyện dịch tiếng Việt.
Nhiệm vụ: xem danh sách ứng viên + context snippets, xác định đây có phải tên nhân vật không và phân tích thông tin.
Chỉ trả về JSON hợp lệ, không giải thích, không markdown.\
"""

_BATCH_USER = """\
Truyện: {story_title}

Dưới đây là danh sách từ ngữ được trích xuất từ truyện + context snippets.
Với mỗi mục: xác định có phải tên nhân vật không. Nếu không phải (ví dụ: tên địa điểm, tên vật phẩm, từ thông thường) thì bỏ qua.

Trả về JSON array chỉ gồm nhân vật thực sự:
[
  {{
    "name": "tên chính xác nhất trong truyện",
    "aliases": ["biến thể tên khác nếu có"],
    "gender": "nam" | "nữ" | "không rõ",
    "pronoun_3rd": "đại từ ngôi 3 phù hợp (anh ta / cô ta / hắn / nàng / cậu ta / ...)",
    "self_address": "cách tự xưng trong lời thoại (tôi / ta / tôi / mình / ...)",
    "personality": "2-4 từ mô tả tính cách nổi bật",
    "speech_style": "1-2 câu mô tả cách nói đặc trưng",
    "role": "nhân vật chính / đồng đội / phản diện / phụ",
    "first_seen_chapter": {số_chapter_đầu_tiên},
    "last_seen_chapter": {số_chapter_cuối_cùng}
  }}
]

Nếu không có nhân vật nào trong danh sách → trả về []

---
Danh sách ứng viên:
{candidates_block}\
"""


def _format_candidates_block(batch: list[tuple[str, CandidateInfo]]) -> str:
    lines: list[str] = []
    for name, info in batch:
        lines.append(
            f"### {name}  (xuất hiện {info.count} lần, "
            f"chapter {info.first_ch}–{info.last_ch})"
        )
        for snippet in info.contexts[:2]:
            lines.append(f"  > {snippet[:280]}")
        lines.append("")
    return "\n".join(lines)


def _call_ollama_batch(
    base_url: str,
    model: str,
    story_title: str,
    batch: list[tuple[str, CandidateInfo]],
    temperature: float,
    num_ctx: int,
    timeout: int,
    session: requests.Session,
) -> list[dict[str, Any]]:
    candidates_block = _format_candidates_block(batch)
    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": _BATCH_SYSTEM},
            {"role": "user", "content": _BATCH_USER.format(
                story_title=story_title,
                candidates_block=candidates_block,
                # placeholders cho first/last — Ollama sẽ điền từ context
                số_chapter_đầu_tiên="số chapter đầu tiên thấy",
                số_chapter_cuối_cùng="số chapter cuối cùng thấy",
            )},
        ],
        "options": {"temperature": temperature, "num_ctx": num_ctx},
        "keep_alive": "10m",
    }
    resp = session.post(base_url.rstrip("/") + "/api/chat", json=payload, timeout=timeout)
    resp.raise_for_status()
    content = resp.json().get("message", {}).get("content", "")
    content = re.sub(r"<think>.*?</think>\s*", "", content, flags=re.DOTALL | re.IGNORECASE)
    content = re.sub(r"^```(?:json)?\s*", "", content.strip(), flags=re.IGNORECASE)
    content = re.sub(r"\s*```$", "", content)
    try:
        result = json.loads(content)
        return result if isinstance(result, list) else []
    except json.JSONDecodeError:
        m = re.search(r"\[[\s\S]+\]", content)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                pass
    return []


def pass2_llm(
    candidates: dict[str, CandidateInfo],
    story_title: str,
    base_url: str,
    model: str,
    temperature: float,
    num_ctx: int,
    timeout: int,
    batch_size: int,
) -> list[dict[str, Any]]:
    # Sort by frequency desc — most frequent = most important characters first
    sorted_cands = sorted(candidates.items(), key=lambda x: x[1].count, reverse=True)
    total_batches = (len(sorted_cands) + batch_size - 1) // batch_size

    print(
        f"[PASS2] {len(sorted_cands)} candidates → "
        f"{total_batches} LLM batch(es) of {batch_size}"
    )

    all_chars: list[dict[str, Any]] = []
    with requests.Session() as session:
        for batch_idx in range(total_batches):
            batch = sorted_cands[batch_idx * batch_size:(batch_idx + 1) * batch_size]
            names_in_batch = [b[0] for b in batch]
            print(
                f"  [batch {batch_idx+1}/{total_batches}] "
                f"{len(batch)} candidates: {', '.join(names_in_batch[:6])}"
                f"{'...' if len(batch) > 6 else ''}",
                end=" ",
                flush=True,
            )
            try:
                chars = _call_ollama_batch(
                    base_url=base_url,
                    model=model,
                    story_title=story_title,
                    batch=batch,
                    temperature=temperature,
                    num_ctx=num_ctx,
                    timeout=timeout,
                    session=session,
                )
                # Merge Pass 1 chapter tracking into LLM results
                for char in chars:
                    name = (char.get("name") or "").strip()
                    for cand_name, info in batch:
                        if cand_name.lower() == name.lower():
                            char.setdefault("first_seen_chapter", info.first_ch)
                            char.setdefault("last_seen_chapter", info.last_ch)
                            break
                all_chars.extend(chars)
                print(f"→ {len(chars)} character(s) identified")
            except Exception as exc:
                print(f"FAIL: {exc}")
                time.sleep(2)

    return all_chars


# ── Merge & format ─────────────────────────────────────────────────────────────

def _normalize(name: str) -> str:
    return name.strip().lower()


def _extract_section(content: str, header_pattern: str) -> str:
    """Extract text under a ## section matching pattern, until next ## section."""
    lines = content.splitlines()
    in_section = False
    result: list[str] = []
    header_re = re.compile(header_pattern, re.IGNORECASE)
    for line in lines:
        stripped = line.strip()
        if re.match(r"^##\s+", stripped):
            if header_re.search(stripped):
                in_section = True
                result.append(line)
                continue
            elif in_section:
                break
        if in_section:
            result.append(line)
    return "\n".join(result).strip()


def _extract_alias_block(content: str) -> str:
    lines = content.splitlines()
    in_section = False
    result: list[str] = []
    for line in lines:
        if line.strip().lower() == "[aliases]":
            in_section = True
            continue
        if in_section:
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                break
            result.append(line)
    return "\n".join(result).strip()


def parse_existing_char_map(content: str) -> dict[str, dict[str, Any]]:
    chars: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    current_key = ""

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if line.startswith("### "):
            name = line[4:].strip()
            if not name:
                current = None
                continue
            current_key = _normalize(name)
            current = chars.setdefault(current_key, {"name": name})
            continue
        if not current or not line.startswith("- "):
            continue
        body = line[2:].strip()
        lower = body.lower()
        value = body.split(":", 1)[-1].strip() if ":" in body else ""
        if lower.startswith("tên khác:"):
            current["aliases"] = [x.strip() for x in value.split(",") if x.strip()]
        elif lower.startswith("giới tính:"):
            current["gender"] = value
        elif lower.startswith("ngôi thứ ba:"):
            current["pronoun_3rd"] = value
        elif lower.startswith("tự xưng:"):
            current["self_address"] = value
        elif lower.startswith("tính cách:"):
            current["personality"] = value
        elif lower.startswith(("giọng nói:", "giọng thoại:")):
            current["speech_style"] = value
        elif lower.startswith("vai trò:"):
            current["role"] = value
        elif lower.startswith("lần đầu xuất hiện:"):
            try:
                current["first_seen_chapter"] = int(re.search(r"\d+", value).group())  # type: ignore[union-attr]
            except Exception:
                pass
        elif lower.startswith("lần cuối xuất hiện:"):
            try:
                current["last_seen_chapter"] = int(re.search(r"\d+", value).group())  # type: ignore[union-attr]
            except Exception:
                pass
        elif lower.startswith("ghi chú:"):
            current.setdefault("notes", []).append(value)
        elif lower.startswith("tránh:"):
            current.setdefault("avoid_notes", []).append(value)

    return chars


def _merge_new_into_existing(
    existing: dict[str, dict[str, Any]],
    new_chars: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Returns (updated_existing, truly_new_chars)."""
    result = dict(existing)
    truly_new: list[dict[str, Any]] = []

    for char in new_chars:
        name = (char.get("name") or "").strip()
        if not name:
            continue
        key = _normalize(name)
        aliases_new = [_normalize(a) for a in (char.get("aliases") or [])]

        matched_key: str | None = None
        for ex_key, ex_char in result.items():
            ex_aliases = [_normalize(a) for a in (ex_char.get("aliases") or [])]
            all_ex = {ex_key} | set(ex_aliases)
            all_new = {key} | set(aliases_new)
            if all_ex & all_new:
                matched_key = ex_key
                break

        if matched_key:
            ex = result[matched_key]
            # Merge aliases
            merged_al = list({
                *[_normalize(a) for a in (ex.get("aliases") or [])],
                *aliases_new,
                *([key] if key != matched_key else []),
            } - {matched_key})
            if merged_al:
                result[matched_key]["aliases"] = [a.title() for a in merged_al]
            # Fill missing fields only (don't overwrite manual entries)
            for field in ("gender", "pronoun_3rd", "self_address", "personality", "speech_style", "role"):
                if not ex.get(field) and char.get(field):
                    result[matched_key][field] = char[field]
            # Update chapter tracking
            for field in ("first_seen_chapter", "last_seen_chapter"):
                new_val = char.get(field)
                if new_val:
                    ex_val = ex.get(field)
                    if not ex_val:
                        result[matched_key][field] = new_val
                    elif field == "first_seen_chapter":
                        result[matched_key][field] = min(ex_val, new_val)
                    else:
                        result[matched_key][field] = max(ex_val, new_val)
        else:
            result[key] = {"name": name, **{k: v for k, v in char.items() if k != "name"}}
            truly_new.append(char)

    return result, truly_new


def _render_char_entry(char: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    name = char.get("name", "").strip()
    if not name:
        return lines
    lines.append(f"### {name}")
    aliases = char.get("aliases") or []
    if aliases:
        lines.append(f"- Tên khác: {', '.join(str(a) for a in aliases)}")
    gender = char.get("gender", "")
    if gender:
        lines.append(f"- Giới tính: {gender}")
    pronoun = char.get("pronoun_3rd", "")
    if pronoun:
        lines.append(f"- Ngôi thứ ba: {pronoun}")
    self_addr = char.get("self_address", "")
    if self_addr:
        lines.append(f"- Tự xưng: {self_addr}")
    personality = char.get("personality", "")
    if personality:
        lines.append(f"- Tính cách: {personality}")
    speech = char.get("speech_style", "")
    if speech:
        lines.append(f"- Giọng nói: {speech}")
    role = char.get("role", "")
    if role:
        lines.append(f"- Vai trò: {role}")
    first_ch = char.get("first_seen_chapter")
    last_ch = char.get("last_seen_chapter")
    if first_ch:
        lines.append(f"- Lần đầu xuất hiện: chapter {first_ch}")
    if last_ch:
        lines.append(f"- Lần cuối xuất hiện: chapter {last_ch}")
    for note in char.get("notes") or []:
        lines.append(f"- Ghi chú: {note}")
    for note in char.get("avoid_notes") or []:
        lines.append(f"- Tránh: {note}")
    return lines


def format_full_char_map(
    all_chars: dict[str, dict[str, Any]],
    story_title: str,
    story_id: str,
    chapter_range: str,
    existing_content: str = "",
) -> str:
    now = datetime.now().strftime("%Y-%m-%d")
    lines: list[str] = [
        f"## Truyện: {story_title} ({story_id})",
        f"## Cập nhật: {now} từ chapters {chapter_range}",
        "## Auto-generated bởi build_char_map_from_story.py — chỉnh sửa thủ công để tinh chỉnh",
        "",
    ]

    # Giữ lại ALIASES block từ file cũ
    alias_block = _extract_alias_block(existing_content) if existing_content else ""
    lines += ["[ALIASES]", alias_block if alias_block else "# wrong_name = correct_name", ""]

    lines += ["---", ""]

    def sort_key(item: tuple[str, dict[str, Any]]) -> tuple:
        char = item[1]
        role = (char.get("role") or "").lower()
        is_main = 0 if "chính" in role else (1 if "đồng đội" in role or "ally" in role else 2)
        return (is_main, char.get("first_seen_chapter") or 999, (char.get("name") or "").lower())

    for _key, char in sorted(all_chars.items(), key=sort_key):
        lines.extend(_render_char_entry(char))
        lines.append("")

    # Giữ lại story voice rules section từ file cũ
    voice_section = _extract_section(
        existing_content,
        r"Quy tắc văn phong|Giọng văn|Phong cách|Tone|Voice",
    ) if existing_content else ""
    if voice_section:
        lines += ["---", "", voice_section, ""]

    return "\n".join(lines).rstrip() + "\n"


def format_append_new_chars(
    existing_content: str,
    new_chars: list[dict[str, Any]],
    chapter_range: str,
) -> str:
    if not new_chars:
        return existing_content.rstrip() + "\n"
    now = datetime.now().strftime("%Y-%m-%d")
    lines: list[str] = [
        existing_content.rstrip(),
        "",
        f"## Auto Update: nhân vật mới (chapters {chapter_range}) — {now}",
        "",
    ]
    for char in new_chars:
        lines.extend(_render_char_entry(char))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ── Ollama utilities ───────────────────────────────────────────────────────────

def unload_ollama_model(base_url: str, model: str) -> None:
    try:
        requests.post(
            base_url.rstrip("/") + "/api/generate",
            json={"model": model, "keep_alive": 0},
            timeout=30,
        )
    except Exception:
        pass


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Two-pass character map builder: local scan + targeted LLM batches.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--story-title", default="")
    parser.add_argument("--story-id", default="")
    parser.add_argument("--from-chapter", type=int, default=0)
    parser.add_argument("--to-chapter", type=int, default=0)
    parser.add_argument(
        "--use-translated",
        action="store_true",
        help="Dùng translated text thay vì polished text.",
    )
    parser.add_argument(
        "--append-only",
        action="store_true",
        help="Chỉ thêm nhân vật mới, không ghi đè map hiện có.",
    )
    parser.add_argument(
        "--min-frequency",
        type=int,
        default=DEFAULT_MIN_FREQUENCY,
        help=f"Tần suất tối thiểu để là candidate. Mặc định: {DEFAULT_MIN_FREQUENCY}",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Số candidates gửi Ollama mỗi batch. Mặc định: {DEFAULT_BATCH_SIZE}",
    )
    parser.add_argument("--output-file", default="")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default="qwen3:14b")
    parser.add_argument("--temperature", type=float, default=0.1)
    parser.add_argument("--num-ctx", type=int, default=8192)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--keep-loaded", action="store_true")
    parser.add_argument(
        "--pass1-only",
        action="store_true",
        help="Chỉ chạy Pass 1 (local scan), in candidates, không gọi LLM.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Pass 1 only + in kết quả.")

    args = parser.parse_args()

    if not args.story_title and not args.story_id:
        parser.error("Cần --story-title hoặc --story-id")

    print("[QUERY] Lấy chapters từ DB...")
    rows = fetch_chapters(
        story_title=args.story_title,
        story_id=args.story_id,
        from_chapter=args.from_chapter,
        to_chapter=args.to_chapter,
        use_polished=not args.use_translated,
    )

    if not rows:
        print("[ERROR] Không tìm thấy chapter nào.")
        return

    story_id = str(rows[0]["story_id"])
    story_title = rows[0].get("story_title") or args.story_title
    slug = story_slug_from_row(rows[0])
    chapter_nums = sorted(int(r["chapter_number"]) for r in rows)
    chapter_range = f"{chapter_nums[0]:04d}-{chapter_nums[-1]:04d}"

    print(
        f"[INFO] {story_title} ({story_id}) — {len(rows)} chapters "
        f"({chapter_range}), slug={slug}"
    )

    # Output path
    if args.output_file:
        out_path = Path(args.output_file)
    else:
        out_path = ROOT / "story_data" / "char_maps" / f"{story_id}-{slug}.txt"

    # Load existing char map
    existing_content = ""
    existing_chars: dict[str, dict[str, Any]] = {}
    if out_path.exists():
        existing_content = out_path.read_text(encoding="utf-8")
        existing_chars = parse_existing_char_map(existing_content)
        print(f"[EXIST] {out_path} — {len(existing_chars)} nhân vật đã có")

    # ── Pass 1 ────────────────────────────────────────────────────────────────
    candidates = pass1_scan(rows, use_polished=not args.use_translated, min_frequency=args.min_frequency)

    if args.pass1_only or args.dry_run:
        print(f"\n[PASS1 TOP CANDIDATES]")
        for name, info in sorted(candidates.items(), key=lambda x: x[1].count, reverse=True)[:60]:
            in_map = _normalize(name) in {_normalize(k) for k in existing_chars}
            flag = " ✓" if in_map else " ★NEW"
            print(f"  {name}: {info.count}x, ch{info.first_ch}-{info.last_ch}{flag}")
        return

    if not candidates:
        print("[WARN] Không tìm được candidate nào.")
        return

    # Nếu append_only: bỏ qua names đã có trong map
    if args.append_only and existing_chars:
        existing_name_set: set[str] = set(existing_chars.keys())
        for ch in existing_chars.values():
            existing_name_set.update(_normalize(a) for a in (ch.get("aliases") or []))
        candidates = {k: v for k, v in candidates.items() if _normalize(k) not in existing_name_set}
        print(f"[FILTER] Sau khi loại existing: {len(candidates)} candidates mới cần xử lý")

    if not candidates:
        print("[INFO] Không có candidate mới cần xử lý.")
        return

    # ── Pass 2 ────────────────────────────────────────────────────────────────
    new_chars = pass2_llm(
        candidates=candidates,
        story_title=story_title,
        base_url=args.ollama_url,
        model=args.model,
        temperature=args.temperature,
        num_ctx=args.num_ctx,
        timeout=args.timeout,
        batch_size=args.batch_size,
    )

    if not args.keep_loaded:
        unload_ollama_model(args.ollama_url, args.model)
        print(f"[OLLAMA] unload requested: {args.model}")

    print(f"\n[RESULT] {len(new_chars)} nhân vật từ {len(candidates)} candidates")

    if not new_chars:
        print("[WARN] LLM không xác định được nhân vật nào.")
        return

    # Format & write
    if args.append_only and existing_content:
        _updated, truly_new = _merge_new_into_existing(existing_chars, new_chars)
        output_content = format_append_new_chars(existing_content, truly_new, chapter_range)
        print(f"[APPEND] {len(truly_new)} nhân vật mới thêm vào map")
    else:
        merged, _ = _merge_new_into_existing(existing_chars, new_chars)
        output_content = format_full_char_map(
            all_chars=merged,
            story_title=story_title,
            story_id=story_id,
            chapter_range=chapter_range,
            existing_content=existing_content,
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(output_content, encoding="utf-8")

    try:
        repo.update_story_metadata(
            story_id,
            {
                "char_map_path": out_path.relative_to(ROOT).as_posix(),
                "char_map_updated_to_chapter": chapter_nums[-1],
                "char_map_updated_at": datetime.now().isoformat(timespec="seconds"),
            },
        )
    except Exception as exc:
        print(f"[DB WARN] Không cập nhật metadata: {exc}")

    print(f"[SAVED] {out_path}")
    print(f"\nDùng lệnh tiếp theo:")
    print(f'  python scripts/story_pipeline/repolish_story_from_db.py \\')
    print(f'    --story-title "{story_title}" --overwrite')


if __name__ == "__main__":
    main()
