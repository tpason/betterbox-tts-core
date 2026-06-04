from __future__ import annotations

import re
import unicodedata

DEFAULT_MAX_PARAGRAPH_LENGTH = 520
MAX_TITLE_STRIP_LINES = 4
MIN_NARRATIVE_MERGE_LENGTH = 180

CHAPTER_NUMBER_PATTERN = r"(?:\d+|[ivxlcdm]+)"
LEADING_CHAPTER_NUMBER_RE = re.compile(
    rf"^(?:(?:chương|chapter)\s*)?{CHAPTER_NUMBER_PATTERN}(?:\s*(?:[-–—:：.．]|::)\s*)?",
    re.IGNORECASE,
)
CHAPTER_HEADING_RE = re.compile(
    rf"^(?:#{{1,6}}\s*)?(?:chương|chapter)\s*{CHAPTER_NUMBER_PATTERN}"
    rf"(?:\s*(?:[-–—:：.．]|::)\s*.+)?(?:\s*\(\d+\))?$",
    re.IGNORECASE,
)
INLINE_CHAPTER_HEADING_RE = re.compile(
    rf"^(?:#{{1,6}}\s*)?(?:chương|chapter)\s*{CHAPTER_NUMBER_PATTERN}\s*"
    rf"(?:(?:[-–—:：.．]|::)\s*)?[^.!?。！？…\n]{{0,90}}[.!?。！？…]\s*",
    re.IGNORECASE,
)
SENTENCE_RE = re.compile(r"[^.!?。！？…]+(?:[.!?。！？…]+[”\"'’]*)?")
OPENING_QUOTE_RE = re.compile(r"([.!?。！？…][”\"'’]?)\s+(?=[\"'“‘])")
SOUND_ONLY_RE = re.compile(r"^(?:Keng|Đinh|Tinh|Ting|Ầm|Rầm|Vù|Xoẹt|Két|Bốp|Chát)!$", re.IGNORECASE)


def normalize_punctuation_spacing(value: str) -> str:
    value = re.sub(r"\s+([,.!?;:，。！？；：])", r"\1", value)
    value = re.sub(r"([({\[])\s+", r"\1", value)
    value = re.sub(r"\s+([)}\]])", r"\1", value)
    value = re.sub(r"(^|[\s([{])([\"'])\s+([^\"'\n]*?)\s+\2(?=$|[\s,.!?;:)\]}])", r"\1\2\3\2", value)
    value = re.sub(r"([,.!?;:，。！？；：])(?=[^\s,.!?;:，。！？；：)\"'\]}])", r"\1 ", value)
    return re.sub(r"\s{2,}", " ", value).strip()


def strip_markdown_noise(value: str) -> str:
    value = re.sub(r"^#{1,6}\s+", "", value)
    value = re.sub(r"^>\s+", "", value)
    value = re.sub(r"^[-*_]{3,}$", "", value)
    value = re.sub(r"^[-*+]\s+(?=\S)", "", value)
    value = re.sub(r"\*\*([^*]+?)\s*\*\*", r"\1", value)
    value = re.sub(r"__([^_]+?)\s*__", r"\1", value)
    value = re.sub(r"(^|[\s(\"'\[])\*([^*\n]+?)\*(?=[\s,.!?;:)\"'\]]|$)", r"\1\2", value)
    return re.sub(r"(^|[\s(\"'\[])\_([^_\n]+?)\_(?=[\s,.!?;:)\"'\]]|$)", r"\1\2", value)


def comparable_title(value: str | None) -> str:
    normalized = strip_markdown_noise(value or "")
    normalized = unicodedata.normalize("NFKD", normalized)
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    normalized = normalized.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    normalized = re.sub(r"[\W_]+", " ", normalized.casefold(), flags=re.UNICODE)
    return re.sub(r"\s+", " ", normalized).strip()


def strip_leading_chapter_number(value: str) -> str:
    return LEADING_CHAPTER_NUMBER_RE.sub("", strip_markdown_noise(value.strip())).strip()


def chapter_title_keys(job: dict) -> set[str]:
    payload = job.get("payload") or {}
    chapter_title = (
        job.get("chapter_title")
        or payload.get("chapter_title")
        or payload.get("title")
        or payload.get("chapterTitle")
        or ""
    )
    candidates = [comparable_title(chapter_title), comparable_title(strip_leading_chapter_number(str(chapter_title)))]
    return {item for item in candidates if len(item) >= 3}


def is_chapter_heading_only(value: str) -> bool:
    if len(value) > 140 or not CHAPTER_HEADING_RE.search(value):
        return False
    return re.search(r"[.!?。！？…]\s+[\"']?\S", value) is None


