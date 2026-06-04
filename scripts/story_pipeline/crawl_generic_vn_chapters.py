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
    clean_text,
    compact_text,
    fetch_html as _fetch_html_base,
    make_session,
    parse_chapter_number as _parse_chapter_number,
    safe_slug as _safe_slug,
)


SOURCE_BY_HOST = {
    "sttruyen.com": ("sttruyen", "STTruyen", "https://sttruyen.com"),
    "truyenchuhay.vn": ("truyenchuhay", "TruyenChuHay", "https://truyenchuhay.vn"),
    "truyenchuhay.org": ("truyenchuhay", "TruyenChuHay", "https://truyenchuhay.org"),
    "www.truyenhoangdung.xyz": ("truyenhoangdung", "TruyenHoangDung", "https://www.truyenhoangdung.xyz"),
    "truyenhoangdung.xyz": ("truyenhoangdung", "TruyenHoangDung", "https://www.truyenhoangdung.xyz"),
}


@dataclass
class GenericVNChapter:
    number: int
    title: str
    url: str
    source_chapter_id: str


def safe_slug(value: str) -> str:
    return _safe_slug(value, fallback="vn-story")


def source_info(url: str) -> tuple[str, str, str]:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return SOURCE_BY_HOST.get(host, (re.sub(r"[^a-z0-9]+", "_", host).strip("_"), host, f"{urlparse(url).scheme}://{host}"))


def story_slug(story_url: str, title: str = "") -> str:
    parts = [part for part in urlparse(story_url).path.split("/") if part]
    for part in reversed(parts):
        if part not in {"story", "truyen"}:
            return safe_slug(re.sub(r"\.html?$", "", part, flags=re.IGNORECASE))
    return safe_slug(title)


def fetch_html(url: str, timeout: int = 30, retries: int = 3, retry_sleep: float = 2.0, *, session=None) -> str:
    return _fetch_html_base(url, timeout, retries, retry_sleep, session=session, label="generic_vn")


def parse_chapter_number(title: str, url: str, fallback: int) -> int:
    return _parse_chapter_number(title, url, fallback)


def parse_chapter_id(url: str, fallback: int) -> str:
    parts = [part for part in urlparse(url).path.split("/") if part]
    return re.sub(r"\.html?$", "", parts[-1], flags=re.IGNORECASE) if parts else str(fallback)


def is_chapter_url(url: str, title: str) -> bool:
    path = urlparse(url).path
    text = f"{path} {title}"
    return bool(
        re.search(r"(?:/render/|/chuong-|/chuong/|/chapter/|/chapter-|/chap-)", path, flags=re.IGNORECASE)
        or re.search(r"\b(?:chương|chuong|chapter|chap)\s*\d+", text, flags=re.IGNORECASE)
    )


def collect_page_urls(soup: BeautifulSoup, story_url: str, max_pages: int) -> list[str]:
    urls = [story_url.rstrip("/")]
    seen = set(urls)
    base_path = urlparse(story_url).path.rstrip("/")
    for anchor in soup.select(".pagination a[href], .wp-pagenavi a[href], a[href*='/page/'], a[href*='/trang-']"):
        href = anchor.get("href") or ""
        url = urljoin(story_url, href).split("#", 1)[0].rstrip("/")
        parsed = urlparse(url)
        if parsed.netloc != urlparse(story_url).netloc:
            continue
        if not parsed.path.rstrip("/").startswith(base_path):
            continue
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
        if max_pages and len(urls) >= max_pages:
            break
    return urls


def extract_chapters(soups: list[BeautifulSoup], story_url: str) -> list[GenericVNChapter]:
    chapters: list[GenericVNChapter] = []
    seen: set[str] = set()
    story_host = urlparse(story_url).netloc
    for soup in soups:
        for anchor in soup.select(
            "#list-chapter a[href], .list-chapter a[href], .chapter-list a[href], "
            ".chapters a[href], .episode-list a[href], a[href*='/chuong'], a[href*='/render/'], a[href*='chapter']"
        ):
            href = anchor.get("href") or ""
            url = urljoin(story_url, href).split("#", 1)[0].rstrip("/")
            if urlparse(url).netloc != story_host:
                continue
            title = compact_text(anchor.get_text(" ", strip=True))
            if not is_chapter_url(url, title) or url in seen:
                continue
            number = parse_chapter_number(title, url, len(chapters) + 1)
            if number <= 0 or re.search(r"đọc\s+(từ\s+)?đầu", title, flags=re.IGNORECASE):
                continue
            seen.add(url)
            chapters.append(
                GenericVNChapter(
                    number=number,
                    title=title or f"Chương {number}",
                    url=url,
                    source_chapter_id=parse_chapter_id(url, number),
                )
            )
    chapters.sort(key=lambda item: item.number)
    return chapters


