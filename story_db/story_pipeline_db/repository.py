from __future__ import annotations

import re
import unicodedata
from typing import Any, Sequence

from psycopg.types.json import Jsonb

from .db import connect

# Sources whose default raw language is not Vietnamese (translate+polish path).
NON_VI_SOURCE_CODES = (
    "qidian",
    "royalroad",
    "novelbin",
    "freewebnovel",
    "lightnovelpub",
    "skydemonorder",
    "wetriedtls",
    "fanmtl",
    "novelfire",
    "novelhub",
    "naver_series",
    "jadescrolls",
)
NON_VI_RAW_LANGUAGE_CODES = ("en", "zh", "ko", "ja", "zh-cn", "zh-tw")

# English-origin sources that support bilingual reader (EN raw + VI polish).
ENGLISH_LEARNING_SOURCE_CODES = (
    "royalroad",
    "lightnovelpub",
    "novelbin",
    "freewebnovel",
    "novelhub",
    "skydemonorder",
    "wetriedtls",
    "fanmtl",
    "novelfire",
)

_non_vi_sources_sql = ", ".join(f"'{code}'" for code in NON_VI_SOURCE_CODES)
_non_vi_langs_sql = ", ".join(f"'{code}'" for code in NON_VI_RAW_LANGUAGE_CODES)

# Rank tiers: 1-3, 4-6, 7-9, … so top-ranked stories finish before lower tiers.
RANK_TIER_SIZE = 3

_NON_VI_PRIORITY_CASE = f"""
CASE
  WHEN j.source_code IN ({_non_vi_sources_sql}) THEN 0
  WHEN lower(COALESCE(j.payload->>'raw_language', s.language, '')) IN ({_non_vi_langs_sql}) THEN 0
  ELSE 1
END
""".strip()

_NON_VI_STORY_PRIORITY_CASE = f"""
CASE
  WHEN src.code IN ({_non_vi_sources_sql}) THEN 0
  WHEN lower(COALESCE(s.language, '')) IN ({_non_vi_langs_sql}) THEN 0
  ELSE 1
END
""".strip()

_RANK_TIER_CASE = f"""
CASE
  WHEN s.rank_position IS NULL OR s.rank_position < 1 THEN 2147483647
  ELSE (s.rank_position - 1) / {RANK_TIER_SIZE}
END
""".strip()

_CHAPTER_ORDER_CASE = """
COALESCE(
  c.chapter_number,
  NULLIF((j.payload->>'chapter_number')::int, 0),
  2147483647
)
""".strip()

STORY_PRIORITY_ORDER_SQL = f"""
{_NON_VI_STORY_PRIORITY_CASE} ASC,
{_RANK_TIER_CASE} ASC,
s.rank_position ASC NULLS LAST,
src.code ASC,
s.updated_at DESC,
s.created_at DESC
""".strip()


def rank_tier(position: int | None, *, tier_size: int = RANK_TIER_SIZE) -> int:
    if position is None or position < 1:
        return 2_147_483_647
    return (int(position) - 1) // max(1, tier_size)


def is_non_vi_story(*, source_code: str, language: str | None = None) -> bool:
    if source_code in NON_VI_SOURCE_CODES:
        return True
    lang = (language or "").strip().lower()
    return lang in NON_VI_RAW_LANGUAGE_CODES


def story_priority_sort_key(
    *,
    source_code: str,
    rank_position: int | None,
    language: str | None = None,
) -> tuple[int, int, int, str]:
    """Python mirror of STORY_PRIORITY_ORDER_SQL for in-memory sorts."""
    non_vi = 0 if is_non_vi_story(source_code=source_code, language=language) else 1
    rank = int(rank_position) if rank_position and rank_position > 0 else 2_147_483_647
    return (non_vi, rank_tier(rank_position), rank, source_code)


def slugify_category(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.strip().lower())
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")
    if slug:
        return slug
    return re.sub(r"\s+", "-", value.strip().lower()).strip("-") or "category"


def normalize_category_name(value: str) -> str:
    compact = re.sub(r"\s+", " ", value or "").strip().lower()
    ascii_value = unicodedata.normalize("NFKD", compact).encode("ascii", "ignore").decode("ascii")
    return ascii_value if ascii_value else compact


def split_category_names(value: str | None) -> list[str]:
    if not value:
        return []
    names = [
        re.sub(r"\s+", " ", item).strip()
        for item in re.split(r"[,，/|;；、·]+", value)
        if re.sub(r"\s+", " ", item).strip()
    ]
    return list(dict.fromkeys(names))


def upsert_source(code: str, name: str, base_url: str) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute(
            """
            INSERT INTO sources (code, name, base_url)
            VALUES (%s, %s, %s)
            ON CONFLICT (code)
            DO UPDATE SET name = EXCLUDED.name, base_url = EXCLUDED.base_url, updated_at = now()
            RETURNING *
            """,
            (code, name, base_url),
        ).fetchone()
        assert row is not None
        return dict(row)


def get_source(code: str) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM sources WHERE code = %s", (code,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown source: {code}")
        return dict(row)


def get_story_by_id(story_id: str) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT s.*, src.code AS source_code, src.base_url AS source_base_url
            FROM stories s
            JOIN sources src ON src.id = s.source_id
            WHERE s.id = %s
            """,
            (story_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Unknown story id: {story_id}")
        return dict(row)


def get_chapter_by_story_number(story_id: str, chapter_number: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT id, chapter_number, raw_text_content, translated_text_content, polished_text_content
            FROM chapters
            WHERE story_id = %(story_id)s::uuid AND chapter_number = %(chapter_number)s
            """,
            {"story_id": story_id, "chapter_number": chapter_number},
        ).fetchone()
        return dict(row) if row else None


def find_stories(
    *,
    title_contains: str | None = None,
    source_url: str | None = None,
    source_codes: list[str] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    query = """
        SELECT s.*, src.code AS source_code, src.base_url AS source_base_url
        FROM stories s
        JOIN sources src ON src.id = s.source_id
        WHERE s.is_active = TRUE
    """
    params: list[Any] = []
    if title_contains:
        query += " AND (s.title ILIKE %s OR s.original_title ILIKE %s OR s.display_title ILIKE %s)"
        needle = f"%{title_contains}%"
        params.extend([needle, needle, needle])
    if source_url:
        query += " AND rtrim(s.source_url, '/') = %s"
        params.append(source_url.rstrip("/"))
    if source_codes:
        query += " AND src.code = ANY(%s)"
        params.append(source_codes)
    query += " ORDER BY s.updated_at DESC, s.created_at DESC"
    if limit > 0:
        query += " LIMIT %s"
        params.append(limit)

    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]


def list_bilingual_ready_stories(
    *,
    source_codes: Sequence[str] | None = None,
    min_polished: int = 1,
    min_bilingual_chapters: int = 1,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Stories with both EN raw and VI polished text on the same chapter (bilingual reader)."""
    codes = list(source_codes or ENGLISH_LEARNING_SOURCE_CODES)
    min_polished = max(0, int(min_polished))
    min_bilingual = max(1, int(min_bilingual_chapters))
    limit = max(1, int(limit))

    query = """
        SELECT
          s.id,
          s.title,
          src.code AS source_code,
          s.rank_position,
          COUNT(*) FILTER (WHERE c.is_polished) AS polished_count,
          COUNT(*) FILTER (
            WHERE length(COALESCE(c.raw_text_content, '')) > 100
          ) AS raw_count,
          COUNT(*) FILTER (
            WHERE c.is_polished
              AND length(COALESCE(c.raw_text_content, '')) > 100
              AND length(COALESCE(c.polished_text_content, '')) > 100
          ) AS bilingual_ready_count,
          MAX(c.chapter_number) FILTER (
            WHERE c.is_polished
              AND length(COALESCE(c.raw_text_content, '')) > 100
              AND length(COALESCE(c.polished_text_content, '')) > 100
          ) AS max_bilingual_chapter
        FROM stories s
        JOIN sources src ON src.id = s.source_id
        JOIN chapters c ON c.story_id = s.id
        WHERE s.is_active = TRUE
          AND src.code = ANY(%s)
        GROUP BY s.id, s.title, src.code, s.rank_position
        HAVING COUNT(*) FILTER (WHERE c.is_polished) >= %s
           AND COUNT(*) FILTER (
             WHERE c.is_polished
               AND length(COALESCE(c.raw_text_content, '')) > 100
               AND length(COALESCE(c.polished_text_content, '')) > 100
           ) >= %s
        ORDER BY bilingual_ready_count DESC, s.rank_position ASC NULLS LAST, s.title ASC
        LIMIT %s
    """
    with connect() as conn:
        rows = conn.execute(
            query,
            (codes, min_polished, min_bilingual, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def update_story_metadata(story_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute(
            """
            UPDATE stories
            SET metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb,
                updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            (Jsonb(metadata), story_id),
        ).fetchone()
        if row is None:
            raise ValueError(f"Unknown story id: {story_id}")
        return dict(row)


