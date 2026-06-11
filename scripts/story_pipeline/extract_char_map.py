#!/usr/bin/env python3
"""
Tự động trích xuất và cập nhật character map từ các chapter đã polish/dịch.

Dùng Ollama để phân tích text → nhận dạng nhân vật, giới tính, xưng hô, giọng văn.
Kết quả ghi vào story_data/char_maps/{story_id}-{slug}.txt

Use cases:
  # Tạo char map từ 50 chapter đầu
  python scripts/story_pipeline/extract_char_map.py \\
    --story-title "Vĩnh Thoái Hiệp Sĩ" --sample-chapters 50

  # Cập nhật char map với chapter mới (thêm nhân vật mới, không xóa cũ)
  python scripts/story_pipeline/extract_char_map.py \\
    --story-title "Vĩnh Thoái Hiệp Sĩ" --from-chapter 500 --to-chapter 600 --append-only

  # Xem nhân vật tại arc cụ thể, ghi vào section arc mới
  python scripts/story_pipeline/extract_char_map.py \\
    --story-title "Vĩnh Thoái Hiệp Sĩ" --from-chapter 300 --to-chapter 400 --arc-name "Biên Giới"
"""
from __future__ import annotations

import argparse
import json
import re
import socket
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

# ── Prompt cho Ollama ──────────────────────────────────────────────────────────

EXTRACT_SYSTEM = """Bạn là chuyên gia phân tích văn học, chuyên nhận diện nhân vật trong truyện dịch tiếng Việt.
Nhiệm vụ: đọc đoạn truyện và trích xuất thông tin nhân vật chính xác.
Chỉ trả về JSON hợp lệ, không giải thích, không markdown.
Với nhân vật không rõ giới tính, ghi gender: "không rõ"."""

EXTRACT_USER = """Đọc đoạn truyện sau và liệt kê TẤT CẢ nhân vật có tên xuất hiện.

Với mỗi nhân vật trả về JSON object:
{{
  "name": "tên chính xác nhất",
  "aliases": ["biến thể tên khác nếu có"],
  "gender": "nam" | "nữ" | "không rõ",
  "pronoun_3rd": "đại từ ngôi 3 dùng trong văn bản (hắn/anh ta/cô/nàng/...)",
  "self_address": "cách tự xưng trong lời thoại (tôi/ta/mình/...)",
  "addressing_others": "cách nhân vật gọi người khác theo nhóm: đồng đội/cấp trên/người lớn hơn/trẻ nhỏ/kẻ thù/người lạ",
  "relative_status": "tuổi/vị thế/quyền lực tương đối nếu suy ra được",
  "relationships": ["các quan hệ cụ thể nếu thấy, ví dụ: Enkrid -> Luagarne: đồng đội, xưng tôi/cô; tên cướp -> Enkrid: thù địch, không gọi anh/bạn/cậu; nếu là Western/Korean thì dùng mày/tên kia/lược đại từ"],
  "addressing_by_target": ["cặp xưng hô CỤ THỂ theo đối tượng nếu có bằng chứng, ví dụ: Enkrid -> Luagarne: tôi/cô; tên cướp -> Enkrid: không dùng anh/bạn"],
  "forbidden_pronouns": ["xưng hô KHÔNG nên dùng cho nhân vật này nếu có bằng chứng, ví dụ: không dùng hắn/nàng với Korean char"],
  "title_terms": ["danh hiệu/chức vụ quan trọng và cách dịch nên giữ, ví dụ: Hiệp sĩ trưởng / Knight Commander"],
  "personality": "2-4 từ mô tả tính cách",
  "speech_style": "1-2 câu mô tả cách nói đặc trưng — đặc biệt chú ý: câu ngắn/dài, cảm xúc ẩn/rõ, mức độ kiệm lời",
  "role": "nhân vật chính/đồng đội/phản diện/phụ"
}}

Chú ý đặc biệt với tiếng Việt:
- Nếu nhân vật là kẻ thù/kẻ tấn công/người lạ thù địch, ghi rõ họ KHÔNG nên gọi đối phương là "anh/bạn/cậu".
- Với truyện Trung/tiên hiệp/kiếm hiệp/cổ phong, có thể đề xuất "ngươi/mi/tên kia".
- Với Western fantasy, Korean light novel, truyện văn phòng/học viện/hiện đại, KHÔNG đề xuất "ngươi/mi"; dùng "mày", "tên kia", "thằng kia", gọi thẳng vai trò hoặc lược đại từ.
- Nếu nhân vật nhỏ tuổi hơn nói với người lớn/cấp trên, ghi rõ tự xưng "em/con/cháu" và gọi đối phương "anh/chú/bác/ngài"; không dùng "mày/tao" trừ khi thật sự thù địch.
- Một nhân vật có thể xưng hô khác nhau với từng người nghe. Hãy ghi các cặp quan hệ quan trọng khi có bằng chứng.
- addressing_by_target, forbidden_pronouns, title_terms: trả về [] nếu không có bằng chứng rõ ràng.

Trả về JSON array. Ví dụ:
[
  {{
    "name": "Enkrid",
    "aliases": ["Encrid"],
    "gender": "nam",
    "pronoun_3rd": "anh ta",
    "self_address": "tôi",
    "addressing_others": "với đồng đội: tôi/cô/cậu; với kẻ thù Western/Korean: mày/tên kia/lược đại từ; với kẻ thù Trung/cổ phong: ngươi/mi; với trẻ nhỏ: cậu/em",
    "relative_status": "hiệp sĩ trưởng thành, thường có vị thế cao hơn lính thường và dân thường",
    "relationships": ["Enkrid -> Luagarne: đồng đội, tôi/cô", "kẻ cướp -> Enkrid: thù địch, không dùng anh/bạn"],
    "addressing_by_target": ["Enkrid -> Luagarne: tôi/cô"],
    "forbidden_pronouns": [],
    "title_terms": ["Đội trưởng / Squad Leader"],
    "personality": "lạnh lùng, phân tích, kiệm lời",
    "speech_style": "câu ngắn, quyết đoán, hiếm khi giải thích; không cảm thán",
    "role": "nhân vật chính"
  }}
]

Văn bản (chapter {chapter_num}):
{text}
"""

