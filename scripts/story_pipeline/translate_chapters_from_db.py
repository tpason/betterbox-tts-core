#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import socket
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

import requests
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from story_db.story_pipeline_db import repository as repo
from story_db.story_pipeline_db.db import connect
from genre_prompts import detect_genre, find_char_map_file, load_char_map, resolve_genre_from_context
from story_memory import (
    apply_seed_glossary_replacements,
    build_story_memory_prompt,
    load_story_memory,
)
from scripts.story_pipeline.polish_chapter_texts_ollama import clean_for_audiobook_tts, polish_file
from scripts.story_pipeline.polish_worker import read_formatted_output, read_formatted_polished_output
from scripts.story_pipeline.translate_chapter_texts_ollama import translate_file


LOG_FILE: Path | None = None

NON_VI_SOURCE_LANGUAGES = {
    "qidian": "zh",
    "royalroad": "en",
    "novelbin": "en",
    "freewebnovel": "en",
    "lightnovelpub": "en",
    "skydemonorder": "en",
    "wetriedtls": "en",
    "fanmtl": "en",
    "novelfire": "en",
    "novelhub": "en",
    "naver_series": "ko",
}

VI_LANGUAGE_CODES = {"vi", "vn", "vie", "vietnamese", "tiếng việt", "tieng viet"}
CJK_PATTERN = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")
VI_DIACRITIC_PATTERN = re.compile(
    r"[àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợ"
    r"ùúủũụưừứửữựỳýỷỹỵđ"
    r"ÀÁẢÃẠĂẰẮẲẴẶÂẦẤẨẪẬÈÉẺẼẸÊỀẾỂỄỆÌÍỈĨỊÒÓỎÕỌÔỒỐỔỖỘƠỜỚỞỠỢ"
    r"ÙÚỦŨỤƯỪỨỬỮỰỲÝỶỸỴĐ]"
)
VI_WORDS = {
    "của",
    "và",
    "là",
    "không",
    "một",
    "người",
    "đã",
    "trong",
    "tôi",
    "hắn",
    "nàng",
    "với",
    "này",
    "đó",
    "cho",
    "được",
    "như",
    "về",
    "khi",
    "thì",
    "cũng",
    "lại",
}
EN_WORDS = {
    "the",
    "and",
    "was",
    "were",
    "with",
    "that",
    "this",
    "from",
    "have",
    "not",
    "you",
    "his",
    "her",
    "she",
    "he",
}

METADATA_TERM_RULES = """Quy ước thuật ngữ bắt buộc:
- regressor / regression / regressed: dịch theo nghĩa hồi quy thời gian/hoàn nguyên về quá khứ. Dùng "Hồi Quy Giả", "người hồi quy", hoặc "hồi quy" tùy ngữ cảnh. Tuyệt đối không dịch thành "hồi phục".
- returner: người trở về / kẻ trở về; nếu ngữ cảnh là quay lại quá khứ thì ưu tiên "hồi quy".
- reincarnator / reincarnation: chuyển sinh / tái sinh, không lẫn với hồi quy.
- cultivation: tu chân / tu luyện; cultivator: tu sĩ / người tu luyện.
- tale / story trong tên truyện: ưu tiên "Truyện", "Chuyện", hoặc bỏ nếu tên Việt tự nhiên hơn.
- Không dịch word-by-word nếu tạo tên Việt gượng. Ưu tiên tên tự nhiên cho web đọc truyện nhưng giữ đúng trope chính.
"""


def _metadata_context_block(context: str = "") -> str:
    cleaned = re.sub(r"\n{3,}", "\n\n", (context or "").strip())
    if not cleaned:
        cleaned = "Không có context thêm."
    return f"{METADATA_TERM_RULES}\n\nContext truyện:\n{cleaned}"


STORY_TITLE_PROMPT = """Bạn là biên tập viên tên truyện tiếng Việt.

Hãy dịch/chỉnh tên truyện sau thành một tên tiếng Việt tự nhiên, dễ đọc cho web đọc truyện.
Yêu cầu:
- Chỉ trả về đúng một tên truyện.
- Không thêm giải thích.
- Không thêm dấu ngoặc kép.
- Giữ đúng nghĩa chính, không bịa thể loại mới.
- Dùng context và quy ước thuật ngữ bên dưới; không dịch từng chữ máy móc.
- Nếu tên đã là tiếng Việt, chỉ chuẩn hóa chính tả/viết hoa.

{context}

Tên nguồn: {title}
"""

STORY_DESCRIPTION_PROMPT = """Bạn là dịch giả và biên tập viên mô tả truyện tiếng Việt.

Dịch và biên tập phần giới thiệu truyện sau sang tiếng Việt tự nhiên, dễ đọc cho web đọc truyện.
Yêu cầu:
- Giữ đúng nội dung, không thêm tình tiết mới.
- Không lược bỏ ý chính.
- Văn phong mượt, rõ nghĩa, hợp truyện chữ.
- Không markdown, không tiêu đề, không giải thích.
- Chỉ trả về phần mô tả tiếng Việt.
- Dùng context và quy ước thuật ngữ bên dưới để giữ trope/thuật ngữ nhất quán với nội dung chương.

{context}

Mô tả nguồn:
{description}
"""

CHAPTER_TITLE_PROMPT = """Bạn là biên tập viên tên chương truyện tiếng Việt.

Dịch tiêu đề chương sau sang tiếng Việt tự nhiên cho web đọc truyện.
Yêu cầu:
- Chỉ trả về đúng tiêu đề chương, không thêm giải thích.
- Không thêm dấu ngoặc kép.
- Giữ đúng nghĩa, không bịa thêm nội dung.
- Nếu có số chương (Chapter 1, Ch. 42...), giữ nguyên hoặc đổi thành "Chương X".
- Dùng context và quy ước thuật ngữ bên dưới; không dịch từng chữ máy móc.
- Nếu tiêu đề đã là tiếng Việt, chỉ chuẩn hóa chính tả/viết hoa.

{context}

Tiêu đề nguồn: {title}
"""

