#!/usr/bin/env python3
"""Backfill chapter titles từ alternative sources.

Vấn đề: Một số chapter có title sai hoặc thiếu subtitle do:
  1. Source gốc không cung cấp subtitle (bare "Chapter N")
  2. Source gốc không còn accessible (404 / site đã đóng)
  3. maybe_update_translated_chapter_title ghi đè title đúng bằng prose

Strategies (thử theo thứ tự):
  raw-scan      Scan dòng đầu raw_text_content để tìm subtitle (không cần HTTP)
  refetch       Re-fetch chapter/story page để lấy subtitle từ HTML/API
  cross-source  Tìm cùng chapter trong story khác trong DB
  llm-infer     Dùng LLM để tạo title từ raw_text_content (experimental)

Sau khi tìm được title:
  --translate   Dịch title EN/KO sang tiếng Việt qua Ollama

Usage:
  # Xem những gì sẽ đổi (dry-run mặc định)
  python backfill_chapter_titles.py --source hako

  # Áp dụng thay đổi
  python backfill_chapter_titles.py --source hako --apply

  # Royalroad: refresh catalog (1 req/story thay vì 1 req/chapter)
  python backfill_chapter_titles.py --source royalroad --apply

  # Dịch title EN/KO tìm được sang tiếng Việt
  python backfill_chapter_titles.py --source jadescrolls --strategy cross-source --translate --apply

  # Legacy lightnovelpub fix-files mode
  python backfill_chapter_titles.py --fix-files --apply
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCRIPTS_DIR = ROOT / "scripts" / "story_pipeline"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from story_db.story_pipeline_db.db import connect  # noqa: E402
from scripts.story_pipeline.crawl_utils import compact_text  # noqa: E402


# ── Title validation ───────────────────────────────────────────────────────────
_BARE_TITLE_RE = re.compile(r"^(Chapter|Chương|chapter|chương)\s+\d+[\s.]*$", re.IGNORECASE)
_LLM_ARTIFACT_RE = re.compile(r"^\((No text|The text|Narration|văn bản)", re.IGNORECASE)
_PROSE_SENTENCE_RE = re.compile(r"[!?]\s+\S|[.]\s+[A-ZÀÁÂÃÈÉÊÌÍÒÓÔÕÙÚÝĐÀ-ỹ]")


def is_suspicious_title(title: str) -> bool:
    if not title or not title.strip():
        return True
    t = title.strip()
    if _BARE_TITLE_RE.match(t):
        return True
    if _LLM_ARTIFACT_RE.match(t):
        return True
    return False


def is_valid_replacement(title: str) -> bool:
    """True nếu title đủ tốt để ghi vào DB."""
    if not title or not title.strip():
        return False
    t = title.strip()
    if len(t) > 120:
        return False
    if _BARE_TITLE_RE.match(t):
        return False
    if _LLM_ARTIFACT_RE.match(t):
        return False
    if _PROSE_SENTENCE_RE.search(t):
        return False
    return True


# ── Language detection ─────────────────────────────────────────────────────────
_KOREAN_RE = re.compile(r"[가-힣ᄀ-ᇿ]")
_CHINESE_RE = re.compile(r"[一-鿿]")
_VIETNAMESE_DIACRITIC_RE = re.compile(
    r"[ắằẳẵặấầẩẫậếềểễệốồổỗộớờởỡợứừửữựỉịọụỳỷỹỵđ]", re.IGNORECASE
)


def detect_language(text: str) -> str:
    """Trả về 'vi', 'en', 'ko', 'zh', hoặc 'unknown'."""
    if not text:
        return "unknown"
    if _KOREAN_RE.search(text):
        return "ko"
    if _CHINESE_RE.search(text):
        return "zh"
    if _VIETNAMESE_DIACRITIC_RE.search(text):
        return "vi"
    ascii_ratio = sum(1 for c in text if ord(c) < 128) / max(len(text), 1)
    if ascii_ratio > 0.75:
        return "en"
    return "unknown"


# ── DB queries ─────────────────────────────────────────────────────────────────
def fetch_affected_chapters(
    conn,
    *,
    source_code: str | None,
    story_id: str | None,
    story_title_filter: str | None,
    include_artifacts: bool,
    limit: int,
) -> list[dict]:
    conditions = ["c.is_downloaded = TRUE"]
    params: list = []

    title_conds = [r"c.title ~ '^(Chapter|Chương|chapter|chương)\s+\d+[\s.]*$'"]
    if include_artifacts:
        title_conds.append(r"c.title ~ '^\((No text|The text|Narration)'")
        title_conds.append("(c.title IS NULL OR c.title = '')")
    conditions.append(f"({' OR '.join(title_conds)})")

    if source_code:
        conditions.append("sc.code = %s")
        params.append(source_code)
    if story_id:
        conditions.append("c.story_id = %s")
        params.append(story_id)
    if story_title_filter:
        conditions.append("s.title ILIKE %s")
        params.append(f"%{story_title_filter}%")

    where = " AND ".join(conditions)
    params.append(limit)

    rows = conn.execute(
        f"""
        SELECT c.id, c.story_id, c.chapter_number, c.title AS old_title,
               c.source_url, c.raw_text_path, c.raw_text_content,
               c.is_translated, c.is_polished,
               s.title AS story_title, s.original_title,
               s.source_url AS story_source_url,
               sc.code AS source_code
        FROM chapters c
        JOIN stories s ON s.id = c.story_id
        JOIN sources sc ON sc.id = s.source_id
        WHERE {where}
        ORDER BY sc.code, c.story_id, c.chapter_number
        LIMIT %s
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


