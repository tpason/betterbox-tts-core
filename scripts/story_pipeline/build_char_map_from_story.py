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
from genre_prompts import find_char_map_file, genre_header_line

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
    text_source: str = "polished",
) -> list[dict[str, Any]]:
    text_source = text_source if text_source in {"raw", "translated", "polished"} else "polished"
    query = """
        SELECT
            c.id AS chapter_id,
            c.chapter_number,
            c.raw_text_content,
            c.raw_text_path,
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

    if text_source == "raw":
        query += " AND (c.raw_text_content IS NOT NULL OR c.raw_text_path IS NOT NULL)"
    elif text_source == "translated":
        query += " AND (c.translated_text_content IS NOT NULL OR c.translated_text_path IS NOT NULL)"
    else:
        query += " AND (c.polished_text_content IS NOT NULL OR c.polished_text_path IS NOT NULL)"

    query += " ORDER BY c.chapter_number"

    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]


def get_chapter_text(row: dict[str, Any], text_source: str = "polished") -> str:
    text_source = text_source if text_source in {"raw", "translated", "polished"} else "polished"
    if text_source == "raw":
        content = row.get("raw_text_content") or ""
        path_str = row.get("raw_text_path") or ""
    elif text_source == "translated":
        content = row.get("translated_text_content") or ""
        path_str = row.get("translated_text_path") or ""
    else:
        content = row.get("polished_text_content") or ""
        path_str = row.get("polished_text_path") or ""

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

# Tên riêng Latin/Vietnamese: 1-4 từ viết hoa. Heuristic này vẫn được LLM lọc lại ở pass 2.
_PROPER_NAME_RE = re.compile(
    r"\b([A-ZÀ-ỴĐ][a-zA-ZÀ-ỹĐđ'’-]{2,}(?:\s+[A-ZÀ-ỴĐ][a-zA-ZÀ-ỹĐđ'’-]{1,}){0,3})\b",
    re.UNICODE,
)

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
    text_source: str = "polished",
    min_frequency: int = DEFAULT_MIN_FREQUENCY,
) -> dict[str, CandidateInfo]:
    candidates: dict[str, CandidateInfo] = {}

    print(f"[PASS1] Scanning {len(rows)} chapters locally...")
    for row in rows:
        ch_num = int(row["chapter_number"])
        text = get_chapter_text(row, text_source=text_source)
        if not text or len(text) < 100:
            continue

        names_found: set[str] = set()

        for m in _PROPER_NAME_RE.finditer(text):
            name = m.group(1)
            if name not in _EXCLUDE_WORDS and name.split()[0] not in _EXCLUDE_WORDS and len(name) >= 3:
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
    "addressing_others": "cách nhân vật gọi người khác theo nhóm: đồng đội/cấp trên/cấp dưới/dân thường/kẻ thù/trẻ nhỏ",
    "relative_status": "tuổi/vị thế/quyền lực tương đối nếu suy ra được",
    "relationships": ["cặp quan hệ cụ thể có bằng chứng, ví dụ: A -> B: bạn/cấp dưới/thù địch, xưng tôi/cậu hoặc mày/tên kia"],
    "addressing_by_target": ["A -> B: xưng hô cụ thể nếu thấy hoặc suy ra chắc từ context"],
    "forbidden_pronouns": ["xưng hô không nên dùng cho nhân vật này nếu có bằng chứng"],
    "title_terms": ["danh hiệu/chức vụ/biệt danh quan trọng và cách dịch nên giữ"],
    "personality": "2-4 từ mô tả tính cách nổi bật",
    "speech_style": "1-2 câu mô tả cách nói đặc trưng",
    "role": "nhân vật chính / đồng đội / phản diện / phụ",
    "first_seen_chapter": {số_chapter_đầu_tiên},
    "last_seen_chapter": {số_chapter_cuối_cùng}
  }}
]

Nếu không có nhân vật nào trong danh sách → trả về []

Ưu tiên thông tin giúp dịch/polish tiếng Việt tự nhiên:
- Ai lớn/nhỏ tuổi hơn, cấp trên/cấp dưới, vua/tướng/lính/dân thường.
- Nhân vật A gọi nhân vật B thế nào ở riêng tư và công khai.
- Kẻ thù/người lạ không được dùng "bạn/anh/cậu" lịch sự nếu cảnh đang thù địch.
- Không tự ý cổ phong hóa nếu bối cảnh không phải tiên hiệp/cổ trang.

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
        elif lower.startswith("cách gọi người khác:") or lower.startswith("gọi người khác:"):
            current["addressing_others"] = value
        elif lower.startswith("tuổi/vị thế:") or lower.startswith("vị thế:"):
            current["relative_status"] = value
        elif lower.startswith("quan hệ:"):
            current.setdefault("relationships", []).append(value)
        elif lower.startswith("xưng hô theo đối tượng:"):
            current.setdefault("addressing_by_target", []).append(value)
        elif lower.startswith("xưng hô cấm:"):
            current.setdefault("forbidden_pronouns", []).append(value)
        elif lower.startswith("danh hiệu/chức vụ:"):
            current.setdefault("title_terms", []).append(value)
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
            for field in (
                "gender",
                "pronoun_3rd",
                "self_address",
                "addressing_others",
                "relative_status",
                "personality",
                "speech_style",
                "role",
            ):
                if not ex.get(field) and char.get(field):
                    result[matched_key][field] = char[field]
            for field in ("relationships", "addressing_by_target", "forbidden_pronouns", "title_terms"):
                old_items = ex.get(field) or []
                if isinstance(old_items, str):
                    old_items = [old_items]
                new_items = char.get(field) or []
                if isinstance(new_items, str):
                    new_items = [new_items]
                merged_items: list[str] = []
                seen_items: set[str] = set()
                for item in [*old_items, *new_items]:
                    text = str(item).strip()
                    key_item = text.casefold()
                    if text and key_item not in seen_items:
                        seen_items.add(key_item)
                        merged_items.append(text)
                if merged_items:
                    result[matched_key][field] = merged_items
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


def _text_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def _find_existing_char_key(existing: dict[str, dict[str, Any]], char: dict[str, Any]) -> str:
    name = (char.get("name") or "").strip()
    if not name:
        return ""
    key = _normalize(name)
    aliases_new = {_normalize(a) for a in (char.get("aliases") or [])}
    all_new = {key, *aliases_new}
    for ex_key, ex_char in existing.items():
        all_ex = {ex_key, *(_normalize(a) for a in (ex_char.get("aliases") or []))}
        if all_ex & all_new:
            return ex_key
    return ""


def _collect_append_updates(
    existing: dict[str, dict[str, Any]],
    new_chars: list[dict[str, Any]],
) -> list[tuple[str, list[str]]]:
    updates: list[tuple[str, list[str]]] = []
    scalar_fields = (
        ("addressing_others", "Cập nhật cách gọi người khác"),
        ("relative_status", "Cập nhật tuổi/vị thế"),
    )
    list_fields = (
        ("relationships", "Quan hệ"),
        ("addressing_by_target", "Xưng hô theo đối tượng"),
        ("forbidden_pronouns", "Xưng hô cấm"),
        ("title_terms", "Danh hiệu/chức vụ"),
    )

    for char in new_chars:
        ex_key = _find_existing_char_key(existing, char)
        if not ex_key:
            continue
        ex = existing[ex_key]
        lines: list[str] = []
        for field, label in scalar_fields:
            new_text = str(char.get(field) or "").strip()
            old_text = str(ex.get(field) or "").strip()
            if new_text and new_text.casefold() != old_text.casefold():
                lines.append(f"- {label}: {new_text}")
        for field, label in list_fields:
            old_items = {item.casefold() for item in _text_list(ex.get(field))}
            for item in _text_list(char.get(field)):
                if item.casefold() not in old_items:
                    lines.append(f"- {label}: {item}")
        if lines:
            updates.append((ex.get("name") or char.get("name") or ex_key.title(), lines))
    return updates


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
    addressing = char.get("addressing_others", "")
    if addressing:
        lines.append(f"- Cách gọi người khác: {addressing}")
    status = char.get("relative_status", "")
    if status:
        lines.append(f"- Tuổi/vị thế: {status}")
    for relationship in char.get("relationships") or []:
        relationship = str(relationship).strip()
        if relationship:
            lines.append(f"- Quan hệ: {relationship}")
    for rule in char.get("addressing_by_target") or []:
        rule = str(rule).strip()
        if rule:
            lines.append(f"- Xưng hô theo đối tượng: {rule}")
    for rule in char.get("forbidden_pronouns") or []:
        rule = str(rule).strip()
        if rule:
            lines.append(f"- Xưng hô cấm: {rule}")
    for term in char.get("title_terms") or []:
        term = str(term).strip()
        if term:
            lines.append(f"- Danh hiệu/chức vụ: {term}")
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
    genre: str = "",
) -> str:
    now = datetime.now().strftime("%Y-%m-%d")
    lines: list[str] = [
        f"## Truyện: {story_title} ({story_id})",
        f"## Cập nhật: {now} từ chapters {chapter_range}",
        "## Auto-generated bởi build_char_map_from_story.py — chỉnh sửa thủ công để tinh chỉnh",
    ]
    # Preserve existing genre header; fall back to injecting from genre arg
    _existing_genre_match = re.search(r"^(##\s*Thể loại[^\n]*)", existing_content or "", re.MULTILINE)
    if _existing_genre_match:
        lines.append(_existing_genre_match.group(1))
    elif genre:
        _genre_line = genre_header_line(genre)
        if _genre_line:
            lines.append(_genre_line)
    lines.append("")

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
    existing_chars: dict[str, dict[str, Any]],
    chapter_range: str,
) -> str:
    updates = _collect_append_updates(existing_chars, new_chars)
    truly_new = [
        char for char in new_chars
        if not _find_existing_char_key(existing_chars, char)
    ]
    if not truly_new and not updates:
        return existing_content.rstrip() + "\n"
    now = datetime.now().strftime("%Y-%m-%d")
    lines: list[str] = [
        existing_content.rstrip(),
    ]
    if truly_new:
        lines += [
            "",
            f"## Auto Update: nhân vật mới (chapters {chapter_range}) — {now}",
            "",
        ]
        for char in truly_new:
            lines.extend(_render_char_entry(char))
            lines.append("")
    if updates:
        lines += [
            "",
            f"## Auto Update: quan hệ/xưng hô (chapters {chapter_range}) — {now}",
            "",
        ]
        for name, update_lines in updates:
            lines.append(f"### {name}")
            lines.extend(update_lines)
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


def update_char_map_metadata(
    story_id: str,
    content: str,
    chapter_nums: list[int],
    text_source: str,
) -> None:
    if not chapter_nums:
        return
    try:
        metadata: dict[str, Any] = {
            "char_map_updated_to_chapter": chapter_nums[-1],
            "char_map_scanned_from_chapter": chapter_nums[0],
            "char_map_scanned_to_chapter": chapter_nums[-1],
            "char_map_scanned_chapters": len(chapter_nums),
            "char_map_text_source": text_source,
            "char_map_updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        if content:
            metadata["char_map_content"] = content
        repo.update_story_metadata(story_id, metadata)
    except Exception as exc:
        print(f"[DB WARN] Không cập nhật metadata: {exc}")


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
        help="Dùng translated text thay vì polished text. Tương đương --text-source translated.",
    )
    parser.add_argument(
        "--text-source",
        choices=("raw", "translated", "polished"),
        default="polished",
        help="Nguồn text để build char-map. Auto-worker dùng raw cho truyện Việt, translated cho truyện dịch.",
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
    parser.add_argument(
        "--genre",
        default="",
        help=(
            "Thể loại để inject vào header char_map khi tạo mới: "
            "western_fantasy, tien_hiep, huyen_huyen, he_thong, kiem_hiep, "
            "do_thi, xuyen_khong, mat_the, vong_du, lang_man. "
            "Bỏ trống nếu file cũ đã có dòng Thể loại."
        ),
    )
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
    parser.add_argument("--validate", action="store_true",
                        help="Chỉ validate char map hiện có (alias/pronoun/genre conflicts) rồi thoát.")

    args = parser.parse_args()

    if not args.story_title and not args.story_id:
        parser.error("Cần --story-title hoặc --story-id")

    text_source = "translated" if args.use_translated else args.text_source

    print("[QUERY] Lấy chapters từ DB...")
    rows = fetch_chapters(
        story_title=args.story_title,
        story_id=args.story_id,
        from_chapter=args.from_chapter,
        to_chapter=args.to_chapter,
        text_source=text_source,
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
        f"({chapter_range}), slug={slug}, text_source={text_source}"
    )

    # Load existing char map from DB
    existing_content = ""
    existing_chars: dict[str, dict[str, Any]] = {}
    if args.output_file and Path(args.output_file).exists():
        existing_content = Path(args.output_file).read_text(encoding="utf-8")
        print(f"[EXIST] --output-file {args.output_file} — {len(parse_existing_char_map(existing_content))} nhân vật đã có")
    else:
        try:
            story_meta = (repo.get_story_by_id(story_id) or {}).get("metadata") or {}
            existing_content = story_meta.get("char_map_content") or ""
        except Exception as exc:
            print(f"[WARN] Không đọc được char_map_content từ DB: {exc}")
        if existing_content:
            print(f"[EXIST] char map in DB — {len(parse_existing_char_map(existing_content))} nhân vật đã có")
    if existing_content:
        existing_chars = parse_existing_char_map(existing_content)

    if args.validate:
        from genre_prompts import validate_char_map
        if not existing_content:
            print(f"[VALIDATE] Không có char map trong DB story_id={story_id}")
            return
        issues = validate_char_map(existing_content)
        if not issues:
            print(f"[VALIDATE] OK — không phát hiện issue nào (story_id={story_id})")
        else:
            print(f"[VALIDATE] {len(issues)} issue(s) story_id={story_id}:")
            for issue in issues:
                print(f"  - {issue}")
        return

    # ── Pass 1 ────────────────────────────────────────────────────────────────
    candidates = pass1_scan(rows, text_source=text_source, min_frequency=args.min_frequency)

    if args.pass1_only or args.dry_run:
        print(f"\n[PASS1 TOP CANDIDATES]")
        for name, info in sorted(candidates.items(), key=lambda x: x[1].count, reverse=True)[:60]:
            in_map = _normalize(name) in {_normalize(k) for k in existing_chars}
            flag = " ✓" if in_map else " ★NEW"
            print(f"  {name}: {info.count}x, ch{info.first_ch}-{info.last_ch}{flag}")
        return

    if not candidates:
        print("[WARN] Không tìm được candidate nào.")
        if existing_content:
            update_char_map_metadata(story_id, existing_content, chapter_nums, text_source)
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
        if existing_content:
            update_char_map_metadata(story_id, existing_content, chapter_nums, text_source)
        return

    # Format & write
    if args.append_only and existing_content:
        output_content = format_append_new_chars(existing_content, new_chars, existing_chars, chapter_range)
        appended_new = len([char for char in new_chars if not _find_existing_char_key(existing_chars, char)])
        appended_updates = len(_collect_append_updates(existing_chars, new_chars))
        print(f"[APPEND] new_chars={appended_new} relationship_updates={appended_updates}")
    else:
        merged, _ = _merge_new_into_existing(existing_chars, new_chars)
        output_content = format_full_char_map(
            all_chars=merged,
            story_title=story_title,
            story_id=story_id,
            chapter_range=chapter_range,
            existing_content=existing_content,
            genre=getattr(args, "genre", ""),
        )

    if args.output_file:
        out_path = Path(args.output_file)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output_content, encoding="utf-8")
        print(f"[SAVED] {out_path}")

    update_char_map_metadata(story_id, output_content, chapter_nums, text_source)

    print(f"[SAVED] char_map to DB story_id={story_id}")
    print(f"\nDùng lệnh tiếp theo:")
    print(f'  python scripts/story_pipeline/repolish_story_from_db.py \\')
    print(f'    --story-title "{story_title}" --overwrite')


if __name__ == "__main__":
    main()