STORY_AUTHOR_PROMPT = """Bạn là biên tập viên tên tác giả tiếng Việt.

Hãy chuyển tên tác giả sau sang dạng tiếng Việt tự nhiên cho web đọc truyện.
Yêu cầu:
- Chỉ trả về đúng tên tác giả.
- Không thêm giải thích.
- Không thêm dấu ngoặc kép.
- Nếu đây là bút danh Latin/tiếng Anh nên giữ nguyên, chỉ chuẩn hóa khoảng trắng/viết hoa.
- Nếu là Hán/Hàn/Nhật hoặc dạng có thể phiên âm quen thuộc, chuyển sang cách đọc tiếng Việt tự nhiên.

Tên tác giả nguồn: {author}
"""


def configure_logging(log_file: str) -> None:
    global LOG_FILE
    LOG_FILE = Path(log_file) if log_file else None
    if LOG_FILE:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def log(message: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} {message}"
    print(line, flush=True)
    if LOG_FILE:
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def safe_slug(value: str) -> str:
    value = re.sub(r"\s+", "-", (value or "").strip().lower())
    value = re.sub(r"[^a-z0-9\u00c0-\u1ef9-]+", "", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value or "story"


def story_slug(story: dict[str, Any]) -> str:
    metadata = story.get("story_metadata") or {}
    if isinstance(metadata, dict) and metadata.get("slug"):
        return safe_slug(str(metadata["slug"]))
    parsed = urlparse(str(story.get("story_url") or ""))
    tail = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    return safe_slug(tail or str(story.get("story_title") or story.get("story_id") or "story"))


def normalize_language(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def likely_raw_language(row: dict[str, Any]) -> str:
    raw_language = normalize_language(row.get("raw_language"))
    if raw_language:
        return raw_language
    story_language = normalize_language(row.get("story_language"))
    if story_language:
        return story_language
    return NON_VI_SOURCE_LANGUAGES.get(str(row.get("source_code") or ""), "")


def resolve_project_path(value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    return path


def read_text_sample(content: str | None, path_value: str | None, max_chars: int) -> str:
    if content:
        return content[:max_chars]
    path = resolve_project_path(path_value)
    if not path or not path.exists():
        return ""
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        return handle.read(max_chars)


def is_probably_vietnamese(text: str) -> bool:
    sample = re.sub(r"\s+", " ", text or "").strip()
    if len(sample) < 80:
        return False
    cjk_count = len(CJK_PATTERN.findall(sample))
    if cjk_count >= 8 or cjk_count / max(len(sample), 1) > 0.01:
        return False
    diacritic_count = len(VI_DIACRITIC_PATTERN.findall(sample))
    words = re.findall(r"[\wÀ-ỹ]+", sample.lower(), flags=re.UNICODE)
    if not words:
        return False
    vi_hits = len({word for word in words if word in VI_WORDS})
    en_hits = len({word for word in words if word in EN_WORDS})
    return diacritic_count >= 12 or vi_hits >= 4 or (vi_hits >= 2 and en_hits <= 2)


def is_probably_vietnamese_title(text: str) -> bool:
    sample = re.sub(r"\s+", " ", text or "").strip()
    if not sample:
        return False
    if CJK_PATTERN.search(sample):
        return False
    if VI_DIACRITIC_PATTERN.search(sample):
        return True
    words = re.findall(r"[\wÀ-ỹ]+", sample.lower(), flags=re.UNICODE)
    return len({word for word in words if word in VI_WORDS}) >= 1


def clean_model_text(value: str, *, max_len: int = 0) -> str:
    text = (value or "").strip()
    text = re.sub(r"^```(?:text|markdown)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip().strip("\"'“”‘’")
    text = re.sub(r"\s+", " ", text)
    if max_len > 0:
        return text[:max_len].strip()
    return text


def _truncate_context(value: str, max_chars: int = 2400) -> str:
    cleaned = re.sub(r"\s+\n", "\n", (value or "").strip())
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[:max_chars].rstrip() + "\n..."


def build_metadata_memory_context(
    *,
    story_id: str = "",
    story_slug_value: str = "",
    char_map_file: str = "",
    genre: str = "",
    story_memory_dir: str = "",
    reference_text: str = "",
    max_chars: int = 3500,
) -> str:
    """Compact story memory / glossary block for metadata translation prompts."""
    memory = load_story_memory(
        story_memory_dir=story_memory_dir,
        story_id=story_id,
        slug=story_slug_value,
        char_map_file=char_map_file,
    )
    memory = apply_seed_glossary_replacements(memory, genre)
    if not memory.loaded and not genre:
        return ""
    hint = reference_text.strip() or "story title description chapter heading glossary terms"
    block = build_story_memory_prompt(
        memory,
        hint,
        genre=genre,
        max_chars=max_chars,
        current_chapter=0,
    )
    if block:
        return f"story_memory_excerpt:\n{block}"
    return ""


def build_metadata_translation_context(
    *,
    story_id: str = "",
    story_slug_value: str = "",
    source_code: str = "",
    story_title: str = "",
    original_title: str = "",
    display_title: str = "",
    description: str = "",
    category: str = "",
    raw_language: str = "",
    char_map_file: str = "",
    story_memory_dir: str = "",
) -> str:
    """Build compact story context for title/description translation.

    Uses the same genre, char-map, and story-memory sources as chapter translate/polish
    so metadata strings stay terminology-consistent with body text.
    """
    effective_char_map = char_map_file or find_char_map_file(story_id=story_id, slug=story_slug_value)
    char_map_text = load_char_map(effective_char_map, story_id=story_id) if effective_char_map else ""
    genre = resolve_genre_from_context(
        category or "",
        raw_language=raw_language,
        source_code=source_code,
        char_map_file=effective_char_map,
        char_map=char_map_text,
        title=original_title or story_title,
        description=description,
    )
    lines = [
        f"source_code: {source_code or '-'}",
        f"raw_language: {raw_language or '-'}",
        f"genre: {genre or detect_genre(category or '', raw_language=raw_language, source_code=source_code) or '-'}",
        f"story_slug: {story_slug_value or '-'}",
        f"source_title: {original_title or story_title or '-'}",
    ]
    if display_title:
        lines.append(f"current_vi_title: {display_title}")
    if category:
        lines.append(f"category: {category}")
    if char_map_text:
        lines.append("char_map_excerpt:")
        lines.append(_truncate_context(char_map_text, 1800))
    ref = " ".join(p for p in (original_title or story_title, description) if p).strip()
    memory_block = build_metadata_memory_context(
        story_id=story_id,
        story_slug_value=story_slug_value,
        char_map_file=effective_char_map,
        genre=genre,
        story_memory_dir=story_memory_dir,
        reference_text=ref,
    )
    if memory_block:
        lines.append(memory_block)
    return "\n".join(lines)


def build_metadata_translation_context_from_row(row: dict[str, Any], args: argparse.Namespace) -> str:
    raw_language = likely_raw_language(row)
    slug = story_slug(row)
    char_map_file = getattr(args, "char_map_file", "") or find_char_map_file(
        story_id=str(row.get("story_id") or ""),
        slug=slug,
    )
    return build_metadata_translation_context(
        story_id=str(row.get("story_id") or ""),
        story_slug_value=slug,
        source_code=str(row.get("source_code") or ""),
        story_title=str(row.get("story_title") or ""),
        original_title=str(row.get("story_original_title") or ""),
        display_title=str(row.get("story_display_title") or ""),
        description=str(row.get("story_description") or row.get("source_description") or ""),
        category=str(row.get("story_category") or ""),
        raw_language=raw_language,
        char_map_file=char_map_file,
        story_memory_dir=getattr(args, "story_memory_dir", ""),
    )


def first_content_line(text: str) -> str:
    for line in (text or "").splitlines():
        cleaned = re.sub(r"\s+", " ", line).strip()
        if cleaned:
            return cleaned
    return ""


_PROSE_SENTENCE_RE = re.compile(r"[!?]\s+\S|[.]\s+[A-ZÀÁÂÃÈÉÊÌÍÒÓÔÕÙÚÝĐÀ-ỹ]")


def chapter_title_from_content(text: str, *, tts_clean: bool = True) -> str:
    """Derive chapter heading from polished/translated body (first line, TTS-safe).

    Single source of truth for chapter titles — matches what TTS reads first.
    """
    body = (text or "").strip()
    if not body:
        return ""
    if tts_clean:
        body = clean_for_audiobook_tts(body).strip()
    title = first_content_line(body)
    if not title or len(title) > 120:
        return ""
    if _PROSE_SENTENCE_RE.search(title):
        return ""
    return title


def maybe_update_translated_chapter_title(chapter_id: str, translated_text_content: str) -> str:
    title = chapter_title_from_content(translated_text_content)
    if not title:
        return ""
    repo.update_chapter_title(chapter_id, title)
    return title


def call_ollama_generate(
    *,
    base_url: str,
    model: str,
    prompt: str,
    temperature: float,
    num_ctx: int,
    timeout: int,
    retries: int,
    keep_alive: str,
) -> str:
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_ctx": num_ctx,
        },
        "keep_alive": keep_alive,
    }
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.post(f"{base_url.rstrip('/')}/api/generate", json=payload, timeout=timeout)
            response.raise_for_status()
            text = str(response.json().get("response") or "")
            if not text.strip():
                raise ValueError("Ollama returned empty response")
            return text
        except Exception as exc:
            last_error = exc
            log(f"[WARN] story metadata Ollama error attempt {attempt}/{retries}: {exc}")
    raise RuntimeError(f"Ollama failed after {retries} retries: {last_error}")


def translate_story_title(source_title: str, args: argparse.Namespace, *, context: str = "") -> str:
    raw = call_ollama_generate(
        base_url=args.ollama_url,
        model=args.story_model or args.translate_model,
        prompt=STORY_TITLE_PROMPT.format(title=source_title, context=_metadata_context_block(context)),
        temperature=0.15,
        num_ctx=max(4096, int(getattr(args, "num_ctx", 4096) or 4096)),
        timeout=args.ollama_timeout,
        retries=args.ollama_retries,
        keep_alive=args.keep_alive,
    )
    return clean_model_text(raw, max_len=180)


def translate_story_description(description: str, args: argparse.Namespace, *, context: str = "") -> str:
    raw = call_ollama_generate(
        base_url=args.ollama_url,
        model=args.story_model or args.translate_model,
        prompt=STORY_DESCRIPTION_PROMPT.format(
            description=description,
            context=_metadata_context_block(context),
        ),
        temperature=args.temperature,
        num_ctx=args.num_ctx,
        timeout=args.ollama_timeout,
        retries=args.ollama_retries,
        keep_alive=args.keep_alive,
    )
    return clean_model_text(raw)


def translate_story_author(author: str, args: argparse.Namespace) -> str:
    raw = call_ollama_generate(
        base_url=args.ollama_url,
        model=args.story_model or args.translate_model,
        prompt=STORY_AUTHOR_PROMPT.format(author=author),
        temperature=0.1,
        num_ctx=2048,
        timeout=args.ollama_timeout,
        retries=args.ollama_retries,
        keep_alive=args.keep_alive,
    )
    return clean_model_text(raw, max_len=180)


def translate_chapter_title(source_title: str, args: argparse.Namespace, *, context: str = "") -> str:
    raw = call_ollama_generate(
        base_url=args.ollama_url,
        model=args.story_model or args.translate_model,
        prompt=CHAPTER_TITLE_PROMPT.format(title=source_title, context=_metadata_context_block(context)),
        temperature=0.1,
        num_ctx=max(4096, int(getattr(args, "num_ctx", 4096) or 4096)),
        timeout=args.ollama_timeout,
        retries=args.ollama_retries,
        keep_alive=args.keep_alive,
    )
    return clean_model_text(raw, max_len=200)


def update_story_translation(
    story_id: str,
    *,
    display_title: str | None,
    description: str | None,
    original_description: str | None,
    model: str,
    author: str | None = None,
) -> None:
    metadata: dict[str, Any] = {
        "story_metadata_translated_to": "vi",
        "story_metadata_translation_model": model,
    }
    if original_description and description and original_description != description:
        metadata["original_description_before_vi_translate"] = original_description
    with connect() as conn:
        conn.execute(
            """
            UPDATE stories
            SET display_title = COALESCE(%(display_title)s, display_title),
                author = COALESCE(%(author)s, author),
                description = COALESCE(%(description)s, description),
                title_polished_at = CASE WHEN %(display_title)s IS NOT NULL THEN now() ELSE title_polished_at END,
                title_polish_model = CASE WHEN %(display_title)s IS NOT NULL THEN %(model)s ELSE title_polish_model END,
                metadata = metadata || %(metadata)s,
                updated_at = now()
            WHERE id = %(story_id)s
            """,
            {
                "story_id": story_id,
                "display_title": display_title,
                "author": author,
                "description": description,
                "model": model,
                "metadata": Jsonb(metadata),
            },
        )


def list_translation_candidates(args: argparse.Namespace) -> list[dict[str, Any]]:
    log(
        "[QUERY] scanning DB "
        f"limit={args.limit} source={','.join(args.source_code) if args.source_code else 'all'} "
        f"story_id={args.story_id or '-'} story_title={args.story_title or '-'} "
        f"chapters={args.from_chapter or '-'}..{args.to_chapter or '-'}"
    )
    query = """
        SELECT
            c.id AS chapter_id,
            c.story_id,
            c.chapter_number,
            c.title AS chapter_title,
            c.raw_language,
            c.raw_text_path,
            c.translated_text_path,
            c.polished_text_path,
            c.raw_text_content,
            c.translated_text_content,
            c.polished_text_content,
            c.is_downloaded,
            c.is_translated,
            c.is_polished,
            s.title AS story_title,
            s.original_title AS story_original_title,
            s.display_title AS story_display_title,
            s.author AS story_author,
            s.description AS story_description,
            s.source_url AS story_url,
            s.language AS story_language,
            s.category AS story_category,
            s.metadata AS story_metadata,
            src.code AS source_code
        FROM chapters c
        JOIN stories s ON s.id = c.story_id
        JOIN sources src ON src.id = s.source_id
        WHERE s.is_active = TRUE
          AND c.is_downloaded = TRUE
          AND (c.raw_text_path IS NOT NULL OR c.raw_text_content IS NOT NULL)
          AND (
                lower(COALESCE(NULLIF(c.raw_language, ''), NULLIF(s.language, ''), '')) <> ALL((%(vi_codes)s)::text[])
                OR src.code = ANY((%(non_vi_sources)s)::text[])
              )
    """
    params: dict[str, Any] = {
        "vi_codes": sorted(VI_LANGUAGE_CODES),
        "non_vi_sources": sorted(NON_VI_SOURCE_LANGUAGES),
    }
    if args.source_code:
        query += " AND src.code = ANY((%(source_codes)s)::text[])"
        params["source_codes"] = args.source_code
    if args.story_id:
        query += " AND s.id = %(story_id)s"
        params["story_id"] = args.story_id
    if args.story_url:
        query += " AND rtrim(s.source_url, '/') = %(story_url)s"
        params["story_url"] = args.story_url.rstrip("/")
    if args.story_title:
        query += " AND (s.title ILIKE %(story_title)s OR s.original_title ILIKE %(story_title)s OR s.display_title ILIKE %(story_title)s)"
        params["story_title"] = f"%{args.story_title}%"
    if args.story_slug:
        query += " AND (s.metadata->>'slug' = %(story_slug)s OR s.source_url ILIKE %(story_slug_like)s OR c.raw_text_path ILIKE %(story_slug_path)s)"
        params["story_slug"] = args.story_slug
        params["story_slug_like"] = f"%{args.story_slug}%"
        params["story_slug_path"] = f"%/{args.story_slug}/%"
    if args.from_chapter:
        query += " AND c.chapter_number >= %(from_chapter)s"
        params["from_chapter"] = args.from_chapter
    if args.to_chapter:
        query += " AND c.chapter_number <= %(to_chapter)s"
        params["to_chapter"] = args.to_chapter
    query += f" ORDER BY {repo.STORY_PRIORITY_ORDER_SQL}, c.chapter_number ASC"
    if args.limit > 0:
        query += " LIMIT %(limit)s"
        params["limit"] = args.limit

    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]


