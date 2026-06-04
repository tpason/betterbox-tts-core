#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from story_db.story_pipeline_db import repository as repo  # noqa: E402


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}


@dataclass
class NaverEpisode:
    number: int
    title: str
    url: str
    source_chapter_id: str
    is_locked: bool = True
    lock_reason: str = "naver_series_metadata_only"


@dataclass
class NaverSeriesCatalog:
    source: str
    product_no: str
    source_url: str
    mobile_url: str
    title: str
    author: str
    category: str
    status: str
    description: str
    cover_image_url: str
    total_chapters: int
    free_chapters: int
    chapters: list[NaverEpisode]


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def parse_int(value: str | None) -> int:
    digits = re.sub(r"[^\d]", "", value or "")
    return int(digits) if digits else 0


def product_no_from_url(url: str) -> str:
    parsed = urlparse(url)
    product_no = parse_qs(parsed.query).get("productNo", [""])[0]
    if not product_no:
        raise ValueError(f"Không tìm thấy productNo trong Naver Series URL: {url}")
    return product_no


def canonical_detail_url(product_no: str) -> str:
    return f"https://series.naver.com/novel/detail.series?productNo={product_no}"


def mobile_detail_url(product_no: str) -> str:
    return f"https://m.series.naver.com/novel/detail.series?productNo={product_no}"


def with_mobile_host(url: str) -> str:
    product_no = product_no_from_url(url)
    return mobile_detail_url(product_no)


def fetch_html(url: str, timeout: int, retries: int, retry_sleep: float) -> str:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, headers=HEADERS, timeout=timeout)
            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            last_error = exc
            if attempt < retries:
                print(f"[WARN] naver_series retry {attempt}/{retries}: {url} | {exc}")
                time.sleep(retry_sleep)
    raise RuntimeError(f"Không fetch được Naver Series URL sau {retries} lần: {url} | {last_error}") from last_error


def meta_content(soup: BeautifulSoup, prop: str) -> str:
    node = soup.select_one(f'meta[property="{prop}"], meta[name="{prop}"]')
    return clean_text(str(node.get("content") or "")) if node else ""


def extract_labeled_value(text: str, label: str) -> str:
    match = re.search(rf"{re.escape(label)}\s+([^\s|]+(?:\s+[^\s|]+){{0,4}})", text)
    return clean_text(match.group(1)) if match else ""


def extract_story_meta(soup: BeautifulSoup, detail_url: str, mobile_url: str) -> dict[str, Any]:
    page_text = clean_text(soup.get_text(" ", strip=True))

    title = ""
    for selector in ["h1", "h2", ".end_head h2", ".detail_title", ".title", "strong.title"]:
        node = soup.select_one(selector)
        if node:
            title = clean_text(node.get_text(" ", strip=True))
            if title and "NAVER" not in title.upper():
                break
    if not title:
        title = re.sub(r"\s*[:|].*$", "", meta_content(soup, "og:title")) or product_no_from_url(detail_url)
    title = re.sub(r"^(새로운 에피소드|신규)\s*", "", title).strip()

    cover = meta_content(soup, "og:image")
    if cover:
        cover = urljoin(mobile_url, cover)

    author = extract_labeled_value(page_text, "작가")
    category = extract_labeled_value(page_text, "장르")
    status = extract_labeled_value(page_text, "연재상태")

    compact = re.sub(r"\s+", " ", page_text)
    meta_match = re.search(r"([가-힣A-Za-z]+)\s*\|\s*([^|]+?)\s*총\s*([\d,]+)\s*화\s*\(?([^)|\s]+)?", compact)
    if meta_match:
        category = category or clean_text(meta_match.group(1))
        author = author or clean_text(meta_match.group(2))
        status = status or clean_text(meta_match.group(4))
        total_chapters = parse_int(meta_match.group(3))
    else:
        total_match = re.search(r"총\s*([\d,]+)\s*화\s*\(?([^)|\s]+)?", compact)
        total_chapters = parse_int(total_match.group(1)) if total_match else 0
        if total_match and not status:
            status = clean_text(total_match.group(2))

    free_match = re.search(r"([\d,]+)\s*화\s*무료", compact)
    free_chapters = parse_int(free_match.group(1)) if free_match else 0

    description = ""
    description_match = re.search(r"작품\s*소개\s*(.*?)(?:공지|가격\s*정보|총\s*[\d,]+\s*화|$)", compact)
    if description_match:
        description = clean_text(description_match.group(1))[:1000]
    if not description:
        description = meta_content(soup, "og:description")[:1000]

    return {
        "title": title,
        "author": author,
        "category": category or "Korean Web Novel",
        "status": status,
        "description": description,
        "cover_image_url": cover,
        "total_chapters": total_chapters,
        "free_chapters": free_chapters,
    }