def is_chapter_title_duplicate(line: str, title_keys: set[str]) -> bool:
    if not title_keys:
        return False
    return comparable_title(line) in title_keys or comparable_title(strip_leading_chapter_number(line)) in title_keys


def is_separator_line(value: str) -> bool:
    return (
        re.search(r"^[-=_~*]{3,}$", value) is not None
        or re.search(r"^[-=_~*]*\s*[oO0]\s*[oO0]\s*[oO0]\s*[-=_~*]*$", value) is not None
        or re.search(r"^[•●◆◇★☆()+\s]{1,12}$", value) is not None
    )


def is_editorial_note_heading(value: str) -> bool:
    return (
        re.search(
            r"^(?:ghi chú|chú thích|note|notes|lưu ý|nhận xét|tóm tắt|hậu kiểm|các thay đổi|"
            r"những thay đổi|đã chỉnh sửa|bản đã chỉnh|bản biên tập|văn bản đã biên tập|"
            r"phong cách chỉnh sửa)\b\s*:?.*$",
            value,
            flags=re.IGNORECASE,
        )
        is not None
        or re.search(
            r"^(?:dưới đây là|sau đây là)\s+(?:bản|văn bản|nội dung)\s+(?:đã\s+)?"
            r"(?:biên tập|chỉnh sửa|polish)",
            value,
            flags=re.IGNORECASE,
        )
        is not None
    )


def is_story_note_heading(value: str) -> bool:
    return (
        re.search(r"^(?:ghi chú|chú thích|note|notes|lưu ý)\b\s*:?.*$", value, flags=re.IGNORECASE)
        is not None
    )


def is_editorial_noise_line(value: str) -> bool:
    if is_editorial_note_heading(value):
        return True
    patterns = [
        r"^(?:bản biên tập|văn bản đã biên tập|nội dung đã biên tập|bản đã chỉnh sửa)\s*:",
        r"^(?:tôi|mình|em)\s+đã\s+(?:biên tập|chỉnh sửa|sửa|giữ|loại|xóa|chuẩn hóa)\b",
        r"^(?:đã\s+)?(?:sửa|chỉnh|chuẩn hóa|loại bỏ|xóa|giữ nguyên|thay|đổi)\b.{0,120}\b"
        r"(?:văn phong|câu|từ|lỗi|dấu câu|tên riêng|thuật ngữ|markdown|title|tiêu đề|line break|xuống dòng)\b",
        r"^(?:không\s+)?(?:tóm tắt|rút gọn|thêm tình tiết|bỏ nội dung)\b",
        r"^(?:output|kết quả)\s+(?:đã\s+)?(?:được\s+)?(?:biên tập|chỉnh sửa|polish)",
    ]
    return any(re.search(pattern, value, flags=re.IGNORECASE) for pattern in patterns)