def needs_translation(row: dict[str, Any], args: argparse.Namespace) -> tuple[bool, str]:
    has_translated_output = bool(row.get("translated_text_path") or row.get("translated_text_content"))
    if not row.get("is_translated") or not has_translated_output:
        return True, "missing_translation"
    if getattr(args, "overwrite_translation", False):
        return True, "overwrite"
    if args.only_missing_translation or not args.check_polished_language:
        return False, ""
    if not row.get("is_polished") and not row.get("polished_text_path") and not row.get("polished_text_content"):
        return False, ""
    sample = read_text_sample(row.get("polished_text_content"), row.get("polished_text_path"), args.sample_chars)
    if sample and not is_probably_vietnamese(sample):
        return True, "polished_not_vi"
    return False, ""


def build_model_args(
    args: argparse.Namespace,
    model: str,
    max_chars: int,
    char_map_file: str = "",
    genre: str = "",
    story_id: str = "",
    story_slug_value: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        ollama_url=args.ollama_url,
        model=model,
        temperature=args.temperature,
        num_ctx=args.num_ctx,
        timeout=args.ollama_timeout,
        retries=args.ollama_retries,
        keep_alive=args.keep_alive,
        max_chars_per_chunk=max_chars,
        prompt_profile=args.prompt_profile,
        polish_mode=args.polish_mode,
        min_output_ratio=args.min_output_ratio,
        genre=genre or getattr(args, "genre", ""),
        story_id=story_id or str(getattr(args, "story_id", "") or ""),
        story_slug=story_slug_value
        or str(getattr(args, "story_slug", "") or getattr(args, "target_slug", "") or ""),
        char_map_file=char_map_file or getattr(args, "char_map_file", ""),
        story_memory_dir=getattr(args, "story_memory_dir", ""),
        fail_on_story_memory_issues=getattr(args, "fail_on_story_memory_issues", False),
    )