def delete_story_metadata_keys(story_id: str, keys: list[str]) -> dict[str, Any]:
    """Remove specific keys from stories.metadata JSONB."""
    if not keys:
        return get_story_by_id(story_id)
    # Build successive JSONB key-deletion expression: metadata - 'k1' - 'k2' ...
    deletions = " - ".join(f"'{k}'" for k in keys)
    with connect() as conn:
        row = conn.execute(
            f"""
            UPDATE stories
            SET metadata = COALESCE(metadata, '{{}}'::jsonb) - {deletions},
                updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            (story_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Unknown story id: {story_id}")
        return dict(row)


def get_story_source_urls_by_source(source_code: str) -> set[str]:
    """Return all source_urls for a given source_code (for new-story detection)."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT s.source_url
            FROM stories s
            JOIN sources src ON src.id = s.source_id
            WHERE src.code = %s AND s.source_url IS NOT NULL
            """,
            (source_code,),
        ).fetchall()
        return {row["source_url"] for row in rows}


def update_story_author(story_id: str, author: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    cleaned_author = re.sub(r"\s+", " ", author or "").strip()
    if not cleaned_author:
        raise ValueError("author is empty")
    with connect() as conn:
        row = conn.execute(
            """
            UPDATE stories
            SET author = %s,
                metadata = metadata || %s::jsonb,
                updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            (cleaned_author, Jsonb(metadata or {}), story_id),
        ).fetchone()
        if row is None:
            raise ValueError(f"Unknown story id: {story_id}")
        return dict(row)