# ── Strategy: raw-scan ─────────────────────────────────────────────────────────
# Một số crawlers (lightnovelpub, wetriedtls) store thêm full title trong
# các dòng ngay sau dòng đầu. Pattern: "Chapter N - Subtitle" hoặc "Chapter N: Subtitle"
_CHAPTER_WITH_SUBTITLE_RE = re.compile(
    r"^(?:Chapter|Chương)\s+\d+\s*[-:]\s*(.+)", re.IGNORECASE
)


def raw_scan_title(chapter: dict) -> str | None:
    """Scan 10 dòng đầu của raw_text_content để tìm subtitle.

    Xử lý trường hợp lightnovelpub/wetriedtls lưu bare title ở line 1,
    nhưng full title dạng "Chapter N - Subtitle" ở line 3 hoặc 5.
    """
    raw = chapter.get("raw_text_content", "") or ""
    if not raw:
        return None
    for line in raw.split("\n")[:10]:
        line = line.strip()
        if not line:
            continue
        m = _CHAPTER_WITH_SUBTITLE_RE.match(line)
        if m:
            subtitle = m.group(1).strip()
            # Tránh "Chapter N: Chapter N - Subtitle" (nested prefix)
            inner = _CHAPTER_WITH_SUBTITLE_RE.match(subtitle)
            if inner:
                return subtitle.strip()  # unwrap: lấy phần "Chapter N - Subtitle"
            return line.strip()  # giữ full "Chapter N - Subtitle"
    return None


# ── Strategy: refetch ──────────────────────────────────────────────────────────
_SOURCE_HTML_SELECTORS: dict[str, list[str]] = {
    "royalroad": [".chapter-title h1", "h1.font-white", ".page-content-wrapper h1", "h1"],
    "hako": ["h1.chapter-title", ".chapter-title", "h1", "h2"],
    "skydemonorder": ["h1", ".chapter-title", "h2"],
    "wetriedtls": ["h1", ".entry-title", ".chapter-title", "h2"],
    "jadescrolls": ["h1", ".chapter-title"],
    "truyenyy": ["h1", ".chapter-title"],
    "naver_series": ["h1", ".episode-title", ".chapter-title"],
}


