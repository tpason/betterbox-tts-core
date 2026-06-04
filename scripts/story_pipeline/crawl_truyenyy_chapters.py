#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import time
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
    make_session,
    parse_chapter_number as _parse_chapter_number,
    safe_slug as _safe_slug,
)


@dataclass
class TruyenYYChapter:
    number: int
    title: str
    url: str
    source_chapter_id: str


def safe_slug(value: str) -> str:
    return _safe_slug(value, fallback="truyenyy-story")


def story_slug(story_url: str, title: str = "") -> str:
    parts = [part for part in urlparse(story_url).path.split("/") if part]
    if parts:
        return safe_slug(parts[-1])
    return safe_slug(title)


def fetch_html(url: str, timeout: int = 30, retries: int = 3, retry_sleep: float = 2.0, *, session=None) -> str:
    return _fetch_html_base(url, timeout, retries, retry_sleep, session=session, label="truyenyy")


def parse_chapter_number(title: str, url: str, fallback: int) -> int:
    result = _parse_chapter_number(title, url, fallback)
    if result != fallback:
        return result
    # Extra: /chuong-N or /chapter-N path pattern
    match = re.search(r"/(?:chuong|chapter)-?0*(\d{1,5})(?:\D|$)", url, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))
    return fallback


def parse_chapter_id(url: str, fallback: int) -> str:
    parts = [part for part in urlparse(url).path.split("/") if part]
    return parts[-1] if parts else str(fallback)


def extract_total_chapters(page_text: str) -> int:
    matches = [
        int(match.group(1).replace(".", "").replace(",", ""))
        for match in re.finditer(r"(?:Số chương|DS\.Chương|Chương)\s*[:.]?\s*(\d[\d.,]*)", page_text, flags=re.IGNORECASE)
    ]
    return max(matches) if matches else 0


def collect_page_urls(first_soup: BeautifulSoup, story_url: str, max_pages: int) -> list[str]:
    page_urls = [story_url]
    seen = {story_url.rstrip("/")}
    for anchor in first_soup.select("a[href]"):
        href = anchor.get("href") or ""
        url = urljoin(story_url, href).split("#", 1)[0].rstrip("/")
        if url in seen:
            continue
        parsed = urlparse(url)
        if parsed.netloc != urlparse(story_url).netloc:
            continue
        if not parsed.path.startswith(urlparse(story_url).path.rstrip("/")):
            continue
        if not re.search(r"(?:page|p=|trang|danh-sach)", url, flags=re.IGNORECASE):
            continue
        seen.add(url)
        page_urls.append(url)
        if max_pages and len(page_urls) >= max_pages:
            break
    return page_urls


def extract_chapters_from_soup(soup: BeautifulSoup, story_url: str, start_index: int) -> list[TruyenYYChapter]:
    chapters: list[TruyenYYChapter] = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href") or ""
        text = clean_text(anchor.get_text(" ", strip=True))
        url = urljoin(story_url, href).split("#", 1)[0].rstrip("/")
        parsed = urlparse(url)
        if parsed.netloc != urlparse(story_url).netloc:
            continue
        if "/truyen/" not in parsed.path:
            continue
        if not ("/chuong-" in parsed.path or re.search(r"\b(?:chương|chuong)\s*\d+", text, flags=re.IGNORECASE)):
            continue
        number = parse_chapter_number(text, url, start_index + len(chapters))
        title = text or f"Chương {number}"
        chapters.append(
            TruyenYYChapter(
                number=number,
                title=title,
                url=url,
                source_chapter_id=parse_chapter_id(url, number),
            )
        )
    return chapters