def enqueue_job(row: dict[str, Any], args: argparse.Namespace, reason: str) -> dict[str, Any]:
    slug = story_slug(row)
    chapter_num = int(row.get("chapter_number") or 0)
    chapter_stem = Path(row["raw_text_path"]).stem if row.get("raw_text_path") else f"chapter{chapter_num:04d}"
    raw_language = likely_raw_language(row)
    char_map_file = getattr(args, "char_map_file", "") or find_char_map_file(
        story_id=str(row.get("story_id") or ""),
        slug=slug,
    )
    genre = getattr(args, "genre", "") or resolve_genre_from_context(
        str(row.get("story_category") or ""),
        raw_language=raw_language,
        source_code=str(row.get("source_code") or ""),
        char_map_file=char_map_file,
    )
    job = repo.enqueue_chapter_job(
        "polish_chapter",
        row["chapter_id"],
        story_id=row["story_id"],
        source_code=row["source_code"],
        model=args.translate_model,
        input_path=None,
        output_path=None,
        payload={
            "raw_language": raw_language,
            "story_slug": slug,
            "chapter_number": row["chapter_number"],
            "chapter_title": row.get("chapter_title") or chapter_stem,
            "source_chapter_title": row.get("chapter_title") or chapter_stem,
            "translate_story_metadata": True,
            "source_story_title": row.get("story_original_title") or row.get("story_title") or "",
            "source_story_author": (row.get("story_metadata") or {}).get("source_author")
            or row.get("story_author")
            or "",
            "source_story_description": (row.get("story_metadata") or {}).get("source_description")
            or row.get("story_description")
            or "",
            "post_translate": args.post_translate,
            "translate_from_db_reason": reason,
            "genre": genre,
            "char_map_file": char_map_file,
        },
        max_attempts=args.max_attempts,
    )
    log(
        "[JOB] upserted "
        f"job={job.get('id')} status={job.get('status')} story={slug} "
        f"chapter={row['chapter_number']} raw_language={raw_language} reason={reason}"
    )
    if args.force_requeue_done and job.get("status") == "done":
        with connect() as conn:
            updated = conn.execute(
                """
                UPDATE story_jobs
                SET status = 'pending',
                    attempts = 0,
                    run_after = now(),
                    locked_by = NULL,
                    locked_at = NULL,
                    finished_at = NULL,
                    last_error = NULL,
                    updated_at = now()
                WHERE id = %s
                RETURNING *
                """,
                (job["id"],),
            ).fetchone()
            if updated is not None:
                job = dict(updated)
                log(f"[JOB] force requeued job={job.get('id')} status={job.get('status')}")
    return job