def get_story_chapter_bounds(story_id: str) -> dict[str, int]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                COALESCE(MIN(chapter_number), 0) AS min_chapter,
                COALESCE(MAX(chapter_number), 0) AS max_chapter,
                COUNT(*)::int AS chapter_count
            FROM chapters
            WHERE story_id = %s
            """,
            (story_id,),
        ).fetchone()
        assert row is not None
        return {
            "min_chapter": int(row["min_chapter"] or 0),
            "max_chapter": int(row["max_chapter"] or 0),
            "chapter_count": int(row["chapter_count"] or 0),
        }


def get_story_chapter_progress(story_id: str) -> dict[str, int]:
    with connect() as conn:
        stats = conn.execute(
            """
            SELECT
                COALESCE(MIN(chapter_number), 0) AS min_chapter,
                COALESCE(MAX(chapter_number), 0) AS max_chapter,
                COALESCE(MAX(chapter_number) FILTER (WHERE is_downloaded), 0) AS max_downloaded_chapter,
                COALESCE(MAX(chapter_number) FILTER (WHERE is_polished), 0) AS max_polished_chapter,
                COUNT(*)::int AS chapter_count,
                COUNT(*) FILTER (WHERE is_downloaded)::int AS downloaded_count,
                COUNT(*) FILTER (WHERE is_polished)::int AS polished_count
            FROM chapters
            WHERE story_id = %s
            """,
            (story_id,),
        ).fetchone()
        assert stats is not None
        first_unpolished = conn.execute(
            """
            SELECT COALESCE(MIN(chapter_number), 0) AS chapter_number
            FROM chapters
            WHERE story_id = %s
              AND is_polished = FALSE
            """,
            (story_id,),
        ).fetchone()
        assert first_unpolished is not None
        first_tail_unpolished = conn.execute(
            """
            WITH stats AS (
                SELECT COALESCE(MAX(chapter_number), 0) AS max_chapter
                FROM chapters
                WHERE story_id = %s
            )
            SELECT COALESCE(MIN(c.chapter_number), 0) AS chapter_number
            FROM chapters c, stats
            WHERE c.story_id = %s
              AND c.is_polished = FALSE
              AND c.chapter_number >= GREATEST(stats.max_chapter - 50, 1)
            """,
            (story_id, story_id),
        ).fetchone()
        assert first_tail_unpolished is not None
        return {
            "min_chapter": int(stats["min_chapter"] or 0),
            "max_chapter": int(stats["max_chapter"] or 0),
            "max_downloaded_chapter": int(stats["max_downloaded_chapter"] or 0),
            "max_polished_chapter": int(stats["max_polished_chapter"] or 0),
            "chapter_count": int(stats["chapter_count"] or 0),
            "downloaded_count": int(stats["downloaded_count"] or 0),
            "polished_count": int(stats["polished_count"] or 0),
            "first_unpolished_chapter": int(first_unpolished["chapter_number"] or 0),
            "first_tail_unpolished_chapter": int(first_tail_unpolished["chapter_number"] or 0),
        }


def upsert_category(
    name: str,
    *,
    language: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_name = normalize_category_name(name)
    if not normalized_name:
        raise ValueError("Category name is empty")
    with connect() as conn:
        row = conn.execute(
            """
            INSERT INTO categories (slug, name, normalized_name, language, metadata)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (normalized_name)
            DO UPDATE SET
                name = EXCLUDED.name,
                language = COALESCE(EXCLUDED.language, categories.language),
                metadata = categories.metadata || EXCLUDED.metadata,
                updated_at = now()
            RETURNING *
            """,
            (slugify_category(name), name.strip(), normalized_name, language, Jsonb(metadata or {})),
        ).fetchone()
        assert row is not None
        return dict(row)


def sync_story_categories(story_id: str, names: list[str], *, language: str | None = None) -> list[dict[str, Any]]:
    categories: list[dict[str, Any]] = []
    cleaned_names = [name for name in dict.fromkeys(names) if normalize_category_name(name)]
    if not cleaned_names:
        return categories

    with connect() as conn:
        for name in cleaned_names:
            normalized_name = normalize_category_name(name)
            category = conn.execute(
                """
                INSERT INTO categories (slug, name, normalized_name, language)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (normalized_name)
                DO UPDATE SET
                    name = EXCLUDED.name,
                    language = COALESCE(EXCLUDED.language, categories.language),
                    updated_at = now()
                RETURNING *
                """,
                (slugify_category(name), name.strip(), normalized_name, language),
            ).fetchone()
            assert category is not None
            conn.execute(
                """
                INSERT INTO story_categories (story_id, category_id, source_category_name)
                VALUES (%s, %s, %s)
                ON CONFLICT (story_id, category_id)
                DO UPDATE SET source_category_name = EXCLUDED.source_category_name
                """,
                (story_id, category["id"], name.strip()),
            )
            categories.append(dict(category))

        conn.execute(
            """
            UPDATE stories
            SET primary_category_id = %s, updated_at = now()
            WHERE id = %s
            """,
            (categories[0]["id"], story_id),
        )
    return categories


_PUNCT_STRIP_RE = re.compile(r"[^\w\s]", re.UNICODE)
_STRONG_ORIG_TITLE_MIN_LEN = 10   # original_title must be at least this long to trust
_MEDIUM_TITLE_AUTHOR_MIN_AUTHOR_LEN = 3  # author must be at least this long for title+author match


def _normalize_for_dedup(s: str) -> str:
    t = (s or "").strip().lower()
    t = _PUNCT_STRIP_RE.sub(" ", t)
    return " ".join(t.split())

# Keep legacy alias used by deduplicate_stories imports (if any)
_normalize_title_for_dedup = _normalize_for_dedup


def find_canonical_story(
    title: str,
    original_title: str | None,
    author: str | None,
    source_id: str,
    *,
    title_en: str | None = None,
    title_ko: str | None = None,
    title_zh: str | None = None,
) -> tuple[dict[str, Any], str] | tuple[None, None]:
    """Return (canonical_story, match_reason) from a *different* source, or (None, None).

    Match levels (all require different source_id):

    Strong — title_en/title_ko/title_zh column match (len >= 10):
      Uses the new language-specific title columns which are reliably populated.
      Two EN sources sharing the same title_en are almost certainly the same work.

    Medium — normalized title + normalized author both match (author len >= 3):
      Safe when both signals agree. Avoids false positives from generic titles alone.

    original_title is kept as a fallback signal when language-specific cols are absent.
    SQL normalizes punct on both sides so apostrophe variants compare equal.
    """
    norm_title_en = _normalize_for_dedup(title_en or "")
    norm_title_ko = _normalize_for_dedup(title_ko or "")
    norm_title_zh = _normalize_for_dedup(title_zh or "")
    norm_title = _normalize_for_dedup(title or "")
    norm_author = _normalize_for_dedup(author or "")

    # Build SQL norm helper (inline function string)
    _sql_norm = ("trim(regexp_replace(regexp_replace(lower(trim({col})),"
                 " '[^[:alnum:][:space:]]', ' ', 'g'), '\\s+', ' ', 'g'))")

    def sql_norm(col: str) -> str:
        return _sql_norm.format(col=col)

    # Collect non-empty language-specific norms to search by
    lang_filters = []
    lang_params: dict[str, Any] = {}
    for param, col in [
        ("norm_title_en", "s.title_en"),
        ("norm_title_ko", "s.title_ko"),
        ("norm_title_zh", "s.title_zh"),
    ]:
        val = locals()[param]
        if val and len(val) >= _STRONG_ORIG_TITLE_MIN_LEN:
            lang_filters.append(
                f"({sql_norm(col)} = %({param})s AND {col} IS NOT NULL)"
            )
            lang_params[param] = val

    title_where = sql_norm("s.title") + " = %(norm_title)s"
    where_parts = lang_filters + [title_where]

    with connect() as conn:
        candidates = conn.execute(
            f"""
            SELECT s.*, src.code AS source_code
            FROM stories s
            JOIN sources src ON src.id = s.source_id
            WHERE s.source_id != %(source_id)s
              AND s.is_active = TRUE
              AND ({' OR '.join(where_parts)})
            ORDER BY
              (SELECT COUNT(*) FROM chapters c WHERE c.story_id = s.id AND c.is_polished) DESC,
              (SELECT COUNT(*) FROM chapters c WHERE c.story_id = s.id AND c.is_translated) DESC,
              (SELECT COUNT(*) FROM chapters c WHERE c.story_id = s.id AND c.is_downloaded) DESC,
              s.created_at ASC
            LIMIT 10
            """,
            {"source_id": source_id, "norm_title": norm_title, **lang_params},
        ).fetchall()

    for row in candidates:
        row_norm_en = _normalize_for_dedup(row.get("title_en") or "")
        row_norm_ko = _normalize_for_dedup(row.get("title_ko") or "")
        row_norm_zh = _normalize_for_dedup(row.get("title_zh") or "")
        row_norm_title = _normalize_for_dedup(row.get("title") or "")
        row_norm_author = _normalize_for_dedup(row.get("author") or "")

        # Strong match: language-specific title column
        for norm_val, row_val, label in [
            (norm_title_en, row_norm_en, "title_en"),
            (norm_title_ko, row_norm_ko, "title_ko"),
            (norm_title_zh, row_norm_zh, "title_zh"),
        ]:
            if (norm_val and len(norm_val) >= _STRONG_ORIG_TITLE_MIN_LEN
                    and row_val and norm_val == row_val):
                return dict(row), f"{label}={norm_val!r}"

        # Medium match: title + author both match
        if (norm_title and norm_author and len(norm_author) >= _MEDIUM_TITLE_AUTHOR_MIN_AUTHOR_LEN
                and norm_title == row_norm_title and norm_author == row_norm_author):
            return dict(row), f"title+author={norm_title!r}+{norm_author!r}"

    return None, None


# Backward-compatible alias (used by deduplicate_stories.py if imported directly)
def find_canonical_story_by_title(title: str, source_id: str) -> dict[str, Any] | None:
    canonical, _ = find_canonical_story(title, None, None, source_id)
    return canonical


def upsert_story(source_code: str, story: dict[str, Any]) -> dict[str, Any]:
    source = get_source(source_code)
    touch_catalog_checked_at = story.get("touch_catalog_checked_at", True)

    # Auto-derive language-specific title fields if caller doesn't provide them.
    # Callers may override by passing title_en/title_ko/title_zh explicitly.
    # Must be derived before the dedup call so they're available as search signals.
    lang = (story.get("language") or "zh").lower()
    title_en = story.get("title_en") or (story["title"] if lang == "en" else None)
    title_ko = story.get("title_ko") or (story["title"] if lang == "ko" else None)
    title_zh = story.get("title_zh") or (story["title"] if lang == "zh" else None)

    # Cross-source duplicate check using language-specific title columns + author match.
    # original_title alone is NOT used: crawlers may store the display title there.
    # Title-only match is also intentionally NOT used (too many false positives).
    if not story.get("skip_dedup"):
        canonical, match_reason = find_canonical_story(
            title=story.get("title", ""),
            original_title=None,
            author=story.get("author"),
            source_id=source["id"],
            title_en=title_en,
            title_ko=title_ko,
            title_zh=title_zh,
        )
        if canonical:
            import sys
            print(
                f"[DEDUP] '{story.get('title')}' matched canonical "
                f"'{canonical.get('title')}' from '{canonical.get('source_code', '?')}' "
                f"via {match_reason} (id={canonical['id']}). Skipping duplicate insert.",
                file=sys.stderr,
            )
            return canonical

    with connect() as conn:
        row = conn.execute(
            """
            INSERT INTO stories (
                source_id, source_story_id, title, original_title, display_title, author, category, status,
                language, source_url, catalog_url, description, cover_image_url, metadata, rank_name, rank_position,
                total_chapters, free_chapters, locked_chapters, is_completed, last_catalog_checked_at,
                title_en, title_ko, title_zh
            )
            VALUES (
                %(source_id)s, %(source_story_id)s, %(title)s, %(original_title)s, %(display_title)s, %(author)s,
                %(category)s, %(status)s, %(language)s, %(source_url)s, %(catalog_url)s,
                %(description)s, %(cover_image_url)s, %(metadata)s, %(rank_name)s, %(rank_position)s, %(total_chapters)s,
                %(free_chapters)s, %(locked_chapters)s, %(is_completed)s,
                CASE WHEN %(touch_catalog_checked_at)s THEN now() ELSE NULL END,
                %(title_en)s, %(title_ko)s, %(title_zh)s
            )
            ON CONFLICT (source_id, source_url)
            DO UPDATE SET
                source_story_id = EXCLUDED.source_story_id,
                title = EXCLUDED.title,
                original_title = EXCLUDED.original_title,
                display_title = COALESCE(EXCLUDED.display_title, stories.display_title),
                author = EXCLUDED.author,
                category = EXCLUDED.category,
                status = EXCLUDED.status,
                catalog_url = EXCLUDED.catalog_url,
                description = EXCLUDED.description,
                cover_image_url = COALESCE(EXCLUDED.cover_image_url, stories.cover_image_url),
                metadata = stories.metadata || EXCLUDED.metadata,
                rank_name = EXCLUDED.rank_name,
                rank_position = EXCLUDED.rank_position,
                total_chapters = GREATEST(stories.total_chapters, EXCLUDED.total_chapters),
                free_chapters = EXCLUDED.free_chapters,
                locked_chapters = EXCLUDED.locked_chapters,
                is_completed = EXCLUDED.is_completed,
                last_catalog_checked_at = CASE
                    WHEN %(touch_catalog_checked_at)s THEN now()
                    ELSE stories.last_catalog_checked_at
                END,
                title_en = COALESCE(EXCLUDED.title_en, stories.title_en),
                title_ko = COALESCE(EXCLUDED.title_ko, stories.title_ko),
                title_zh = COALESCE(EXCLUDED.title_zh, stories.title_zh),
                updated_at = now()
            RETURNING *, (xmax = 0) AS is_new_insert
            """,
            {
                "source_id": source["id"],
                "source_story_id": story.get("source_story_id"),
                "title": story["title"],
                "original_title": story.get("original_title") or story.get("title"),
                "display_title": story.get("display_title"),
                "author": story.get("author"),
                "category": story.get("category"),
                "status": story.get("status"),
                "language": lang,
                "source_url": story["source_url"],
                "catalog_url": story.get("catalog_url"),
                "description": story.get("description"),
                "cover_image_url": story.get("cover_image_url"),
                "metadata": Jsonb(story.get("metadata") or {}),
                "rank_name": story.get("rank_name"),
                "rank_position": story.get("rank_position"),
                "total_chapters": story.get("total_chapters") or 0,
                "free_chapters": story.get("free_chapters") or 0,
                "locked_chapters": story.get("locked_chapters") or 0,
                "is_completed": story.get("is_completed") or False,
                "touch_catalog_checked_at": touch_catalog_checked_at,
                "title_en": title_en,
                "title_ko": title_ko,
                "title_zh": title_zh,
            },
        ).fetchone()
        assert row is not None
        story_row = dict(row)

    metadata = story.get("metadata") or {}
    category_names = split_category_names(story.get("category"))
    for tag in metadata.get("tags") or []:
        if isinstance(tag, str):
            category_names.extend(split_category_names(tag))
    sync_story_categories(story_row["id"], category_names, language=story.get("language"))
    return story_row


def upsert_chapter(story_id: str, chapter: dict[str, Any]) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute(
            """
            INSERT INTO chapters (
                story_id, source_chapter_id, chapter_number, title, source_url, volume,
                is_locked, lock_reason, raw_language, raw_text_path, translated_text_path,
                polished_text_path, audio_path, is_downloaded, is_translated, is_polished,
                is_audio_generated, raw_text_content, translated_text_content,
                polished_text_content, text_content_backfilled_at, last_lock_checked_at
            )
            VALUES (
                %(story_id)s, %(source_chapter_id)s, %(chapter_number)s, %(title)s,
                %(source_url)s, %(volume)s, %(is_locked)s, %(lock_reason)s, %(raw_language)s,
                %(raw_text_path)s, %(translated_text_path)s, %(polished_text_path)s,
                %(audio_path)s, %(is_downloaded)s, %(is_translated)s, %(is_polished)s,
                %(is_audio_generated)s, %(raw_text_content)s::text, %(translated_text_content)s::text,
                %(polished_text_content)s::text,
                CASE
                    WHEN %(raw_text_content)s::text IS NOT NULL
                      OR %(translated_text_content)s::text IS NOT NULL
                      OR %(polished_text_content)s::text IS NOT NULL
                    THEN now()
                    ELSE NULL
                END,
                now()
            )
            ON CONFLICT (story_id, chapter_number)
            DO UPDATE SET
                source_chapter_id = EXCLUDED.source_chapter_id,
                title = EXCLUDED.title,
                source_url = EXCLUDED.source_url,
                volume = EXCLUDED.volume,
                is_locked = EXCLUDED.is_locked,
                lock_reason = EXCLUDED.lock_reason,
                raw_text_path = COALESCE(EXCLUDED.raw_text_path, chapters.raw_text_path),
                translated_text_path = COALESCE(EXCLUDED.translated_text_path, chapters.translated_text_path),
                polished_text_path = COALESCE(EXCLUDED.polished_text_path, chapters.polished_text_path),
                audio_path = COALESCE(EXCLUDED.audio_path, chapters.audio_path),
                raw_text_content = COALESCE(EXCLUDED.raw_text_content, chapters.raw_text_content),
                translated_text_content = COALESCE(EXCLUDED.translated_text_content, chapters.translated_text_content),
                polished_text_content = COALESCE(EXCLUDED.polished_text_content, chapters.polished_text_content),
                text_content_backfilled_at = CASE
                    WHEN EXCLUDED.raw_text_content IS NOT NULL
                      OR EXCLUDED.translated_text_content IS NOT NULL
                      OR EXCLUDED.polished_text_content IS NOT NULL
                    THEN now()
                    ELSE chapters.text_content_backfilled_at
                END,
                is_downloaded = chapters.is_downloaded OR EXCLUDED.is_downloaded,
                is_translated = chapters.is_translated OR EXCLUDED.is_translated,
                is_polished = chapters.is_polished OR EXCLUDED.is_polished,
                is_audio_generated = chapters.is_audio_generated OR EXCLUDED.is_audio_generated,
                last_lock_checked_at = now(),
                updated_at = now()
            RETURNING *
            """,
            {
                "story_id": story_id,
                "source_chapter_id": chapter.get("source_chapter_id"),
                "chapter_number": chapter["chapter_number"],
                "title": chapter["title"],
                "source_url": chapter["source_url"],
                "volume": chapter.get("volume"),
                "is_locked": chapter.get("is_locked") or False,
                "lock_reason": chapter.get("lock_reason"),
                "raw_language": chapter.get("raw_language") or "zh",
                "raw_text_path": chapter.get("raw_text_path"),
                "translated_text_path": chapter.get("translated_text_path"),
                "polished_text_path": chapter.get("polished_text_path"),
                "raw_text_content": chapter.get("raw_text_content"),
                "translated_text_content": chapter.get("translated_text_content"),
                "polished_text_content": chapter.get("polished_text_content"),
                "audio_path": chapter.get("audio_path"),
                "is_downloaded": chapter.get("is_downloaded") or False,
                "is_translated": chapter.get("is_translated") or False,
                "is_polished": chapter.get("is_polished") or False,
                "is_audio_generated": chapter.get("is_audio_generated") or False,
            },
        ).fetchone()
        assert row is not None
        conn.execute(
            """
            UPDATE stories
            SET total_chapters = GREATEST(total_chapters, %s),
                updated_at = now()
            WHERE id = %s
            """,
            (chapter["chapter_number"], story_id),
        )
        return dict(row)


def update_story_display_title(
    story_id: str,
    display_title: str,
    *,
    model: str | None = None,
) -> dict[str, Any]:
    cleaned_title = re.sub(r"\s+", " ", display_title or "").strip()
    if not cleaned_title:
        raise ValueError("display_title is empty")

    with connect() as conn:
        row = conn.execute(
            """
            UPDATE stories
            SET display_title = %s,
                title_polished_at = now(),
                title_polish_model = %s,
                updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            (cleaned_title, model, story_id),
        ).fetchone()
        if row is None:
            raise ValueError(f"Unknown story id: {story_id}")
        return dict(row)


