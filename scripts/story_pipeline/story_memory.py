#!/usr/bin/env python3
"""Story memory / character-term bible support for long fiction pipelines.

This module intentionally has no third-party dependency. It gives translate and
polish scripts a shared way to load durable story memory, normalize high-confidence
name/term drift, inject compact context, and warn about render-blocking quality
issues. Future pgvector/RAG support can be built on top of the same public
functions without changing callers.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MEMORY_ROOT = ROOT / "story_data" / "story_memory"


DEFAULT_ROLE_POLICIES: dict[str, list[str]] = {
    "default": [
        "Dân thường/lính thường gọi nhân vật chính theo tuổi + vị thế + mức hiểu biết; không tự động dùng 'ông' chỉ vì nhân vật có chức vị.",
        "Người nhỏ tuổi gọi người trưởng thành là 'anh/chị/chú/bác/ngài' tùy tuổi và hoàn cảnh; tự xưng 'em/con/cháu'.",
        "Cấp dưới/lính đồn trú trong bối cảnh công vụ gọi cấp trên là 'ngài', 'chỉ huy', 'đội trưởng', 'tướng quân' theo chức danh đã ổn định.",
        "Kẻ thù hoặc người lạ thù địch không gọi đối phương thân thiện là 'anh/bạn/cậu'; dùng mày/tên kia/ngươi tùy thể loại.",
        "Bạn bè riêng tư có thể xưng hô mềm và ngắn hơn bối cảnh triều đình/công vụ; cùng một cặp nhân vật có thể đổi xưng hô theo scene.",
    ],
    "western_fantasy": [
        "Fantasy Hàn/Tây: không dùng khẩu khí tiên hiệp như 'ngươi/mi/bổn tọa/lão tử' trừ khi memory truyện chỉ định riêng.",
        "Văn kể ưu tiên tên nhân vật, 'anh/anh ta/cô/cậu'; tránh 'hắn/nàng/lão/y' nếu không phải lựa chọn đã ổn định.",
        "Hostile dialogue dùng 'mày', 'tên kia', 'thằng kia', lược đại từ, hoặc gọi thẳng vai trò.",
        "Military captain không phải naval captain: dịch theo bối cảnh là 'đội trưởng', 'chỉ huy', 'trung đội trưởng', 'tướng quân'; không dùng 'thuyền trưởng' nếu không có tàu/thuyền.",
    ],
    "tien_hiep": [
        "Tiên hiệp/cổ phong: có thể dùng 'ta/ngươi/hắn/nàng/lão/y' nếu hợp tuổi, phe phái và vị thế.",
        "Danh xưng tôn kính dùng 'tiền bối', 'đại nhân', 'sư phụ', 'ngài'; không hiện đại hóa lời thoại cổ phong.",
    ],
    "kiem_hiep": [
        "Kiếm hiệp: xưng hô giang hồ theo vai vế, môn phái, ân oán; Hán Việt vừa phải, câu vẫn dễ nghe.",
    ],
    "do_thi": [
        "Đô thị/hiện đại: dùng 'anh/chị/em/cô/chú/bác/tôi' tự nhiên đời thường; tránh cổ phong.",
        "Phản diện hiện đại thô lỗ dùng 'mày/tao/tên kia', không dùng 'ngươi/mi'.",
    ],
    "lang_man": [
        "Romance: ưu tiên sắc thái cảm xúc và quan hệ thân mật; xưng hô thay đổi tinh tế theo khoảng cách tình cảm.",
    ],
}


@dataclass
class StoryMemory:
    directory: Path | None = None
    story_id: str = ""
    slug: str = ""
    story_bible: str = ""
    style_guide: str = ""
    characters: list[dict[str, Any]] = field(default_factory=list)
    roles: list[dict[str, Any]] = field(default_factory=list)
    glossary: list[dict[str, Any]] = field(default_factory=list)
    aliases: dict[str, str] = field(default_factory=dict)
    replacements: dict[str, str] = field(default_factory=dict)
    recaps: dict[str, dict[str, Any]] = field(default_factory=dict)
    loaded_files: list[Path] = field(default_factory=list)

    @property
    def loaded(self) -> bool:
        return bool(
            self.story_bible
            or self.style_guide
            or self.characters
            or self.roles
            or self.glossary
            or self.aliases
            or self.replacements
            or self.recaps
        )


def _resolve(path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else ROOT / p


def _safe_read(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


def _load_json(path: Path) -> Any:
    if not path.exists() or not path.is_file():
        return None
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return None
    return json.loads(text)


def _as_list(data: Any) -> list[dict[str, Any]]:
    if data is None:
        return []
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        if isinstance(data.get("items"), list):
            return [item for item in data["items"] if isinstance(item, dict)]
        result: list[dict[str, Any]] = []
        for key, value in data.items():
            if isinstance(value, dict):
                item = {"id": key, **value}
            else:
                item = {"id": key, "value": value}
            result.append(item)
        return result
    return []


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, dict):
        values: list[str] = []
        for key, item in value.items():
            if isinstance(item, str):
                values.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("name") or item.get("value") or key
                if text:
                    values.append(str(text))
        return values
    if isinstance(value, list):
        values = []
        for item in value:
            if isinstance(item, str):
                values.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("name") or item.get("value")
                if text:
                    values.append(str(text))
        return values
    return []


def _normalize_key(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().casefold())


def _name_boundary_pattern(surface: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?<![A-Za-zÀ-ỹ0-9_]){re.escape(surface)}(?![A-Za-zÀ-ỹ0-9_])",
        re.IGNORECASE,
    )


def _replace_surface(text: str, surface: str, replacement: str) -> str:
    if not surface or not replacement or _normalize_key(surface) == _normalize_key(replacement):
        return text
    pattern = _name_boundary_pattern(surface)

    def _replace(match: re.Match[str]) -> str:
        value = match.group(0)
        if value and value[0].isupper() and replacement:
            return replacement[0].upper() + replacement[1:]
        return replacement

    return pattern.sub(_replace, text)


def _truncate(text: str, max_chars: int) -> str:
    text = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    cut = text[:max_chars].rstrip()
    last_break = max(cut.rfind("\n"), cut.rfind(". "), cut.rfind("; "))
    if last_break > max_chars * 0.55:
        cut = cut[: last_break + 1].rstrip()
    return cut + "\n[...]"


def infer_slug_from_char_map(char_map_file: str = "") -> tuple[str, str]:
    """Return (story_id, slug) guessed from a char-map filename."""
    if not char_map_file:
        return "", ""
    stem = Path(char_map_file).stem
    match = re.match(r"^(\d+)[-_](.+)$", stem)
    if match:
        return match.group(1), match.group(2)
    if stem.isdigit():
        return stem, ""
    return "", stem


def find_story_memory_dir(
    *,
    story_memory_dir: str = "",
    story_id: str = "",
    slug: str = "",
    char_map_file: str = "",
) -> str:
    """Find a story memory directory by explicit path, story id/slug, or char-map name."""
    inferred_id, inferred_slug = infer_slug_from_char_map(char_map_file)
    story_id = story_id or inferred_id
    slug = slug or inferred_slug

    root = DEFAULT_MEMORY_ROOT
    if story_memory_dir:
        p = _resolve(story_memory_dir)
        if not p.exists() or not p.is_dir():
            return ""
        marker_files = (
            "story_bible.md",
            "style_guide.md",
            "characters.json",
            "roles.json",
            "glossary.json",
            "aliases.json",
        )
        if any((p / marker).exists() for marker in marker_files):
            return str(p)
        root = p

    candidates: list[Path] = []
    if story_id and slug:
        candidates.append(root / f"{story_id}-{slug}")
    if story_id:
        candidates.append(root / story_id)
    if slug:
        candidates.append(root / slug)

    for candidate in candidates:
        if candidate.exists() and candidate.is_dir():
            return str(candidate)
    return ""


def _load_aliases(path: Path) -> dict[str, str]:
    data = _load_json(path)
    aliases: dict[str, str] = {}
    if isinstance(data, dict):
        if isinstance(data.get("aliases"), dict):
            data = data["aliases"]
        for wrong, correct in data.items():
            if isinstance(correct, str) and wrong:
                aliases[str(wrong)] = correct
            elif isinstance(correct, dict):
                value = correct.get("canonical") or correct.get("correct") or correct.get("replace_with")
                if value:
                    aliases[str(wrong)] = str(value)
    return aliases


def _collect_character_replacements(characters: list[dict[str, Any]]) -> dict[str, str]:
    replacements: dict[str, str] = {}
    for char in characters:
        canonical = str(char.get("canonical_name") or char.get("name") or char.get("id") or "").strip()
        if not canonical:
            continue
        for key in ("wrong_spellings", "misspellings", "name_variants_to_normalize", "normalize_aliases"):
            for surface in _string_list(char.get(key)):
                replacements[surface] = canonical
        for item in char.get("aliases") or []:
            if isinstance(item, dict) and (item.get("normalize") or item.get("replace_with_canonical")):
                surface = str(item.get("text") or item.get("name") or item.get("value") or "").strip()
                if surface:
                    replacements[surface] = canonical
    return replacements


def _collect_glossary_replacements(glossary: list[dict[str, Any]]) -> dict[str, str]:
    replacements: dict[str, str] = {}
    for item in glossary:
        if item.get("auto_replace") is False:
            continue
        canonical = str(
            item.get("canonical_vi")
            or item.get("vi")
            or item.get("canonical")
            or item.get("preferred")
            or ""
        ).strip()
        if not canonical:
            continue
        for key in ("wrong_translations", "wrong_terms", "normalize_variants", "forbidden_literal"):
            for surface in _string_list(item.get(key)):
                replacements[surface] = canonical
        if item.get("auto_replace_forbidden", True):
            for surface in _string_list(item.get("forbidden")):
                replacements[surface] = canonical
    return replacements


def load_story_memory(
    *,
    story_memory_dir: str = "",
    story_id: str = "",
    slug: str = "",
    char_map_file: str = "",
) -> StoryMemory:
    """Load story memory files if present. Missing memory returns an empty object."""
    resolved_dir = find_story_memory_dir(
        story_memory_dir=story_memory_dir,
        story_id=story_id,
        slug=slug,
        char_map_file=char_map_file,
    )
    memory = StoryMemory(
        directory=Path(resolved_dir) if resolved_dir else None,
        story_id=story_id,
        slug=slug,
    )
    if not resolved_dir:
        return memory

    base = Path(resolved_dir)
    story_id_from_map, slug_from_map = infer_slug_from_char_map(char_map_file)
    memory.story_id = story_id or story_id_from_map
    memory.slug = slug or slug_from_map or base.name

    for filename, attr in (
        ("story_bible.md", "story_bible"),
        ("style_guide.md", "style_guide"),
    ):
        path = base / filename
        text = _safe_read(path)
        if text:
            setattr(memory, attr, text)
            memory.loaded_files.append(path)

    for filename, attr in (
        ("characters.json", "characters"),
        ("roles.json", "roles"),
        ("glossary.json", "glossary"),
    ):
        path = base / filename
        data = _load_json(path)
        items = _as_list(data)
        if items:
            setattr(memory, attr, items)
            memory.loaded_files.append(path)

    aliases_path = base / "aliases.json"
    memory.aliases.update(_load_aliases(aliases_path))
    if memory.aliases:
        memory.loaded_files.append(aliases_path)

    recaps_path = base / "recaps.json"
    recaps_data = _load_json(recaps_path)
    if isinstance(recaps_data, dict):
        memory.recaps = {
            str(k): v for k, v in recaps_data.items() if isinstance(v, dict) and v.get("recap")
        }
        if memory.recaps:
            memory.loaded_files.append(recaps_path)

    memory.replacements.update(memory.aliases)
    memory.replacements.update(_collect_character_replacements(memory.characters))
    memory.replacements.update(_collect_glossary_replacements(memory.glossary))
    return memory


def _locked_json_update(path: Path, update_fn) -> bool:
    """Atomic read-modify-write một JSON dict file, an toàn với nhiều worker.

    - Lock per-file qua fcntl.flock trên `<path>.lock`; reload JSON mới nhất
      TRONG lock rồi mới gọi update_fn(data) → data mới.
    - Ghi temp file cùng dir + os.replace() (atomic trên cùng filesystem).
    - Fail-closed: fcntl không khả dụng (hệ filesystem/OS lạ) → KHÔNG ghi,
      trả False — caller coi như warning, tránh risk corrupt JSON.
    """
    try:
        import fcntl
    except ImportError:
        return False
    import os as _os
    import tempfile as _tempfile

    path = _resolve(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    try:
        with open(lock_path, "w", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            current = _load_json(path)
            data = current if isinstance(current, dict) else {}
            updated = update_fn(dict(data))
            if not isinstance(updated, dict):
                return False
            fd, tmp_name = _tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
            try:
                with _os.fdopen(fd, "w", encoding="utf-8") as tmp:
                    json.dump(updated, tmp, ensure_ascii=False, indent=2)
                _os.replace(tmp_name, path)
            except BaseException:
                try:
                    _os.unlink(tmp_name)
                except OSError:
                    pass
                raise
        return True
    except OSError:
        return False


def save_chapter_recap(
    directory: str | Path,
    chapter_number: int,
    recap: str,
    *,
    max_entries: int = 50,
) -> bool:
    """Ghi recap 1 chương vào recaps.json (atomic + locked). Giữ max_entries
    chương gần nhất. Trả False nếu không ghi được — caller chỉ log warning."""
    recap = (recap or "").strip()
    if not recap or chapter_number <= 0:
        return False
    path = _resolve(Path(directory) / "recaps.json")

    def _update(data: dict[str, Any]) -> dict[str, Any]:
        from datetime import datetime, timezone
        data[str(chapter_number)] = {
            "recap": recap,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        # Prune: giữ các chương mới nhất theo chapter number
        def _ch(k: str) -> int:
            try:
                return int(k)
            except ValueError:
                return -1
        keys = sorted((k for k in data if _ch(k) > 0), key=_ch, reverse=True)
        return {k: data[k] for k in keys[:max_entries]}

    return _locked_json_update(path, _update)


def build_recap_context(memory: StoryMemory, current_chapter: int, *, max_prev: int = 3, max_chars: int = 600) -> str:
    """Recap của <= max_prev chương liền trước current_chapter (chỉ chương nhỏ hơn —
    an toàn với out-of-order processing). Empty nếu không có gì."""
    if current_chapter <= 0 or not memory.recaps:
        return ""
    prev: list[tuple[int, str]] = []
    for key, entry in memory.recaps.items():
        try:
            num = int(key)
        except ValueError:
            continue
        if 0 < num < current_chapter:
            recap = str(entry.get("recap") or "").strip()
            if recap:
                prev.append((num, recap))
    if not prev:
        return ""
    prev.sort(reverse=True)
    lines = [f"- Chương {num}: {recap}" for num, recap in prev[:max_prev]]
    lines.reverse()  # in theo thứ tự thời gian
    return _truncate("\n".join(lines), max_chars)


def apply_story_memory_replacements(text: str, memory: StoryMemory) -> str:
    """Normalize high-confidence name/term drift. Nicknames are not replaced."""
    if not text or not memory.replacements:
        return text
    result = text
    for surface, replacement in sorted(memory.replacements.items(), key=lambda item: len(item[0]), reverse=True):
        result = _replace_surface(result, str(surface), str(replacement))
    return result


def _entry_surfaces(entry: dict[str, Any]) -> list[str]:
    surfaces: list[str] = []
    for key in (
        "canonical_name",
        "name",
        "id",
        "aliases",
        "wrong_spellings",
        "misspellings",
        "allowed_nicknames",
        "nicknames",
        "relational_refs",
    ):
        value = entry.get(key)
        if isinstance(value, str):
            surfaces.append(value)
        else:
            surfaces.extend(_string_list(value))
    for title in entry.get("epithets_titles") or entry.get("titles") or []:
        if isinstance(title, str):
            surfaces.append(title)
        elif isinstance(title, dict):
            surfaces.extend(_string_list([
                title.get("text"),
                title.get("vi"),
                title.get("canonical_vi"),
                title.get("source"),
            ]))
    return [s for s in dict.fromkeys(surfaces) if s]


def _text_has_surface(text_key: str, surface: str) -> bool:
    if not surface:
        return False
    return _normalize_key(surface) in text_key


def _relevant_characters(memory: StoryMemory, text: str, max_items: int = 12) -> list[dict[str, Any]]:
    if not memory.characters:
        return []
    text_key = _normalize_key(text)
    relevant: list[dict[str, Any]] = []
    priority: list[dict[str, Any]] = []
    for char in memory.characters:
        role = str(char.get("role") or "").lower()
        if char.get("priority") or "main" in role or "chính" in role:
            priority.append(char)
        if any(_text_has_surface(text_key, surface) for surface in _entry_surfaces(char)):
            relevant.append(char)
    merged: list[dict[str, Any]] = []
    seen: set[int] = set()
    for char in [*relevant, *priority]:
        ident = id(char)
        if ident not in seen:
            seen.add(ident)
            merged.append(char)
    return merged[:max_items]


def _relevant_glossary(memory: StoryMemory, text: str, max_items: int = 18) -> list[dict[str, Any]]:
    if not memory.glossary:
        return []
    text_key = _normalize_key(text)
    relevant: list[dict[str, Any]] = []
    priority: list[dict[str, Any]] = []
    for item in memory.glossary:
        if item.get("priority"):
            priority.append(item)
        surfaces: list[str] = []
        for key in (
            "source",
            "source_terms",
            "canonical_vi",
            "vi",
            "canonical",
            "variants",
            "wrong_translations",
            "forbidden",
            "forbidden_literal",
        ):
            value = item.get(key)
            if isinstance(value, str):
                surfaces.append(value)
            else:
                surfaces.extend(_string_list(value))
        if any(_text_has_surface(text_key, surface) for surface in surfaces):
            relevant.append(item)
    merged: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in [*relevant, *priority]:
        ident = id(item)
        if ident not in seen:
            seen.add(ident)
            merged.append(item)
    return merged[:max_items]


def _format_character(char: dict[str, Any]) -> str:
    name = char.get("canonical_name") or char.get("name") or char.get("id") or "Unknown"
    lines = [f"- {name}"]
    fields = (
        ("wrong_spellings", "lỗi tên cần sửa"),
        ("allowed_nicknames", "nickname được giữ"),
        ("relational_refs", "cách gọi theo quan hệ"),
        ("epithets_titles", "danh hiệu/biệt danh"),
        ("role", "vai trò"),
        ("age_band", "tuổi"),
        ("rank_status", "vị thế"),
        ("third_person_narration", "ngôi kể"),
        ("self_address", "tự xưng"),
        ("addressing_by_target", "xưng hô theo đối tượng"),
        ("voice_style", "giọng thoại"),
        ("notes", "ghi chú"),
    )
    for key, label in fields:
        value = char.get(key)
        if not value:
            continue
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False)
        else:
            text = str(value)
        lines.append(f"  - {label}: {text}")
    return "\n".join(lines)


def _format_glossary_item(item: dict[str, Any]) -> str:
    canonical = item.get("canonical_vi") or item.get("vi") or item.get("canonical") or item.get("id") or ""
    source = item.get("source") or item.get("source_terms") or ""
    parts = [f"- {canonical}" if canonical else "- Thuật ngữ"]
    if source:
        parts.append(f"source={source}")
    for key, label in (
        ("meaning", "ý nghĩa"),
        ("tone", "sắc thái"),
        ("policy", "policy"),
        ("variants", "biến thể đúng"),
        ("wrong_translations", "dịch sai cần sửa"),
        ("forbidden", "cấm dùng"),
        ("forbidden_literal", "cấm dịch sát"),
    ):
        value = item.get(key)
        if value:
            parts.append(f"{label}={json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value}")
    return "; ".join(parts)


def build_story_memory_prompt(
    memory: StoryMemory,
    text: str,
    *,
    genre: str = "",
    max_chars: int = 5200,
    current_chapter: int = 0,
) -> str:
    """Build compact story-specific prompt context for the current chunk.

    current_chapter > 0 bật block recap các chương liền trước (continuity).
    current_chapter == 0 (caller không truyền) → bỏ recaps, không đoán.
    """
    sections: list[str] = []

    if memory.story_bible:
        sections.append("STORY BIBLE:\n" + _truncate(memory.story_bible, 1200))
    if memory.style_guide:
        sections.append("STYLE GUIDE:\n" + _truncate(memory.style_guide, 1200))

    recap_block = build_recap_context(memory, current_chapter)
    if recap_block:
        sections.append(
            "TÓM TẮT CÁC CHƯƠNG TRƯỚC (chỉ là bối cảnh để giữ mạch truyện và xưng hô — "
            "KHÔNG dịch lại; nếu mâu thuẫn thì char map/glossary thắng):\n" + recap_block
        )

    role_lines = DEFAULT_ROLE_POLICIES.get("default", [])
    if genre:
        role_lines = [*role_lines, *DEFAULT_ROLE_POLICIES.get(genre, [])]
    if role_lines:
        sections.append("ROLE / XƯNG HÔ DEFAULT THEO THỂ LOẠI:\n" + "\n".join(f"- {line}" for line in role_lines))

    if memory.roles:
        role_text = "\n".join(
            f"- {role.get('name') or role.get('id')}: "
            f"{json.dumps({k: v for k, v in role.items() if k not in {'name', 'id'}}, ensure_ascii=False)}"
            for role in memory.roles[:18]
        )
        sections.append("ROLE BIBLE RIÊNG CỦA TRUYỆN:\n" + role_text)

    chars = _relevant_characters(memory, text)
    if chars:
        sections.append("NHÂN VẬT LIÊN QUAN TRONG ĐOẠN NÀY:\n" + "\n".join(_format_character(char) for char in chars))

    glossary = _relevant_glossary(memory, text)
    if glossary:
        sections.append("GLOSSARY / DANH HIỆU / THUẬT NGỮ LIÊN QUAN:\n" + "\n".join(_format_glossary_item(item) for item in glossary))

    if memory.replacements:
        replacements = list(memory.replacements.items())[:40]
        sections.append(
            "NORMALIZATION BẮT BUỘC:\n"
            + "\n".join(f"- {wrong} -> {correct}" for wrong, correct in replacements)
        )

    prompt = "\n\n".join(section for section in sections if section.strip())
    return _truncate(prompt, max_chars)


def _forbidden_surfaces(memory: StoryMemory) -> list[tuple[str, str]]:
    surfaces: list[tuple[str, str]] = []
    for wrong, correct in memory.replacements.items():
        surfaces.append((wrong, f"nên là {correct}"))
    for char in memory.characters:
        name = str(char.get("canonical_name") or char.get("name") or char.get("id") or "nhân vật")
        for value in _string_list(char.get("forbidden_terms")):
            surfaces.append((value, f"cấm với {name}"))
        for value in _string_list(char.get("forbidden_pronouns")):
            surfaces.append((value, f"xưng hô cấm với {name}"))
    for item in memory.glossary:
        canonical = str(item.get("canonical_vi") or item.get("vi") or item.get("canonical") or "").strip()
        reason = f"thuật ngữ nên là {canonical}" if canonical else "thuật ngữ bị cấm"
        for key in ("wrong_translations", "wrong_terms", "forbidden", "forbidden_literal"):
            for value in _string_list(item.get(key)):
                surfaces.append((value, reason))
    return [(s, r) for s, r in surfaces if s]


def find_story_memory_quality_issues(text: str, memory: StoryMemory, *, genre: str = "") -> list[str]:
    """Return deterministic warnings for name/term drift and broad register issues."""
    issues: list[str] = []
    if not text:
        return issues

    for surface, reason in _forbidden_surfaces(memory):
        if _name_boundary_pattern(surface).search(text):
            issues.append(f"term/name drift: `{surface}` ({reason})")

    if genre == "western_fantasy":
        for surface in ("ngươi", "mi", "bổn tọa", "lão tử"):
            if _name_boundary_pattern(surface).search(text):
                issues.append(f"register drift western_fantasy: `{surface}` có vẻ cổ phong/tiên hiệp")
        if _name_boundary_pattern("thuyền trưởng").search(text) and not re.search(
            r"tàu|thuyền|boong|bến cảng|hải quân|hải tặc", text, flags=re.IGNORECASE
        ):
            issues.append("term drift: `thuyền trưởng` trong non-naval context; captain quân sự cần dịch theo chức vụ")
        if re.search(r'"[^"\n]{0,120}\b[Bb]ạn\b[^"\n]{0,120}"', text):
            issues.append("dialogue register: lời thoại có `bạn`, dễ là dấu máy dịch trong western fantasy")

    if re.search(r"(?m)^\"\s*$|^\s*\"\s+", text):
        issues.append("format drift: dấu ngoặc kép bị tách dòng bất thường")

    return list(dict.fromkeys(issues))


def story_memory_status(memory: StoryMemory) -> str:
    if not memory.loaded:
        return "story_memory=no"
    base = memory.directory.as_posix() if memory.directory else "(inline)"
    parts = [
        f"story_memory=yes:{base}",
        f"chars={len(memory.characters)}",
        f"roles={len(memory.roles)}",
        f"terms={len(memory.glossary)}",
        f"repl={len(memory.replacements)}",
    ]
    return " ".join(parts)
