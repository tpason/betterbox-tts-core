#!/usr/bin/env python3
"""Crawl NovelFire (novelfire.net) story catalog and chapters using Playwright.

NovelFire is Cloudflare-protected, requiring browser automation.
All chapters are free, no login required.

URL patterns:
  Story:   https://novelfire.net/novel/{slug}
  Chapters list: https://novelfire.net/novel/{slug}/chapters (or paginated)
  Chapter: https://novelfire.net/novel/{slug}/{chapter-slug}

Usage (from Docker — recommended):
  # Crawl catalog only
  docker compose exec story-crawler-scheduler python /app/scripts/story_pipeline/crawl_novelfire_chapters.py \
    --story-url https://novelfire.net/novel/shadow-slave --upsert-db

  # Crawl catalog + download chapters
  docker compose exec story-crawler-scheduler python /app/scripts/story_pipeline/crawl_novelfire_chapters.py \
    --story-url https://novelfire.net/novel/shadow-slave --upsert-db --download-text --enqueue-polish

  # With existing story_id in DB
  docker compose exec story-crawler-scheduler python /app/scripts/story_pipeline/crawl_novelfire_chapters.py \
    --story-url https://novelfire.net/novel/shadow-slave --story-id <uuid> --download-text

  # With manual Cloudflare bypass (headful mode)
  docker compose exec story-crawler-scheduler python /app/scripts/story_pipeline/crawl_novelfire_chapters.py \
    --story-url https://novelfire.net/novel/shadow-slave --headful --manual-wait 30
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.story_pipeline.crawl_utils import looks_blocked  # noqa: E402
from scripts.story_pipeline.genre_prompts import find_char_map_file, resolve_genre_from_context  # noqa: E402
from scripts.story_pipeline.crawl_stories_from_db import enqueue_polish_for_args, upsert_downloaded_chapter  # noqa: E402
from story_db.story_pipeline_db import repository as repo  # noqa: E402


BASE_URL = "https://novelfire.net"


def import_playwright():
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "Playwright not installed. Install with:\n"
            "  pip install playwright\n"
            "  playwright install chromium\n"
        ) from exc
    return sync_playwright, PlaywrightTimeoutError


def compact_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def safe_slug(value: str, fallback: str = "novelfire-story") -> str:
    import unicodedata
    normalized = unicodedata.normalize("NFKD", (value or "").strip().lower())
    ascii_val = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_val).strip("-")
    return slug or fallback


def story_slug_from_url(url: str) -> str:
    parts = [p for p in urlparse(url).path.split("/") if p]
    if "novel" in parts:
        idx = parts.index("novel")
        if len(parts) > idx + 1:
            return parts[idx + 1]
    return parts[-1] if parts else ""


def canonical_story_url(url: str) -> str:
    slug = story_slug_from_url(url)
    return f"{BASE_URL}/novel/{slug}" if slug else url


def chapter_path(root: Path, slug: str, number: int) -> Path:
    return root / slug / f"chapter{number:04d}.txt"


def write_if_needed(path: Path, text: str, overwrite: bool) -> bool:
    # DB-only mode: never write raw text to disk.
    return True


# ---------------------------------------------------------------------------
# Catalog parsing (from rendered page HTML)
# ---------------------------------------------------------------------------

@dataclass
class NovelFireChapter:
    number: int
    title: str
    url: str
    source_chapter_id: str


def _extract_chapter_number(title: str, url: str, fallback: int) -> int:
    for text in (url, title):
        m = re.search(r"chapter[-_]?(\d+)", text, re.IGNORECASE)
        if m:
            return int(m.group(1))
    m = re.search(r"/(\d+)(?:[^/]*)?$", urlparse(url).path)
    if m:
        return int(m.group(1))
    return fallback


def parse_chapter_list_html(html: str, base_url: str) -> list[NovelFireChapter]:
    """Parse chapter list from NovelFire catalog or chapter-list page."""
    soup = BeautifulSoup(html, "html.parser")
    chapters: list[NovelFireChapter] = []
    seen: set[str] = set()

    # NovelFire chapter list: .chapter-card or li with chapter links
    for anchor in soup.select(
        "a[href*='/novel/'][href*='chapter'], "
        ".chapter-list a, .chapter-card a, "
        "li.chapter-item a, .chapters-list a"
    ):
        href = str(anchor.get("href") or "")
        if not href:
            continue
        chapter_url = urljoin(base_url, href).split("?", 1)[0].rstrip("/")
        if chapter_url in seen or "novel" not in chapter_url:
            continue
        seen.add(chapter_url)

        title_text = compact_text(anchor.get_text(" ", strip=True))
        number = _extract_chapter_number(title_text, chapter_url, len(chapters) + 1)
        title = title_text or f"Chapter {number}"

        chapters.append(
            NovelFireChapter(
                number=number,
                title=title,
                url=chapter_url,
                source_chapter_id=str(number),
            )
        )

    return sorted(chapters, key=lambda c: c.number)


def extract_story_metadata(html: str, story_url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")

    title_node = soup.select_one("h1.novel-title, h1[class*='title'], h1") or soup.select_one("h1")
    title = compact_text(title_node.get_text(" ", strip=True)) if title_node else ""

    # og:title fallback
    if not title:
        og = soup.select_one("meta[property='og:title']")
        title = compact_text(str(og.get("content") or "")) if og else ""

    author_node = (
        soup.select_one("a[href*='/author/']")
        or soup.select_one(".novel-author")
        or soup.select_one("[class*='author']")
    )
    author = compact_text(author_node.get_text(" ", strip=True)) if author_node else ""

    cover_node = soup.select_one("img.novel-cover, .novel-cover img, img[alt*='cover']")
    cover = ""
    if not cover_node:
        og = soup.select_one("meta[property='og:image']")
        cover = compact_text(str(og.get("content") or "")) if og else ""
    else:
        cover = str(cover_node.get("data-src") or cover_node.get("src") or "")
    cover = urljoin(story_url, cover) if cover else ""

    tags = [
        compact_text(a.get_text(" ", strip=True))
        for a in soup.select(".genre-tag a, .tag-item a, a[href*='/genre/'], a[href*='/tag/']")
        if compact_text(a.get_text(" ", strip=True))
    ]

    desc_node = (
        soup.select_one(".novel-description")
        or soup.select_one(".novel-desc")
        or soup.select_one("meta[name='description']")
    )
    if desc_node and desc_node.name == "meta":
        description = compact_text(str(desc_node.get("content") or ""))
    else:
        description = compact_text(desc_node.get_text(" ", strip=True)) if desc_node else ""

    status = ""
    page_text = soup.get_text(" ", strip=True)
    if re.search(r"\bcompleted?\b", page_text, re.IGNORECASE):
        status = "Completed"
    elif re.search(r"\bongoing\b", page_text, re.IGNORECASE):
        status = "Ongoing"

    slug = story_slug_from_url(story_url)
    return {
        "source": "novelfire",
        "story_url": canonical_story_url(story_url),
        "slug": slug,
        "title": title or slug.replace("-", " ").title(),
        "author": author,
        "tags": list(dict.fromkeys(tags)),
        "category": ", ".join(tags),
        "status": status,
        "description": description,
        "cover_image_url": cover,
    }


# ---------------------------------------------------------------------------
# Chapter text extraction
# ---------------------------------------------------------------------------

DROP_MARKERS = (
    "Prev Chapter",
    "Next Chapter",
    "NovelFire",
    "novelfire.net",
    "Report chapter",
    "Add to library",
    "Read more",
)


def extract_chapter_text_from_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    node = (
        soup.select_one("#chapter-container")
        or soup.select_one(".chapter-content")
        or soup.select_one(".chapter-text")
        or soup.select_one("#chapterText")
        or soup.select_one("article")
    )
    if node is None:
        return ""
    for removable in node.select("script, style, noscript, iframe, .ads, .advertisement, ins, button"):
        removable.decompose()
    lines = [compact_text(line) for line in node.get_text("\n", strip=True).splitlines()]
    lines = [line for line in lines if line and not any(m.lower() in line.lower() for m in DROP_MARKERS)]
    return "\n\n".join(lines).strip()


def extract_chapter_text_from_page(page: Any) -> str:
    """Extract chapter text from a live Playwright page."""
    html = page.content()
    content = extract_chapter_text_from_html(html)
    if not content:
        # Fallback: inner_text of body, then filter
        try:
            body_text = page.locator("body").inner_text(timeout=10_000)
            lines = [compact_text(line) for line in body_text.splitlines()]
            lines = [line for line in lines if line and not any(m.lower() in line.lower() for m in DROP_MARKERS)]
            content = "\n\n".join(lines).strip()
        except Exception:
            pass
    return content


def extract_chapter_title_from_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for sel in ["h1.chapter-title", "h1", ".chapter-title", "h2"]:
        node = soup.select_one(sel)
        if node:
            text = compact_text(node.get_text(" ", strip=True))
            if text and "novelfire" not in text.lower():
                return text
    return ""


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def upsert_story_to_db(catalog: dict[str, Any]) -> dict[str, Any]:
    repo.upsert_source("novelfire", "NovelFire", BASE_URL)
    return repo.upsert_story(
        "novelfire",
        {
            "source_story_id": catalog.get("slug"),
            "title": catalog.get("title") or catalog.get("slug") or "NovelFire Story",
            "original_title": catalog.get("title"),
            "author": catalog.get("author"),
            "category": ", ".join(catalog.get("tags") or []) or catalog.get("category"),
            "status": catalog.get("status"),
            "language": "en",
            "source_url": catalog.get("story_url"),
            "catalog_url": catalog.get("story_url"),
            "description": catalog.get("description"),
            "cover_image_url": catalog.get("cover_image_url"),
            "total_chapters": catalog.get("total_chapters") or len(catalog.get("chapters") or []),
            "is_completed": str(catalog.get("status") or "").lower() in {"completed", "complete"},
            "metadata": {
                "slug": catalog.get("slug"),
                "source": "novelfire",
                "tags": catalog.get("tags") or [],
                "source_author": catalog.get("author") or "",
                "source_description": catalog.get("description") or "",
            },
        },
    )


def upsert_chapters_to_db(story: dict[str, Any], chapters: list[dict[str, Any]]) -> int:
    for idx, ch in enumerate(chapters, start=1):
        number = int(ch.get("number") or idx)
        repo.upsert_chapter(
            story["id"],
            {
                "source_chapter_id": str(ch.get("source_chapter_id") or number),
                "chapter_number": number,
                "title": ch.get("title") or f"Chapter {number}",
                "source_url": ch.get("url") or "",
                "raw_language": "en",
                "is_downloaded": False,
            },
        )
    return len(chapters)


# ---------------------------------------------------------------------------
# Main Playwright crawl loop
# ---------------------------------------------------------------------------

def run_crawl(args: argparse.Namespace) -> None:
    story_url = canonical_story_url(args.story_url)
    slug = story_slug_from_url(story_url)
    if not slug:
        raise SystemExit(f"Cannot derive slug from URL: {story_url}")

    chapters_url = f"{story_url}/chapters"
    output_dir = Path(args.raw_en_output_root) / slug
    output_dir.mkdir(parents=True, exist_ok=True)

    sync_playwright, PlaywrightTimeoutError = import_playwright()
    profile_dir = Path(args.profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)

    story: dict[str, Any] | None = None
    if args.story_id:
        story = repo.get_story_by_id(args.story_id)
        print(f"[DB] using existing story_id={story['id']}", flush=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=profile_dir.as_posix(),
            headless=not args.headful,
            viewport={"width": 1366, "height": 900},
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            args=["--disable-blink-features=AutomationControlled", "--lang=en-US"],
        )
        page = context.pages[0] if context.pages else context.new_page()

        # Step 1: Load story page for metadata
        print(f"[FETCH] story page: {story_url}", flush=True)
        page.goto(story_url, wait_until="domcontentloaded", timeout=args.timeout * 1000)
        if args.manual_wait > 0:
            print(f"[WAIT] manual wait {args.manual_wait}s for Cloudflare bypass...", flush=True)
            page.wait_for_timeout(args.manual_wait * 1000)
        page.wait_for_timeout(args.wait_ms)

        story_html = page.content()
        metadata = extract_story_metadata(story_html, page.url)
        print(f"[META] title={metadata['title']!r} author={metadata['author']!r}", flush=True)

        # Step 2: Load chapter list
        all_chapters: list[NovelFireChapter] = []
        chapter_page_num = 1
        while True:
            list_url = chapters_url if chapter_page_num == 1 else f"{chapters_url}?page={chapter_page_num}"
            print(f"[FETCH] chapter list page {chapter_page_num}: {list_url}", flush=True)
            page.goto(list_url, wait_until="domcontentloaded", timeout=args.timeout * 1000)
            page.wait_for_timeout(args.wait_ms)

            html = page.content()
            page_chapters = parse_chapter_list_html(html, page.url)
            if not page_chapters:
                break
            before = len(all_chapters)
            seen_nums = {c.number for c in all_chapters}
            new_chs = [c for c in page_chapters if c.number not in seen_nums]
            all_chapters.extend(new_chs)
            if len(all_chapters) == before:
                break

            # Check for next page
            soup = BeautifulSoup(html, "html.parser")
            next_btn = soup.select_one("a[href*='?page='][class*='next'], .pagination a[rel='next']")
            if not next_btn or args.max_catalog_pages and chapter_page_num >= args.max_catalog_pages:
                break
            chapter_page_num += 1
            time.sleep(1.0)

        all_chapters.sort(key=lambda c: c.number)
        print(f"[CATALOG] found {len(all_chapters)} chapters", flush=True)

        # Filter range
        chapters = all_chapters
        if args.from_chapter:
            chapters = [c for c in chapters if c.number >= args.from_chapter]
        if args.to_chapter:
            chapters = [c for c in chapters if c.number <= args.to_chapter]
        if args.max_chapters:
            chapters = chapters[:args.max_chapters]

        # Save catalog JSON
        catalog_data = {
            **metadata,
            "total_chapters": len(all_chapters),
            "chapters": [asdict(c) for c in all_chapters],
            "crawled_at": datetime.now(timezone.utc).isoformat(),
        }
        catalog_path = Path(args.catalog_output_root) / "novelfire" / slug / "chapters.json"
        catalog_path.parent.mkdir(parents=True, exist_ok=True)
        catalog_path.write_text(json.dumps(catalog_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        # Step 3: Upsert to DB
        if args.upsert_db and story is None:
            catalog_data["total_chapters"] = len(all_chapters)
            story = upsert_story_to_db(catalog_data)
            upsert_chapters_to_db(story, [asdict(c) for c in all_chapters])
            print(f"[DB] upserted story_id={story['id']} chapters={len(all_chapters)}", flush=True)

        if not args.download_text:
            context.close()
            print(f"[DONE] {metadata['title']} | chapters={len(all_chapters)} | catalog={catalog_path}", flush=True)
            return

        # Step 4: Download chapter text
        imported = skipped = failed = 0
        for ch in chapters:
            raw_path = chapter_path(output_dir, slug, ch.number)
            if raw_path.exists() and raw_path.stat().st_size > 0 and not args.overwrite:
                skipped += 1
                print(f"[SKIP] exists ch{ch.number:04d}", flush=True)
                if story and args.enqueue_polish:
                    db_ch = upsert_downloaded_chapter(
                        story,
                        source_chapter_id=ch.source_chapter_id,
                        chapter_number=ch.number,
                        title=ch.title,
                        source_url=ch.url,
                        raw_language="en",
                        raw_path=raw_path,
                        raw_text_content=raw_path.read_text(encoding="utf-8"),
                    )
                    enqueue_polish_for_args("novelfire", story, db_ch, slug, raw_path, "en", args)
                continue

            try:
                page.goto(ch.url, wait_until="domcontentloaded", timeout=args.timeout * 1000)
                page.wait_for_timeout(args.wait_ms)

                content = extract_chapter_text_from_page(page)
                if not content or len(content) < args.min_text_chars:
                    print(f"[SKIP] short/empty ch{ch.number:04d} url={ch.url}", flush=True)
                    skipped += 1
                    continue
                if looks_blocked(content):
                    print(f"[SKIP] locked/paywall ch{ch.number:04d}", flush=True)
                    skipped += 1
                    continue

                # Get better title from page if available
                title = ch.title
                page_title = extract_chapter_title_from_html(page.content())
                if page_title and re.match(r"^Chapter\s+\d+$", title, re.IGNORECASE):
                    title = page_title

                text = f"{title}\n\n{content}".strip() + "\n"
                imported += 1
                print(f"[OK] ch{ch.number:04d} chars={len(content)} title={title!r}", flush=True)

                if story:
                    db_ch = upsert_downloaded_chapter(
                        story,
                        source_chapter_id=ch.source_chapter_id,
                        chapter_number=ch.number,
                        title=title,
                        source_url=ch.url,
                        raw_language="en",
                        raw_path=None,
                        raw_text_content=text,
                    )
                    if args.enqueue_polish:
                        enqueue_polish_for_args("novelfire", story, db_ch, slug, None, "en", args)

            except PlaywrightTimeoutError as exc:
                failed += 1
                print(f"[WARN] timeout ch{ch.number:04d} url={ch.url}: {exc}", flush=True)
            except Exception as exc:
                failed += 1
                print(f"[WARN] failed ch{ch.number:04d} url={ch.url}: {type(exc).__name__}: {exc}", flush=True)

            time.sleep(args.chapter_delay)

        context.close()
        print(
            f"[DONE] {metadata['title']} | imported={imported} skipped={skipped} failed={failed}",
            flush=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Crawl NovelFire chapters via Playwright.")
    parser.add_argument("--story-url", required=True, help="NovelFire novel URL, e.g. https://novelfire.net/novel/shadow-slave")
    parser.add_argument("--story-id", default="", help="Existing DB story UUID to use instead of creating new.")
    parser.add_argument("--from-chapter", type=int, default=0)
    parser.add_argument("--to-chapter", type=int, default=0)
    parser.add_argument("--max-chapters", type=int, default=0)
    parser.add_argument("--max-catalog-pages", type=int, default=0)
    parser.add_argument("--profile-dir", default=".browser/novelfire", help="Persistent browser profile for Cloudflare bypass.")
    parser.add_argument("--headful", action="store_true", help="Run browser in headful mode (for manual Cloudflare bypass).")
    parser.add_argument("--manual-wait", type=int, default=0, help="Seconds to pause on first page for manual CAPTCHA solving.")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--wait-ms", type=int, default=2000, help="Wait after page load (ms).")
    parser.add_argument("--chapter-delay", type=float, default=1.5)
    parser.add_argument("--min-text-chars", type=int, default=200)
    parser.add_argument("--upsert-db", action="store_true")
    parser.add_argument("--download-text", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--enqueue-polish", action="store_true")
    parser.add_argument("--raw-en-output-root", default="story_data/raw_en")
    parser.add_argument("--catalog-output-root", default="story_data/catalogs")
    parser.add_argument("--translated-output-root", default="story_data/translated")
    parser.add_argument("--polished-output-root", default="story_data/polished")
    parser.add_argument("--translate-model", default="qwen3:14b")
    parser.add_argument("--polish-max-attempts", type=int, default=3)
    parser.add_argument("--post-translate", choices=("polish", "copy"), default="copy")
    args = parser.parse_args()
    run_crawl(args)


if __name__ == "__main__":
    main()
