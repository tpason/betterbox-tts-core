#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


DEFAULT_RANK_URLS = {
    "free": "https://www.qidian.com/free/",
    "free_all": "https://www.qidian.com/free/all/",
    "free_completed": "https://www.qidian.com/free/all/action1/",
    "hotsales": "https://www.qidian.com/rank/hotsales/",
    "readindex": "https://www.qidian.com/rank/readindex/",
    "yuepiao": "https://www.qidian.com/rank/yuepiao/",
    "recom": "https://www.qidian.com/rank/recom/",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


@dataclass
class QidianBook:
    rank_name: str
    rank_url: str
    position: int
    title: str
    author: str
    category: str
    status: str
    intro: str
    latest_update: str
    book_url: str


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
                print(f"[WARN] qidian fetch retry {attempt}/{retries}: {url} | {exc}")
                time.sleep(retry_sleep)
    raise RuntimeError(f"Không fetch được Qidian URL sau {retries} lần: {url} | {last_error}") from last_error


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def parse_book_li(rank_name: str, rank_url: str, li, position: int) -> QidianBook | None:
    title_node = li.select_one("h2 a, h4 a, .book-mid-info h2 a, .book-mid-info h4 a")
    if title_node is None:
        return None

    title = clean_text(title_node.get_text(" ", strip=True))
    book_url = urljoin(rank_url, title_node.get("href", ""))

    author_node = li.select_one(".author .name, .author a.name, .author a")
    author = clean_text(author_node.get_text(" ", strip=True)) if author_node else ""

    author_text = ""
    author_container = li.select_one(".author")
    if author_container:
        author_text = clean_text(author_container.get_text(" ", strip=True))

    category = ""
    status = ""
    parts = [part for part in re.split(r"[|·]", author_text) if part.strip()]
    if len(parts) >= 2:
        category = clean_text(parts[1])
    if len(parts) >= 3:
        status = clean_text(parts[2])

    intro_node = li.select_one(".intro, .book-mid-info .intro")
    intro = clean_text(intro_node.get_text(" ", strip=True)) if intro_node else ""

    update_node = li.select_one(".update, .book-mid-info .update")
    latest_update = clean_text(update_node.get_text(" ", strip=True)) if update_node else ""

    return QidianBook(
        rank_name=rank_name,
        rank_url=rank_url,
        position=position,
        title=title,
        author=author,
        category=category,
        status=status,
        intro=intro,
        latest_update=latest_update,
        book_url=book_url,
    )


def parse_rank_page(
    rank_name: str,
    rank_url: str,
    limit: int,
    timeout: int = 25,
    retries: int = 3,
    retry_sleep: float = 2.0,
) -> list[QidianBook]:
    soup = BeautifulSoup(fetch_html(rank_url, timeout, retries, retry_sleep), "html.parser")

    candidates = soup.select(".book-img-text li")
    if not candidates:
        candidates = soup.select("li")

    books: list[QidianBook] = []
    seen_urls: set[str] = set()
    for li in candidates:
        book = parse_book_li(rank_name, rank_url, li, len(books) + 1)
        if book is None or not book.title or not book.book_url:
            continue
        if book.book_url in seen_urls:
            continue
        seen_urls.add(book.book_url)
        books.append(book)
        if limit and len(books) >= limit:
            break

    return books


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Crawl metadata các truyện hot từ Qidian ranking pages. "
            "Script này chỉ lấy bảng xếp hạng/metadata công khai, không tải nội dung chương."
        )
    )
    parser.add_argument(
        "--rank",
        choices=sorted(DEFAULT_RANK_URLS),
        default="hotsales",
        help="Bảng xếp hạng Qidian cần crawl.",
    )
    parser.add_argument("--url", default="", help="URL ranking tùy chỉnh nếu không dùng --rank.")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--timeout", type=int, default=25)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-sleep", type=float, default=2.0)
    parser.add_argument("--output", default="", help="Mặc định: story_data/qidian/<rank>/books.json")
    args = parser.parse_args()

    rank_url = args.url or DEFAULT_RANK_URLS[args.rank]
    books = parse_rank_page(args.rank, rank_url, args.limit, args.timeout, args.retries, args.retry_sleep)
    if not books:
        raise SystemExit(
            "Không parse được book nào. Qidian có thể đã đổi HTML, chặn request, hoặc cần cookie trình duyệt."
        )

    output_path = Path(args.output) if args.output else Path("story_data/qidian") / args.rank / "books.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            {
                "source": "qidian",
                "rank": args.rank,
                "rank_url": rank_url,
                "total": len(books),
                "books": [asdict(book) for book in books],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"Hoàn tất. Đã lưu {len(books)} truyện vào {output_path}")


if __name__ == "__main__":
    main()