def select_rows(args: argparse.Namespace) -> list[tuple[dict[str, Any], str]]:
    rows = []
    skipped_missing_file = 0
    skipped_not_needed = 0
    candidates = list_translation_candidates(args)
    log(f"[QUERY] candidates={len(candidates)}")
    for row in candidates:
        needed, reason = needs_translation(row, args)
        if not needed:
            skipped_not_needed += 1
            continue
        raw_path = resolve_project_path(row.get("raw_text_path"))
        has_db_content = bool(row.get("raw_text_content"))
        if args.require_input_file and (not raw_path or not raw_path.exists()) and not has_db_content:
            skipped_missing_file += 1
            log(f"[SKIP] missing raw file chapter={row['chapter_id']} path={row.get('raw_text_path')}")
            continue
        rows.append((row, reason))
    if skipped_not_needed:
        log(f"[QUERY] skipped_not_needed={skipped_not_needed}")
    if skipped_missing_file:
        log(f"[QUERY] skipped_missing_raw_files={skipped_missing_file}")
    log(f"[QUERY] selected={len(rows)}")
    return rows


def translate_story_metadata_for_rows(rows: list[tuple[dict[str, Any], str]], args: argparse.Namespace) -> None:
    if not args.translate_story_metadata:
        log("[STORY] metadata translation disabled")
        return
    seen: set[str] = set()
    updated = 0
    skipped = 0
    for row, _reason in rows:
        story_id = str(row["story_id"])
        if story_id in seen:
            continue
        seen.add(story_id)
        title_source = str(row.get("story_original_title") or row.get("story_title") or "").strip()
        display_title = str(row.get("story_display_title") or "").strip()
        story_metadata = row.get("story_metadata") or {}
        author_source = str(story_metadata.get("source_author") or row.get("story_author") or "").strip()
        description_source = str(row.get("story_description") or "").strip()
        translation_context = build_metadata_translation_context_from_row(row, args)
        next_title: str | None = None
        next_author: str | None = None
        next_description: str | None = None

        if title_source and (
            args.overwrite_story_metadata
            or not display_title
            or not is_probably_vietnamese_title(display_title)
        ):
            if args.dry_run:
                log(f"[DRY] story title {row['source_code']} {story_slug(row)}: {title_source}")
            else:
                log(f"[STORY] translating title story={story_slug(row)} source={row['source_code']}")
                next_title = translate_story_title(title_source, args, context=translation_context)
                log(f"[STORY] title {title_source} -> {next_title}")

        if author_source and (
            args.overwrite_story_metadata
            or story_metadata.get("story_author_translated_to") != "vi"
        ):
            if args.dry_run:
                log(f"[DRY] story author {row['source_code']} {story_slug(row)}: {author_source}")
            else:
                log(f"[STORY] translating author story={story_slug(row)} source={row['source_code']}")
                next_author = translate_story_author(author_source, args)
                log(f"[STORY] author {author_source} -> {next_author}")

        if description_source and (
            args.overwrite_story_metadata
            or not is_probably_vietnamese(description_source)
        ):
            if args.dry_run:
                log(f"[DRY] story description {row['source_code']} {story_slug(row)} chars={len(description_source)}")
            else:
                log(
                    f"[STORY] translating description story={story_slug(row)} "
                    f"source={row['source_code']} chars={len(description_source)}"
                )
                next_description = translate_story_description(description_source, args, context=translation_context)
                log(f"[STORY] description translated story={story_slug(row)} chars={len(next_description)}")

        if args.dry_run or (next_title is None and next_author is None and next_description is None):
            skipped += 1
            continue
        update_story_translation(
            story_id,
            display_title=next_title,
            author=next_author,
            description=next_description,
            original_description=description_source,
            model=args.story_model or args.translate_model,
        )
        if next_author:
            repo.update_story_metadata(
                story_id,
                {"source_author": author_source, "story_author_translated_to": "vi"},
            )
        updated += 1
        log(
            f"[STORY] updated story={story_slug(row)} "
            f"title={'yes' if next_title else 'no'} author={'yes' if next_author else 'no'} "
            f"description={'yes' if next_description else 'no'}"
        )
    log(f"[STORY] metadata summary stories={len(seen)} updated={updated} skipped={skipped}")


