#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests import Session
from requests.adapters import HTTPAdapter


DEFAULT_STORY_URL = "https://truyenfull.today/than-dao-dan-ton/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "vi,en-US;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


@dataclass
class TruyenFullChapter:
    number: int
    title: str
    url: str
    source_chapter_id: str


def safe_slug(value: str) -> str:
    value = re.sub(r"\s+", "-", value.strip().lower())
    value = re.sub(r"[^a-z0-9\u00c0-\u1ef9-]+", "", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value or "truyenfull-story"


def story_slug(story_url: str, title: str = "") -> str:
    parsed = urlparse(story_url)
    parts = [part for part in parsed.path.split("/") if part]
    if parts:
        return safe_slug(parts[-1])
    if title:
        return safe_slug(title)
    return "truyenfull-story"


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _make_session() -> Session:
    sess = Session()
    sess.headers.update(HEADERS)
    adapter = HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0)
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    return sess


def fetch_html(url: str, timeout: int = 30, retries: int = 3, retry_sleep: float = 2.0, *, session: Session | None = None) -> str:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            if session is not None:
                response = session.get(url, timeout=timeout)
            else:
                response = requests.get(url, headers=HEADERS, timeout=timeout)
            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            last_error = exc
            if attempt < retries:
                print(f"[WARN] truyenfull_today fetch retry {attempt}/{retries}: {url} | {exc}")
                time.sleep(retry_sleep * attempt)
    raise RuntimeError(f"Cannot fetch TruyenFull Today URL after {retries} attempts: {url} | {last_error}") from last_error


def parse_story_id(story_url: str) -> str:
    parsed = urlparse(story_url)
    return story_slug(story_url) or parsed.path.rstrip("/").rsplit("/", 1)[-1]


def parse_chapter_id(url: str, fallback: int) -> str:
    match = re.search(r"/chuong-([^/?#]+)/?", url)
    return match.group(1) if match else str(fallback)


def parse_chapter_number(title: str, url: str, fallback: int) -> int:
    url_match = re.search(r"/chuong-(\d+)", url)
    if url_match:
        return int(url_match.group(1))
    title_match = re.search(r"\b(?:chương|chuong)\s*(\d+)\b", title, flags=re.IGNORECASE)
    if title_match:
        return int(title_match.group(1))
    return fallback


def collect_story_page_urls(soup: BeautifulSoup, story_url: str, max_pages: int) -> list[str]:
    base_path = urlparse(story_url).path.rstrip("/")
    page_numbers: set[int] = {1}
    for anchor in soup.select(".pagination a[href], ul.pagination a[href], a[href*='/trang-']"):
        href = anchor.get("href") or ""
        page_url = urljoin(story_url, href).split("#", 1)[0]
        parsed = urlparse(page_url)
        if "/chuong-" in parsed.path:
            continue
        if not parsed.path.rstrip("/").startswith(base_path):
            continue
        page_match = re.search(r"/trang-(\d+)/?", parsed.path)
        if page_match:
            page_numbers.add(int(page_match.group(1)))
            continue
        text_match = re.fullmatch(r"\d+", clean_text(anchor.get_text(" ", strip=True)))
        if text_match:
            page_numbers.add(int(text_match.group(0)))

    highest_page = max(page_numbers)
    if max_pages:
        highest_page = min(highest_page, max_pages)

    urls = [story_url]
    for page in range(2, highest_page + 1):
        urls.append(urljoin(story_url.rstrip("/") + "/", f"trang-{page}/"))
    return urls