def episode_number_from_text(text: str, fallback: int) -> int:
    match = re.search(r"(\d+)\s*화", text)
    if match:
        return int(match.group(1))
    match = re.search(r"\b0*(\d{1,5})\b", text)
    return int(match.group(1)) if match else fallback


def normalize_episode_url(base_url: str, href: str) -> str:
    url = urljoin(base_url, href)
    parsed = urlparse(url)
    if parsed.netloc == "series.naver.com":
        parsed = parsed._replace(netloc="m.series.naver.com")
    return urlunparse(parsed)


def extract_episode_id(url: str, fallback: int) -> str:
    query = parse_qs(urlparse(url).query)
    for key in ["episodeNo", "volumeNo", "chapterNo", "serviceNo"]:
        if query.get(key):
            return query[key][0]
    return str(fallback)


def extract_episodes(soup: BeautifulSoup, base_url: str) -> list[NaverEpisode]:
    episodes: list[NaverEpisode] = []
    seen_urls: set[str] = set()
    for anchor in soup.select("a[href]"):
        href = anchor.get("href") or ""
        if not any(key in href for key in ["episodeNo=", "volumeNo=", "chapterNo=", "serviceNo="]):
            continue
        url = normalize_episode_url(base_url, href)
        if url in seen_urls:
            continue
        title = clean_text(anchor.get_text(" ", strip=True))
        if not title:
            container = anchor.find_parent(["li", "tr", "div"])
            title = clean_text(container.get_text(" ", strip=True)) if container else ""
        if not title:
            title = f"Episode {len(episodes) + 1}"
        seen_urls.add(url)
        number = episode_number_from_text(title, len(episodes) + 1)
        episodes.append(
            NaverEpisode(
                number=number,
                title=title,
                url=url,
                source_chapter_id=extract_episode_id(url, number),
            )
        )
    episodes.sort(key=lambda item: item.number)
    return episodes


def placeholder_episodes(total_chapters: int, max_placeholders: int) -> list[NaverEpisode]:
    if not max_placeholders or not total_chapters:
        return []
    count = min(total_chapters, max_placeholders)
    return [
        NaverEpisode(
            number=number,
            title=f"{number}화",
            url="",
            source_chapter_id=str(number),
            lock_reason="naver_series_placeholder",
        )
        for number in range(1, count + 1)
    ]


def parse_catalog(
    story_url: str,
    timeout: int,
    retries: int,
    retry_sleep: float,
    max_placeholders: int = 0,
) -> NaverSeriesCatalog:
    product_no = product_no_from_url(story_url)
    detail_url = canonical_detail_url(product_no)
    mobile_url = mobile_detail_url(product_no)
    soup = BeautifulSoup(fetch_html(mobile_url, timeout, retries, retry_sleep), "html.parser")
    meta = extract_story_meta(soup, detail_url, mobile_url)
    episodes = extract_episodes(soup, mobile_url)
    if not episodes:
        episodes = placeholder_episodes(meta["total_chapters"], max_placeholders)
    return NaverSeriesCatalog(
        source="naver_series",
        product_no=product_no,
        source_url=detail_url,
        mobile_url=mobile_url,
        title=meta["title"],
        author=meta["author"],
        category=meta["category"],
        status=meta["status"],
        description=meta["description"],
        cover_image_url=meta["cover_image_url"],
        total_chapters=max(meta["total_chapters"], len(episodes)),
        free_chapters=meta["free_chapters"],
        chapters=episodes,
    )