def _resolve_raw_input(row: dict[str, Any]) -> tuple[Path, "tempfile.NamedTemporaryFile | None"]:
    """Return (path_to_raw_text, temp_file_or_None). Falls back to raw_text_content in DB."""
    raw_path = resolve_project_path(row.get("raw_text_path"))
    if raw_path and raw_path.exists():
        return raw_path, None
    content = row.get("raw_text_content") or ""
    if not content:
        raise FileNotFoundError(
            f"Input file not found and no raw_text_content in DB: {row.get('raw_text_path')!r}"
        )
    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".txt",
        prefix=f"translate_in_{int(row.get('chapter_number') or 0):04d}_",
        delete=False,
    )
    tmp.write(content)
    tmp.flush()
    log(f"[TMP] input from DB -> {tmp.name}")
    return Path(tmp.name), tmp



def _process_row_queue_mode(row: dict[str, Any], reason: str, args: argparse.Namespace, *, index: int, total: int) -> bool:
    """Translate inline rồi enqueue polish_chapter job (raw_language=vi) cho polish_worker xử lý riêng."""
    started = time.monotonic()
    slug = story_slug(row)
    label = f"[{index}/{total}] {row['source_code']} {slug} chapter{int(row['chapter_number']):04d} reason={reason}"
    _tmp_in = None
    _tmp_translated: Path | None = None
    try:
        chapter_num = int(row.get("chapter_number") or 0)
        stable_name = f"chapter{chapter_num:04d}.txt"
        raw_path, _tmp_in = _resolve_raw_input(row)
        raw_language = likely_raw_language(row)
        char_map_file = getattr(args, "char_map_file", "") or find_char_map_file(
            story_id=str(row.get("story_id") or ""),
            slug=slug,
        )
        genre = getattr(args, "genre", "") or resolve_genre_from_context(
            str(row.get("story_category") or ""),
            raw_language=raw_language,
            source_code=str(row.get("source_code") or ""),
            char_map_file=char_map_file,
        )
        if char_map_file:
            log(f"[CHAR_MAP] {char_map_file}")

        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False,
                                         prefix=f"translate_{chapter_num:04d}_") as _tf:
            _tmp_translated = Path(_tf.name)
        log(f"[TRANSLATE] start {label} model={args.translate_model} max_chars={args.translate_max_chars_per_chunk}")
        translate_file(
            raw_path,
            _tmp_translated,
            build_model_args(
                args,
                args.translate_model,
                args.translate_max_chars_per_chunk,
                char_map_file,
                genre,
                story_id=str(row.get("story_id") or ""),
                story_slug_value=slug,
            ),
        )
        log(f"[TRANSLATE] done {label}")

        fake_job = {"chapter_id": row["chapter_id"], "story_id": row["story_id"]}
        translated_text_content = read_formatted_output(_tmp_translated, fake_job, write_back=True, label="translated")
        # Translate chapter title: LLM preferred (source EN title), fallback to first-line heuristic
        source_chapter_title = str(row.get("chapter_title") or "").strip()
        translated_chapter_title = ""
        if source_chapter_title:
            try:
                translated_chapter_title = translate_chapter_title(
                    source_chapter_title,
                    args,
                    context=build_metadata_translation_context_from_row(row, args),
                )
                if translated_chapter_title:
                    repo.update_chapter_title(row["chapter_id"], translated_chapter_title)
            except Exception as _exc:  # noqa: BLE001
                log(f"[TITLE] LLM chapter title failed ({_exc}), falling back to first-line")
        if not translated_chapter_title:
            translated_chapter_title = maybe_update_translated_chapter_title(row["chapter_id"], translated_text_content)
        if translated_chapter_title:
            log(f"[DB] chapter title translated {label}: {translated_chapter_title}")

        repo.update_chapter_text_outputs(
            row["chapter_id"],
            translated_text_path=None,
            translated_text_content=translated_text_content,
            clear_audio=getattr(args, "overwrite_translation", False),
        )

        polish_job = repo.enqueue_chapter_job(
            "polish_chapter",
            row["chapter_id"],
            story_id=row["story_id"],
            source_code=row["source_code"],
            model=args.vi_model,
            input_path=None,
            output_path=None,
            payload={
                "raw_language": "vi",
                "is_post_translate": True,
                "story_slug": slug,
                "chapter_number": row["chapter_number"],
                "chapter_title": row.get("chapter_title") or Path(stable_name).stem,
                "source_chapter_title": row.get("chapter_title") or Path(stable_name).stem,
                "genre": genre,
                "char_map_file": char_map_file,
            },
            max_attempts=args.max_attempts,
        )
        if args.force_requeue_done and polish_job.get("status") == "done":
            with connect() as conn:
                updated = conn.execute(
                    """
                    UPDATE story_jobs SET status='pending', attempts=0, run_after=now(),
                        locked_by=NULL, locked_at=NULL, finished_at=NULL, last_error=NULL, updated_at=now()
                    WHERE id=%s RETURNING *
                    """,
                    (polish_job["id"],),
                ).fetchone()
                if updated is not None:
                    polish_job = dict(updated)
                    log(f"[JOB] force requeued job={polish_job.get('id')} status={polish_job.get('status')}")

        elapsed = time.monotonic() - started
        log(f"[DONE] {label} polish_job={polish_job.get('id')} status={polish_job.get('status')} elapsed={elapsed:.1f}s")
        return True
    except Exception as exc:
        elapsed = time.monotonic() - started
        log(f"[ERROR] {label} elapsed={elapsed:.1f}s {type(exc).__name__}: {exc}")
        if args.stop_on_error:
            raise
        return False
    finally:
        for _tmp in (_tmp_in, _tmp_translated):
            if _tmp is not None:
                try:
                    import os; os.unlink(_tmp.name if hasattr(_tmp, "name") else str(_tmp))
                except OSError:
                    pass