# ── DB helpers ─────────────────────────────────────────────────────────────────

def fetch_chapters(
    story_title: str = "",
    story_id: str = "",
    from_chapter: int = 0,
    to_chapter: int = 0,
    limit: int = 0,
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
            s.source_url AS story_url,
            src.code AS source_code
        FROM chapters c
        JOIN stories s ON s.id = c.story_id
        JOIN sources src ON src.id = s.source_id
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
    if limit > 0:
        query += " LIMIT %(limit)s"
        params["limit"] = limit

    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]


def get_chapter_text(row: dict[str, Any], text_source: str = "polished") -> str:
    """Lấy text từ content trong DB hoặc từ file."""
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

    if content and len(content) > 200:
        return content[:8000]  # Chỉ cần sample đầu chapter

    if path_str:
        p = Path(path_str)
        if not p.is_absolute():
            p = ROOT / p
        if p.exists():
            return p.read_text(encoding="utf-8")[:8000]
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


# ── Ollama extraction ──────────────────────────────────────────────────────────

def call_ollama_extract(
    base_url: str,
    model: str,
    text: str,
    temperature: float = 0.1,
    num_ctx: int = 8192,
    timeout: int = 180,
    session: requests.Session | None = None,
    chapter_num: int = 0,
) -> list[dict[str, Any]]:
    url = base_url.rstrip("/") + "/api/chat"
    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": EXTRACT_SYSTEM},
            {"role": "user", "content": EXTRACT_USER.format(
                text=text[:6000],
                chapter_num=chapter_num or "?",
            )},
        ],
        "options": {"temperature": temperature, "num_ctx": num_ctx},
        "keep_alive": "10m",
    }
    client = session or requests
    resp = client.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    content = resp.json().get("message", {}).get("content", "")
    # Strip think blocks
    content = re.sub(r"<think>.*?</think>\s*", "", content, flags=re.DOTALL | re.IGNORECASE)
    content = re.sub(r"^```(?:json)?\s*", "", content.strip(), flags=re.IGNORECASE)
    content = re.sub(r"\s*```$", "", content)
    try:
        result = json.loads(content)
        return result if isinstance(result, list) else []
    except json.JSONDecodeError:
        # Cố tìm JSON array trong output nếu model thêm text thừa
        m = re.search(r"\[[\s\S]+\]", content)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                pass
    return []