def parse_catalog(
    story_url: str,
    timeout: int = 30,
    retries: int = 3,
    retry_sleep: float = 2.0,
    max_pages: int = 0,
) -> dict:
    source_code, source_name, _ = source_info(story_url)
    session = make_session()
    try:
        first_html = fetch_html(story_url, timeout, retries, retry_sleep, session=session)
        first_soup = BeautifulSoup(first_html, "html.parser")
        title_node = first_soup.select_one("h1, .title, .story-title, .truyen-title, .entry-title")
        title = compact_text(title_node.get_text(" ", strip=True)) if title_node else story_slug(story_url)
        title = re.sub(r"\s*[-|]\s*(STTRUYEN|TruyenChuHay|Truyện Hoàng Dung).*$", "", title, flags=re.IGNORECASE).strip()
        author_node = first_soup.select_one("a[href*='tac-gia'], a[href*='author'], .author a, .story-author a")
        author = compact_text(author_node.get_text(" ", strip=True)) if author_node else ""
        tags = [
            compact_text(node.get_text(" ", strip=True))
            for node in first_soup.select("a[href*='the-loai'], a[href*='tag'], a[href*='genre'], .genres a")
            if compact_text(node.get_text(" ", strip=True))
        ]
        description = ""
        for selector in [".description", ".desc", ".summary", ".entry-content", ".story-detail-info", "[class*='desc']", "[class*='summary']"]:
            node = first_soup.select_one(selector)
            if node:
                text = compact_text(node.get_text(" ", strip=True))
                if len(text) > 80:
                    description = text
                    break
        if not description:
            meta_desc = first_soup.select_one("meta[name='description'], meta[property='og:description']")
            description = compact_text(meta_desc.get("content") if meta_desc else "")
        page_text = compact_text(first_soup.get_text(" ", strip=True))
        status = "Hoàn thành" if re.search(r"\b(full|hoàn thành|trọn bộ)\b", page_text, flags=re.IGNORECASE) else ""
        cover_node = first_soup.select_one("meta[property='og:image'], .cover img, .book img, img")
        cover_image_url = ""
        if cover_node:
            cover_image_url = cover_node.get("content") or cover_node.get("data-src") or cover_node.get("src") or ""
            cover_image_url = urljoin(story_url, cover_image_url) if cover_image_url else ""
        page_urls = collect_page_urls(first_soup, story_url, max_pages)
        soups = [first_soup]
        for page_url in page_urls[1:]:
            soups.append(BeautifulSoup(fetch_html(page_url, timeout, retries, retry_sleep, session=session), "html.parser"))
            time.sleep(retry_sleep)
        chapters = extract_chapters(soups, story_url)
        return {
            "source": source_code,
            "source_name": source_name,
            "source_story_id": story_slug(story_url, title),
            "story_url": story_url.rstrip("/"),
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
    finally:
        session.close()


def extract_chapter_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    title_node = soup.select_one("h1, .chapter-title, .entry-title, .title")
    node = (
        soup.select_one("#chapter-content")
        or soup.select_one("#chapter-c")
        or soup.select_one(".chapter-content")
        or soup.select_one(".chapter-c")
        or soup.select_one(".reading-content")
        or soup.select_one(".truyencv-read-content")
        or soup.select_one(".read-content")
        or soup.select_one(".entry-content")
        or soup.select_one(".content")
        or soup.select_one("article")
    )
    if node is None:
        raise ValueError("Cannot find VN chapter content.")
    for removable in node.select("script, style, noscript, iframe, nav, header, footer, form, .ads, .advertisement"):
        removable.decompose()
    title = compact_text(title_node.get_text(" ", strip=True)) if title_node else ""
    text = clean_text(node.get_text("\n", strip=True))
    if title and not text.startswith(title):
        return f"{title}\n\n{text}".strip()
    return text


def fetch_chapter_text(url: str, timeout: int = 30, retries: int = 3, retry_sleep: float = 2.0) -> str:
    return extract_chapter_text(fetch_html(url, timeout, retries, retry_sleep))


def main() -> None:
    parser = argparse.ArgumentParser(description="Crawl a generic Vietnamese text story catalog.")
    parser.add_argument("story_url")
    parser.add_argument("--output", default="")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-sleep", type=float, default=2.0)
    parser.add_argument("--max-pages", type=int, default=0)
    args = parser.parse_args()

    catalog = parse_catalog(args.story_url, args.timeout, args.retries, args.retry_sleep, args.max_pages)
    output_path = Path(args.output) if args.output else Path("story_data/catalogs") / catalog["source"] / catalog["slug"] / "chapters.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Đã lưu catalog: {output_path}")
    print(f"Story: {catalog['title']} | source={catalog['source']} | chapters={len(catalog.get('chapters') or [])}")


if __name__ == "__main__":
    main()
