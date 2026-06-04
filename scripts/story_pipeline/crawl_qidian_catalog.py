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


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


@dataclass
class QidianChapter:
    position: int
    title: str
    url: str
    volume: str
    is_vip: bool


def fetch_html(url: str, timeout: int = 25, retries: int = 3, retry_sleep: float = 2.0) -> str:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, headers=HEADERS, timeout=timeout)
            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(retry_sleep * attempt)
    raise RuntimeError(f"Cannot fetch Qidian URL after {retries} attempts: {url} | {last_error}") from last_error


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def book_id_from_url(book_url: str) -> str:
    match = re.search(r"/book/(\d+)", book_url)
    if not match:
        raise ValueError(f"Không nhận diện được book id từ URL: {book_url}")
    return match.group(1)


def catalog_url_from_book_url(book_url: str) -> str:
    parsed = urlparse(book_url)
    book_id = book_id_from_url(book_url)
    return f"{parsed.scheme or 'https'}://{parsed.netloc or 'www.qidian.com'}/book/{book_id}/catalog/"


def safe_slug(value: str) -> str:
    value = clean_text(value)
    value = re.sub(r"[\\/:*?\"<>|]+", "_", value)
    value = re.sub(r"\s+", "_", value)
    return value.strip("._") or "qidian_book"


def parse_catalog(
    book_url: str,
    timeout: int = 25,
    retries: int = 3,
    retry_sleep: float = 2.0,
) -> dict:
    catalog_url = catalog_url_from_book_url(book_url)
    soup = BeautifulSoup(fetch_html(catalog_url, timeout, retries, retry_sleep), "html.parser")

    title_node = soup.select_one("h1, .book-info h1, .book-name")
    title = clean_text(title_node.get_text(" ", strip=True)) if title_node else book_id_from_url(book_url)

    author_node = soup.select_one(".writer, .author, .book-info .writer")
    author = clean_text(author_node.get_text(" ", strip=True)) if author_node else ""

    chapters: list[QidianChapter] = []
    seen_urls: set[str] = set()
    current_volume = ""

    for node in soup.select("h3, h4, li, a"):
        if node.name in {"h3", "h4"}:
            text = clean_text(node.get_text(" ", strip=True))
            if text:
                current_volume = text
            continue

        anchors = node.select("a") if node.name != "a" else [node]
        for anchor in anchors:
            href = anchor.get("href", "")
            if "/chapter/" not in href:
                continue
            chapter_url = urljoin(catalog_url, href)
            if chapter_url in seen_urls:
                continue

            title_text = clean_text(anchor.get_text(" ", strip=True))
            if not title_text:
                continue

            container_text = clean_text(node.get_text(" ", strip=True))
            is_vip = any(marker in container_text for marker in ("VIP", "订阅", "付费"))

            seen_urls.add(chapter_url)
            chapters.append(
                QidianChapter(
                    position=len(chapters) + 1,
                    title=title_text,
                    url=chapter_url,
                    volume=current_volume,
                    is_vip=is_vip,
                )
            )

    if not chapters:
        raise ValueError(
            "Không parse được chapter nào. Qidian có thể đã đổi HTML, chặn request, hoặc cần cookie trình duyệt."
        )

    return {
        "source": "qidian",
        "book_url": book_url,
        "catalog_url": catalog_url,
        "book_id": book_id_from_url(book_url),
        "title": title,
        "author": author,
        "total_chapters": len(chapters),
        "free_chapters": sum(1 for chapter in chapters if not chapter.is_vip),
        "vip_chapters": sum(1 for chapter in chapters if chapter.is_vip),
        "chapters": [asdict(chapter) for chapter in chapters],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Crawl catalog chapter từ một book Qidian.")
    parser.add_argument("book_url", help="Ví dụ: https://www.qidian.com/book/1043886198/")
    parser.add_argument("--output", default="", help="Mặc định: story_data/qidian/catalogs/<book_id>/chapters.json")
    parser.add_argument("--timeout", type=int, default=25)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-sleep", type=float, default=2.0)
    args = parser.parse_args()

    payload = parse_catalog(args.book_url, args.timeout, args.retries, args.retry_sleep)
    output_path = (
        Path(args.output)
        if args.output
        else Path("story_data/qidian/catalogs") / payload["book_id"] / "chapters.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        f"Hoàn tất. {payload['title']} | total={payload['total_chapters']} | "
        f"free={payload['free_chapters']} | vip={payload['vip_chapters']}"
    )
    print(f"Đã lưu: {output_path}")


if __name__ == "__main__":
    main()
