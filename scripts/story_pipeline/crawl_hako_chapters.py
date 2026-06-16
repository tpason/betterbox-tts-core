#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


def slug_from_url(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    slug = path.split("/")[-1] if path else "hako_story"
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", slug).strip("-") or "hako_story"


def extract_chapter_number(title: str, fallback: int) -> int:
    match = re.search(r"(?:chương|chapter)\s*0*(\d+)", title, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))
    match = re.search(r"\b0*(\d{1,4})\b", title)
    if match:
        return int(match.group(1))
    return fallback


def crawl_catalog(story_url: str, timeout: int, retries: int = 3, retry_sleep: float = 2.0) -> dict:
    headers = {"User-Agent": "Mozilla/5.0 BetterBox-TTS story crawler"}
    last_error: Exception | None = None
    html = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(story_url, headers=headers, timeout=timeout)
            response.raise_for_status()
            html = response.text
            break
        except requests.RequestException as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(retry_sleep * attempt)
    if html is None:
        raise RuntimeError(f"Cannot fetch Hako URL after {retries} attempts: {story_url} | {last_error}") from last_error
    soup = BeautifulSoup(html, "html.parser")

    title_node = soup.select_one("h1, .series-name, .series-title")
    title = title_node.get_text(" ", strip=True) if title_node else slug_from_url(story_url)
    author_node = (
        soup.select_one(".series-author a")
        or soup.select_one(".series-author")
        or soup.select_one(".author a")
        or soup.select_one(".author")
        or soup.select_one("a[href*='/thanh-vien/']")
        or soup.select_one("[class*='author'] a")
        or soup.select_one("[class*='author']")
    )
    author = author_node.get_text(" ", strip=True) if author_node else None
    cover_node = soup.select_one("meta[property='og:image'], .series-cover img, .cover img")
    cover_image_url = None
    if cover_node:
        cover_image_url = cover_node.get("content") or cover_node.get("src")
        if cover_image_url:
            cover_image_url = urljoin(story_url, cover_image_url)

    chapters: list[dict] = []
    seen: set[str] = set()
    for link in soup.select("a[href]"):
        href = link.get("href") or ""
        text = link.get_text(" ", strip=True)
        if not text or not re.search(r"(?:chương|chapter)\s*\d+", text, flags=re.IGNORECASE):
            continue
        if "/c" not in href:
            continue
        url = urljoin(story_url, href)
        if url in seen:
            continue
        seen.add(url)
        chapters.append(
            {
                "number": extract_chapter_number(text, len(chapters) + 1),
                "title": text,
                "url": url,
                "is_locked": False,
            }
        )

    chapters.sort(key=lambda item: item["number"])
    return {
        "source": "hako",
        "story_url": story_url,
        "slug": slug_from_url(story_url),
        "title": title,
        "author": author,
        "cover_image_url": cover_image_url,
        "chapter_count": len(chapters),
        "chapters": chapters,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Crawl Hako story catalog into chapters.json.")
    parser.add_argument("story_url")
    parser.add_argument("--output-root", default="story_data/hako")
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    catalog = crawl_catalog(args.story_url, args.timeout)
    output_dir = Path(args.output_root) / catalog["slug"]
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "chapters.json"
    output_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Đã lưu catalog: {output_path}")
    print(f"Story: {catalog['title']} | chapters={catalog['chapter_count']}")


if __name__ == "__main__":
    main()