def _extract_from_html_generic(html: str, selectors: list[str]) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for sel in selectors:
        node = soup.select_one(sel)
        if node:
            text = compact_text(node.get_text(" ", strip=True))
            if text:
                return text
    return ""


def _fetch_html_safe(url: str, session: requests.Session, timeout: int = 20) -> str | None:
    try:
        resp = session.get(url, timeout=timeout)
        if resp.status_code == 404:
            print(f"    [404] {url}")
            return None
        if resp.status_code >= 400:
            print(f"    [HTTP {resp.status_code}] {url}")
            return None
        return resp.text
    except requests.exceptions.Timeout:
        print(f"    [TIMEOUT] {url}")
        return None
    except Exception as exc:
        print(f"    [FETCH ERROR] {url}: {exc}")
        return None


def refetch_chapter_title_html(chapter: dict, session: requests.Session, delay: float) -> str | None:
    """Fetch chapter page HTML và extract title. None nếu không tìm được."""
    source_code = chapter.get("source_code", "")
    source_url = chapter.get("source_url", "")
    if not source_url:
        return None

    # jadescrolls dùng API; royalroad dùng catalog (xử lý riêng)
    if source_code in ("jadescrolls", "royalroad"):
        return None

    time.sleep(delay)

    if source_code == "hako" and "lightnovelpub" in source_url:
        # lightnovelpub có dedicated extractor (tránh import vòng tròn)
        html = _fetch_html_safe(source_url, session)
        if not html:
            return None
        selectors = ["h1.chapter-title", ".chapter-title", "h1"]
        title = _extract_from_html_generic(html, selectors)
        if not title or _BARE_TITLE_RE.match(title.strip()):
            return None
        # Reject site name artifact
        if "lightnovelpub" in title.lower():
            return None
        return title.strip()

    html = _fetch_html_safe(source_url, session)
    if not html:
        return None
    selectors = _SOURCE_HTML_SELECTORS.get(source_code, ["h1"])
    title = _extract_from_html_generic(html, selectors)
    if not title or _BARE_TITLE_RE.match(title.strip()):
        return None
    return title.strip()


def royalroad_batch_catalog_titles(
    story_url: str, session: requests.Session, delay: float
) -> dict[int, str]:
    """Re-fetch royalroad story catalog → dict[chapter_number → title].

    1 HTTP request per story thay vì 1 request per chapter.
    """
    from scripts.story_pipeline.crawl_royalroad_chapters import parse_catalog

    time.sleep(delay)
    try:
        catalog = parse_catalog(story_url, timeout=30)
        return {int(ch["number"]): ch["title"] for ch in catalog.get("chapters", [])}
    except Exception as exc:
        print(f"    [RR-CATALOG ERROR] {story_url}: {exc}")
        return {}


def jadescrolls_batch_catalog_titles(
    story_url: str, session: requests.Session, delay: float
) -> dict[int, str]:
    """Re-fetch jadescrolls story catalog → dict[chapter_number → title]."""
    import argparse as _argparse
    from scripts.story_pipeline.crawl_jadescrolls_chapters import (
        parse_story_slug,
        get_story_by_slug,
        fetch_all_chapters,
    )

    time.sleep(delay)
    try:
        slug = parse_story_slug(story_url)
        _args = _argparse.Namespace(
            timeout=30, retries=2, retry_sleep=1.0,
            page_size=100, max_catalog_pages=0, catalog_delay=0.2,
        )
        story_payload = get_story_by_slug(slug, _args)
        chapters = fetch_all_chapters(str(story_payload["id"]), slug, _args)
        return {ch.number: ch.title for ch in chapters}
    except Exception as exc:
        print(f"    [JADESCROLLS-CATALOG ERROR] {story_url}: {exc}")
        return {}


