#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.story_pipeline.crawl_qidian_rankings import (  # noqa: E402
    DEFAULT_RANK_URLS,
    QidianBook,
    parse_book_li,
)
from story_db.story_pipeline_db import repository as repo  # noqa: E402


def add_qidian_page(url: str, page: int) -> str:
    if page <= 1:
        return url
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") + f"/page{page}/"
    return urlunparse(parsed._replace(path=path))


def stable_source_story_id(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1] or url


def parse_rank_html(rank_name: str, rank_url: str, html: str, limit: int) -> list[QidianBook]:
    soup = BeautifulSoup(html, "html.parser")
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


def looks_like_waf_or_captcha(html: str) -> bool:
    lowered = html.lower()
    return any(
        marker in lowered
        for marker in [
            "x-waf",
            "captcha",
            "verify",
            "安全验证",
            "人机验证",
            "访问验证",
        ]
    )


def import_playwright():
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "Thiếu dependency playwright. Cài bằng:\n"
            "  ./viterbox/venv/bin/python -m pip install playwright\n"
            "  ./viterbox/venv/bin/python -m playwright install chromium\n"
        ) from exc
    return sync_playwright, PlaywrightTimeoutError


def fetch_rendered_html(
    page: Any,
    url: str,
    *,
    timeout_ms: int,
    wait_ms: int,
    manual_wait_seconds: int,
) -> str:
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    if wait_ms > 0:
        page.wait_for_timeout(wait_ms)
    html = page.content()
    if looks_like_waf_or_captcha(html) and manual_wait_seconds > 0:
        print(
            f"[ACTION] Qidian có vẻ đang hiển thị WAF/captcha: {url}\n"
            f"         Xử lý trong browser đang mở. Script sẽ chờ {manual_wait_seconds}s rồi đọc lại HTML."
        )
        page.wait_for_timeout(manual_wait_seconds * 1000)
        html = page.content()
    return html


def discover_books(args: argparse.Namespace) -> list[QidianBook]:
    sync_playwright, PlaywrightTimeoutError = import_playwright()
    profile_dir = Path(args.profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    launch_options: dict[str, Any] = {
        "user_data_dir": profile_dir.as_posix(),
        "headless": not args.headful,
        "slow_mo": args.slow_mo,
        "locale": "zh-CN",
        "viewport": {"width": 1366, "height": 900},
        "user_agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--lang=zh-CN",
        ],
    }
    if args.channel:
        launch_options["channel"] = args.channel
    if args.executable_path:
        launch_options["executable_path"] = args.executable_path

    books: list[QidianBook] = []
    seen_urls: set[str] = set()
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(**launch_options)
        page = context.pages[0] if context.pages else context.new_page()
        for rank in args.ranks:
            base_rank_url = args.rank_url or DEFAULT_RANK_URLS[rank]
            for page_number in range(1, args.pages + 1):
                rank_url = add_qidian_page(base_rank_url, page_number)
                try:
                    html = fetch_rendered_html(
                        page,
                        rank_url,
                        timeout_ms=args.timeout * 1000,
                        wait_ms=args.wait_ms,
                        manual_wait_seconds=args.manual_wait,
                    )
                except PlaywrightTimeoutError as exc:
                    print(f"[WARN] qidian playwright timeout: {rank_url} | {exc}")
                    continue
                except Exception as exc:
                    print(f"[WARN] qidian playwright skip: {rank_url} | {type(exc).__name__}: {exc}")
                    continue

                parsed = parse_rank_html(rank, rank_url, html, args.limit_per_page)
                if not parsed:
                    html_path = Path(args.debug_html_dir) / f"{rank}_page{page_number}.html"
                    html_path.parent.mkdir(parents=True, exist_ok=True)
                    html_path.write_text(html, encoding="utf-8")
                    print(f"[WARN] qidian parsed 0 books: {rank_url} debug_html={html_path}")
                    continue

                for book in parsed:
                    if book.book_url in seen_urls:
                        continue
                    seen_urls.add(book.book_url)
                    books.append(book)
                print(f"[OK] qidian {rank} page={page_number} parsed={len(parsed)} total={len(books)}")
        context.close()
    return books


def upsert_books(books: list[QidianBook]) -> int:
    repo.upsert_source("qidian", "Qidian", "https://www.qidian.com")
    count = 0
    for index, book in enumerate(books, start=1):
        repo.upsert_story(
            "qidian",
            {
                "source_story_id": stable_source_story_id(book.book_url),
                "title": book.title,
                "original_title": book.title,
                "author": book.author or None,
                "category": book.category or None,
                "status": book.status or None,
                "language": "zh",
                "source_url": book.book_url,
                "catalog_url": book.book_url,
                "description": book.intro or None,
                "rank_name": book.rank_name,
                "rank_position": index,
                "metadata": {
                    "source": "qidian",
                    "rank_url": book.rank_url,
                    "latest_update": book.latest_update,
                    "tags": [part for part in [book.category, book.status] if part],
                    "discovery_method": "playwright",
                    "discovery_updated_at": datetime.now(timezone.utc).isoformat(),
                },
            },
        )
        count += 1
    return count


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Discover Qidian bằng Playwright/browser profile. Script không bypass captcha; "
            "nếu Qidian hiện WAF/captcha, dùng --headful và xử lý thủ công trong browser."
        )
    )
    parser.add_argument(
        "--ranks",
        nargs="+",
        choices=sorted(DEFAULT_RANK_URLS),
        default=["free", "free_all", "hotsales", "readindex", "yuepiao"],
    )
    parser.add_argument("--rank-url", default="", help="URL ranking tùy chỉnh, dùng chung với rank đầu tiên.")
    parser.add_argument("--pages", type=int, default=1)
    parser.add_argument("--limit-per-page", type=int, default=40)
    parser.add_argument("--profile-dir", default=".browser/qidian")
    parser.add_argument(
        "--channel",
        default="",
        help="Browser channel, ví dụ: chrome hoặc chromium. Dùng --channel chrome để mở Chrome thật nếu đã cài.",
    )
    parser.add_argument(
        "--executable-path",
        default="",
        help="Đường dẫn Chrome/Chromium tùy chỉnh nếu Playwright không tìm thấy browser channel.",
    )
    parser.add_argument("--headful", action="store_true", help="Mở browser thật để login/verify captcha thủ công.")
    parser.add_argument("--manual-wait", type=int, default=90, help="Số giây chờ khi phát hiện WAF/captcha.")
    parser.add_argument("--wait-ms", type=int, default=2500)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--slow-mo", type=int, default=0)
    parser.add_argument("--debug-html-dir", default="story_data/debug/qidian_playwright")
    parser.add_argument("--output", default="")
    parser.add_argument("--no-db", action="store_true")
    args = parser.parse_args()

    books = discover_books(args)
    output_path = (
        Path(args.output)
        if args.output
        else Path("story_data/qidian/playwright")
        / f"books_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            {
                "source": "qidian",
                "method": "playwright",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "total": len(books),
                "books": [asdict(book) for book in books],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    db_count = 0
    if not args.no_db:
        db_count = upsert_books(books)

    print(f"Qidian Playwright discovery xong: books={len(books)} db_upsert={db_count} output={output_path}")


if __name__ == "__main__":
    main()
