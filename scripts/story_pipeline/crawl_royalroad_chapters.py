#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.story_pipeline.crawl_utils import (  # noqa: E402
    compact_text as clean_text,
    fetch_html as _fetch_html_base,
    safe_slug as _safe_slug,
)


DEFAULT_STORY_URL = "https://www.royalroad.com/fiction/21220/mother-of-learning"


@dataclass
class RoyalRoadChapter:
    number: int
    title: str
    url: str
    source_chapter_id: str


def safe_slug(value: str) -> str:
    return _safe_slug(value, fallback="royalroad-story")


def story_slug(story_url: str, title: str = "") -> str:
    parsed = urlparse(story_url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 3 and parts[0] == "fiction":
        return safe_slug(parts[2])
    if title:
        return safe_slug(title)
    return safe_slug(parts[-1] if parts else "royalroad-story")


def fetch_html(url: str, timeout: int = 30, retries: int = 3, retry_sleep: float = 2.0) -> str:
    return _fetch_html_base(url, timeout, retries, retry_sleep, label="royalroad")


def parse_story_id(story_url: str) -> str:
    match = re.search(r"/fiction/(\d+)", story_url)
    return match.group(1) if match else story_url.rstrip("/").rsplit("/", 1)[-1]


def parse_chapter_id(url: str, fallback: int) -> str:
    match = re.search(r"/chapter/(\d+)", url)
    return match.group(1) if match else str(fallback)


def parse_catalog(story_url: str, timeout: int = 30, retries: int = 3, retry_sleep: float = 2.0) -> dict:
    soup = BeautifulSoup(fetch_html(story_url, timeout, retries, retry_sleep), "html.parser")

    title_node = soup.select_one("h1") or soup.select_one(".fic-title")
    title = clean_text(title_node.get_text(" ", strip=True)) if title_node else story_slug(story_url)

    author_node = (
        soup.select_one("h4 a[href*='/profile/']")
        or soup.select_one(".fic-header a[href*='/profile/']")
        or soup.select_one("[itemprop='author'] a")
        or soup.select_one("[itemprop='author']")
        or soup.select_one(".fic-author a")
        or soup.select_one(".fic-author")
    )
    author = clean_text(author_node.get_text(" ", strip=True)) if author_node else ""

    description_node = soup.select_one(".description") or soup.select_one(".fiction-info .hidden-content")
    description = clean_text(description_node.get_text(" ", strip=True)) if description_node else ""

    status = ""
    page_text = clean_text(soup.get_text(" ", strip=True))
    status_match = re.search(r"Status\s*:\s*([A-Za-z ]+?)(?:\s{2,}|Tags\s*:|Genres\s*:|$)", page_text)
    if status_match:
        status = clean_text(status_match.group(1))

    tags = [
        clean_text(node.get_text(" ", strip=True))
        for node in soup.select("a[href*='/fictions/search?tagsAdd='], .tags a, .label")
        if clean_text(node.get_text(" ", strip=True))
    ]

    cover_node = soup.select_one("img.thumbnail") or soup.select_one(".fiction-info img") or soup.select_one("img[src]")
    cover_image_url = ""
    if cover_node:
        cover_image_url = cover_node.get("data-src") or cover_node.get("src") or ""
        cover_image_url = urljoin(story_url, cover_image_url) if cover_image_url else ""

    chapter_links: list[tuple[str, str]] = []
    for anchor in soup.select("table#chapters a[href*='/chapter/'], a[href*='/chapter/']"):
        href = anchor.get("href") or ""
        text = clean_text(anchor.get_text(" ", strip=True))
        if not href or not text:
            continue
        chapter_url = urljoin(story_url, href).split("#", 1)[0]
        chapter_links.append((text, chapter_url))

    seen: set[str] = set()
    chapters: list[RoyalRoadChapter] = []
    for index, (chapter_title, chapter_url) in enumerate(chapter_links, start=1):
        if chapter_url in seen:
            continue
        seen.add(chapter_url)
        chapters.append(
            RoyalRoadChapter(
                number=len(chapters) + 1,
                title=chapter_title,
                url=chapter_url,
                source_chapter_id=parse_chapter_id(chapter_url, index),
            )
        )

    return {
        "source": "royalroad",
        "source_story_id": parse_story_id(story_url),
        "story_url": story_url,
        "slug": story_slug(story_url, title),
        "title": title,
        "author": author,
        "description": description,
        "status": status,
        "tags": list(dict.fromkeys(tags)),
        "cover_image_url": cover_image_url,
        "total_chapters": len(chapters),
        "chapters": [asdict(chapter) for chapter in chapters],
    }


def extract_chapter_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one(".chapter-content")
    if node is None:
        node = soup.select_one("div[id*='chapter']")
    if node is None:
        raise ValueError("Cannot find Royal Road chapter content.")

    for removable in node.select("script, style, noscript, .hidden, .visible-print"):
        removable.decompose()
    return node.get_text("\n", strip=True)


def fetch_chapter_text(url: str, timeout: int = 30, retries: int = 3, retry_sleep: float = 2.0) -> str:
    return extract_chapter_text(fetch_html(url, timeout, retries, retry_sleep))


def main() -> None:
    parser = argparse.ArgumentParser(description="Crawl Royal Road story catalog.")
    parser.add_argument("story_url", nargs="?", default=DEFAULT_STORY_URL)
    parser.add_argument("--output", default="")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-sleep", type=float, default=2.0)
    args = parser.parse_args()

    catalog = parse_catalog(args.story_url, args.timeout, args.retries, args.retry_sleep)
    output_path = (
        Path(args.output)
        if args.output
        else Path("story_data/catalogs/royalroad") / catalog["slug"] / "chapters.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Done. Saved {len(catalog['chapters'])} Royal Road chapters to {output_path}")


if __name__ == "__main__":
    main()