def update_chapter_title(chapter_id: str, title: str) -> dict[str, Any]:
    cleaned_title = re.sub(r"\s+", " ", title or "").strip()
    if not cleaned_title:
        raise ValueError("chapter title is empty")

    with connect() as conn:
        row = conn.execute(
            """
            UPDATE chapters
            SET title = %s,
                updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            (cleaned_title, chapter_id),
        ).fetchone()
        if row is None:
            raise ValueError(f"Unknown chapter id: {chapter_id}")
        return dict(row)


def list_pending_locked(limit: int = 100) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT c.*, s.title AS story_title, s.source_url AS story_url, src.code AS source_code
            FROM chapters c
            JOIN stories s ON s.id = c.story_id
            JOIN sources src ON src.id = s.source_id
            WHERE c.is_locked = TRUE
            ORDER BY c.last_lock_checked_at NULLS FIRST, c.chapter_number
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def list_categories(limit: int = 100) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT cat.*, COUNT(sc.story_id) AS story_count
            FROM categories cat
            LEFT JOIN story_categories sc ON sc.category_id = cat.id
            GROUP BY cat.id
            ORDER BY story_count DESC, cat.name
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def enqueue_chapter_job(
    job_type: str,
    chapter_id: str | None,
    *,
    story_id: str | None = None,
    source_code: str | None = None,
    model: str | None = None,
    input_path: str | None = None,
    output_path: str | None = None,
    payload: dict[str, Any] | None = None,
    priority: int = 100,
    max_attempts: int = 3,
) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute(
            """
            INSERT INTO story_jobs (
                job_type, chapter_id, story_id, source_code, model, input_path, output_path,
                payload, priority, max_attempts
            )
            VALUES (
                %(job_type)s, %(chapter_id)s, %(story_id)s, %(source_code)s, %(model)s,
                %(input_path)s, %(output_path)s, %(payload)s, %(priority)s, %(max_attempts)s
            )
            ON CONFLICT (job_type, chapter_id)
            DO UPDATE SET
                story_id = COALESCE(EXCLUDED.story_id, story_jobs.story_id),
                source_code = COALESCE(EXCLUDED.source_code, story_jobs.source_code),
                model = COALESCE(EXCLUDED.model, story_jobs.model),
                input_path = COALESCE(EXCLUDED.input_path, story_jobs.input_path),
                output_path = COALESCE(EXCLUDED.output_path, story_jobs.output_path),
                payload = story_jobs.payload || EXCLUDED.payload,
                priority = LEAST(story_jobs.priority, EXCLUDED.priority),
                max_attempts = GREATEST(story_jobs.max_attempts, EXCLUDED.max_attempts),
                status = CASE
                    WHEN story_jobs.status IN ('done', 'running') THEN story_jobs.status
                    ELSE 'pending'
                END,
                run_after = CASE
                    WHEN story_jobs.status IN ('done', 'running') THEN story_jobs.run_after
                    ELSE now()
                END,
                updated_at = now()
            RETURNING *
            """,
            {
                "job_type": job_type,
                "chapter_id": chapter_id,
                "story_id": story_id,
                "source_code": source_code,
                "model": model,
                "input_path": input_path,
                "output_path": output_path,
                "payload": Jsonb(payload or {}),
                "priority": priority,
                "max_attempts": max_attempts,
            },
        ).fetchone()
        assert row is not None
        return dict(row)


_CLAIM_ORDER_SQL = {
    "fifo": "j.priority, j.created_at ASC",
    "newest_story": "j.priority, s.created_at DESC NULLS LAST, j.created_at ASC",
    "non_vi_first": (
        f"j.priority, {_NON_VI_PRIORITY_CASE} ASC, s.created_at DESC NULLS LAST, j.created_at ASC"
    ),
    # Non-VI sources first; within each rank tier (1-3, 4-6, …) prefer lower rank_position,
    # then round-robin across sources at the same rank, then chapter order.
    "non_vi_rank_tier": (
        f"j.priority, {_NON_VI_PRIORITY_CASE} ASC, {_RANK_TIER_CASE} ASC, "
        f"s.rank_position ASC NULLS LAST, j.source_code ASC, "
        f"{_CHAPTER_ORDER_CASE} ASC, j.created_at ASC"
    ),
}


def claim_story_jobs(
    job_type: str,
    worker_id: str,
    limit: int = 1,
    *,
    source_codes: Sequence[str] | None = None,
    story_ids: Sequence[str] | None = None,
    claim_order: str = "fifo",
) -> list[dict[str, Any]]:
    source_codes = [code for code in (source_codes or []) if code]
    story_ids = [story_id for story_id in (story_ids or []) if story_id]
    order_sql = _CLAIM_ORDER_SQL.get(claim_order, _CLAIM_ORDER_SQL["fifo"])
    with connect() as conn:
        rows = conn.execute(
            f"""
            WITH selected AS (
                SELECT j.id
                FROM story_jobs j
                LEFT JOIN stories s ON s.id = j.story_id
                LEFT JOIN chapters c ON c.id = j.chapter_id
                WHERE j.job_type = %s
                  AND j.status = 'pending'
                  AND j.run_after <= now()
                  AND j.attempts < j.max_attempts
                  AND (%s::text[] IS NULL OR j.source_code = ANY(%s::text[]))
                  AND (%s::uuid[] IS NULL OR j.story_id = ANY(%s::uuid[]))
                ORDER BY {order_sql}
                LIMIT %s
                FOR UPDATE OF j SKIP LOCKED
            )
            UPDATE story_jobs j
            SET status = 'running',
                locked_by = %s,
                locked_at = now(),
                attempts = attempts + 1,
                updated_at = now()
            FROM selected
            WHERE j.id = selected.id
            RETURNING j.*
            """,
            (job_type, source_codes or None, source_codes or None, story_ids or None, story_ids or None, limit, worker_id),
        ).fetchall()
        return [dict(row) for row in rows]


def complete_story_job(job_id: str, *, result_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute(
            """
            UPDATE story_jobs
            SET status = 'done',
                payload = payload || %s,
                finished_at = now(),
                locked_by = NULL,
                locked_at = NULL,
                updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            (Jsonb(result_payload or {}), job_id),
        ).fetchone()
        assert row is not None
        return dict(row)