def parse_catalog(
    story_url: str,
    timeout: int = 30,
    retries: int = 3,
    retry_sleep: float = 2.0,
    max_pages: int = 0,
) -> dict:
    session = make_session()
    try:
        first_html = fetch_html(story_url, timeout, retries, retry_sleep, session=session)
        first_soup = BeautifulSoup(first_html, "html.parser")
        title_node = first_soup.select_one("h1")
        title = clean_text(title_node.get_text(" ", strip=True)) if title_node else story_slug(story_url)
        title = re.sub(r"\s*[-|]\s*TruyenYY.*$", "", title, flags=re.IGNORECASE).strip()

        page_text = clean_text(first_soup.get_text(" ", strip=True))
        author_node = first_soup.select_one("a[href*='/tac-gia'], a[href*='/author']")
        author = clean_text(author_node.get_text(" ", strip=True)) if author_node else ""
        tags = [
            clean_text(anchor.get_text(" ", strip=True))
            for anchor in first_soup.select("a[href*='/the-loai'], a[href*='he-thong'], a[href*='tien-hiep'], a[href*='huyen-huyen']")
            if clean_text(anchor.get_text(" ", strip=True))
        ]
        status_match = re.search(r"Trạng thái\s*([^\n|]+?)(?:\s+Số chương|\s+Cập nhật|$)", page_text, flags=re.IGNORECASE)
        status = clean_text(status_match.group(1)) if status_match else ""
        total_chapters = extract_total_chapters(page_text)

        description = ""
        heading = first_soup.find(string=re.compile(r"Giới Thiệu|Giói Thiệu", re.IGNORECASE))
        if heading:
            parent = heading.find_parent()
            parts: list[str] = []
            node = parent.find_next_sibling() if parent else None
            while node is not None and len(parts) < 6:
                text = clean_text(node.get_text(" ", strip=True))
                if re.search(r"Thông tin tác giả|Có Thể Bạn|Chương Mới Nhất|Truyện Cùng", text, flags=re.IGNORECASE):
                    break
                if text:
                    parts.append(text)
                node = node.find_next_sibling()
            description = clean_text(" ".join(parts))
        if not description:
            meta_desc = first_soup.select_one("meta[name='description'], meta[property='og:description']")
            description = clean_text(meta_desc.get("content") if meta_desc else "")

        cover_node = first_soup.select_one("meta[property='og:image'], img[alt]")
        cover_image_url = ""
        if cover_node:
            cover_image_url = cover_node.get("content") or cover_node.get("data-src") or cover_node.get("src") or ""
            cover_image_url = urljoin(story_url, cover_image_url) if cover_image_url else ""

        chapters: list[TruyenYYChapter] = []
        seen: set[str] = set()
        for page_url in collect_page_urls(first_soup, story_url, max_pages):
            soup = first_soup if page_url.rstrip("/") == story_url.rstrip("/") else BeautifulSoup(fetch_html(page_url, timeout, retries, retry_sleep, session=session), "html.parser")
            for chapter in extract_chapters_from_soup(soup, story_url, len(chapters) + 1):
                if chapter.url in seen:
                    continue
                seen.add(chapter.url)
                chapters.append(chapter)
            if page_url.rstrip("/") != story_url.rstrip("/"):
                time.sleep(retry_sleep)

        chapters.sort(key=lambda item: item.number)
        return {
            "source": "truyenyy",
            "source_story_id": story_slug(story_url, title),
            "story_url": story_url.rstrip("/"),
            "slug": story_slug(story_url, title),
            "title": title,
            "author": author,
            "description": description,
            "status": status,
            "tags": list(dict.fromkeys(tags)),
            "cover_image_url": cover_image_url,
            "total_chapters": total_chapters or len(chapters),
            "chapters": [asdict(chapter) for chapter in chapters],
        }
    finally:
        session.close()


def extract_chapter_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    title_node = soup.select_one("h1")
    node = (
        soup.select_one("#chapter-c")
        or soup.select_one(".chapter-c")
        or soup.select_one(".chapter-content")
        or soup.select_one(".reading-content")
        or soup.select_one("article")
        or soup.select_one("main")
    )
    if node is None:
        raise ValueError("Cannot find TruyenYY chapter content.")
    for removable in node.select("script, style, noscript, iframe, nav, header, footer, .ads, .advertisement"):
        removable.decompose()
    title = clean_text(title_node.get_text(" ", strip=True)) if title_node else ""
    text = node.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if title and not text.startswith(title):
        return f"{title}\n\n{text}".strip()
    return text


def fetch_chapter_text(url: str, timeout: int = 30, retries: int = 3, retry_sleep: float = 2.0) -> str:
    return extract_chapter_text(fetch_html(url, timeout, retries, retry_sleep))


def main() -> None:
    parser = argparse.ArgumentParser(description="Crawl TruyenYY story catalog.")
    parser.add_argument("story_url")
    parser.add_argument("--output", default="")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-sleep", type=float, default=2.0)
    parser.add_argument("--max-pages", type=int, default=0)
    args = parser.parse_args()

    catalog = parse_catalog(args.story_url, args.timeout, args.retries, args.retry_sleep, args.max_pages)
    output_path = Path(args.output) if args.output else Path("story_data/catalogs/truyenyy") / catalog["slug"] / "chapters.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Đã lưu catalog: {output_path}")
    print(f"Story: {catalog['title']} | chapters={len(catalog['chapters'])}/{catalog['total_chapters']}")


if __name__ == "__main__":
    main()
