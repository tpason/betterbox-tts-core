#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


DEFAULT_STORY_URL = "https://wattpad.com.vn/cau-tha-thanh-thanh-nhan-tien-quan-trieu-ta-cham-ngua"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "vi,en;q=0.9",
}


@dataclass
class Chapter:
    title: str
    url: str


def story_slug(story_url: str) -> str:
    parsed = urlparse(story_url)
    slug = Path(parsed.path).parts[-1]
    return slug or "story"


def fetch_html(url: str, timeout: int = 20, retries: int = 3, retry_sleep: float = 2.0) -> str:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, headers=HEADERS, timeout=timeout)
            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            last_error = exc
            if attempt < retries:
                print(f"[WARN] wattpad_vn fetch retry {attempt}/{retries}: {url} | {exc}")
                time.sleep(retry_sleep)
    raise RuntimeError(f"Không fetch được Wattpad VN URL sau {retries} lần: {url} | {last_error}") from last_error


def extract_book_id(soup: BeautifulSoup) -> str:
    bid_input = soup.select_one('input[name="bid"]')
    if not bid_input or not bid_input.get("value"):
        raise ValueError("Không tìm thấy book id trong trang truyện.")
    return str(bid_input["value"])


def extract_max_page(paging_container: BeautifulSoup | None) -> int:
    if paging_container is None:
        return 1

    pages: list[int] = []
    for link in paging_container.select("a[onclick]"):
        onclick_value = link.get("onclick", "")
        match = re.search(r"page\(\d+,\s*(\d+)\)", onclick_value)
        if match:
            pages.append(int(match.group(1)))
    return max(pages) if pages else 1


def extract_chapters_from_container(container: BeautifulSoup | None, base_url: str) -> list[Chapter]:
    if container is None:
        return []

    chapters: list[Chapter] = []
    for anchor in container.select("li > a"):
        href = anchor.get("href")
        if not href:
            continue
        chapters.append(Chapter(title=anchor.get_text(strip=True), url=urljoin(base_url, href)))
    return chapters


def fetch_paginated_chapters(
    book_id: str,
    base_url: str,
    page: int,
    timeout: int = 20,
    retries: int = 3,
    retry_sleep: float = 2.0,
) -> Iterable[Chapter]:
    endpoint = f"{base_url}/get/listchap/{book_id}"
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(endpoint, headers=HEADERS, params={"page": page}, timeout=timeout)
            response.raise_for_status()
            data = response.json().get("data", "")
            break
        except requests.RequestException as exc:
            last_error = exc
            if attempt < retries:
                print(f"[WARN] wattpad_vn chapter list retry {attempt}/{retries}: {endpoint} page={page} | {exc}")
                time.sleep(retry_sleep)
    else:
        raise RuntimeError(
            f"Không fetch được Wattpad VN chapter page sau {retries} lần: {endpoint} page={page} | {last_error}"
        ) from last_error
    snippet_soup = BeautifulSoup(data, "html.parser")
    return extract_chapters_from_container(snippet_soup, base_url)


def collect_story_chapters(
    story_url: str,
    timeout: int = 20,
    retries: int = 3,
    retry_sleep: float = 2.0,
) -> list[Chapter]:
    parsed = urlparse(story_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    soup = BeautifulSoup(fetch_html(story_url, timeout, retries, retry_sleep), "html.parser")
    book_id = extract_book_id(soup)

    chapters = extract_chapters_from_container(soup.select_one("#chapter-list"), base_url)
    max_page = extract_max_page(soup.select_one("#chapter-list .paging"))

    for page in range(2, max_page + 1):
        print(f"Đang lấy danh sách chương page {page}/{max_page}")
        chapters.extend(fetch_paginated_chapters(book_id, base_url, page, timeout, retries, retry_sleep))

    return chapters


def main() -> None:
    parser = argparse.ArgumentParser(description="Crawl danh sách chapter từ wattpad.com.vn.")
    parser.add_argument("story_url", nargs="?", default=DEFAULT_STORY_URL)
    parser.add_argument(
        "--output",
        default="",
        help="Mặc định: story_data/<slug-truyen>/chapters.json",
    )
    args = parser.parse_args()

    chapters = collect_story_chapters(args.story_url)
    payload = {
        "story_url": args.story_url,
        "total_chapters": len(chapters),
        "chapters": [asdict(chapter) for chapter in chapters],
        "stores": [chapter.url for chapter in chapters],
    }

    output_path = Path(args.output) if args.output else Path("story_data") / story_slug(args.story_url) / "chapters.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Hoàn tất. Đã lưu {len(chapters)} chapter vào {output_path}")


if __name__ == "__main__":
    main()