def reset_stale_running_jobs(job_type: str, *, stale_after_minutes: int = 120) -> int:
    """Reset running jobs locked longer than stale_after_minutes back to pending (or failed if out of attempts).
    Call at worker startup to recover from container crashes."""
    with connect() as conn:
        rows = conn.execute(
            """
            UPDATE story_jobs
            SET status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'pending' END,
                locked_by = NULL,
                locked_at = NULL,
                run_after = now(),
                last_error = COALESCE(last_error, '') || ' [reset: stale running job]',
                updated_at = now()
            WHERE job_type = %s
              AND status = 'running'
              AND locked_at < now() - make_interval(mins => %s)
            RETURNING id
            """,
            (job_type, stale_after_minutes),
        ).fetchall()
        return len(rows)


def fail_story_job(job_id: str, error: str, *, retry_delay_seconds: int = 60) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute(
            """
            UPDATE story_jobs
            SET status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'pending' END,
                last_error = %s,
                run_after = now() + make_interval(secs => %s),
                locked_by = NULL,
                locked_at = NULL,
                updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            (error[:4000], retry_delay_seconds, job_id),
        ).fetchone()
        assert row is not None
        return dict(row)


def update_chapter_text_outputs(
    chapter_id: str,
    *,
    translated_text_path: str | None = None,
    polished_text_path: str | None = None,
    raw_text_content: str | None = None,
    translated_text_content: str | None = None,
    polished_text_content: str | None = None,
    clear_audio: bool = False,
    quality_status: str | None = None,
) -> dict[str, Any]:
    """Update text output columns for a chapter.

    clear_audio: khi True và polished_text_content được set, xóa is_audio_generated + audio_path.
    Dùng khi re-polish (--overwrite) để audio cũ không còn được serve sau khi text thay đổi.
    quality_status: when set, updated atomically with text columns (save guard).
    """
    with connect() as conn:
        row = conn.execute(
            """
            UPDATE chapters
            SET translated_text_path = COALESCE(%(translated_text_path)s::text, translated_text_path),
                polished_text_path = COALESCE(%(polished_text_path)s::text, polished_text_path),
                raw_text_content = COALESCE(%(raw_text_content)s::text, raw_text_content),
                translated_text_content = COALESCE(%(translated_text_content)s::text, translated_text_content),
                polished_text_content = COALESCE(%(polished_text_content)s::text, polished_text_content),
                is_translated = is_translated OR (%(translated_text_path)s::text IS NOT NULL OR %(translated_text_content)s::text IS NOT NULL),
                is_polished = is_polished OR (%(polished_text_path)s::text IS NOT NULL OR %(polished_text_content)s::text IS NOT NULL),
                is_audio_generated = CASE
                    WHEN %(clear_audio)s THEN FALSE
                    ELSE is_audio_generated
                END,
                audio_path = CASE
                    WHEN %(clear_audio)s THEN NULL
                    ELSE audio_path
                END,
                translated_at = CASE
                    WHEN %(translated_text_path)s::text IS NOT NULL OR %(translated_text_content)s::text IS NOT NULL THEN now()
                    ELSE translated_at
                END,
                polished_at = CASE
                    WHEN %(polished_text_path)s::text IS NOT NULL OR %(polished_text_content)s::text IS NOT NULL THEN now()
                    ELSE polished_at
                END,
                text_content_backfilled_at = CASE
                    WHEN %(raw_text_content)s::text IS NOT NULL
                      OR %(translated_text_content)s::text IS NOT NULL
                      OR %(polished_text_content)s::text IS NOT NULL
                    THEN now()
                    ELSE text_content_backfilled_at
                END,
                quality_status = COALESCE(%(quality_status)s::text, quality_status),
                updated_at = now()
            WHERE id = %(chapter_id)s
            RETURNING *
            """,
            {
                "chapter_id": chapter_id,
                "translated_text_path": translated_text_path,
                "polished_text_path": polished_text_path,
                "raw_text_content": raw_text_content,
                "translated_text_content": translated_text_content,
                "polished_text_content": polished_text_content,
                "clear_audio": clear_audio,
                "quality_status": quality_status,
            },
        ).fetchone()
        assert row is not None
        return dict(row)


def update_chapter_polished_by_raw_path(
    raw_text_paths: list[str],
    *,
    polished_text_path: str,
    polished_text_content: str | None = None,
) -> dict[str, Any] | None:
    if not raw_text_paths:
        return None
    with connect() as conn:
        row = conn.execute(
            """
            UPDATE chapters
            SET polished_text_path = %s,
                polished_text_content = COALESCE(%s::text, polished_text_content),
                is_polished = TRUE,
                polished_at = now(),
                text_content_backfilled_at = CASE WHEN %s::text IS NOT NULL THEN now() ELSE text_content_backfilled_at END,
                updated_at = now()
            WHERE raw_text_path = ANY(%s)
            RETURNING *
            """,
            (polished_text_path, polished_text_content, polished_text_content, raw_text_paths),
        ).fetchone()
        return dict(row) if row is not None else None


def list_story_jobs(status: str = "pending", limit: int = 50) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT j.*, c.chapter_number, c.title AS chapter_title, s.title AS story_title
            FROM story_jobs j
            LEFT JOIN chapters c ON c.id = j.chapter_id
            LEFT JOIN stories s ON s.id = j.story_id
            WHERE j.status = %s
            ORDER BY j.priority, j.created_at
            LIMIT %s
            """,
            (status, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def update_pending_translate_jobs_post_translate(
    *,
    story_id: str | None = None,
    source_codes: list[str] | None = None,
    post_translate: str = "copy",
    dry_run: bool = True,
) -> int:
    params: list[Any] = [post_translate]
    where = [
        "j.job_type = 'polish_chapter'",
        "j.status IN ('pending', 'failed')",
        "COALESCE(j.payload->>'raw_language', '') <> ''",
        "lower(j.payload->>'raw_language') <> 'vi'",
    ]
    if story_id:
        where.append("j.story_id = %s")
        params.append(story_id)
    if source_codes:
        where.append("j.source_code = ANY(%s)")
        params.append(source_codes)
    where_sql = " AND ".join(where)
    with connect() as conn:
        if dry_run:
            row = conn.execute(
                f"""
                SELECT COUNT(*)::int AS count
                FROM story_jobs j
                WHERE {where_sql}
                  AND COALESCE(j.payload->>'post_translate', 'polish') <> %s
                """,
                [*params[1:], post_translate],
            ).fetchone()
            assert row is not None
            return int(row["count"] or 0)
        row = conn.execute(
            f"""
            UPDATE story_jobs j
            SET payload = payload || jsonb_build_object('post_translate', %s),
                status = CASE WHEN status = 'failed' THEN 'pending' ELSE status END,
                run_after = now(),
                updated_at = now()
            WHERE {where_sql}
              AND COALESCE(j.payload->>'post_translate', 'polish') <> %s
            RETURNING j.id
            """,
            [*params, post_translate],
        ).fetchall()
        return len(row)


def list_audio_pending(limit: int = 100) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT c.*, s.title AS story_title, src.code AS source_code
            FROM chapters c
            JOIN stories s ON s.id = c.story_id
            JOIN sources src ON src.id = s.source_id
            WHERE c.is_downloaded = TRUE
              AND c.is_polished = TRUE
              AND c.polished_text_path IS NOT NULL
              AND c.is_audio_generated = FALSE
            ORDER BY s.title, c.chapter_number
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_chapter_raw_content(chapter_id: str) -> str | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT raw_text_content FROM chapters WHERE id = %s",
            (chapter_id,),
        ).fetchone()
        return row["raw_text_content"] if row else None


def get_chapter_translated_content(chapter_id: str) -> str | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT translated_text_content FROM chapters WHERE id = %s",
            (chapter_id,),
        ).fetchone()
        return row["translated_text_content"] if row else None


def get_chapter_polished_content(chapter_id: str) -> str | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT polished_text_content FROM chapters WHERE id = %s",
            (chapter_id,),
        ).fetchone()
        return row["polished_text_content"] if row else None