# ── Strategy: cross-source lookup ─────────────────────────────────────────────
def cross_source_title(chapter: dict, conn) -> str | None:
    """Tìm cùng chapter trong story khác trong DB, ưu tiên polished > translated > downloaded."""
    story_title = chapter.get("story_title", "")
    original_title = chapter.get("original_title") or story_title
    chapter_number = chapter.get("chapter_number")
    story_id = chapter.get("story_id")

    rows = conn.execute(
        r"""
        SELECT c.title, c.is_polished, c.is_translated
        FROM chapters c
        JOIN stories s ON s.id = c.story_id
        WHERE c.story_id != %s
          AND c.chapter_number = %s
          AND c.is_downloaded = TRUE
          AND c.title IS NOT NULL AND c.title != ''
          AND NOT (c.title ~ '^(Chapter|Chương|chapter|chương)\s+\d+[\s.]*$')
          AND (
              s.title = %s OR s.original_title = %s
              OR s.title ILIKE %s OR s.original_title ILIKE %s
          )
        ORDER BY c.is_polished DESC, c.is_translated DESC
        LIMIT 5
        """,
        (story_id, chapter_number,
         story_title, original_title,
         f"%{story_title}%", f"%{original_title}%"),
    ).fetchall()

    for row in rows:
        title = dict(row).get("title", "")
        if title and not is_suspicious_title(title):
            return title
    return None


