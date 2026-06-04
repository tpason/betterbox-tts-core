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
    clean_text,
    compact_text,
    fetch_html as _fetch_html_base,
    looks_blocked,
    parse_chapter_number as _parse_chapter_number,
    safe_slug as _safe_slug,
)


@dataclass
class DocLNChapter:
    number: int
    title: str
    url: str
    source_chapter_id: str
    is_locked: bool = False


def safe_slug(value: str) -> str:
    return _safe_slug(value, fallback="docln-story")


def story_slug(story_url: str, title: str = "") -> str:
    parts = [part for part in urlparse(story_url).path.split("/") if part]
    if parts:
        return safe_slug(parts[-1])
    return safe_slug(title)


def fetch_html(url: str, timeout: int = 30, retries: int = 3, retry_sleep: float = 2.0) -> str:
    return _fetch_html_base(url, timeout, retries, retry_sleep, label="docln")


def parse_chapter_number(title: str, url: str, fallback: int) -> int:
    return _parse_chapter_number(title, url, fallback)


def parse_chapter_id(url: str, fallback: int) -> str:
    parts = [part for part in urlparse(url).path.split("/") if part]
    return parts[-1] if parts else str(fallback)


def looks_locked(text: str) -> bool:
    return looks_blocked(text)


def extract_tags(soup: BeautifulSoup) -> list[str]:
    tags: list[str] = []
    for selector in [
        "a[href*='/the-loai']",
        "a[href*='/the-loai/']",
        "a[href*='/danh-sach']",
        ".series-gernes a",
        ".series-genres a",
    ]:
        for node in soup.select(selector):
            text = compact_text(node.get_text(" ", strip=True))
            if text:
                tags.append(text)
    return list(dict.fromkeys(tags))


def extract_description(soup: BeautifulSoup) -> str:
    for selector in [".summary-content", ".series-summary", ".summary", ".description", ".scontent", "#series-summary"]:
        node = soup.select_one(selector)
        if node:
            text = compact_text(node.get_text(" ", strip=True))
            if text:
                return text
    meta_desc = soup.select_one("meta[name='description'], meta[property='og:description']")
    return compact_text(meta_desc.get("content") if meta_desc else "")


def extract_chapters(soup: BeautifulSoup, story_url: str) -> list[DocLNChapter]:
    chapters: list[DocLNChapter] = []
    seen: set[str] = set()
    for anchor in soup.select("a[href]"):
        href = anchor.get("href") or ""
        url = urljoin(story_url, href).split("#", 1)[0].rstrip("/")
        parsed = urlparse(url)
        if parsed.netloc != urlparse(story_url).netloc:
            continue
        if "/truyen/" not in parsed.path or "/c" not in parsed.path:
            continue
        text = compact_text(anchor.get_text(" ", strip=True))
        if not text or not re.search(r"(?:chương|chuong|chapter|\bc\d+)", f"{text} {parsed.path}", flags=re.IGNORECASE):
            continue
        if url in seen:
            continue
        seen.add(url)
        number = parse_chapter_number(text, url, len(chapters) + 1)
        chapters.append(
            DocLNChapter(
                number=number,
                title=text or f"Chương {number}",
                url=url,
                source_chapter_id=parse_chapter_id(url, number),
                is_locked=looks_locked(anchor.get_text(" ", strip=True)),
            )
        )
    chapters.sort(key=lambda item: item.number)
    return chapters


def parse_catalog(
    story_url: str,
    timeout: int = 30,
    retries: int = 3,
    retry_sleep: float = 2.0,
) -> dict:
    html = fetch_html(story_url, timeout, retries, retry_sleep)
    soup = BeautifulSoup(html, "html.parser")
    title_node = soup.select_one("h1, .series-name, .series-title")
    title = compact_text(title_node.get_text(" ", strip=True)) if title_node else story_slug(story_url)
    author_node = soup.select_one("a[href*='/tac-gia'], a[href*='/author'], .series-author a, .author a")
    author = compact_text(author_node.get_text(" ", strip=True)) if author_node else ""
    cover_node = soup.select_one("meta[property='og:image'], .series-cover img, .cover img, img")
    cover_image_url = ""
    if cover_node:
        cover_image_url = cover_node.get("content") or cover_node.get("data-src") or cover_node.get("src") or ""
        cover_image_url = urljoin(story_url, cover_image_url) if cover_image_url else ""
    page_text = compact_text(soup.get_text(" ", strip=True))
    status_match = re.search(r"(?:Tình trạng|Trạng thái|Status)\s*[:：]?\s*(.*?)(?:\s{2,}|$)", page_text, flags=re.IGNORECASE)
    status = compact_text(status_match.group(1)) if status_match else ""
    chapters = extract_chapters(soup, story_url)
    return {
        "source": "docln",
        "source_story_id": story_slug(story_url, title),
        "story_url": story_url.rstrip("/"),
        "slug": story_slug(story_url, title),
        "title": title,
        "author": author,
        "description": extract_description(soup),
        "status": status,
        "tags": extract_tags(soup),
        "cover_image_url": cover_image_url,
        "total_chapters": len(chapters),
        "chapters": [asdict(chapter) for chapter in chapters],
    }


def extract_chapter_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for removable in soup.select("script, style, noscript, iframe, nav, header, footer, form, .ads, .advertisement"):
        removable.decompose()
    title_node = soup.select_one("h1, .chapter-title, .title")
    content_node = (
        soup.select_one("#chapter-content")
        or soup.select_one(".chapter-content")
        or soup.select_one(".reading-content")
        or soup.select_one(".rd-article")
        or soup.select_one("#rd-content")
        or soup.select_one("article")
    )
    if content_node is None:
        raise ValueError("Cannot find DocLN chapter content.")
    title = compact_text(title_node.get_text(" ", strip=True)) if title_node else ""
    text = clean_text(content_node.get_text("\n", strip=True))
    if looks_locked(text):
        raise PermissionError("DocLN chapter is locked or requires login.")
    if title and not text.startswith(title):
        return f"{title}\n\n{text}".strip()
    return text


def fetch_chapter_text(url: str, timeout: int = 30, retries: int = 3, retry_sleep: float = 2.0) -> str:
    return extract_chapter_text(fetch_html(url, timeout, retries, retry_sleep))


def main() -> None:
    parser = argparse.ArgumentParser(description="Crawl DocLN story catalog.")
    parser.add_argument("story_url")
    parser.add_argument("--output", default="")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-sleep", type=float, default=2.0)
    args = parser.parse_args()

    catalog = parse_catalog(args.story_url, args.timeout, args.retries, args.retry_sleep)
    output_path = Path(args.output) if args.output else Path("story_data/catalogs/docln") / catalog["slug"] / "chapters.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Đã lưu catalog: {output_path}")
    print(f"Story: {catalog['title']} | chapters={len(catalog.get('chapters') or [])}")


if __name__ == "__main__":
    main()