def list_polished_chapters_for_audio(
    *,
    story_id: str | None = None,
    story_url: str | None = None,
    story_slug: str | None = None,
    story_title: str | None = None,
    source_codes: list[str] | None = None,
    chapter_number: int | None = None,
    from_chapter: int | None = None,
    to_chapter: int | None = None,
    include_existing_audio: bool = False,
    limit: int = 100,
) -> list[dict[str, Any]]:
    query = """
        SELECT c.*, s.title AS story_title, s.source_url AS story_url, s.metadata AS story_metadata,
               src.code AS source_code
        FROM chapters c
        JOIN stories s ON s.id = c.story_id
        JOIN sources src ON src.id = s.source_id
        WHERE c.is_polished = TRUE
          AND (c.polished_text_path IS NOT NULL OR c.polished_text_content IS NOT NULL)
    """
    params: list[Any] = []
    if not include_existing_audio:
        query += " AND c.is_audio_generated = FALSE"
    if story_id:
        query += " AND s.id = %s"
        params.append(story_id)
    if story_url:
        query += " AND s.source_url = %s"
        params.append(story_url)
    if story_slug:
        query += " AND (s.metadata->>'slug' = %s OR s.source_url ILIKE %s OR c.polished_text_path ILIKE %s)"
        params.extend([story_slug, f"%{story_slug}%", f"%/{story_slug}/%"])
    if story_title:
        query += " AND s.title ILIKE %s"
        params.append(f"%{story_title}%")
    if source_codes:
        query += " AND src.code = ANY(%s)"
        params.append(source_codes)
    if chapter_number is not None:
        query += " AND c.chapter_number = %s"
        params.append(chapter_number)
    if from_chapter is not None:
        query += " AND c.chapter_number >= %s"
        params.append(from_chapter)
    if to_chapter is not None:
        query += " AND c.chapter_number <= %s"
        params.append(to_chapter)
    query += " ORDER BY s.title, c.chapter_number"
    if limit > 0:
        query += " LIMIT %s"
        params.append(limit)

    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]


def list_active_stories(
    *,
    source_codes: list[str] | None = None,
    only_incomplete: bool = False,
    min_catalog_check_hours: int = 0,
    limit: int = 0,
    ignore_alternate_backoff: bool = True,
) -> list[dict[str, Any]]:
    query = """
        SELECT s.*, src.code AS source_code, src.base_url AS source_base_url
        FROM stories s
        JOIN sources src ON src.id = s.source_id
        WHERE s.is_active = TRUE
    """
    params: list[Any] = []
    if source_codes:
        query += " AND src.code = ANY(%s)"
        params.append(source_codes)
    if only_incomplete:
        query += " AND s.is_completed = FALSE"
    if min_catalog_check_hours > 0:
        query += " AND (s.last_catalog_checked_at IS NULL OR s.last_catalog_checked_at <= now() - make_interval(hours => %s))"
        params.append(min_catalog_check_hours)
    if not ignore_alternate_backoff:
        query += """
          AND (
                s.metadata->>'alternate_skip_until' IS NULL
                OR (s.metadata->>'alternate_skip_until')::timestamptz <= now()
          )
        """
    query += f" ORDER BY {STORY_PRIORITY_ORDER_SQL}"
    if limit > 0:
        query += " LIMIT %s"
        params.append(limit)

    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]


def list_stories_needing_alternate_source(
    *,
    source_codes: list[str] | None = None,
    only_incomplete: bool = True,
    limit: int = 20,
    ignore_backoff: bool = False,
) -> list[dict[str, Any]]:
    query = """
        SELECT s.*, src.code AS source_code, src.base_url AS source_base_url
        FROM stories s
        JOIN sources src ON src.id = s.source_id
        WHERE s.is_active = TRUE
          AND (
                s.metadata->>'needs_alternate_source' = 'true'
                OR s.metadata->>'source_host_unavailable' = 'true'
          )
    """
    params: list[Any] = []
    if source_codes:
        query += " AND src.code = ANY(%s)"
        params.append(source_codes)
    if only_incomplete:
        query += " AND s.is_completed = FALSE"
    if not ignore_backoff:
        query += """
          AND (
                s.metadata->>'alternate_skip_until' IS NULL
                OR (s.metadata->>'alternate_skip_until')::timestamptz <= now()
          )
        """
    query += f"""
        ORDER BY
            (s.metadata->>'needs_alternate_source')::boolean DESC NULLS LAST,
            (s.metadata->>'source_host_unavailable_at')::timestamptz NULLS LAST,
            {STORY_PRIORITY_ORDER_SQL}
    """
    if limit > 0:
        query += " LIMIT %s"
        params.append(limit)

    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]


def claim_active_stories(
    *,
    worker_id: str,
    source_codes: list[str] | None = None,
    only_incomplete: bool = False,
    min_catalog_check_hours: int = 0,
    limit: int = 1,
    claim_ttl_minutes: int = 240,
    finished_cooldown_minutes: int = 240,
) -> list[dict[str, Any]]:
    source_codes = [code for code in (source_codes or []) if code]
    limit = max(1, int(limit or 1))
    claim_ttl_minutes = max(1, int(claim_ttl_minutes or 1))
    finished_cooldown_minutes = max(0, int(finished_cooldown_minutes or 0))
    query = """
        WITH selected AS (
            SELECT s.id
            FROM stories s
            JOIN sources src ON src.id = s.source_id
            WHERE s.is_active = TRUE
              AND (%s::text[] IS NULL OR src.code = ANY(%s::text[]))
              AND (%s = FALSE OR s.is_completed = FALSE)
              AND (
                    %s = 0
                    OR s.last_catalog_checked_at IS NULL
                    OR s.last_catalog_checked_at <= now() - make_interval(hours => %s)
              )
              AND (
                    s.metadata->>'crawl_claimed_until' IS NULL
                    OR (s.metadata->>'crawl_claimed_until')::timestamptz <= now()
              )
              AND (
                    %s = 0
                    OR s.metadata->>'last_crawl_finished_at' IS NULL
                    OR (s.metadata->>'last_crawl_finished_at')::timestamptz <= now() - make_interval(mins => %s)
              )
              AND (s.metadata->>'source_host_unavailable' IS NULL OR s.metadata->>'source_host_unavailable' = 'false')
    """
    query += f"""
            ORDER BY {STORY_PRIORITY_ORDER_SQL}
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        )
    """
    query += """
        UPDATE stories s
        SET metadata = COALESCE(s.metadata, '{}'::jsonb) || jsonb_build_object(
                'crawl_claimed_by', %s::text,
                'crawl_claimed_at', now(),
                'crawl_claimed_until', now() + make_interval(mins => %s)
            ),
            updated_at = now()
        FROM selected
        WHERE s.id = selected.id
        RETURNING s.*, (
            SELECT code FROM sources WHERE id = s.source_id
        ) AS source_code, (
            SELECT base_url FROM sources WHERE id = s.source_id
        ) AS source_base_url
    """
    params = (
        source_codes or None,
        source_codes or None,
        only_incomplete,
        min_catalog_check_hours,
        min_catalog_check_hours,
        finished_cooldown_minutes,
        finished_cooldown_minutes,
        limit,
        worker_id,
        claim_ttl_minutes,
    )
    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]


def release_story_claim(
    story_id: str,
    *,
    worker_id: str,
    status: str,
) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            """
            UPDATE stories
            SET metadata = (COALESCE(metadata, '{}'::jsonb) - 'crawl_claimed_by' - 'crawl_claimed_at' - 'crawl_claimed_until')
                    || jsonb_build_object(
                        'last_crawl_worker', %s::text,
                        'last_crawl_status', %s::text,
                        'last_crawl_finished_at', now()
                    ),
                updated_at = now()
            WHERE id = %s
              AND metadata->>'crawl_claimed_by' = %s::text
            RETURNING *
            """,
            (worker_id, status, story_id, worker_id),
        ).fetchone()
        return dict(row) if row is not None else None


def list_story_claims(limit: int = 100) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                s.id,
                src.code AS source_code,
                s.title,
                s.source_url,
                s.metadata->>'crawl_claimed_by' AS crawl_claimed_by,
                s.metadata->>'crawl_claimed_at' AS crawl_claimed_at,
                s.metadata->>'crawl_claimed_until' AS crawl_claimed_until
            FROM stories s
            JOIN sources src ON src.id = s.source_id
            WHERE s.metadata->>'crawl_claimed_until' IS NOT NULL
              AND (s.metadata->>'crawl_claimed_until')::timestamptz > now()
            ORDER BY (s.metadata->>'crawl_claimed_at')::timestamptz DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def clear_story_claims(*, worker_id: str | None = None) -> int:
    where = "metadata->>'crawl_claimed_until' IS NOT NULL"
    params: list[Any] = []
    if worker_id:
        where += " AND metadata->>'crawl_claimed_by' = %s"
        params.append(worker_id)
    with connect() as conn:
        rows = conn.execute(
            f"""
            UPDATE stories
            SET metadata = COALESCE(metadata, '{{}}'::jsonb)
                    - 'crawl_claimed_by'
                    - 'crawl_claimed_at'
                    - 'crawl_claimed_until',
                updated_at = now()
            WHERE {where}
            RETURNING id
            """,
            params,
        ).fetchall()
        return len(rows)


def update_chapter_audio_output(chapter_id: str, *, audio_path: str) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute(
            """
            UPDATE chapters
            SET audio_path = %s,
                is_audio_generated = TRUE,
                audio_generated_at = now(),
                updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            (audio_path, chapter_id),
        ).fetchone()
        assert row is not None
        return dict(row)


def update_chapter_audio_by_polished_path(
    polished_text_paths: list[str],
    *,
    audio_path: str,
) -> dict[str, Any] | None:
    if not polished_text_paths:
        return None
    with connect() as conn:
        row = conn.execute(
            """
            UPDATE chapters
            SET audio_path = %s,
                is_audio_generated = TRUE,
                audio_generated_at = now(),
                updated_at = now()
            WHERE polished_text_path = ANY(%s)
            RETURNING *
            """,
            (audio_path, polished_text_paths),
        ).fetchone()
        return dict(row) if row is not None else None


def list_pending_chapter_audio_segments(chapter_id: str, *, voice_key: str = "xianxia_story_male") -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM chapter_audio_segments
            WHERE chapter_id = %s
              AND voice_key = %s
              AND status IN ('pending', 'failed')
            ORDER BY segment_index ASC
            """,
            (chapter_id, voice_key),
        ).fetchall()
        return [dict(row) for row in rows]