def write_catalog(catalog: NaverSeriesCatalog, output_root: Path) -> Path:
    output_dir = output_root / catalog.product_no
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "chapters.json"
    output_path.write_text(
        json.dumps(asdict(catalog), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output_path


def upsert_catalog(catalog: NaverSeriesCatalog) -> dict[str, Any]:
    repo.upsert_source("naver_series", "Naver Series", "https://series.naver.com")
    story = repo.upsert_story(
        "naver_series",
        {
            "source_story_id": catalog.product_no,
            "title": catalog.title,
            "original_title": catalog.title,
            "author": catalog.author or None,
            "category": catalog.category or None,
            "status": catalog.status or None,
            "language": "ko",
            "source_url": catalog.source_url,
            "catalog_url": catalog.mobile_url,
            "description": catalog.description or None,
            "cover_image_url": catalog.cover_image_url or None,
            "total_chapters": catalog.total_chapters,
            "free_chapters": catalog.free_chapters,
            "locked_chapters": max(catalog.total_chapters - catalog.free_chapters, 0),
            "is_completed": catalog.status in {"완결", "completed", "Complete", "COMPLETE"},
            "metadata": {
                "source": "naver_series",
                "product_no": catalog.product_no,
                "mobile_url": catalog.mobile_url,
                "tags": [tag for tag in ["Korean", "Naver Series", catalog.category, catalog.status] if tag],
            },
        },
    )
    for episode in catalog.chapters:
        repo.upsert_chapter(
            story["id"],
            {
                "source_chapter_id": episode.source_chapter_id,
                "chapter_number": episode.number,
                "title": episode.title,
                "source_url": episode.url or catalog.mobile_url,
                "is_locked": episode.is_locked,
                "lock_reason": episode.lock_reason,
                "raw_language": "ko",
                "is_downloaded": False,
            },
        )
    return story


def story_urls_from_db(limit: int) -> list[str]:
    repo.upsert_source("naver_series", "Naver Series", "https://series.naver.com")
    stories = repo.list_active_stories(source_codes=["naver_series"], limit=limit)
    return [str(story["source_url"]) for story in stories if story.get("source_url")]


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Crawl metadata/catalog public của Naver Series. Script này không bypass login/paywall "
            "và không tải nội dung chapter bằng viewer."
        )
    )
    parser.add_argument("story_urls", nargs="*", help="Naver Series detail URL có productNo.")
    parser.add_argument("--from-db", action="store_true", help="Crawl các story naver_series đang active trong DB.")
    parser.add_argument("--limit-stories", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-sleep", type=float, default=2.0)
    parser.add_argument("--output-root", default="story_data/catalogs/naver_series")
    parser.add_argument("--max-placeholders", type=int, default=0, help="0 = không tạo chapter giả khi HTML không expose episode.")
    parser.add_argument("--no-db", action="store_true")
    args = parser.parse_args()

    urls = list(args.story_urls)
    if args.from_db:
        urls.extend(story_urls_from_db(args.limit_stories))
    urls = list(dict.fromkeys(urls))
    if not urls:
        raise SystemExit("Cần truyền story URL hoặc dùng --from-db.")

    ok = 0
    failed = 0
    for story_url in urls:
        try:
            catalog = parse_catalog(story_url, args.timeout, args.retries, args.retry_sleep, args.max_placeholders)
            output_path = write_catalog(catalog, Path(args.output_root))
            if not args.no_db:
                upsert_catalog(catalog)
            ok += 1
            print(
                f"[OK] naver_series {catalog.title} | chapters={len(catalog.chapters)}/"
                f"{catalog.total_chapters} free={catalog.free_chapters} output={output_path}"
            )
        except Exception as exc:
            failed += 1
            print(f"[WARN] skip naver_series {story_url}: {type(exc).__name__}: {exc}")

    print(f"Done. naver_series catalog ok={ok} failed={failed}")


if __name__ == "__main__":
    main()