def clean_reader_line(line: str) -> str:
    cleaned = normalize_punctuation_spacing(strip_markdown_noise(line.strip()))
    if not cleaned:
        return ""
    cleaned = re.sub(
        r"^(?:phiên bản chỉnh sửa|bản chỉnh sửa|bản dịch|bản tiếng việt)\s*:?\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    if not cleaned:
        return ""
    if is_editorial_note_heading(cleaned) and not is_story_note_heading(cleaned):
        return ""
    if is_editorial_noise_line(cleaned) and not is_story_note_heading(cleaned):
        return ""
    if is_separator_line(cleaned) or is_chapter_heading_only(cleaned):
        return ""
    without_inline_heading = INLINE_CHAPTER_HEADING_RE.sub("", cleaned).strip()
    if without_inline_heading and without_inline_heading != cleaned:
        return normalize_punctuation_spacing(without_inline_heading)
    return cleaned


def strip_leading_chapter_title_lines(lines: list[str], title_keys: set[str]) -> list[str]:
    first_content_index = 0
    stripped_count = 0

    while first_content_index < len(lines) and stripped_count < MAX_TITLE_STRIP_LINES:
        line = lines[first_content_index]
        if not line:
            first_content_index += 1
            continue
        if not is_chapter_title_duplicate(line, title_keys) and not is_chapter_heading_only(line):
            break
        first_content_index += 1
        stripped_count += 1

    return lines[first_content_index:] if first_content_index > 0 else lines


def strip_trailing_editorial_notes(lines: list[str]) -> list[str]:
    end = len(lines)
    while end > 0 and not lines[end - 1]:
        end -= 1

    for index in range(end - 1, max(-1, end - 13), -1):
        line = lines[index]
        if line and is_editorial_note_heading(line) and not is_story_note_heading(line):
            end = index
            break

    result: list[str] = []
    source = lines[:end]
    for index, line in enumerate(source):
        if line and not is_editorial_note_heading(line) and is_editorial_noise_line(line):
            continue
        if line:
            result.append(line)
        elif index > 0 and index < len(source) - 1 and source[index - 1]:
            result.append("")
    return result


def split_long_paragraph(paragraph: str, max_length: int = DEFAULT_MAX_PARAGRAPH_LENGTH) -> list[str]:
    if len(paragraph) <= max_length:
        return [paragraph]

    sentences = [match.group(0).strip() for match in SENTENCE_RE.finditer(paragraph)] or [paragraph]
    result: list[str] = []
    current = ""

    for sentence in sentences:
        if not sentence:
            continue
        candidate = f"{current} {sentence}".strip() if current else sentence
        if len(candidate) > max_length and current:
            result.append(current)
            current = sentence
        else:
            current = candidate

    if current:
        result.append(current)
    return result


def format_paragraph_block(block: str) -> list[str]:
    paragraph = normalize_punctuation_spacing(re.sub(r"\n+", " ", block).strip())
    if not paragraph:
        return []
    paragraph = OPENING_QUOTE_RE.sub(r"\1\n\n", paragraph)
    parts = [part.strip() for part in re.split(r"\n{2,}", paragraph) if part.strip()]
    return [item for part in parts for item in split_long_paragraph(part)]


def can_merge_narrative_paragraph(value: str) -> bool:
    if len(value) >= DEFAULT_MAX_PARAGRAPH_LENGTH:
        return False
    if re.search(r"^[\"']", value):
        return False
    if re.search(r"([\"'])$", value) and re.search(r"[\"']", value[:-1]):
        return False
    return SOUND_ONLY_RE.search(value) is None


def merge_short_narrative_paragraphs(paragraphs: list[str]) -> list[str]:
    result: list[str] = []
    buffer = ""

    def flush() -> None:
        nonlocal buffer
        if buffer:
            result.append(buffer)
            buffer = ""

    for paragraph in paragraphs:
        if not can_merge_narrative_paragraph(paragraph):
            flush()
            result.append(paragraph)
            continue

        candidate = f"{buffer} {paragraph}".strip() if buffer else paragraph
        if buffer and (len(candidate) > DEFAULT_MAX_PARAGRAPH_LENGTH or len(paragraph) >= MIN_NARRATIVE_MERGE_LENGTH):
            flush()
            buffer = paragraph
        else:
            buffer = candidate

    flush()
    return result


def merge_dangling_quote_paragraphs(paragraphs: list[str]) -> list[str]:
    result: list[str] = []
    for paragraph in paragraphs:
        if paragraph in {'"', "'"} and result:
            result[-1] = normalize_punctuation_spacing(f"{result[-1]}{paragraph}")
        elif paragraph not in {'"', "'"}:
            result.append(paragraph)
    return result


def format_polished_content(content: str, job: dict | None = None) -> str:
    if not content:
        return ""

    text = unicodedata.normalize("NFKC", content)
    text = (
        text.replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\u00a0", " ")
        .replace("“", '"')
        .replace("”", '"')
        .replace("‘", "'")
        .replace("’", "'")
    )
    text = re.sub(r"[\u200b-\u200d\ufeff]", "", text)
    text = re.sub(r"([^\n])\n+\s*([\"'])(?=\n|$)", r"\1\2", text)

    precleaned_lines = [
        normalize_punctuation_spacing(strip_markdown_noise(line.strip()))
        for line in text.split("\n")
    ]
    precleaned_lines = strip_trailing_editorial_notes(
        strip_leading_chapter_title_lines(precleaned_lines, chapter_title_keys(job or {}))
    )
    lines = [clean_reader_line(line) for line in precleaned_lines]
    lines = strip_trailing_editorial_notes(lines)
    normalized = "\n".join(lines)
    normalized = re.sub(r"([^\n])\n([\"'])(?=\n|$)", r"\1\2", normalized)
    normalized = re.sub(r"[ \t]+", " ", normalized)
    normalized = re.sub(r" *\n *", "\n", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()
    if not normalized:
        return ""

    paragraphs = [
        normalize_punctuation_spacing(re.sub(r"\n+", " ", paragraph).strip())
        for block in re.split(r"\n{2,}", normalized)
        for paragraph in format_paragraph_block(block)
    ]
    paragraphs = [paragraph for paragraph in paragraphs if paragraph]
    paragraphs = merge_short_narrative_paragraphs(merge_dangling_quote_paragraphs(paragraphs))
    return "\n\n".join(paragraphs).strip()
