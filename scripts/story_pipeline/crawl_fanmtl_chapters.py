#!/usr/bin/env python3
"""Crawl FanMTL (fanmtl.com) story catalog and chapter text.

FanMTL hosts machine-translated Chinese/Korean/Japanese web novels in English.
All chapters are free, no login required.

URL patterns:
  Story:   https://fanmtl.com/novel/{novel-id}.html
  Chapter: https://fanmtl.com/novel/{novel-id}_{chapter_num}.html
  AJAX chapters: https://fanmtl.com/e/extend/fy.php?page={N}&wjm={novel-id}

Usage (from Docker — recommended):
  # Standalone crawl
  docker compose exec story-crawler-scheduler python /app/scripts/story_pipeline/crawl_fanmtl_chapters.py \
    https://fanmtl.com/novel/ke383028.html --upsert-db --download-text --enqueue-polish

  # With known story_id
  docker compose exec story-crawler-scheduler python /app/scripts/story_pipeline/crawl_fanmtl_chapters.py \
    https://fanmtl.com/novel/ke383028.html --upsert-db --download-text

  # Discovery from homepage (rank/ path may 404 after www redirect)
  docker compose exec story-crawler-scheduler python /app/scripts/story_pipeline/crawl_fanmtl_chapters.py \
    --discover-url https://www.fanmtl.com/ --discover-limit 20 --upsert-db
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
from requests import Session
from requests.adapters import HTTPAdapter

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from genre_prompts import find_char_map_file, resolve_genre_from_context  # noqa: E402
from crawl_utils import looks_blocked  # noqa: E402


BASE_URL = "https://fanmtl.com"
AJAX_URL = f"{BASE_URL}/e/extend/fy.php"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BASE_URL,
}


@dataclass
class FanMTLChapter:
    number: int
    title: str
    url: str
    source_chapter_id: str


def compact_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def safe_slug(value: str, fallback: str = "fanmtl-story") -> str:
    import unicodedata
    normalized = unicodedata.normalize("NFKD", (value or "").strip().lower())
    ascii_val = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_val).strip("-")
    return slug or fallback


def novel_id_from_url(url: str) -> str:
    """Extract novel ID like 'ke383028' from /novel/ke383028.html"""
    path = urlparse(url).path
    match = re.search(r"/novel/([^/._]+?)(?:_\d+)?\.html", path)
    return match.group(1) if match else ""


def chapter_num_from_url(url: str) -> int:
    """Extract chapter number from /novel/{id}_{N}.html"""
    match = re.search(r"_(\d+)\.html$", urlparse(url).path)
    return int(match.group(1)) if match else 0


def canonical_story_url(url: str) -> str:
    novel_id = novel_id_from_url(url)
    if novel_id:
        return f"{BASE_URL}/novel/{novel_id}.html"
    return url


def _make_session() -> Session:
    sess = Session()
    sess.headers.update(HEADERS)
    adapter = HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0)
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    return sess


def fetch_html(
    url: str,
    timeout: int = 30,
    retries: int = 5,
    retry_sleep: float = 2.0,
    *,
    session: Session | None = None,
) -> str:
    import random
    own = session is None
    sess = session if session is not None else _make_session()
    last_err: Exception | None = None
    try:
        for attempt in range(1, retries + 1):
            try:
                resp = sess.get(url, timeout=timeout)
                resp.raise_for_status()
                return resp.text
            except requests.RequestException as exc:
                last_err = exc
                if attempt < retries:
                    sleep = retry_sleep * attempt + random.uniform(0, 0.5)
                    print(f"[WARN] fanmtl retry {attempt}/{retries} {url}: {exc}", flush=True)
                    time.sleep(sleep)
    finally:
        if own:
            sess.close()
    raise RuntimeError(f"FanMTL fetch failed after {retries} attempts: {url} | {last_err}") from last_err


def _parse_chapter_cards(html: str, base_url: str) -> list[FanMTLChapter]:
    """Parse chapter cards from story page or AJAX fragment."""
    soup = BeautifulSoup(html, "html.parser")
    chapters: list[FanMTLChapter] = []
    for li in soup.select("ul.chapter-list li, li.volume-item"):
        anchor = li.select_one("a[href]")
        if not anchor:
            continue
        href = str(anchor.get("href") or "")
        chapter_url = urljoin(base_url, href).split("#", 1)[0]
        if not chapter_url.endswith(".html"):
            continue
        num = chapter_num_from_url(chapter_url)
        if not num:
            continue
        no_node = anchor.select_one("span.chapter-no, .chapter-no")
        title_node = anchor.select_one("strong.chapter-title, .chapter-title")
        no_text = compact_text(no_node.get_text(" ", strip=True)) if no_node else ""
        title_text = compact_text(title_node.get_text(" ", strip=True)) if title_node else ""
        # title_text already contains "Chapter N ..." so don't prepend the bare number
        if title_text:
            title = title_text
        elif no_text:
            title = f"Chapter {num}" if re.fullmatch(r"\d+", no_text) else no_text
        else:
            title = f"Chapter {num}"
        chapters.append(
            FanMTLChapter(
                number=num,
                title=title,
                url=chapter_url,
                source_chapter_id=str(num),
            )
        )
    return chapters


def parse_catalog(
    story_url: str,
    timeout: int = 30,
    retries: int = 5,
    retry_sleep: float = 2.0,
    max_catalog_pages: int = 0,
    from_chapter: int = 0,
    to_chapter: int = 0,
    max_chapters: int = 0,
) -> dict[str, Any]:
    story_url = canonical_story_url(story_url)
    novel_id = novel_id_from_url(story_url)
    session = _make_session()
    try:
        story_html = fetch_html(story_url, timeout, retries, retry_sleep, session=session)
        soup = BeautifulSoup(story_html, "html.parser")

        # Metadata
        title_node = soup.select_one("h1[itemprop='name'], h1.novel-title, h1")
        title = compact_text(title_node.get_text(" ", strip=True)) if title_node else ""

        author_node = soup.select_one("span[itemprop='author'], .author a, .author")
        author = compact_text(author_node.get_text(" ", strip=True)) if author_node else ""
        author = re.sub(r"^(Author|By)\s*:?\s*", "", author, flags=re.IGNORECASE).strip()

        cover_node = soup.select_one("figure.cover img, .cover img, .novel-cover img")
        cover = ""
        if cover_node:
            cover = str(cover_node.get("data-src") or cover_node.get("src") or "")
            cover = urljoin(story_url, cover) if cover else ""

        tags = [
            compact_text(a.get_text(" ", strip=True))
            for a in soup.select(".categories li a, .tags a, a[href*='/category/'], a[href*='/genre/']")
            if compact_text(a.get_text(" ", strip=True))
        ]

        desc_node = soup.select_one(".summary .content, .summary p, .description, #summary")
        description = compact_text(desc_node.get_text(" ", strip=True)) if desc_node else ""

        status_text = soup.get_text(" ", strip=True)
        status = ""
        if re.search(r"\bcompleted?\b", status_text, re.IGNORECASE):
            status = "Completed"
        elif re.search(r"\bongoing\b", status_text, re.IGNORECASE):
            status = "Ongoing"

        total_node = soup.select_one(".header-stats strong, .chapter-count")
        total_chapters = 0
        if total_node:
            m = re.search(r"(\d+)", total_node.get_text())
            total_chapters = int(m.group(1)) if m else 0

        # Chapter list — page 1 from story HTML
        chapters_seen: dict[int, FanMTLChapter] = {}
        for ch in _parse_chapter_cards(story_html, story_url):
            chapters_seen[ch.number] = ch

        # AJAX pages for remaining chapters
        page = 1
        while True:
            if max_catalog_pages and page >= max_catalog_pages:
                break
            ajax_html = fetch_html(
                f"{AJAX_URL}?page={page}&wjm={novel_id}",
                timeout, retries, retry_sleep, session=session,
            )
            new_chapters = _parse_chapter_cards(ajax_html, BASE_URL)
            if not new_chapters:
                break
            before = len(chapters_seen)
            for ch in new_chapters:
                chapters_seen[ch.number] = ch
            if len(chapters_seen) == before:
                break
            page += 1
            time.sleep(retry_sleep * 0.5)

        chapters = sorted(chapters_seen.values(), key=lambda c: c.number)

        if from_chapter:
            chapters = [c for c in chapters if c.number >= from_chapter]
        if to_chapter:
            chapters = [c for c in chapters if c.number <= to_chapter]
        if max_chapters:
            chapters = chapters[:max_chapters]

        return {
            "source": "fanmtl",
            "novel_id": novel_id,
            "story_url": story_url,
            "slug": safe_slug(title) or novel_id,
            "title": title or novel_id,
            "author": author,
            "tags": list(dict.fromkeys(tags)),
            "category": ", ".join(tags),
            "status": status,
            "description": description,
            "cover_image_url": cover,
            "total_chapters": total_chapters or (chapters[-1].number if chapters else 0),
            "chapters": [asdict(c) for c in chapters],
            "crawled_at": datetime.now(timezone.utc).isoformat(),
        }
    finally:
        session.close()


def extract_chapter_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    node = (
        soup.select_one("div.chapter-content")
        or soup.select_one("#chapter-content")
        or soup.select_one(".reading-content")
        or soup.select_one("article")
    )
    if node is None:
        return ""
    for removable in node.select("script, style, noscript, iframe, .ads, .advertisement, ins"):
        removable.decompose()
    lines = [compact_text(line) for line in node.get_text("\n", strip=True).splitlines()]
    drop_markers = (
        "Prev Chapter",
        "Next Chapter",
        "FanMTL",
        "fanmtl.com",
        "Read more chapters",
    )
    lines = [line for line in lines if line and not any(m.lower() in line.lower() for m in drop_markers)]
    return "\n\n".join(lines).strip()


def fetch_chapter_text(url: str, timeout: int = 30, retries: int = 5, retry_sleep: float = 2.0) -> str:
    return extract_chapter_text(fetch_html(url, timeout, retries, retry_sleep))


def discover_story_urls(
    urls: list[str],
    *,
    timeout: int = 30,
    retries: int = 3,
    retry_sleep: float = 2.0,
    limit: int = 0,
) -> list[str]:
    discovered: list[str] = []
    seen: set[str] = set()
    for seed in urls:
        html = fetch_html(seed, timeout, retries, retry_sleep)
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.select("a[href*='/novel/']"):
            href = str(a.get("href") or "")
            if not href.endswith(".html") or "_" in href.split("/novel/")[-1]:
                continue
            url = urljoin(seed, href).split("?", 1)[0]
            novel_id = novel_id_from_url(url)
            if not novel_id or url in seen:
                continue
            seen.add(url)
            discovered.append(canonical_story_url(url))
            if limit and len(discovered) >= limit:
                return discovered
    return discovered


def upsert_catalog_to_db(catalog: dict[str, Any]) -> dict[str, Any]:
    from story_db.story_pipeline_db import repository as repo
    repo.upsert_source("fanmtl", "FanMTL", BASE_URL)
    return repo.upsert_story(
        "fanmtl",
        {
            "source_story_id": catalog.get("novel_id") or catalog.get("slug"),
            "title": catalog.get("title") or catalog.get("slug") or "FanMTL Story",
            "original_title": catalog.get("title") or catalog.get("slug"),
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
                "novel_id": catalog.get("novel_id"),
                "source": "fanmtl",
                "tags": catalog.get("tags") or [],
                "source_author": catalog.get("author") or "",
                "source_description": catalog.get("description") or "",
            },
        },
    )


def upsert_catalog_chapters_to_db(story: dict[str, Any], catalog: dict[str, Any]) -> int:
    from story_db.story_pipeline_db import repository as repo
    count = 0
    for idx, ch in enumerate(catalog.get("chapters") or [], start=1):
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
        count += 1
    return count


def chapter_path(root: Path, slug: str, number: int) -> Path:
    return root / slug / f"chapter{number:04d}.txt"


def write_if_needed(path: Path, text: str, overwrite: bool, persist: bool = True) -> bool:
    # DB-only mode: never write raw text to disk.
    return True


def enqueue_polish_job(
    *,
    story: dict[str, Any],
    db_chapter: dict[str, Any],
    slug: str,
    raw_path: Path | None,
    args: argparse.Namespace,
) -> dict[str, Any]:
    from story_db.story_pipeline_db import repository as repo
    chapter_stem = f"chapter{db_chapter['chapter_number']:04d}"
    category = str(story.get("category") or " ".join((story.get("metadata") or {}).get("tags") or []))
    char_map_file = find_char_map_file(story_id=str(story.get("id") or ""), slug=slug)
    return repo.enqueue_chapter_job(
        "polish_chapter",
        db_chapter["id"],
        story_id=story["id"],
        source_code="fanmtl",
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
            "source_story_author": (story.get("metadata") or {}).get("source_author") or story.get("author") or "",
            "source_story_description": (story.get("metadata") or {}).get("source_description") or story.get("description") or "",
            "post_translate": args.post_translate,
            "genre": resolve_genre_from_context(
                category,
                raw_language="en",
                source_code="fanmtl",
                char_map_file=char_map_file,
            ),
            "char_map_file": char_map_file,
        },
        max_attempts=args.polish_max_attempts,
    )


def download_chapters_to_db(
    story: dict[str, Any],
    catalog: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, int]:
    from story_db.story_pipeline_db import repository as repo

    slug = str(catalog.get("slug") or story.get("source_story_id") or "fanmtl-story")
    saved = skipped = failed = jobs = 0

    for idx, ch in enumerate(catalog.get("chapters") or [], start=1):
        number = int(ch.get("number") or idx)
        raw_path = chapter_path(Path(args.raw_en_output_root), slug, number)
        title = ch.get("title") or f"Chapter {number}"

        if raw_path.exists() and not args.overwrite:
            content = raw_path.read_text(encoding="utf-8")
            db_chapter = repo.upsert_chapter(
                story["id"],
                {
                    "source_chapter_id": str(ch.get("source_chapter_id") or number),
                    "chapter_number": number,
                    "title": title,
                    "source_url": ch.get("url") or "",
                    "raw_language": "en",
                    "raw_text_path": None,
                    "raw_text_content": content,
                    "is_downloaded": True,
                },
            )
            skipped += 1
            if args.enqueue_polish:
                enqueue_polish_job(story=story, db_chapter=db_chapter, slug=slug, raw_path=raw_path, args=args)
                jobs += 1
            continue

        try:
            content = fetch_chapter_text(ch["url"], timeout=args.timeout, retries=args.retries, retry_sleep=args.retry_sleep)
            if not content or len(content) < args.min_text_chars:
                print(f"[SKIP] short/empty fanmtl {slug}/chapter{number:04d}")
                skipped += 1
                continue
            if looks_blocked(content):
                print(f"[SKIP] locked/paywall fanmtl {slug}/chapter{number:04d}")
                skipped += 1
                continue
            text = f"{title}\n\n{content}".strip() + "\n"
            write_if_needed(raw_path, text, args.overwrite)
            db_chapter = repo.upsert_chapter(
                story["id"],
                {
                    "source_chapter_id": str(ch.get("source_chapter_id") or number),
                    "chapter_number": number,
                    "title": title,
                    "source_url": ch.get("url") or "",
                    "raw_language": "en",
                    "raw_text_path": None,
                    "raw_text_content": text,
                    "is_downloaded": True,
                },
            )
            saved += 1
            if args.enqueue_polish:
                enqueue_polish_job(story=story, db_chapter=db_chapter, slug=slug, raw_path=raw_path, args=args)
                jobs += 1
            print(f"[TEXT] saved {slug}/chapter{number:04d}: {raw_path}")
        except Exception as exc:
            failed += 1
            print(f"[WARN] fanmtl chapter failed {ch.get('url')}: {type(exc).__name__}: {exc}")
            if args.stop_on_error:
                raise
        time.sleep(args.chapter_delay)

    return {"saved": saved, "skipped": skipped, "failed": failed, "jobs": jobs}


def main() -> None:
    parser = argparse.ArgumentParser(description="Crawl FanMTL story catalog and chapter text.")
    parser.add_argument("story_url", nargs="*", default=[])
    parser.add_argument("--discover-url", nargs="*", default=[])
    parser.add_argument("--discover-limit", type=int, default=0)
    parser.add_argument("--from-chapter", type=int, default=0)
    parser.add_argument("--to-chapter", type=int, default=0)
    parser.add_argument("--max-chapters", type=int, default=0)
    parser.add_argument("--max-catalog-pages", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--retry-sleep", type=float, default=2.0)
    parser.add_argument("--output-root", default="story_data/catalogs/fanmtl")
    parser.add_argument("--upsert-db", action="store_true")
    parser.add_argument("--upsert-chapters", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--download-text", action="store_true")
    parser.add_argument("--raw-en-output-root", default="story_data/raw_en")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--min-text-chars", type=int, default=80)
    parser.add_argument("--chapter-delay", type=float, default=1.5)
    parser.add_argument("--stop-on-error", action="store_true")
    parser.add_argument("--enqueue-polish", action="store_true")
    parser.add_argument("--polished-output-root", default="story_data/polished")
    parser.add_argument("--translated-output-root", default="story_data/translated")
    parser.add_argument("--translate-model", default="qwen3:14b")
    parser.add_argument("--polish-max-attempts", type=int, default=3)
    parser.add_argument("--post-translate", choices=("polish", "copy"), default="copy")
    args = parser.parse_args()

    story_urls = list(args.story_url)
    if args.discover_url:
        story_urls.extend(
            discover_story_urls(
                list(args.discover_url),
                timeout=args.timeout,
                retries=args.retries,
                retry_sleep=args.retry_sleep,
                limit=args.discover_limit,
            )
        )
    story_urls = list(dict.fromkeys(canonical_story_url(u) for u in story_urls))
    if not story_urls:
        parser.error("Provide at least one story URL or --discover-url.")

    for story_url in story_urls:
        catalog = parse_catalog(
            story_url,
            timeout=args.timeout,
            retries=args.retries,
            retry_sleep=args.retry_sleep,
            max_catalog_pages=args.max_catalog_pages,
            from_chapter=args.from_chapter,
            to_chapter=args.to_chapter,
            max_chapters=args.max_chapters,
        )
        output_path = Path(args.output_root) / str(catalog["slug"]) / "chapters.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        story: dict[str, Any] | None = None
        db_suffix = download_suffix = ""

        if args.upsert_db:
            story = upsert_catalog_to_db(catalog)
            ch_count = upsert_catalog_chapters_to_db(story, catalog) if args.upsert_chapters else 0
            db_suffix = f" | db_story={story['id']} db_chapters={ch_count}"

        if args.download_text:
            if story is None:
                story = upsert_catalog_to_db(catalog)
                if args.upsert_chapters:
                    upsert_catalog_chapters_to_db(story, catalog)
            stats = download_chapters_to_db(story, catalog, args)
            download_suffix = (
                f" | raw_saved={stats['saved']} raw_skipped={stats['skipped']} "
                f"raw_failed={stats['failed']} jobs={stats['jobs']}"
            )

        print(f"[OK] {catalog['title']} | chapters={len(catalog.get('chapters') or [])}/{catalog.get('total_chapters') or 0} | {output_path}")
        if db_suffix:
            print(f"[DB] {catalog['title']}{db_suffix}")
        if download_suffix:
            print(f"[TEXT] {catalog['title']}{download_suffix}")


if __name__ == "__main__":
    main()