def unload_ollama_model(base_url: str, model: str) -> None:
    """Release model from Ollama GPU memory after extraction completes."""
    try:
        requests.post(
            base_url.rstrip("/") + "/api/generate",
            json={"model": model, "keep_alive": 0},
            timeout=30,
        )
    except Exception:
        pass


# ── Merge logic ────────────────────────────────────────────────────────────────

def normalize_name(name: str) -> str:
    return name.strip().lower()


def merge_characters(
    existing: dict[str, dict[str, Any]],
    new_chars: list[dict[str, Any]],
    chapter_num: int = 0,
) -> dict[str, dict[str, Any]]:
    """
    Merge danh sách nhân vật mới vào existing.
    Key là tên chuẩn hóa (lowercase). Ưu tiên thông tin existing, chỉ thêm trường mới.
    Aliases và first/last_seen_chapter được merge.
    """
    result = dict(existing)

    for char in new_chars:
        name = char.get("name", "").strip()
        if not name:
            continue
        key = normalize_name(name)

        matched_key: str | None = None
        aliases_new = [normalize_name(a) for a in (char.get("aliases") or [])]

        for ex_key, ex_char in result.items():
            ex_aliases = [normalize_name(a) for a in (ex_char.get("aliases") or [])]
            all_ex_names = {ex_key} | set(ex_aliases)
            all_new_names = {key} | set(aliases_new)
            if all_ex_names & all_new_names:
                matched_key = ex_key
                break

        if matched_key:
            existing_char = result[matched_key]
            merged_aliases = list(set(
                [normalize_name(a) for a in (existing_char.get("aliases") or [])]
                + aliases_new
                + ([key] if key != matched_key else [])
            ))
            merged_aliases = [a for a in merged_aliases if a != matched_key]
            if merged_aliases:
                result[matched_key]["aliases"] = [a.title() for a in merged_aliases]
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
                if not existing_char.get(field) and char.get(field):
                    result[matched_key][field] = char[field]
            for list_field in ("relationships", "addressing_by_target", "forbidden_pronouns", "title_terms"):
                new_items = char.get(list_field) or []
                if not new_items:
                    continue
                old_items = existing_char.get(list_field) or []
                if isinstance(old_items, str):
                    old_items = [old_items]
                if isinstance(new_items, str):
                    new_items = [new_items]
                merged_items: list[str] = []
                seen_items: set[str] = set()
                for item in [*old_items, *new_items]:
                    item = str(item).strip()
                    key_item = item.lower()
                    if item and key_item not in seen_items:
                        seen_items.add(key_item)
                        merged_items.append(item)
                if merged_items:
                    result[matched_key][list_field] = merged_items
            # Track first/last seen chapter
            if chapter_num > 0:
                ex_first = existing_char.get("first_seen_chapter")
                ex_last = existing_char.get("last_seen_chapter")
                result[matched_key]["first_seen_chapter"] = min(ex_first, chapter_num) if ex_first else chapter_num
                result[matched_key]["last_seen_chapter"] = max(ex_last, chapter_num) if ex_last else chapter_num
        else:
            result[key] = {
                "name": name,
                **{k: v for k, v in char.items() if k != "name"},
            }
            if chapter_num > 0:
                result[key].setdefault("first_seen_chapter", chapter_num)
                result[key].setdefault("last_seen_chapter", chapter_num)

    return result


def parse_existing_char_map(content: str) -> dict[str, dict[str, Any]]:
    """Best-effort parse of existing markdown char-map sections."""
    chars: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    current_key = ""

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if line.startswith("### "):
            name = line[4:].strip()
            if not name:
                current = None
                current_key = ""
                continue
            current_key = normalize_name(name)
            current = chars.setdefault(current_key, {"name": name})
            continue
        if not current or not line.startswith("- "):
            continue

        body = line[2:].strip()
        lower = body.lower()
        value = body.split(":", 1)[-1].strip() if ":" in body else ""
        if lower.startswith("tên khác:"):
            current["aliases"] = [item.strip() for item in value.split(",") if item.strip()]
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
        elif lower.startswith("giọng nói:") or lower.startswith("giọng thoại:"):
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

    return chars


