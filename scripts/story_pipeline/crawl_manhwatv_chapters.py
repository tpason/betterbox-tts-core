#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import time
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "vi,en-US;q=0.9,en;q=0.8",
}

LOCK_PATTERNS = [
    "bạn phải đăng nhập",
    "phải đăng nhập",
    "vui lòng đăng nhập",
    "chương này bị khóa",
    "fa-lock",
]


@dataclass
class ManhwaTVChapter:
    number: int
    title: str
    url: str
    source_chapter_id: str
    is_locked: bool = False
    lock_reason: str = ""


def compact_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def clean_text(value: str | None) -> str:
    value = (value or "").replace("\xa0", " ")
    lines: list[str] = []
    for line in value.splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line:
            lines.append(line)
    return "\n\n".join(lines).strip()


def safe_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.strip().lower())
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-") or "manhwatv-story"


def story_slug(story_url: str, title: str = "") -> str:
    path = urlparse(story_url).path.rstrip("/")
    name = path.rsplit("/", 1)[-1] if path else title
    return safe_slug(re.sub(r"\.html?$", "", name, flags=re.IGNORECASE))


def fetch_html(
    session: requests.Session,
    url: str,
    timeout: int = 30,
    retries: int = 3,
    retry_sleep: float = 2.0,
) -> str:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = session.get(url, headers=HEADERS, timeout=timeout)
            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            last_error = exc
            if attempt < retries:
                print(f"[WARN] manhwatv fetch retry {attempt}/{retries}: {url} | {exc}")
                time.sleep(retry_sleep * attempt)
    raise RuntimeError(f"Cannot fetch ManhwaTV URL after {retries} attempts: {url} | {last_error}") from last_error