def parse_catalog(story_url: str, timeout: int = 30, retries: int = 3, retry_sleep: float = 2.0, max_pages: int = 0) -> dict:
    session = _make_session()
    try:
        first_html = fetch_html(story_url, timeout, retries, retry_sleep, session=session)
        first_soup = BeautifulSoup(first_html, "html.parser")

        title_node = first_soup.select_one("h1, .title, .truyen-title")
        title = clean_text(title_node.get_text(" ", strip=True)) if title_node else story_slug(story_url)

        author_node = (
            first_soup.select_one("a[href*='/tac-gia/']")
            or first_soup.select_one(".author a")
            or first_soup.select_one(".author")
            or first_soup.find(string=re.compile(r"Tác giả", re.IGNORECASE))
        )
        author = ""
        if hasattr(author_node, "get_text"):
            raw = clean_text(author_node.get_text(" ", strip=True))
            author = re.sub(r"^Tác giả\s*:?\s*", "", raw, flags=re.IGNORECASE).strip()
        elif author_node:
            author = re.sub(r"^Tác giả\s*:?\s*", "", clean_text(str(author_node)), flags=re.IGNORECASE).strip()

        tags = [
            clean_text(node.get_text(" ", strip=True))
            for node in first_soup.select("a[href*='/the-loai/']")
            if clean_text(node.get_text(" ", strip=True))
        ]

        description_node = first_soup.select_one(".desc-text, .description, .info, .truyen-info")
        description = clean_text(description_node.get_text(" ", strip=True)) if description_node else ""

        page_text = clean_text(first_soup.get_text(" ", strip=True))
        status = "Hoàn thành" if re.search(r"\b(full|hoàn thành|trọn bộ)\b", page_text, flags=re.IGNORECASE) else ""

        cover_node = first_soup.select_one("img")
        cover_image_url = ""
        if cover_node:
            cover_image_url = cover_node.get("data-src") or cover_node.get("src") or ""
            cover_image_url = urljoin(story_url, cover_image_url) if cover_image_url else ""

        page_urls = collect_story_page_urls(first_soup, story_url, max_pages)
        soups = [first_soup]
        for page_url in page_urls[1:]:
            soups.append(BeautifulSoup(fetch_html(page_url, timeout, retries, retry_sleep, session=session), "html.parser"))
            time.sleep(retry_sleep)

        chapters: list[TruyenFullChapter] = []
        seen: set[str] = set()
        for soup in soups:
            for anchor in soup.select("#list-chapter a[href], .list-chapter a[href], a[href*='/chuong-']"):
                href = anchor.get("href") or ""
                chapter_url = urljoin(story_url, href).split("#", 1)[0]
                if "/chuong-" not in urlparse(chapter_url).path or chapter_url in seen:
                    continue
                chapter_title = clean_text(anchor.get_text(" ", strip=True))
                if not chapter_title:
                    chapter_title = f"Chương {len(chapters) + 1}"
                seen.add(chapter_url)
                number = parse_chapter_number(chapter_title, chapter_url, len(chapters) + 1)
                chapters.append(
                    TruyenFullChapter(
                        number=number,
                        title=chapter_title,
                        url=chapter_url,
                        source_chapter_id=parse_chapter_id(chapter_url, number),
                    )
                )

        chapters.sort(key=lambda item: item.number)
        return {
            "source": "truyenfull_today",
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
    finally:
        session.close()


def extract_chapter_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    node = (
        soup.select_one("#chapter-c")
        or soup.select_one(".chapter-c")
        or soup.select_one(".chapter-content")
        or soup.select_one(".chapter")
        or soup.select_one("article")
    )
    if node is None:
        raise ValueError("Cannot find TruyenFull Today chapter content.")
    for removable in node.select("script, style, noscript, iframe, .ads, .advertisement"):
        removable.decompose()
    return node.get_text("\n", strip=True)


def fetch_chapter_text(url: str, timeout: int = 30, retries: int = 3, retry_sleep: float = 2.0) -> str:
    return extract_chapter_text(fetch_html(url, timeout, retries, retry_sleep))


def main() -> None:
    parser = argparse.ArgumentParser(description="Crawl TruyenFull Today story catalog.")
    parser.add_argument("story_url", nargs="?", default=DEFAULT_STORY_URL)
    parser.add_argument("--output", default="")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-sleep", type=float, default=2.0)
    parser.add_argument("--max-pages", type=int, default=0, help="0 = lấy các page catalog nhìn thấy trên trang.")
    args = parser.parse_args()

    catalog = parse_catalog(args.story_url, args.timeout, args.retries, args.retry_sleep, args.max_pages)
    output_path = (
        Path(args.output)
        if args.output
        else Path("story_data/catalogs/truyenfull_today") / catalog["slug"] / "chapters.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Done. Saved {len(catalog['chapters'])} TruyenFull Today chapters to {output_path}")


if __name__ == "__main__":
    main()