def append_new_char_sections(
    existing_content: str,
    all_chars: dict[str, dict[str, Any]],
    existing_chars: dict[str, dict[str, Any]],
    chapter_range: str,
) -> str:
    """Append only newly discovered characters, preserving manual map text."""
    now = datetime.now().strftime("%Y-%m-%d")
    existing_names: set[str] = set(existing_chars.keys())
    for char in existing_chars.values():
        existing_names.update(normalize_name(a) for a in (char.get("aliases") or []))

    new_items: list[tuple[str, dict[str, Any]]] = []
    for key, char in sorted(all_chars.items(), key=lambda item: item[0]):
        names = {key, normalize_name(char.get("name") or "")}
        names.update(normalize_name(a) for a in (char.get("aliases") or []))
        if not (names & existing_names):
            new_items.append((key, char))

    if not new_items:
        return existing_content.rstrip() + "\n"

    lines = [
        existing_content.rstrip(),
        "",
        f"## Auto Update: nhân vật mới (chapters {chapter_range}) — cập nhật {now}",
        "",
    ]
    for _, char in new_items:
        name = char.get("name") or "Unknown"
        lines.append(f"### {name}")
        aliases = char.get("aliases") or []
        if aliases:
            lines.append(f"- Tên khác: {', '.join(aliases)}")
        for label, field in (
            ("Giới tính", "gender"),
            ("Ngôi thứ ba", "pronoun_3rd"),
            ("Tự xưng", "self_address"),
            ("Cách gọi người khác", "addressing_others"),
            ("Tuổi/vị thế", "relative_status"),
            ("Tính cách", "personality"),
            ("Giọng nói", "speech_style"),
            ("Vai trò", "role"),
        ):
            val = char.get(field, "")
            if val:
                lines.append(f"- {label}: {val}")
        relationships = char.get("relationships") or []
        if isinstance(relationships, str):
            relationships = [relationships]
        for relationship in relationships[:8]:
            relationship = str(relationship).strip()
            if relationship:
                lines.append(f"- Quan hệ: {relationship}")
        for rule in (char.get("addressing_by_target") or [])[:8]:
            rule = str(rule).strip()
            if rule:
                lines.append(f"- Xưng hô theo đối tượng: {rule}")
        for rule in (char.get("forbidden_pronouns") or [])[:4]:
            rule = str(rule).strip()
            if rule:
                lines.append(f"- Xưng hô cấm: {rule}")
        for term in (char.get("title_terms") or [])[:4]:
            term = str(term).strip()
            if term:
                lines.append(f"- Danh hiệu/chức vụ: {term}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ── Output formatting ──────────────────────────────────────────────────────────

def format_char_map(
    chars: dict[str, dict[str, Any]],
    story_title: str,
    story_id: str,
    chapter_range: str,
    arc_name: str = "",
    existing_content: str = "",
    genre: str = "",
) -> str:
    """Render char_map file. Nếu có existing_content và arc_name, append arc mới."""
    now = datetime.now().strftime("%Y-%m-%d")

    if arc_name and existing_content:
        # Append arc section vào file hiện có
        arc_header = f"\n\n## Arc: {arc_name} (chapters {chapter_range}) — cập nhật {now}\n"
        arc_lines = [arc_header]
        for key, char in sorted(chars.items(), key=lambda x: x[0]):
            name = char.get("name") or key.title()
            notes = []
            for field in (
                "pronoun_3rd",
                "self_address",
                "addressing_others",
                "relative_status",
                "personality",
                "speech_style",
            ):
                val = char.get(field, "")
                if val:
                    notes.append(f"{field}: {val}")
            arc_lines.append(f"### {name}")
            arc_lines.extend(f"- {n}" for n in notes)
            for rule in (char.get("addressing_by_target") or [])[:8]:
                rule = str(rule).strip()
                if rule:
                    arc_lines.append(f"- Xưng hô theo đối tượng: {rule}")
            for rule in (char.get("forbidden_pronouns") or [])[:4]:
                rule = str(rule).strip()
                if rule:
                    arc_lines.append(f"- Xưng hô cấm: {rule}")
            for term in (char.get("title_terms") or [])[:4]:
                term = str(term).strip()
                if term:
                    arc_lines.append(f"- Danh hiệu/chức vụ: {term}")
            arc_lines.append("")
        return existing_content.rstrip() + "\n" + "\n".join(arc_lines)

    # Full file generation
    lines = [
        f"## Truyện: {story_title} ({story_id})",
        f"## Cập nhật: {now} từ chapters {chapter_range}",
        f"## Auto-generated bởi extract_char_map.py — chỉnh sửa thủ công để tinh chỉnh",
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

    # Giữ lại section ALIASES từ file cũ nếu có
    if existing_content:
        alias_section = _extract_section(existing_content, "[ALIASES]")
        if alias_section:
            lines += ["[ALIASES]", alias_section, ""]
        else:
            lines += ["[ALIASES]", "# wrong_name = correct_name", ""]
    else:
        lines += ["[ALIASES]", "# wrong_name = correct_name", ""]

    lines.append("---")
    lines.append("")

    for key, char in sorted(chars.items(), key=lambda x: (x[1].get("role", "z"), x[0])):
        name = char.get("name") or key.title()
        lines.append(f"### {name}")

        aliases = char.get("aliases") or []
        if aliases:
            lines.append(f"- Tên khác: {', '.join(aliases)}")

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

        relationships = char.get("relationships") or []
        if isinstance(relationships, str):
            relationships = [relationships]
        for relationship in relationships[:8]:
            relationship = str(relationship).strip()
            if relationship:
                lines.append(f"- Quan hệ: {relationship}")

        for rule in (char.get("addressing_by_target") or [])[:8]:
            rule = str(rule).strip()
            if rule:
                lines.append(f"- Xưng hô theo đối tượng: {rule}")

        for rule in (char.get("forbidden_pronouns") or [])[:4]:
            rule = str(rule).strip()
            if rule:
                lines.append(f"- Xưng hô cấm: {rule}")

        for term in (char.get("title_terms") or [])[:4]:
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

        # Giữ lại notes thủ công từ file cũ nếu có
        if existing_content:
            old_notes = _extract_char_notes(existing_content, name)
            if old_notes:
                lines.append(f"- Ghi chú: {old_notes}")

        lines.append("")

    return "\n".join(lines)


def _extract_section(content: str, section_header: str) -> str:
    """Extract nội dung của một section cho đến section tiếp theo."""
    lines = content.splitlines()
    in_section = False
    result = []
    for line in lines:
        if line.strip().lower() == section_header.lower():
            in_section = True
            continue
        if in_section:
            if line.startswith("[") and line.endswith("]"):
                break
            result.append(line)
    return "\n".join(result).strip()


def _extract_char_notes(content: str, char_name: str) -> str:
    """Tìm dòng Ghi chú của nhân vật trong file cũ."""
    lines = content.splitlines()
    in_char = False
    for line in lines:
        if line.strip().lower().startswith(f"### {char_name.lower()}"):
            in_char = True
            continue
        if in_char:
            if line.startswith("###"):
                break
            if "ghi chú:" in line.lower():
                return line.split(":", 1)[-1].strip()
    return ""


# ── Incremental (per-chapter) update ──────────────────────────────────────────

def _default_char_map_path(story_id: str, slug: str) -> Path:
    return ROOT / "story_data" / "char_maps" / f"{story_id}-{slug}.txt"


# Lightweight prompt dùng cho incremental extraction (1 chapter).
# Ngắn hơn EXTRACT_USER để giảm latency, không cần /no_think vì qwen3 đã đủ nhanh.
_INC_EXTRACT_USER = """Đọc đoạn truyện sau (chapter {chapter_num}) và liệt kê nhân vật CÓ TÊN RIÊNG xuất hiện.

Char-map hiện tại (nhân vật ĐÃ BIẾT — KHÔNG lặp lại):
---
{known_names}
---

Chỉ liệt kê nhân vật CHƯA CÓ trong danh sách trên.
Nếu không có nhân vật mới: trả về [].

Với mỗi nhân vật mới, trả về JSON object:
{{
  "name": "tên",
  "aliases": [],
  "gender": "nam" | "nữ" | "không rõ",
  "pronoun_3rd": "anh ta / cô ta / hắn / nàng / ...",
  "self_address": "tôi / ta / ...",
  "addressing_others": "gọi đồng đội/cấp trên/kẻ thù thế nào",
  "relative_status": "tuổi/vị thế nếu suy được",
  "addressing_by_target": [],
  "forbidden_pronouns": [],
  "title_terms": [],
  "personality": "2-4 từ",
  "speech_style": "câu ngắn/dài, kiệm lời/nhiều lời, v.v.",
  "role": "nhân vật chính / đồng đội / phản diện / phụ"
}}

Trả về JSON array. Ví dụ: [{{"name": "Enkrid", ...}}]
Chỉ JSON, không giải thích.

Văn bản:
{text}
"""


def update_char_map_incremental(
    chapter_text: str,
    chapter_num: int,
    char_map_path: str,
    story_id: str,
    story_title: str,
    slug: str,
    genre: str = "",
    ollama_url: str = "http://127.0.0.1:11434",
    model: str = "qwen3:14b",
    timeout: int = 90,
    session: requests.Session | None = None,
) -> bool:
    """
    Incremental char-map update: trích xuất nhân vật MỚI từ 1 chapter vừa polished,
    append vào char-map. Không bao giờ overwrite entries đã có.

    Gọi sau mỗi chapter thành công trong polish_worker.
    Returns True nếu có nhân vật mới được thêm hoặc char-map được tạo mới.
    """
    if not chapter_text or len(chapter_text.strip()) < 200:
        return False

    out_path = Path(char_map_path) if char_map_path else _default_char_map_path(story_id, slug)

    existing_content = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
    existing_chars = parse_existing_char_map(existing_content) if existing_content else {}

    # Liệt kê tên đã biết để model không extract lại
    known_names_str = ", ".join(
        c.get("name") or k for k, c in existing_chars.items()
    ) if existing_chars else "(chưa có)"

    url = ollama_url.rstrip("/") + "/api/chat"
    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": EXTRACT_SYSTEM},
            {"role": "user", "content": _INC_EXTRACT_USER.format(
                chapter_num=chapter_num,
                known_names=known_names_str[:1500],
                text=chapter_text[:4000],
            )},
        ],
        "options": {"temperature": 0.1, "num_ctx": 6144},
        "keep_alive": "5m",
    }
    try:
        client = session or requests
        resp = client.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()
        content = resp.json().get("message", {}).get("content", "")
        content = re.sub(r"<think>.*?</think>\s*", "", content, flags=re.DOTALL | re.IGNORECASE)
        content = re.sub(r"^```(?:json)?\s*", "", content.strip(), flags=re.IGNORECASE)
        content = re.sub(r"\s*```$", "", content)
        try:
            new_chars = json.loads(content)
            if not isinstance(new_chars, list):
                new_chars = []
        except json.JSONDecodeError:
            m = re.search(r"\[[\s\S]*\]", content)
            new_chars = json.loads(m.group()) if m else []
    except Exception as exc:
        print(f"[CHAR_MAP_INC] ch{chapter_num:04d}: extract failed: {exc}")
        return False

    if not new_chars:
        # Không có nhân vật mới — chỉ cập nhật metadata updated_to_chapter
        _bump_char_map_coverage(story_id, chapter_num)
        return False

    all_chars = merge_characters(dict(existing_chars), new_chars, chapter_num=chapter_num)
    new_keys = set(all_chars.keys()) - set(existing_chars.keys())

    if not new_keys:
        _bump_char_map_coverage(story_id, chapter_num)
        return False

    # Build updated content
    if existing_content:
        updated_content = append_new_char_sections(
            existing_content, all_chars, existing_chars, f"{chapter_num:04d}"
        )
    else:
        # Chapter đầu tiên — tạo char-map mới
        updated_content = format_char_map(
            chars=all_chars,
            story_title=story_title,
            story_id=story_id,
            chapter_range=f"0001-{chapter_num:04d}",
            genre=genre,
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(updated_content, encoding="utf-8")

    try:
        metadata_update: dict[str, Any] = {
            "char_map_updated_to_chapter": chapter_num,
            "char_map_content": updated_content,
        }
        if not existing_content:
            metadata_update["char_map_path"] = out_path.relative_to(ROOT).as_posix()
        repo.update_story_metadata(story_id, metadata_update)
    except Exception as exc:
        print(f"[CHAR_MAP_INC] DB metadata failed: {exc}")

    new_names = [all_chars[k].get("name") or k for k in new_keys]
    print(f"[CHAR_MAP_INC] ch{chapter_num:04d}: +{len(new_keys)} nhân vật mới: {new_names}")
    return True


def _bump_char_map_coverage(story_id: str, chapter_num: int) -> None:
    """Cập nhật char_map_updated_to_chapter mà không thay đổi file."""
    try:
        story = repo.get_story_by_id(story_id)
        meta = story.get("metadata") or {}
        current = int(meta.get("char_map_updated_to_chapter") or 0)
        if chapter_num > current:
            repo.update_story_metadata(story_id, {"char_map_updated_to_chapter": chapter_num})
    except Exception:
        pass


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Trích xuất / cập nhật character map từ chapters đã polish.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--story-title", default="")
    parser.add_argument("--story-id", default="")
    parser.add_argument("--from-chapter", type=int, default=0)
    parser.add_argument("--to-chapter", type=int, default=0)
    parser.add_argument(
        "--sample-chapters",
        type=int,
        default=30,
        help="Số chapter để phân tích (chọn đều từ range). 0 = tất cả.",
    )
    parser.add_argument(
        "--use-translated",
        action="store_true",
        help="Dùng translated text thay vì polished text. Tương đương --text-source translated.",
    )
    parser.add_argument(
        "--text-source",
        choices=("raw", "translated", "polished"),
        default="polished",
        help="Nguồn text để extract char-map. Auto-worker dùng raw cho truyện Việt, translated cho truyện dịch.",
    )
    parser.add_argument(
        "--append-only",
        action="store_true",
        help="Chỉ thêm nhân vật mới, không ghi đè thông tin đã có.",
    )
    parser.add_argument(
        "--arc-name",
        default="",
        help="Tên arc — nếu có, append section arc mới vào cuối file thay vì ghi đè.",
    )
    parser.add_argument(
        "--output-file",
        default="",
        help="Override đường dẫn output. Mặc định: story_data/char_maps/{story_id}-{slug}.txt",
    )
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
    parser.add_argument("--keep-loaded", action="store_true", help="Không unload model sau khi extract xong.")
    parser.add_argument("--dry-run", action="store_true", help="In kết quả ra stdout, không ghi file.")

    args = parser.parse_args()

    if not args.story_title and not args.story_id:
        parser.error("Cần --story-title hoặc --story-id")

    text_source = "translated" if args.use_translated else args.text_source

    print(f"[QUERY] Lấy chapters từ DB...")
    rows = fetch_chapters(
        story_title=args.story_title,
        story_id=args.story_id,
        from_chapter=args.from_chapter,
        to_chapter=args.to_chapter,
        limit=0,
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

    print(f"[INFO] {story_title} ({story_id}) — {len(rows)} chapters ({chapter_range}), slug={slug}, text_source={text_source}")

    # Sample chapters đều từ toàn range
    sample_size = args.sample_chapters if args.sample_chapters > 0 else len(rows)
    if sample_size < len(rows):
        if sample_size == 1:
            sampled = [rows[-1]]
        else:
            indexes = [
                round(i * (len(rows) - 1) / (sample_size - 1))
                for i in range(sample_size)
            ]
            sampled = [rows[i] for i in dict.fromkeys(indexes)]
    else:
        sampled = rows

    sampled_nums = sorted(int(r["chapter_number"]) for r in sampled)
    sample_range = f"{sampled_nums[0]}-{sampled_nums[-1]}" if sampled_nums else "?"
    print(
        f"[SAMPLE] {len(sampled)}/{len(rows)} chapters (range {sample_range}) "
        f"| model={args.model} | ollama={args.ollama_url}"
    )

    # Resolve output path
    if args.output_file:
        out_path = Path(args.output_file)
    else:
        out_path = ROOT / "story_data" / "char_maps" / f"{story_id}-{slug}.txt"

    # Load existing char map nếu có
    existing_content = ""
    existing_chars: dict[str, dict[str, Any]] = {}
    if out_path.exists():
        existing_content = out_path.read_text(encoding="utf-8")
        existing_chars = parse_existing_char_map(existing_content)
        print(f"[EXIST] char map: {out_path} ({len(existing_chars)} nhân vật)")
    else:
        print(f"[NEW] char map sẽ được tạo mới: {out_path}")

    # Extract characters from each sampled chapter
    all_chars: dict[str, dict[str, Any]] = dict(existing_chars)
    failed = 0
    successful_nums: list[int] = []
    with requests.Session() as session:
        for i, row in enumerate(sampled, 1):
            ch_num = int(row["chapter_number"])
            text = get_chapter_text(row, text_source=text_source)
            if not text or len(text) < 200:
                print(f"  [{i}/{len(sampled)}] ch{ch_num:04d}: SKIP (text rỗng)")
                continue
            print(f"  [{i}/{len(sampled)}] ch{ch_num:04d}: {len(text)} chars → extract...", end=" ", flush=True)
            try:
                chars = call_ollama_extract(
                    base_url=args.ollama_url,
                    model=args.model,
                    text=text,
                    temperature=args.temperature,
                    num_ctx=args.num_ctx,
                    timeout=args.timeout,
                    session=session,
                    chapter_num=ch_num,
                )
                all_chars = merge_characters(all_chars, chars, chapter_num=ch_num)
                successful_nums.append(ch_num)
                print(f"OK ({len(chars)} chars, total={len(all_chars)})")
            except Exception as exc:
                print(f"FAIL: {exc}")
                failed += 1
                time.sleep(2)

    if not args.keep_loaded:
        unload_ollama_model(args.ollama_url, args.model)
        print(f"[OLLAMA] requested unload: {args.model}")

    print(
        f"\n[RESULT] {len(all_chars)} nhân vật tổng cộng "
        f"| {failed}/{len(sampled)} chapter thất bại "
        f"| sample={len(sampled)} chapters ({sample_range})"
    )

    if not all_chars:
        print("[WARN] Không trích xuất được nhân vật nào.")
        return

    # Format output
    if args.append_only and existing_content and not args.arc_name:
        output_content = append_new_char_sections(existing_content, all_chars, existing_chars, chapter_range)
    else:
        output_content = format_char_map(
            chars=all_chars,
            story_title=story_title,
            story_id=story_id,
            chapter_range=chapter_range,
            arc_name=args.arc_name,
            existing_content=existing_content if args.append_only or args.arc_name else "",
            genre=getattr(args, "genre", ""),
        )

    if args.dry_run:
        print("\n" + "=" * 60)
        print(output_content)
        return

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(output_content, encoding="utf-8")
    coverage_nums = successful_nums or sampled_nums
    try:
        repo.update_story_metadata(
            story_id,
            {
                "char_map_path": out_path.relative_to(ROOT).as_posix(),
                "char_map_content": output_content,
                "char_map_updated_to_chapter": max(coverage_nums),
                "char_map_query_from_chapter": chapter_nums[0],
                "char_map_query_to_chapter": chapter_nums[-1],
                "char_map_sampled_chapters": sampled_nums,
                "char_map_sampled_to_chapter": max(sampled_nums),
                "char_map_successful_chapters": successful_nums,
                "char_map_text_source": text_source,
                "char_map_updated_at": datetime.now().isoformat(timespec="seconds"),
            },
        )
    except Exception as exc:
        print(f"[DB WARN] Không cập nhật được story metadata char map: {exc}")
    print(f"[SAVED] {out_path}")
    print(f"\nDùng lệnh tiếp theo:")
    print(f"  python scripts/story_pipeline/repolish_story_from_db.py \\")
    print(f'    --story-title "{story_title}" --overwrite')
    print(f"  (char map sẽ được auto-resolve từ {out_path.name})")


if __name__ == "__main__":
    main()