def process_row(row: dict[str, Any], reason: str, args: argparse.Namespace, *, index: int, total: int) -> bool:
    started = time.monotonic()
    slug = story_slug(row)
    label = f"[{index}/{total}] {row['source_code']} {slug} chapter{int(row['chapter_number']):04d} reason={reason}"
    if args.dry_run:
        log(
            f"[DRY] {label} raw_path={row.get('raw_text_path')} "
            f"translated={row.get('translated_text_path') or '-'} polished={row.get('polished_text_path') or '-'}"
        )
        return True
    if args.post_translate == "queue":
        return _process_row_queue_mode(row, reason, args, index=index, total=total)
    # Resolve genre before enqueuing so it can be logged with the job.
    _raw_language = likely_raw_language(row)
    _char_map_file = getattr(args, "char_map_file", "") or find_char_map_file(
        story_id=str(row.get("story_id") or ""),
        slug=slug,
    )
    effective_genre = getattr(args, "genre", "") or resolve_genre_from_context(
        str(row.get("story_category") or ""),
        raw_language=_raw_language,
        source_code=str(row.get("source_code") or ""),
        char_map_file=_char_map_file,
    )

    job = enqueue_job(row, args, reason)
    log(f"[START] {label} job={job.get('id')} genre={effective_genre or '(default)'}")
    _tmp_in = None
    _tmp_translated: Path | None = None
    _tmp_polished: Path | None = None
    try:
        chapter_num = int(row.get("chapter_number") or 0)
        stable_name = f"chapter{chapter_num:04d}.txt"
        raw_path, _tmp_in = _resolve_raw_input(row)
        effective_char_map = (job.get("payload") or {}).get("char_map_file") or getattr(args, "char_map_file", "") or find_char_map_file(
            story_id=str(row.get("story_id") or ""),
            slug=slug,
        )
        if effective_char_map:
            log(f"[CHAR_MAP] {effective_char_map}")

        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False,
                                         prefix=f"translate_{chapter_num:04d}_") as _tf:
            _tmp_translated = Path(_tf.name)
        log(
            f"[TRANSLATE] start {label} model={args.translate_model} "
            f"max_chars={args.translate_max_chars_per_chunk}"
        )
        translate_file(
            raw_path,
            _tmp_translated,
            build_model_args(
                args,
                args.translate_model,
                args.translate_max_chars_per_chunk,
                effective_char_map,
                effective_genre,
                story_id=str(row.get("story_id") or ""),
                story_slug_value=slug,
            ),
        )
        log(f"[TRANSLATE] done {label}")
        log(f"[FORMAT] translated start {label}")
        translated_text_content = read_formatted_output(_tmp_translated, job, write_back=True, label="translated")
        log(f"[FORMAT] translated done {label} chars={len(translated_text_content)}")
        # Translate chapter title: LLM preferred (source EN title), fallback to first-line heuristic
        _src_ch_title = str(row.get("chapter_title") or "").strip()
        translated_chapter_title = ""
        if _src_ch_title:
            try:
                translated_chapter_title = translate_chapter_title(
                    _src_ch_title,
                    args,
                    context=build_metadata_translation_context_from_row(row, args),
                )
                if translated_chapter_title:
                    repo.update_chapter_title(row["chapter_id"], translated_chapter_title)
            except Exception as _exc:  # noqa: BLE001
                log(f"[TITLE] LLM chapter title failed ({_exc}), falling back to first-line")
        if not translated_chapter_title:
            translated_chapter_title = maybe_update_translated_chapter_title(row["chapter_id"], translated_text_content)
        if translated_chapter_title:
            log(f"[DB] chapter title translated {label}: {translated_chapter_title}")

        polished_text_content = None
        if args.post_translate == "copy" and args.write_polished_copy:
            polished_text_content = clean_for_audiobook_tts(translated_text_content).strip() + "\n"
            log(f"[COPY] translated output reused as polished content chars={len(polished_text_content)}")
        elif args.post_translate == "copy":
            log(f"[SKIP] polished output disabled; translated only")
        else:
            with tempfile.NamedTemporaryFile(suffix=".txt", delete=False,
                                             prefix=f"polish_{chapter_num:04d}_") as _pf:
                _tmp_polished = Path(_pf.name)
            log(
                f"[POLISH] start {label} model={args.vi_model} "
                f"mode={args.polish_mode} max_chars={args.polish_max_chars_per_chunk}"
            )
            polish_file(
                _tmp_translated,
                _tmp_polished,
                build_model_args(
                    args,
                    args.vi_model,
                    args.polish_max_chars_per_chunk,
                    effective_char_map,
                    effective_genre,
                    story_id=str(row.get("story_id") or ""),
                    story_slug_value=slug,
                ),
            )
            log(f"[POLISH] done {label}")
            log(f"[FORMAT] polished start {label}")
            polished_text_content = read_formatted_polished_output(_tmp_polished, job, write_back=True) if _tmp_polished.exists() else None
            log(f"[FORMAT] polished done {label} chars={len(polished_text_content or '')}")
        log(f"[DB] update chapter outputs start {label}")
        repo.update_chapter_text_outputs(
            row["chapter_id"],
            translated_text_path=None,
            polished_text_path=None,
            translated_text_content=translated_text_content,
            polished_text_content=polished_text_content,
            clear_audio=getattr(args, "overwrite_translation", False),
        )
        log(f"[DB] update chapter outputs done {label}")
        repo.complete_story_job(
            job["id"],
            result_payload={
                "raw_language": likely_raw_language(row),
                "translated_chapter_title": translated_chapter_title or None,
                "translate_from_db_reason": reason,
            },
        )
        elapsed = time.monotonic() - started
        log(f"[DONE] {label} elapsed={elapsed:.1f}s")
        return True
    except Exception as exc:
        elapsed = time.monotonic() - started
        log(f"[ERROR] {label} elapsed={elapsed:.1f}s {type(exc).__name__}: {exc}")
        if job.get("id"):
            repo.fail_story_job(job["id"], str(exc), retry_delay_seconds=args.retry_delay)
        if args.stop_on_error:
            raise
        return False
    finally:
        for _tmp in (_tmp_in, _tmp_translated, _tmp_polished):
            if _tmp is not None:
                try:
                    import os; os.unlink(_tmp.name if hasattr(_tmp, "name") else str(_tmp))
                except OSError:
                    pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Translate trực tiếp các chapter raw không phải tiếng Việt trong DB chưa có translated output."
    )
    parser.add_argument("--source-code", action="append", default=[], help="Lọc source_code. Có thể truyền nhiều lần.")
    parser.add_argument("--story-id")
    parser.add_argument("--story-url")
    parser.add_argument("--story-title")
    parser.add_argument("--story-slug")
    parser.add_argument("--from-chapter", type=int, default=0)
    parser.add_argument("--to-chapter", type=int, default=0)
    parser.add_argument("--limit", type=int, default=20, help="0 = không giới hạn.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--stop-on-error", action="store_true")
    parser.add_argument("--retry-delay", type=int, default=120)
    parser.add_argument("--require-input-file", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument(
        "--include-polished-not-vi",
        action="store_true",
        help="Ngoài chapter chưa translated, kiểm tra cả polished output có vẻ không phải tiếng Việt để dịch lại.",
    )
    parser.add_argument("--sample-chars", type=int, default=4000)
    parser.add_argument("--force-requeue-done", action="store_true")
    parser.add_argument("--polished-output-root", default="story_data/polished")
    parser.add_argument("--translated-output-root", default="story_data/translated")
    parser.add_argument("--vi-model", default="qwen3:14b")
    parser.add_argument("--translate-model", default="qwen3:14b")
    parser.add_argument("--story-model", default="", help="Model dịch title/description story. Mặc định dùng --translate-model.")
    parser.add_argument(
        "--translate-story-metadata",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Dịch display_title và description của story liên quan. Mặc định bật.",
    )
    parser.add_argument(
        "--overwrite-story-metadata",
        action="store_true",
        help="Dịch lại cả title/description story dù hiện tại có vẻ đã là tiếng Việt.",
    )
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--overwrite-translation", action="store_true")
    parser.add_argument("--overwrite-polish", action="store_true")
    parser.add_argument(
        "--write-polished-copy",
        action="store_true",
        help="Khi --post-translate copy, ghi thêm bản translated sang polished. Mặc định tắt: chỉ translate.",
    )
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--temperature", type=float, default=0.25)
    parser.add_argument("--num-ctx", type=int, default=4096)
    parser.add_argument("--ollama-timeout", type=int, default=600)
    parser.add_argument("--ollama-retries", type=int, default=3)
    parser.add_argument("--keep-alive", default="24h")
    parser.add_argument("--prompt-profile", choices=("fast", "full"), default="full")
    parser.add_argument("--polish-mode", choices=("llm", "clean"), default="llm")
    parser.add_argument(
        "--post-translate",
        choices=("polish", "copy", "queue"),
        default="queue",
        help=(
            "Sau khi dịch: queue=enqueue polish_chapter job cho polish_worker (mặc định); "
            "polish=chạy LLM polish ngay inline; copy=copy bản dịch sang polished output."
        ),
    )
    parser.add_argument("--polish-max-chars-per-chunk", type=int, default=5000)
    parser.add_argument("--translate-max-chars-per-chunk", type=int, default=2500)
    parser.add_argument(
        "--min-output-ratio",
        type=float,
        default=0.70,
        help=(
            "Ngưỡng fallback: nếu output ngắn hơn X%% input (ký tự, bỏ whitespace), dùng lại chunk raw. "
            "0.70 = an toàn; 0 = tắt kiểm tra."
        ),
    )
    parser.add_argument(
        "--genre",
        default="",
        help="Thể loại: tien_hiep, huyen_huyen, he_thong, kiem_hiep, do_thi, xuyen_khong, mat_the, vong_du, lang_man, western_fantasy.",
    )
    parser.add_argument(
        "--char-map-file",
        default="",
        help=(
            "File nhân vật (character map) inject vào system prompt khi polish. "
            "VD: story_data/char_maps/21180-vinh-thoai-hiep-si.txt"
        ),
    )
    parser.add_argument(
        "--story-memory-dir",
        default="",
        help=(
            "Root story memory hoặc thư mục memory cụ thể. Nếu bỏ trống, script tự tìm theo "
            "story_data/story_memory/{story_id}-{slug} từ char-map/story slug."
        ),
    )
    parser.add_argument(
        "--fail-on-story-memory-issues",
        action="store_true",
        help="Nếu story memory QA phát hiện lỗi tên/thuật ngữ/register, fail chapter thay vì chỉ cảnh báo.",
    )
    parser.add_argument("--worker-id", default=f"translate-db-{socket.gethostname()}")
    parser.add_argument(
        "--log-file",
        default="story_data/logs/translate_chapters_from_db.log",
        help="Ghi mirror console log vào file. Truyền chuỗi rỗng để tắt.",
    )
    args = parser.parse_args()
    configure_logging(args.log_file)

    args.only_missing_translation = not args.include_polished_not_vi
    args.check_polished_language = args.include_polished_not_vi

    started = time.monotonic()
    log(
        "[RUN] start "
        f"worker={args.worker_id} dry_run={args.dry_run} limit={args.limit} "
        f"translate_model={args.translate_model} vi_model={args.vi_model} "
        f"post_translate={args.post_translate} overwrite_translation={args.overwrite_translation} "
        f"overwrite_polish={args.overwrite_polish} write_polished_copy={args.write_polished_copy} "
        f"log_file={args.log_file or '-'}"
    )
    rows = select_rows(args)
    log(f"[RUN] need_translation={len(rows)}")
    translate_story_metadata_for_rows(rows, args)
    ok = 0
    failed = 0
    for index, (row, reason) in enumerate(rows, start=1):
        if process_row(row, reason, args, index=index, total=len(rows)):
            ok += 1
        else:
            failed += 1
    if args.dry_run:
        log("[RUN] dry run only. No chapters changed.")
    elapsed = time.monotonic() - started
    log(f"[RUN] done ok={ok} failed={failed} elapsed={elapsed:.1f}s")


if __name__ == "__main__":
    main()