def list_all_chapter_audio_segments(chapter_id: str, *, voice_key: str = "xianxia_story_male") -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT segment_index, status, audio_path
            FROM chapter_audio_segments
            WHERE chapter_id = %s
              AND voice_key = %s
            ORDER BY segment_index ASC
            """,
            (chapter_id, voice_key),
        ).fetchall()
        return [dict(row) for row in rows]


def reset_stale_running_chapter_audio_segments(*, stale_after_minutes: int = 120) -> int:
    """Reset running segments older than stale_after_minutes back to pending.
    Call at worker startup to recover from crashes that left segments in running state."""
    with connect() as conn:
        rows = conn.execute(
            """
            UPDATE chapter_audio_segments
            SET status = 'pending',
                updated_at = now()
            WHERE status = 'running'
              AND updated_at < now() - make_interval(mins => %s)
            RETURNING id
            """,
            (stale_after_minutes,),
        ).fetchall()
        return len(rows)


def mark_chapter_audio_segment_running(segment_id: str) -> dict[str, Any] | None:
    """Mark a segment as running. Returns None if the segment is no longer pending/failed
    (e.g., already claimed by another process or invalidated by re-enqueue)."""
    with connect() as conn:
        row = conn.execute(
            """
            UPDATE chapter_audio_segments
            SET status = 'running',
                error = NULL,
                updated_at = now()
            WHERE id = %s
              AND status IN ('pending', 'failed')
            RETURNING *
            """,
            (segment_id,),
        ).fetchone()
        return dict(row) if row is not None else None


def complete_chapter_audio_segment(
    segment_id: str,
    *,
    audio_path: str,
    duration_seconds: float,
    claimed_text_hash: str | None = None,
) -> dict[str, Any] | None:
    """Mark a segment ready.

    claimed_text_hash: hash của text_content khi worker bắt đầu xử lý segment.
    Nếu được truyền, UPDATE chỉ thành công khi DB row vẫn còn hash đó — nếu
    chapter được re-polish trong lúc worker đang chạy, hash sẽ khác và UPDATE
    trả về 0 rows (returns None). Caller nên log và skip stitch trong trường hợp đó.
    """
    with connect() as conn:
        if claimed_text_hash is not None:
            row = conn.execute(
                """
                UPDATE chapter_audio_segments
                SET status = 'ready',
                    audio_path = %s,
                    duration_seconds = %s,
                    error = NULL,
                    updated_at = now()
                WHERE id = %s
                  AND text_hash = %s
                RETURNING *
                """,
                (audio_path, duration_seconds, segment_id, claimed_text_hash),
            ).fetchone()
        else:
            row = conn.execute(
                """
                UPDATE chapter_audio_segments
                SET status = 'ready',
                    audio_path = %s,
                    duration_seconds = %s,
                    error = NULL,
                    updated_at = now()
                WHERE id = %s
                RETURNING *
                """,
                (audio_path, duration_seconds, segment_id),
            ).fetchone()
        return dict(row) if row is not None else None


def fail_chapter_audio_segment(segment_id: str, error: str) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute(
            """
            UPDATE chapter_audio_segments
            SET status = 'failed',
                error = %s,
                updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            (error[:4000], segment_id),
        ).fetchone()
        assert row is not None
        return dict(row)


