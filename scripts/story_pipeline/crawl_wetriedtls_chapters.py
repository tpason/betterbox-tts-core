#!/usr/bin/env python3
"""Crawl We Tried TLs (wetriedtls.com) story catalog and chapters.

URL patterns:
  Series: https://wetriedtls.com/series/{slug}
  Chapter: https://wetriedtls.com/series/{slug}/chapter-{N}

Content is embedded as Next.js RSC streaming scripts in the HTML.
All chapters with price=0 are free.

Usage:
  python crawl_wetriedtls_chapters.py \\
    --series-slug a-regressors-tale-of-cultivation \\
    --upsert-db --download-text --no-write-files

  # With file output:
  python crawl_wetriedtls_chapters.py \\
    --series-slug a-regressors-tale-of-cultivation \\
    --upsert-db --download-text --enqueue-polish --post-translate polish
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup
from requests import Session
from requests.adapters import HTTPAdapter

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from genre_prompts import find_char_map_file, resolve_genre_from_context  # noqa: E402
from crawl_utils import looks_blocked  # noqa: E402

BASE_URL = "https://wetriedtls.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
# Lines to strip from chapter content (translator notes, site noise)
DROP_PREFIXES = (
    "Translator",
    "Editor",
    "Discord:",
    "https://dsc.gg/",
    "Link to my ko-fi",
    "Join the Discord",
    "Support us",
    "Prev Chapter",
    "Next Chapter",
    "Home",
    "Novels",
    "Store",
    "Homepage",
)


def compact_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _make_session() -> Session:
    sess = Session()
    sess.headers.update(HEADERS)
    adapter = HTTPAdapter(pool_connections=2, pool_maxsize=4, max_retries=0)
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    return sess


def fetch_html(url: str, timeout: int = 30, retries: int = 5, retry_sleep: float = 2.0, *, session: Session | None = None) -> str:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = (session or requests).get(url, headers=HEADERS, timeout=timeout)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(retry_sleep * attempt)
    raise RuntimeError(f"Cannot fetch {url} after {retries} attempts: {last_error}") from last_error


def _decode_next_script(raw: str) -> str:
    """Decode a raw __next_f.push string value (JSON-encoded)."""
    try:
        return json.loads('"' + raw + '"')
    except Exception:
        return raw


def extract_chapter_meta(html: str) -> dict[str, Any]:
    """Extract chapter metadata (chapter_name, chapter_title, price) from Next.js RSC."""
    pattern = r'self\.__next_f\.push\(\[1,"(.*?)"\]\)'
    for m in re.finditer(pattern, html, re.DOTALL):
        decoded = _decode_next_script(m.group(1))
        if '"price"' in decoded and '"chapter_name"' in decoded:
            price_m = re.search(r'"price"\s*:\s*(\d+)', decoded)
            name_m = re.search(r'"chapter_name"\s*:\s*"([^"]*)"', decoded)
            title_m = re.search(r'"chapter_title"\s*:\s*"([^"]*)"', decoded)
            slug_m = re.search(r'"chapter_slug"\s*:\s*"([^"]*)"', decoded)
            return {
                "price": int(price_m.group(1)) if price_m else 0,
                "chapter_name": name_m.group(1) if name_m else "",
                "chapter_title": title_m.group(1) if title_m else "",
                "chapter_slug": slug_m.group(1) if slug_m else "",
            }
    return {}


def extract_chapter_content(html: str) -> str:
    """Extract and clean chapter body text from Next.js RSC HTML content."""
    pattern = r'self\.__next_f\.push\(\[1,"(.*?)"\]\)'
    content_html = ""
    for m in re.finditer(pattern, html, re.DOTALL):
        decoded = _decode_next_script(m.group(1))
        if decoded.strip().startswith("<p") and len(decoded) > 300:
            content_html = decoded
            break
    if not content_html:
        return ""
    soup = BeautifulSoup(content_html, "html.parser")
    lines = [compact_text(line) for line in soup.get_text("\n").splitlines()]
    cleaned = []
    for line in lines:
        if not line:
            continue
        if any(line.startswith(p) for p in DROP_PREFIXES):
            continue
        cleaned.append(line)
    return "\n\n".join(cleaned).strip()


def extract_story_metadata(html: str, series_slug: str) -> dict[str, Any]:
    """Extract story title, author, description, cover from series page."""
    soup = BeautifulSoup(html, "html.parser")

    # Title
    title = ""
    og_title = soup.select_one("meta[property='og:title']")
    if og_title:
        raw = compact_text(str(og_title.get("content") or ""))
        title = re.sub(r"\s*[-–|]\s*We Tried TLS.*", "", raw, flags=re.IGNORECASE).strip()
    if not title:
        h1 = soup.select_one("h1")
        title = compact_text(h1.get_text(" ", strip=True)) if h1 else series_slug.replace("-", " ").title()

    # Description — strip HTML tags from meta description
    description = ""
    meta_desc = soup.select_one("meta[name='description']")
    if meta_desc:
        raw_desc = str(meta_desc.get("content") or "")
        # Remove "Read X on We Tried TLS - " prefix
        raw_desc = re.sub(r"^Read .+? on We Tried TLS\s*[-–]\s*", "", raw_desc)
        desc_soup = BeautifulSoup(raw_desc, "html.parser")
        description = compact_text(desc_soup.get_text(" ", strip=True))

    # Cover and author from __next_f data
    cover = ""
    author = ""
    pattern = r'self\.__next_f\.push\(\[1,"(.*?)"\]\)'
    for m in re.finditer(pattern, html, re.DOTALL):
        decoded = _decode_next_script(m.group(1))
        if '"thumbnail"' in decoded and series_slug in decoded:
            thumb_m = re.search(r'"thumbnail"\s*:\s*"(https?://[^"]+)"', decoded)
            if thumb_m:
                cover = thumb_m.group(1)
            author_m = re.search(r'"(?:author|studio)"\s*:\s*"([^"]{2,80})"', decoded)
            if author_m:
                author = author_m.group(1)
            break

    # Max chapter from page
    chapter_nums = [int(n) for n in re.findall(r'chapter-(\d+)', html)]
    max_chapter = max(chapter_nums) if chapter_nums else 0

    return {
        "title": title,
        "author": author,
        "description": description,
        "cover_image_url": cover,
        "max_chapter": max_chapter,
        "series_slug": series_slug,
        "source_story_id": series_slug,
        "story_url": f"{BASE_URL}/series/{series_slug}",
        "source": "wetriedtls",
    }


def upsert_catalog_to_db(metadata: dict[str, Any]) -> dict[str, Any]:
    from story_db.story_pipeline_db import repository as repo
    from genre_prompts import infer_genre_from_story_signals

    genre = infer_genre_from_story_signals(
        title=metadata.get("title") or "",
        description=metadata.get("description") or "",
        raw_language="en",
        source_code="wetriedtls",
    )
    repo.upsert_source("wetriedtls", "We Tried TLs", BASE_URL)
    return repo.upsert_story(
        "wetriedtls",
        {
            "source_story_id": metadata["source_story_id"],
            "title": metadata["title"],
            "original_title": metadata["title"],
            "author": metadata.get("author") or "",
            "description": metadata.get("description") or "",
            "language": "en",
            "source_url": metadata["story_url"],
            "catalog_url": metadata["story_url"],
            "cover_image_url": metadata.get("cover_image_url") or "",
            "total_chapters": metadata.get("max_chapter") or 0,
            "is_completed": False,
            "metadata": {
                "slug": metadata["series_slug"],
                "source": "wetriedtls",
                "source_author": metadata.get("author") or "",
                "source_description": metadata.get("description") or "",
                "genre": genre,
            },
        },
    )


def chapter_file_path(root: Path, slug: str, number: int) -> Path:
    return root / slug / f"chapter{number:04d}.txt"


def write_if_needed(path: Path, text: str, overwrite: bool) -> bool:
    # DB-only mode: never write raw text to disk.
    return True


def enqueue_polish_job(*, story: dict[str, Any], db_chapter: dict[str, Any], slug: str, raw_path: Path | None = None, args: argparse.Namespace) -> None:
    from story_db.story_pipeline_db import repository as repo
    chapter_stem = f"chapter{db_chapter['chapter_number']:04d}"
    char_map_file = find_char_map_file(story_id=str(story.get("id") or ""), slug=slug)
    repo.enqueue_chapter_job(
        "polish_chapter",
        db_chapter["id"],
        story_id=story["id"],
        source_code="wetriedtls",
        model=args.translate_model,
        input_path=None,
        output_path=None,
        payload={
            "raw_language": "en",
            "story_slug": slug,
            "chapter_number": db_chapter["chapter_number"],
            "chapter_title": db_chapter.get("title") or chapter_stem,
            "source_chapter_title": db_chapter.get("title") or chapter_stem,
            "translate_story_metadata": True,
            "source_story_title": story.get("original_title") or story.get("title") or "",
            "post_translate": args.post_translate,
            "genre": resolve_genre_from_context(
                "",
                raw_language="en",
                source_code="wetriedtls",
                char_map_file=char_map_file,
                title=str(story.get("original_title") or story.get("title") or ""),
                description=str((story.get("metadata") or {}).get("source_description") or story.get("description") or ""),
            ),
            "char_map_file": char_map_file,
        },
        max_attempts=args.polish_max_attempts,
    )


def crawl_chapters(story: dict[str, Any], metadata: dict[str, Any], args: argparse.Namespace, session: Session) -> dict[str, int]:
    from story_db.story_pipeline_db import repository as repo

    slug = metadata["series_slug"]
    saved = skipped = failed = locked = 0

    chapter_range = range(args.from_chapter or 1, (args.to_chapter or metadata["max_chapter"]) + 1)
    if args.max_chapters:
        chapter_range = range(chapter_range.start, min(chapter_range.stop, chapter_range.start + args.max_chapters))

    for number in chapter_range:
        url = f"{BASE_URL}/series/{slug}/chapter-{number}"

        try:
            html = fetch_html(url, timeout=args.timeout, retries=args.retries, retry_sleep=args.retry_sleep, session=session)

            # Check price
            ch_meta = extract_chapter_meta(html)
            if ch_meta.get("price", 0) != 0:
                print(f"[SKIP] premium/locked ch{number:04d} price={ch_meta['price']}", flush=True)
                locked += 1
                continue

            content = extract_chapter_content(html)
            if not content or len(content) < args.min_text_chars:
                print(f"[SKIP] short/empty ch{number:04d} ({len(content)} chars)", flush=True)
                skipped += 1
                continue
            # Only run looks_blocked when price field is absent (trustful fallback)
            if ch_meta.get("price") is None and looks_blocked(content):
                print(f"[SKIP] looks_blocked ch{number:04d}", flush=True)
                locked += 1
                continue

            chapter_name = ch_meta.get("chapter_name") or f"Chapter {number}"
            chapter_title = ch_meta.get("chapter_title") or ""
            full_title = f"{chapter_name}: {chapter_title}" if chapter_title else chapter_name
            text = f"{full_title}\n\n{content}".strip() + "\n"

            db_chapter = repo.upsert_chapter(story["id"], {
                "source_chapter_id": str(number),
                "chapter_number": number,
                "title": full_title,
                "source_url": url,
                "raw_language": "en",
                "raw_text_path": None,
                "raw_text_content": text,
                "is_downloaded": True,
            })
            saved += 1
            print(f"[TEXT] db-only ch{number:04d} title={full_title!r} chars={len(text)}", flush=True)
            if args.enqueue_polish:
                enqueue_polish_job(story=story, db_chapter=db_chapter, slug=slug, args=args)

        except Exception as exc:
            failed += 1
            print(f"[WARN] ch{number:04d} failed {url}: {type(exc).__name__}: {exc}", flush=True)
            if args.stop_on_error:
                raise

        time.sleep(args.chapter_delay)

    return {"saved": saved, "skipped": skipped, "failed": failed, "locked": locked}


def main() -> None:
    parser = argparse.ArgumentParser(description="Crawl We Tried TLs chapters.")
    parser.add_argument("--series-slug", default="a-regressors-tale-of-cultivation", help="Series slug from URL.")
    parser.add_argument("--default-author", default="", help="Author name nếu site không cung cấp (vd: Tremendous).")
    parser.add_argument("--from-chapter", type=int, default=0)
    parser.add_argument("--to-chapter", type=int, default=0)
    parser.add_argument("--max-chapters", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--retry-sleep", type=float, default=2.0)
    parser.add_argument("--chapter-delay", type=float, default=1.5)
    parser.add_argument("--min-text-chars", type=int, default=100)
    parser.add_argument("--upsert-db", action="store_true")
    parser.add_argument("--download-text", action="store_true")
    parser.add_argument("--no-write-files", action="store_true", help="Lưu text vào DB, không ghi file ra disk.")
    parser.add_argument("--raw-en-output-root", default="story_data/raw_en")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--stop-on-error", action="store_true")
    parser.add_argument("--enqueue-polish", action="store_true")
    parser.add_argument("--polished-output-root", default="story_data/polished")
    parser.add_argument("--translated-output-root", default="story_data/translated")
    parser.add_argument("--translate-model", default="qwen3:14b")
    parser.add_argument("--polish-max-attempts", type=int, default=3)
    parser.add_argument("--post-translate", choices=("polish", "copy"), default="copy")
    args = parser.parse_args()

    series_slug = args.series_slug
    series_url = f"{BASE_URL}/series/{series_slug}"
    session = _make_session()

    print(f"[FETCH] series page: {series_url}", flush=True)
    series_html = fetch_html(series_url, timeout=args.timeout, retries=args.retries, retry_sleep=args.retry_sleep, session=session)
    metadata = extract_story_metadata(series_html, series_slug)
    if args.default_author and not metadata.get("author"):
        metadata["author"] = args.default_author
    print(f"[META] title={metadata['title']!r} author={metadata.get('author')!r} max_chapter={metadata['max_chapter']}", flush=True)

    story: dict[str, Any] | None = None
    if args.upsert_db or args.download_text:
        story = upsert_catalog_to_db(metadata)
        print(f"[DB] story_id={story['id']}", flush=True)

    # If max_chapter unknown and to_chapter not specified, probe by fetching chapter 900 down
    if metadata["max_chapter"] == 0 and not args.to_chapter:
        print("[PROBE] max_chapter unknown — probing...", flush=True)
        for probe in (900, 870, 861, 850, 830, 813, 800):
            try:
                probe_html = fetch_html(f"{BASE_URL}/series/{series_slug}/chapter-{probe}", timeout=15, retries=2, retry_sleep=1.0, session=session)
                probe_meta = extract_chapter_meta(probe_html)
                if probe_meta.get("chapter_name"):
                    metadata["max_chapter"] = probe
                    print(f"[PROBE] max_chapter={probe}", flush=True)
                    break
            except Exception:
                pass

    if not args.download_text:
        print(f"[DONE] catalog upserted. Use --download-text to fetch chapters.", flush=True)
        return

    assert story is not None
    stats = crawl_chapters(story, metadata, args, session)
    session.close()

    print(
        f"[DONE] {metadata['title']} | "
        f"saved={stats['saved']} skipped={stats['skipped']} "
        f"failed={stats['failed']} locked={stats['locked']}",
        flush=True,
    )


if __name__ == "__main__":
    main()