def parse_chapter_number(title: str, url: str, fallback: int) -> int:
    for text in (title, url):
        match = re.search(r"(?:chương|chuong|chapter)[^\d]{0,8}0*(\d{1,5})", text, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return fallback


def parse_chapter_id(url: str, fallback: int) -> str:
    parts = [part for part in urlparse(url).path.split("/") if part]
    return re.sub(r"\.html?$", "", parts[-1], flags=re.IGNORECASE) if parts else str(fallback)


def looks_locked_text(text: str) -> bool:
    lowered = text.casefold()
    return any(pattern in lowered for pattern in LOCK_PATTERNS)


def extract_story_id(soup: BeautifulSoup) -> str:
    for selector in ["input[name='truyen']", "input#truyen", "[data-truyen]"]:
        node = soup.select_one(selector)
        if not node:
            continue
        value = node.get("value") or node.get("data-truyen") or ""
        if value:
            return value
    return ""


def fetch_ajax_chapter_list(
    session: requests.Session,
    story_url: str,
    story_id: str,
    timeout: int,
    retries: int,
    retry_sleep: float,
) -> str:
    ajax_url = urljoin(story_url, "/process.php")
    data = {"action": "list_chap", "truyen": story_id}
    headers = {**HEADERS, "Referer": story_url, "X-Requested-With": "XMLHttpRequest"}
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = session.post(ajax_url, headers=headers, data=data, timeout=timeout)
            response.raise_for_status()
            payload = response.json()
            return str(payload.get("list") or "")
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < retries:
                print(f"[WARN] manhwatv ajax retry {attempt}/{retries}: {ajax_url} | {exc}")
                time.sleep(retry_sleep * attempt)
    raise RuntimeError(f"Cannot fetch ManhwaTV chapter list after {retries} attempts: {ajax_url} | {last_error}") from last_error


def extract_chapters_from_html(html: str, story_url: str) -> list[ManhwaTVChapter]:
    soup = BeautifulSoup(html, "html.parser")
    chapters: list[ManhwaTVChapter] = []
    seen: set[str] = set()
    for anchor in soup.select("a[href]"):
        href = anchor.get("href") or ""
        url = urljoin(story_url, href).split("#", 1)[0].rstrip("/")
        parsed = urlparse(url)
        if parsed.netloc != urlparse(story_url).netloc:
            continue
        if not re.search(r"/chuong-\d+", parsed.path, flags=re.IGNORECASE):
            continue
        if url in seen:
            continue
        seen.add(url)
        text = compact_text(anchor.get_text(" ", strip=True))
        number = parse_chapter_number(text, url, len(chapters) + 1)
        locked = bool(anchor.select_one(".fa-lock, i[class*='lock'], .price")) or looks_locked_text(str(anchor))
        chapters.append(
            ManhwaTVChapter(
                number=number,
                title=text or f"Chương {number}",
                url=url,
                source_chapter_id=parse_chapter_id(url, number),
                is_locked=locked,
                lock_reason="locked_or_premium" if locked else "",
            )
        )
    chapters.sort(key=lambda item: item.number)
    return chapters


def extract_tags(soup: BeautifulSoup) -> list[str]:
    tags: list[str] = []
    for selector in ["a[href*='the-loai']", "a[href*='tag']", ".genres a", ".genre a", ".list01 a"]:
        for node in soup.select(selector):
            text = compact_text(node.get_text(" ", strip=True))
            if text:
                tags.append(text)
    return list(dict.fromkeys(tags))


def extract_description(soup: BeautifulSoup) -> str:
    for selector in [".summary", ".description", ".desc", ".content", "[class*='description']", "[class*='summary']"]:
        node = soup.select_one(selector)
        if node:
            text = compact_text(node.get_text(" ", strip=True))
            if len(text) > 80:
                return text
    meta_desc = soup.select_one("meta[name='description'], meta[property='og:description']")
    return compact_text(meta_desc.get("content") if meta_desc else "")


def parse_catalog(
    story_url: str,
    timeout: int = 30,
    retries: int = 3,
    retry_sleep: float = 2.0,
) -> dict:
    session = requests.Session()
    try:
        html = fetch_html(session, story_url, timeout, retries, retry_sleep)
        soup = BeautifulSoup(html, "html.parser")
        title_node = soup.select_one("h1, .title, .story-title")
        title = compact_text(title_node.get_text(" ", strip=True)) if title_node else story_slug(story_url)
        author_node = soup.select_one("a[href*='tac-gia'], a[href*='author'], .author a")
        author = compact_text(author_node.get_text(" ", strip=True)) if author_node else ""
        cover_node = soup.select_one("meta[property='og:image'], .info img, .book img, img")
        cover_image_url = ""
        if cover_node:
            cover_image_url = cover_node.get("content") or cover_node.get("data-src") or cover_node.get("src") or ""
            cover_image_url = urljoin(story_url, cover_image_url) if cover_image_url else ""
        story_id = extract_story_id(soup)
        chapter_html = html
        if story_id:
            try:
                chapter_html = fetch_ajax_chapter_list(session, story_url, story_id, timeout, retries, retry_sleep)
            except Exception as exc:
                print(f"[WARN] ManhwaTV ajax list failed, fallback to story HTML: {exc}")
        chapters = extract_chapters_from_html(chapter_html, story_url)
        page_text = compact_text(soup.get_text(" ", strip=True))
        status_match = re.search(r"(?:Tình trạng|Trạng thái|Status)\s*[:：]?\s*(.*?)(?:\s{2,}|$)", page_text, flags=re.IGNORECASE)
        status = compact_text(status_match.group(1)) if status_match else ""
        return {
            "source": "manhwatv",
            "source_story_id": story_id or story_slug(story_url, title),
            "story_url": story_url.rstrip("/"),
            "slug": story_slug(story_url, title),
            "title": title,
            "author": author,
            "description": extract_description(soup),
            "status": status,
            "tags": extract_tags(soup),
            "cover_image_url": cover_image_url,
            "total_chapters": len(chapters),
            "locked_chapters": sum(1 for chapter in chapters if chapter.is_locked),
            "chapters": [asdict(chapter) for chapter in chapters],
        }
    finally:
        session.close()


def extract_chapter_text(html: str) -> str:
    if looks_locked_text(html):
        raise PermissionError("ManhwaTV chapter is locked or requires login.")
    soup = BeautifulSoup(html, "html.parser")
    for removable in soup.select("script, style, noscript, iframe, nav, header, footer, form, .ads, .advertisement"):
        removable.decompose()
    title_node = soup.select_one("h1, .chapter-title, .title")
    content_node = (
        soup.select_one(".content_view_chap")
        or soup.select_one(".content-view-chap")
        or soup.select_one(".chapter-content")
        or soup.select_one(".reading-content")
        or soup.select_one("article")
    )
    if content_node is None:
        raise ValueError("Cannot find ManhwaTV text chapter content.")
    if content_node.select("img") and len(content_node.get_text(" ", strip=True)) < 120:
        raise ValueError("ManhwaTV chapter appears to be image-based, not text.")
    title = compact_text(title_node.get_text(" ", strip=True)) if title_node else ""
    text = clean_text(content_node.get_text("\n", strip=True))
    if looks_locked_text(text):
        raise PermissionError("ManhwaTV chapter is locked or requires login.")
    if title and not text.startswith(title):
        return f"{title}\n\n{text}".strip()
    return text


def fetch_chapter_text(url: str, timeout: int = 30, retries: int = 3, retry_sleep: float = 2.0) -> str:
    session = requests.Session()
    try:
        return extract_chapter_text(fetch_html(session, url, timeout, retries, retry_sleep))
    finally:
        session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Crawl ManhwaTV story catalog.")
    parser.add_argument("story_url")
    parser.add_argument("--output", default="")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-sleep", type=float, default=2.0)
    args = parser.parse_args()

    catalog = parse_catalog(args.story_url, args.timeout, args.retries, args.retry_sleep)
    output_path = Path(args.output) if args.output else Path("story_data/catalogs/manhwatv") / catalog["slug"] / "chapters.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Đã lưu catalog: {output_path}")
    print(
        f"Story: {catalog['title']} | chapters={len(catalog.get('chapters') or [])} "
        f"locked={catalog.get('locked_chapters') or 0}"
    )


if __name__ == "__main__":
    main()