def enqueue_audio_segments_for_chapter(
    chapter_id: str,
    story_id: str,
    segments: list[str],
    *,
    voice_key: str = "xianxia_story_male",
    source_code: str | None = None,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """INSERT pre-split TTS segments vào chapter_audio_segments và tạo audio_chapter_segments job.

    Idempotent:
    - Segment có cùng text_hash → giữ nguyên status/audio (không reset)
    - Segment có text_hash khác (re-polish) → reset về pending, xóa audio_path/duration/error
    - Segments có index >= len(segments) (old chapter có nhiều segment hơn) → xóa nếu không running
    - Job: nếu có segment mới hoặc bị reset → force job về pending (trừ khi đang running)
      Điều này đảm bảo re-polish sau job done vẫn được worker pick up.

    Returns {"total": int, "inserted": int, "reset": int, "unchanged": int, "job": dict}
    """
    import hashlib

    if not segments:
        raise ValueError("segments list is empty — nothing to enqueue")

    with connect() as conn:
        # Xóa segments thừa từ lần enqueue cũ (chỉ xóa nếu không đang chạy).
        # deleted_count > 0 nghĩa là chapter bị rút ngắn → phải re-stitch audio.
        deleted_result = conn.execute(
            """
            DELETE FROM chapter_audio_segments
            WHERE chapter_id = %s AND voice_key = %s
              AND segment_index >= %s AND status != 'running'
            """,
            (chapter_id, voice_key, len(segments)),
        )
        deleted_count = deleted_result.rowcount

        inserted = reset = unchanged = 0
        for idx, text in enumerate(segments):
            text_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
            # (xmax = 0) → INSERT mới; xmax != 0 → row đã tồn tại (UPDATE)
            # status 'pending' sau UPDATE → đã bị reset do text_hash thay đổi
            row = conn.execute(
                """
                INSERT INTO chapter_audio_segments
                    (chapter_id, story_id, segment_index, text_hash, text_content, voice_key, status)
                VALUES (%s, %s, %s, %s, %s, %s, 'pending')
                ON CONFLICT (chapter_id, voice_key, segment_index) DO UPDATE SET
                    text_hash        = EXCLUDED.text_hash,
                    text_content     = EXCLUDED.text_content,
                    status           = CASE
                                         WHEN chapter_audio_segments.text_hash != EXCLUDED.text_hash
                                           OR chapter_audio_segments.status = 'failed'
                                         THEN 'pending'
                                         ELSE chapter_audio_segments.status
                                       END,
                    audio_path       = CASE
                                         WHEN chapter_audio_segments.text_hash != EXCLUDED.text_hash
                                         THEN NULL
                                         ELSE chapter_audio_segments.audio_path
                                       END,
                    duration_seconds = CASE
                                         WHEN chapter_audio_segments.text_hash != EXCLUDED.text_hash
                                         THEN NULL
                                         ELSE chapter_audio_segments.duration_seconds
                                       END,
                    error            = CASE
                                         WHEN chapter_audio_segments.text_hash != EXCLUDED.text_hash
                                           OR chapter_audio_segments.status = 'failed'
                                         THEN NULL
                                         ELSE chapter_audio_segments.error
                                       END,
                    updated_at       = now()
                RETURNING
                    (xmax = 0)                            AS was_inserted,
                    (xmax != 0 AND status = 'pending')    AS was_reset
                """,
                (chapter_id, story_id, idx, text_hash, text, voice_key),
            ).fetchone()
            assert row is not None
            if row["was_inserted"]:
                inserted += 1
            elif row["was_reset"]:
                reset += 1
            else:
                unchanged += 1

        # Job upsert — trong cùng transaction với segment upserts.
        # Nếu có segment mới, bị reset, hoặc bị xóa (chapter rút ngắn): force job về pending (trừ running).
        # deleted_count > 0 cần force-pending để worker re-stitch audio với set segment mới.
        # Nếu tất cả unchanged và không có deletion: đảm bảo job tồn tại nhưng không downgrade done → pending.
        has_pending_work = (inserted + reset + deleted_count) > 0
        payload_json = Jsonb({"voice_key": voice_key, "segment_count": len(segments)})
        if has_pending_work:
            job_row = conn.execute(
                """
                INSERT INTO story_jobs (
                    job_type, chapter_id, story_id, source_code, payload, max_attempts
                )
                VALUES ('audio_chapter_segments', %s, %s, %s, %s, %s)
                ON CONFLICT (job_type, chapter_id) DO UPDATE SET
                    story_id     = COALESCE(EXCLUDED.story_id, story_jobs.story_id),
                    source_code  = COALESCE(EXCLUDED.source_code, story_jobs.source_code),
                    payload      = story_jobs.payload || EXCLUDED.payload,
                    max_attempts = GREATEST(story_jobs.max_attempts, EXCLUDED.max_attempts),
                    status       = CASE
                                     WHEN story_jobs.status = 'running' THEN 'running'
                                     ELSE 'pending'
                                   END,
                    run_after    = CASE
                                     WHEN story_jobs.status = 'running' THEN story_jobs.run_after
                                     ELSE now()
                                   END,
                    attempts     = CASE
                                     WHEN story_jobs.status = 'running' THEN story_jobs.attempts
                                     ELSE 0
                                   END,
                    updated_at   = now()
                RETURNING *
                """,
                (chapter_id, story_id, source_code, payload_json, max_attempts),
            ).fetchone()
        else:
            # Tất cả segments unchanged — chỉ cần đảm bảo job tồn tại
            job_row = conn.execute(
                """
                INSERT INTO story_jobs (
                    job_type, chapter_id, story_id, source_code, payload, max_attempts
                )
                VALUES ('audio_chapter_segments', %s, %s, %s, %s, %s)
                ON CONFLICT (job_type, chapter_id) DO UPDATE SET
                    story_id     = COALESCE(EXCLUDED.story_id, story_jobs.story_id),
                    source_code  = COALESCE(EXCLUDED.source_code, story_jobs.source_code),
                    payload      = story_jobs.payload || EXCLUDED.payload,
                    max_attempts = GREATEST(story_jobs.max_attempts, EXCLUDED.max_attempts),
                    updated_at   = now()
                RETURNING *
                """,
                (chapter_id, story_id, source_code, payload_json, max_attempts),
            ).fetchone()
        assert job_row is not None

    return {
        "total": len(segments),
        "inserted": inserted,
        "reset": reset,
        "unchanged": unchanged,
        "job": dict(job_row),
    }


def request_story_recrawl(story_id: str) -> dict[str, Any] | None:
    """Reset catalog check timestamps so crawl scheduler picks up the story again."""
    with connect() as conn:
        row = conn.execute(
            """
            UPDATE stories
            SET last_catalog_checked_at = NULL,
                metadata = (
                    COALESCE(metadata, '{}'::jsonb)
                    - 'last_crawl_finished_at'
                    - 'crawl_claimed_by'
                    - 'crawl_claimed_at'
                    - 'crawl_claimed_until'
                ) || jsonb_build_object('admin_recrawl_requested_at', now()),
                updated_at = now()
            WHERE id = %s
            RETURNING id, title
            """,
            (story_id,),
        ).fetchone()
        return dict(row) if row is not None else None


def request_chapter_recrawl(
    story_id: str,
    *,
    chapter_numbers: list[int] | None = None,
    from_chapter: int = 0,
    to_chapter: int = 0,
    clear_raw: bool = False,
    touch_story_catalog: bool = True,
) -> int:
    """Mark chapters for re-download on next crawl pass."""
    conditions = ["story_id = %(story_id)s"]
    params: dict[str, Any] = {"story_id": story_id, "clear_raw": clear_raw}
    if chapter_numbers:
        conditions.append("chapter_number = ANY(%(chapter_numbers)s::int[])")
        params["chapter_numbers"] = chapter_numbers
    else:
        if from_chapter:
            conditions.append("chapter_number >= %(from_chapter)s")
            params["from_chapter"] = from_chapter
        if to_chapter:
            conditions.append("chapter_number <= %(to_chapter)s")
            params["to_chapter"] = to_chapter

    with connect() as conn:
        rows = conn.execute(
            f"""
            UPDATE chapters
            SET is_downloaded = FALSE,
                raw_text_content = CASE WHEN %(clear_raw)s THEN NULL ELSE raw_text_content END,
                updated_at = now()
            WHERE {' AND '.join(conditions)}
            RETURNING id
            """,
            params,
        ).fetchall()

    if touch_story_catalog:
        request_story_recrawl(story_id)
    return len(rows)


def set_chapter_quality_status(chapter_id: str, status: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE chapters
            SET quality_status = %(status)s, updated_at = now()
            WHERE id = %(chapter_id)s::uuid
            """,
            {"chapter_id": chapter_id, "status": status},
        )


def update_chapter_quality_audit(
    chapter_id: str,
    *,
    status: str,
    audit_version: int,
    issues: list[dict[str, Any]],
    blocking_count: int = 0,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE chapters
            SET quality_status = %(status)s,
                quality_audit_version = %(audit_version)s,
                quality_checked_at = now(),
                quality_issues = %(issues)s::jsonb,
                updated_at = now()
            WHERE id = %(chapter_id)s::uuid
            """,
            {
                "chapter_id": chapter_id,
                "status": status,
                "audit_version": audit_version,
                "issues": Jsonb(issues),
            },
        )


def request_quality_repair(
    chapter_id: str,
    action: str,
    *,
    repair_hints: str = "",
    dry_run: bool = False,
    force_running: bool = False,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Atomic repolish/retranslate enqueue — resets done polish_chapter jobs."""
    action = (action or "repolish").strip().lower()
    if action not in {"repolish", "retranslate"}:
        raise ValueError(f"unknown repair action: {action}")

    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                c.id, c.chapter_number, c.story_id, c.title AS chapter_title,
                c.raw_text_path, c.raw_language, c.quality_repair_attempts,
                s.title AS story_title, src.code AS source_code
            FROM chapters c
            JOIN stories s ON s.id = c.story_id
            JOIN sources src ON src.id = s.source_id
            WHERE c.id = %(chapter_id)s::uuid
            """,
            {"chapter_id": chapter_id},
        ).fetchone()
        if row is None:
            return {"ok": False, "error": "chapter_not_found"}
        attempts = int(row["quality_repair_attempts"] or 0)
        if attempts >= max_attempts:
            if not dry_run:
                conn.execute(
                    """
                    UPDATE chapters
                    SET quality_status = 'failed_manual', updated_at = now()
                    WHERE id = %(chapter_id)s::uuid
                    """,
                    {"chapter_id": chapter_id},
                )
            return {
                "ok": False,
                "error": "max_repair_attempts",
                "attempts": attempts,
                "chapter_number": row["chapter_number"],
            }

        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "action": action,
                "chapter_number": row["chapter_number"],
                "attempts": attempts + 1,
            }

        repair_payload = {
            "quality_repair": True,
            "repair_action": action,
            "repair_hints": repair_hints,
        }

        if action == "retranslate":
            conn.execute(
                """
                UPDATE chapters
                SET is_translated = FALSE,
                    is_polished = FALSE,
                    translated_text_content = NULL,
                    polished_text_content = NULL,
                    quality_status = 'failed',
                    quality_last_action = 'retranslate',
                    quality_repair_attempts = quality_repair_attempts + 1,
                    updated_at = now()
                WHERE id = %(chapter_id)s::uuid
                """,
                {"chapter_id": chapter_id},
            )
        else:
            conn.execute(
                """
                UPDATE chapters
                SET is_polished = FALSE,
                    quality_status = 'failed',
                    quality_last_action = 'repolish',
                    quality_repair_attempts = quality_repair_attempts + 1,
                    updated_at = now()
                WHERE id = %(chapter_id)s::uuid
                """,
                {"chapter_id": chapter_id},
            )

        status_filter = "" if force_running else "AND status NOT IN ('running')"
        updated = conn.execute(
            f"""
            UPDATE story_jobs
            SET status = 'pending',
                attempts = 0,
                run_after = now(),
                locked_by = NULL,
                locked_at = NULL,
                last_error = NULL,
                payload = COALESCE(payload, '{{}}'::jsonb) || %(repair_payload)s::jsonb,
                updated_at = now()
            WHERE job_type = 'polish_chapter'
              AND chapter_id = %(chapter_id)s::uuid
              {status_filter}
            RETURNING id
            """,
            {"chapter_id": chapter_id, "repair_payload": Jsonb(repair_payload)},
        ).fetchall()

        if not updated:
            from genre_prompts import find_char_map_file, resolve_genre_from_context

            raw_text_path = row["raw_text_path"] or ""
            slug = ""
            if raw_text_path:
                from pathlib import Path

                slug = Path(raw_text_path).parent.name
            chapter_num = int(row["chapter_number"] or 0)
            raw_language = row["raw_language"] or "en"
            story_id = str(row["story_id"])
            source_code = row["source_code"] or ""
            char_map_file = find_char_map_file(story_id=story_id, slug=slug)
            genre = resolve_genre_from_context(
                "",
                raw_language=raw_language,
                source_code=source_code,
                char_map_file=char_map_file,
            )
            chapter_stem = f"chapter{chapter_num:04d}"
            if raw_text_path:
                from pathlib import Path

                chapter_stem = Path(raw_text_path).stem
            payload = {
                "raw_language": raw_language,
                "story_slug": slug,
                "chapter_number": chapter_num,
                "chapter_title": row["chapter_title"] or chapter_stem,
                "post_translate": "polish",
                "genre": genre,
                "char_map_file": char_map_file,
                **repair_payload,
            }
            conn.execute(
                """
                INSERT INTO story_jobs (
                    job_type, chapter_id, story_id, source_code, model,
                    input_path, output_path, payload, priority, max_attempts, status
                )
                VALUES (
                    'polish_chapter', %(chapter_id)s::uuid, %(story_id)s::uuid,
                    %(source_code)s, 'qwen3:14b', %(input_path)s, %(output_path)s,
                    %(payload)s::jsonb, 50, 3, 'pending'
                )
                ON CONFLICT (job_type, chapter_id)
                DO UPDATE SET
                    status = 'pending',
                    attempts = 0,
                    run_after = now(),
                    locked_by = NULL,
                    locked_at = NULL,
                    last_error = NULL,
                    payload = COALESCE(story_jobs.payload, '{}'::jsonb) || EXCLUDED.payload,
                    updated_at = now()
                """,
                {
                    "chapter_id": chapter_id,
                    "story_id": story_id,
                    "source_code": source_code,
                    "input_path": raw_text_path or None,
                    "output_path": None,
                    "payload": Jsonb(payload),
                },
            )

    return {
        "ok": True,
        "action": action,
        "chapter_number": row["chapter_number"],
        "attempts": attempts + 1,
    }