# ── Strategy: LLM infer from content ──────────────────────────────────────────
def llm_infer_title(chapter: dict, ollama_url: str, model: str, timeout: int = 90) -> str | None:
    """Dùng LLM để tạo title từ raw_text_content. Experimental — có thể hallucinate."""
    raw = chapter.get("raw_text_content", "") or ""
    if not raw:
        return None

    lines = raw.split("\n")
    # Skip bare first line
    start = 1 if lines and _BARE_TITLE_RE.match(lines[0].strip()) else 0
    # Skip blank lines at start
    while start < len(lines) and not lines[start].strip():
        start += 1
    preview = "\n".join(lines[start:start + 5])[:500].strip()
    if not preview:
        return None

    src_lang = detect_language(raw[:200])
    lang_instr = {
        "en": "The text is in English. Generate a chapter title in English only.",
        "ko": "The text is in Korean. Generate a short chapter title in English.",
        "zh": "The text is in Chinese. Generate a brief chapter title in Vietnamese.",
        "vi": "The text is in Vietnamese. Generate a brief chapter title in Vietnamese.",
    }.get(src_lang, "Generate a brief chapter title in the same language.")

    prompt = (
        f"/no_think\n"
        f"{lang_instr}\n"
        f"Based on this excerpt, write ONE short chapter title (max 8 words, no explanations):\n\n{preview}"
    )
    try:
        resp = requests.post(
            f"{ollama_url.rstrip('/')}/api/chat",
            json={
                "model": model,
                "stream": False,
                "messages": [{"role": "user", "content": prompt}],
                "options": {"temperature": 0.3, "num_ctx": 2048},
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        result = resp.json().get("message", {}).get("content", "").strip()
        result = re.sub(r"<think>.*?</think>", "", result, flags=re.DOTALL).strip()
        result = result.strip("\"'")
        if not result or _BARE_TITLE_RE.match(result):
            return None
        return result
    except Exception as exc:
        print(f"    [LLM-INFER ERROR] {exc}")
        return None


# ── Translation: EN/KO → Vietnamese ───────────────────────────────────────────
def translate_title_to_vi(
    title: str, src_lang: str, ollama_url: str, model: str, timeout: int = 60
) -> str:
    lang_note = {"ko": "Hàn", "en": "Anh", "zh": "Trung"}.get(src_lang, "nước ngoài")
    prompt = (
        f"/no_think\n"
        f"Dịch tên chương từ tiếng {lang_note} sang tiếng Việt. "
        f"Chỉ trả lời tên đã dịch, không giải thích:\n{title}"
    )
    try:
        resp = requests.post(
            f"{ollama_url.rstrip('/')}/api/chat",
            json={
                "model": model,
                "stream": False,
                "messages": [{"role": "user", "content": prompt}],
                "options": {"temperature": 0.1, "num_ctx": 1024},
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        result = resp.json().get("message", {}).get("content", "").strip()
        result = re.sub(r"<think>.*?</think>", "", result, flags=re.DOTALL).strip()
        return result.strip("\"'")
    except Exception as exc:
        print(f"    [TRANSLATE ERROR] {exc}")
        return ""


# ── DB write ───────────────────────────────────────────────────────────────────
def update_title_in_db(conn, chapter_id: str, new_title: str) -> None:
    conn.execute(
        "UPDATE chapters SET title = %s, updated_at = NOW() WHERE id = %s",
        (new_title, chapter_id),
    )


def update_title_in_file(raw_text_path: str, old_title: str, new_title: str) -> bool:
    """Replace first line của raw text file nếu nó match old_title."""
    path = Path(raw_text_path) if Path(raw_text_path).is_absolute() else ROOT / raw_text_path
    if not path.exists():
        return False
    content = path.read_text(encoding="utf-8")
    lines = content.split("\n", 1)
    if not lines:
        return False
    first_line = lines[0].strip()
    if first_line == old_title:
        new_content = new_title + ("\n" + lines[1] if len(lines) > 1 else "")
        path.write_text(new_content, encoding="utf-8")
        return True
    return False


# ── Legacy --fix-files mode ────────────────────────────────────────────────────
def fix_files_from_db(conn, story_title: str | None, dry_run: bool) -> dict[str, int]:
    """Fix text files có first line là bare title nhưng DB đã có subtitle đầy đủ."""
    params: list = ["%lightnovelpub%", r"^Chapter[[:space:]]+[0-9]+ - .+"]
    story_filter = ""
    if story_title:
        story_filter = "AND s.title ILIKE %s"
        params.append(f"%{story_title}%")

    sql = f"""
        SELECT DISTINCT ON (c.id)
            c.id, c.chapter_number, c.title AS db_title,
            c.raw_text_path, s.title AS story_title
        FROM chapters c
        JOIN stories s ON s.id = c.story_id
        WHERE c.source_url ILIKE %s
          AND c.raw_text_path IS NOT NULL
          AND c.title ~ %s
          {story_filter}
        ORDER BY c.id, c.chapter_number
    """

    updated = skipped = 0
    rows = conn.execute(sql, params).fetchall()
    bare_pattern = re.compile(r"^Chapter\s+\d+$", re.IGNORECASE)

    for row in rows:
        row = dict(row)
        path = Path(row["raw_text_path"]) if Path(row["raw_text_path"]).is_absolute() else ROOT / row["raw_text_path"]
        if not path.exists():
            skipped += 1
            continue
        content = path.read_text(encoding="utf-8")
        first_line = content.split("\n", 1)[0].strip()
        if not bare_pattern.match(first_line):
            skipped += 1
            continue
        db_title = row["db_title"]
        label = f"{row['story_title']} ch{row['chapter_number']:04d}"
        print(f"[FILE] {label}: file={first_line!r} → {db_title!r}")
        if not dry_run:
            new_content = db_title + content[len(first_line):]
            path.write_text(new_content, encoding="utf-8")
            updated += 1
        else:
            updated += 1

    return {"updated": updated, "skipped": skipped}


# ── Main ───────────────────────────────────────────────────────────────────────
def run(args: argparse.Namespace) -> None:
    strategies = set(s.strip() for s in args.strategy.split(","))
    use_all = "all" in strategies
    do_raw_scan = True  # always try raw-scan first (free, no HTTP)
    do_refetch = use_all or "refetch" in strategies
    do_cross = use_all or "cross-source" in strategies
    do_llm = use_all or "llm-infer" in strategies
    dry_run = not args.apply

    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 BetterBox-TTS chapter-title-backfill"

    print(f"[MODE] {'DRY-RUN' if dry_run else 'APPLY'} | strategies={args.strategy} | translate={args.translate}")

    total_updated = 0
    total_translated = 0
    total_file_updated = 0

    with connect() as conn:
        chapters = fetch_affected_chapters(
            conn,
            source_code=args.source,
            story_id=args.story_id,
            story_title_filter=args.story_title or None,
            include_artifacts=args.include_artifacts,
            limit=args.limit,
        )
        print(f"[INFO] {len(chapters)} chapters with suspicious titles")
        if not chapters:
            print("[INFO] Nothing to do.")
            return

        # Pre-fetch royalroad catalogs (1 HTTP per story)
        rr_catalog_cache: dict[str, dict[int, str]] = {}
        if do_refetch:
            rr_stories: dict[str, str] = {}
            js_stories: dict[str, str] = {}
            for ch in chapters:
                if ch["source_code"] == "royalroad":
                    rr_stories.setdefault(ch["story_id"], ch["story_source_url"])
                elif ch["source_code"] == "jadescrolls":
                    js_stories.setdefault(ch["story_id"], ch["story_source_url"])

            for sid, story_url in rr_stories.items():
                story_title = next(c["story_title"] for c in chapters if c["story_id"] == sid)
                print(f"[REFETCH] royalroad catalog: {story_title!r}")
                title_map = royalroad_batch_catalog_titles(story_url, session, args.delay)
                if title_map:
                    rr_catalog_cache[sid] = title_map
                    found = sum(1 for t in title_map.values() if not _BARE_TITLE_RE.match(t))
                    print(f"  → {len(title_map)} chapters, {found} with subtitles")

            for sid, story_url in js_stories.items():
                story_title = next(c["story_title"] for c in chapters if c["story_id"] == sid)
                print(f"[REFETCH] jadescrolls catalog: {story_title!r}")
                title_map = jadescrolls_batch_catalog_titles(story_url, session, args.delay)
                if title_map:
                    rr_catalog_cache[sid] = title_map
                    found = sum(1 for t in title_map.values() if not _BARE_TITLE_RE.match(t))
                    print(f"  → {len(title_map)} chapters, {found} with subtitles")

        for ch in chapters:
            chapter_id = ch["id"]
            chapter_num = ch.get("chapter_number")
            old_title = ch.get("old_title", "")
            source_code = ch.get("source_code", "")
            story_id = ch.get("story_id")

            if args.verbose:
                print(f"\n[CH] #{chapter_num} [{source_code}] {ch['story_title']!r}: {old_title!r}")

            new_title: str | None = None
            strategy_used = ""

            # Strategy 0: raw-scan (no HTTP, always try first)
            if new_title is None:
                candidate = raw_scan_title(ch)
                if candidate and is_valid_replacement(candidate) and candidate != old_title:
                    new_title = candidate
                    strategy_used = "raw-scan"

            # Strategy 1: refetch (HTTP)
            if do_refetch and new_title is None:
                if source_code in ("royalroad", "jadescrolls") and story_id in rr_catalog_cache:
                    candidate = rr_catalog_cache[story_id].get(chapter_num, "")
                    if candidate and is_valid_replacement(candidate) and candidate != old_title:
                        new_title = candidate
                        strategy_used = f"refetch(catalog/{source_code})"
                elif source_code not in ("royalroad", "jadescrolls"):
                    candidate = refetch_chapter_title_html(ch, session, args.delay)
                    if candidate and is_valid_replacement(candidate) and candidate != old_title:
                        new_title = candidate
                        strategy_used = "refetch(html)"

            # Strategy 2: cross-source DB lookup
            if do_cross and new_title is None:
                candidate = cross_source_title(ch, conn)
                if candidate and is_valid_replacement(candidate) and candidate != old_title:
                    new_title = candidate
                    strategy_used = "cross-source"

            # Strategy 3: LLM infer
            if do_llm and new_title is None:
                candidate = llm_infer_title(ch, args.ollama_url, args.model)
                if candidate and is_valid_replacement(candidate):
                    new_title = candidate
                    strategy_used = "llm-infer"

            if new_title is None:
                if args.verbose:
                    print(f"  → no recovery found")
                continue

            # Translation: dịch EN/KO → tiếng Việt
            if args.translate:
                src_lang = detect_language(new_title)
                if src_lang in ("en", "ko", "zh"):
                    translated = translate_title_to_vi(new_title, src_lang, args.ollama_url, args.model)
                    if translated and is_valid_replacement(translated):
                        print(f"  [TRANSLATE/{src_lang.upper()}] {new_title!r} → {translated!r}")
                        new_title = translated
                        total_translated += 1
                    else:
                        print(f"  [TRANSLATE/FAIL] giữ nguyên: {new_title!r}")

            label = f"{ch['story_title']!r} ch{chapter_num:04d}"
            print(
                f"  [{strategy_used}] {label}: {old_title!r} → {new_title!r}"
                + (" [DRY-RUN]" if dry_run else "")
            )

            if not dry_run:
                update_title_in_db(conn, chapter_id, new_title)
                conn.commit()
                raw_path = ch.get("raw_text_path")
                if raw_path and update_title_in_file(raw_path, old_title, new_title):
                    total_file_updated += 1

            total_updated += 1

    print(
        f"\n[DONE] updated={total_updated} translated={total_translated} files={total_file_updated}"
        + (" (dry-run)" if dry_run else "")
    )
    if dry_run:
        print("[DRY-RUN] Chạy với --apply để áp dụng.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill chapter titles từ alternative sources",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--apply", action="store_true", help="Áp dụng thay đổi (mặc định: dry-run)")
    parser.add_argument("--source", help="Chỉ xử lý source này (royalroad, hako, jadescrolls, ...)")
    parser.add_argument("--story-id", dest="story_id", help="Chỉ xử lý story này (UUID)")
    parser.add_argument("--story-title", dest="story_title", default="", help="Filter theo story title (partial match)")
    parser.add_argument(
        "--strategy",
        default="refetch,cross-source",
        help="Strategies: raw-scan, refetch, cross-source, llm-infer, all. Default: refetch,cross-source",
    )
    parser.add_argument("--translate", action="store_true", help="Dịch EN/KO titles sang tiếng Việt")
    parser.add_argument("--include-artifacts", action="store_true", help="Bao gồm cả LLM artifact titles")
    parser.add_argument("--fix-files", action="store_true", help="Fix text files từ DB titles (legacy mode)")
    parser.add_argument("--limit", type=int, default=1000, help="Số chapters tối đa (default: 1000)")
    parser.add_argument("--delay", type=float, default=0.5, help="Delay giữa HTTP requests (default: 0.5s)")
    parser.add_argument(
        "--ollama-url",
        default=os.environ.get("OLLAMA_URL", "http://localhost:11434"),
        help="Ollama URL (default: $OLLAMA_URL hoặc localhost:11434)",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("TRANSLATE_MODEL", "qwen3:14b"),
        help="Ollama model cho translate/llm-infer (default: $TRANSLATE_MODEL hoặc qwen3:14b)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if args.fix_files:
        dry_run = not args.apply
        print(f"[MODE] {'DRY-RUN' if dry_run else 'APPLY'} --fix-files")
        with connect() as conn:
            stats = fix_files_from_db(conn, args.story_title or None, dry_run)
            if dry_run:
                conn.rollback()
        print(f"\n[SUMMARY] files_updated={stats['updated']} skipped={stats['skipped']}")
        if dry_run:
            print("[DRY-RUN] Re-run với --apply để áp dụng.")
        return

    run(args)


if __name__ == "__main__":
    main()
