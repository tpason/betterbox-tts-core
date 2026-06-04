#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from story_db.story_pipeline_db import repository as repo
from scripts.story_pipeline.crawl_docln_chapters import (
    fetch_chapter_text as fetch_docln_text,
    parse_catalog as parse_docln_catalog,
)
from scripts.story_pipeline.crawl_generic_vn_chapters import (
    fetch_chapter_text as fetch_generic_vn_text,
    parse_catalog as parse_generic_vn_catalog,
    source_info as generic_source_info,
)
from scripts.story_pipeline.crawl_hako_chapters import crawl_catalog as parse_hako_catalog
from scripts.story_pipeline.crawl_lightnovelpub_chapters import (
    fetch_chapter_text as fetch_lightnovelpub_text,
    parse_catalog as parse_lightnovelpub_site_catalog,
)
from scripts.story_pipeline.crawl_manhwatv_chapters import (
    fetch_chapter_text as fetch_manhwatv_text,
    parse_catalog as parse_manhwatv_catalog,
)
from scripts.story_pipeline.crawl_qidian_catalog import parse_catalog as parse_qidian_catalog
from scripts.story_pipeline.crawl_royalroad_chapters import (
    fetch_chapter_text as fetch_royalroad_text,
    parse_catalog as parse_royalroad_catalog,
)
from scripts.story_pipeline.crawl_stories_from_db import (
    enqueue_polish_for_args,
    upsert_downloaded_chapter,
)
from scripts.story_pipeline.crawl_truyenfull_today_chapters import (
    fetch_chapter_text as fetch_truyenfull_today_text,
    parse_catalog as parse_truyenfull_today_catalog,
)
from scripts.story_pipeline.crawl_truyenyy_chapters import (
    fetch_chapter_text as fetch_truyenyy_text,
    parse_catalog as parse_truyenyy_catalog,
)
from scripts.story_pipeline.crawl_wattpad_chapters import collect_story_chapters as parse_wattpad_catalog
from scripts.story_pipeline.download_chapter_texts import fetch_chapter_text as fetch_wattpad_text
from scripts.story_pipeline.download_hako_chapter_texts import (
    fetch_chapter as fetch_hako_chapter,
    looks_locked as hako_looks_locked,
)
from scripts.story_pipeline.download_qidian_public_chapters import (
    extract_chapter_text as extract_qidian_text,
    fetch_html as fetch_qidian_html,
)
from scripts.story_pipeline.polish_worker import process_job as process_polish_job


SUPPORTED_SOURCE_BASES = {
    "truyenfull_today": "https://truyenfull.today",
    "truyenyy": "https://truyenyy.vip",
    "docln": "https://docln.net",
    "hako": "https://ln.hako.vn",
    "wattpad_vn": "https://wattpad.com.vn",
    "manhwatv": "https://manhwatv6.com",
    "sttruyen": "https://sttruyen.com",
    "truyenchuhay": "https://truyenchuhay.vn",
    "truyenhoangdung": "https://www.truyenhoangdung.xyz",
    "royalroad": "https://www.royalroad.com",
    "qidian": "https://www.qidian.com",
    "novelbin": "https://novelbin.com",
    "freewebnovel": "https://freewebnovel.com",
    "lightnovelpub": "https://lightnovelpub.org",
    "novelhub": "https://novelhub.net",
    "skydemonorder": "https://skydemonorder.com",
}

SOURCE_LANGUAGES = {
    "qidian": "zh",
    "royalroad": "en",
    "novelbin": "en",
    "freewebnovel": "en",
    "lightnovelpub": "en",
    "novelhub": "en",
    "skydemonorder": "en",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,vi;q=0.8",
}


def log(message: str) -> None:
    print(message, flush=True)


def safe_slug(value: str) -> str:
    value = re.sub(r"\s+", "-", (value or "").strip().lower())
    value = re.sub(r"[^a-z0-9\u00c0-\u1ef9-]+", "", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value or "story"


def detect_source(url: str) -> tuple[str, str, str]:
    host = urlparse(url).netloc.lower()
    if "truyenfull.today" in host:
        return "truyenfull_today", "TruyenFull Today", "https://truyenfull.today"
    if "truyenyy" in host:
        return "truyenyy", "TruyenYY", "https://truyenyy.vip"
    if "docln" in host or "hako.vn" in host:
        if "ln.hako.vn" in host:
            return "hako", "Hako", "https://ln.hako.vn"
        return "docln", "DocLN", "https://docln.net"
    if "wattpad.com.vn" in host:
        return "wattpad_vn", "Wattpad VN", "https://wattpad.com.vn"
    if "manhwatv" in host:
        return "manhwatv", "ManhwaTV", "https://manhwatv6.com"
    if "royalroad.com" in host:
        return "royalroad", "Royal Road", "https://www.royalroad.com"
    if "qidian.com" in host:
        return "qidian", "Qidian", "https://www.qidian.com"
    if "novelbin.com" in host:
        return "novelbin", "NovelBin", "https://novelbin.com"
    if "freewebnovel.com" in host:
        return "freewebnovel", "FreeWebNovel", "https://freewebnovel.com"
    if "lightnovelpub.org" in host:
        return "lightnovelpub", "LightNovelPub", "https://lightnovelpub.org"
    if "novelhub.net" in host:
        return "novelhub", "NovelHub", "https://novelhub.net"
    if "skydemonorder.com" in host:
        return "skydemonorder", "Sky Demon Order", "https://skydemonorder.com"
    source_code, source_name, base_url = generic_source_info(url)
    return source_code, source_name, base_url


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
                log(f"[WARN] retry {attempt}/{retries}: {url} | {exc}")
                time.sleep(retry_sleep * attempt)
    raise RuntimeError(f"Cannot fetch URL after {retries} attempts: {url} | {last_error}") from last_error


def parse_int(value: str) -> int:
    digits = re.sub(r"[^\d]", "", value or "")
    return int(digits) if digits else 0


def parse_novelbin_catalog(story_url: str, args: argparse.Namespace) -> dict[str, Any]:
    html = fetch_html(story_url, args.timeout, args.retries, args.retry_sleep)
    soup = BeautifulSoup(html, "html.parser")
    title_node = soup.select_one("h1, h3")
    title = title_node.get_text(" ", strip=True) if title_node else safe_slug(urlparse(story_url).path)
    author_node = soup.select_one("a[href*='/a/']")
    author = author_node.get_text(" ", strip=True) if author_node else ""
    page_text = soup.get_text(" ", strip=True)
    latest_match = re.search(r"Latest chapter\s+Chapter\s+(\d+)", page_text, flags=re.IGNORECASE)
    latest_chapter = parse_int(latest_match.group(1)) if latest_match else 0
    if not latest_chapter:
        numbers = [
            parse_int(match.group(1))
            for match in re.finditer(r"/chapter-(\d+)", " ".join(a.get("href") or "" for a in soup.select("a[href]")))
        ]
        latest_chapter = max(numbers, default=0)
    if args.to_chapter:
        latest_chapter = min(latest_chapter or args.to_chapter, args.to_chapter)
    if args.max_chapters and not args.from_chapter:
        latest_chapter = min(latest_chapter, args.max_chapters)

    chapters: list[dict[str, Any]] = []
    if latest_chapter:
        start = max(1, args.from_chapter or 1)
        for number in range(start, latest_chapter + 1):
            chapters.append(
                {
                    "number": number,
                    "title": f"Chapter {number}",
                    "url": urljoin(story_url.rstrip("/") + "/", f"chapter-{number}"),
                    "source_chapter_id": str(number),
                }
            )
    else:
        seen: set[str] = set()
        for anchor in soup.select("a[href*='/chapter-']"):
            href = anchor.get("href") or ""
            chapter_url = urljoin(story_url, href).split("#", 1)[0]
            if chapter_url in seen:
                continue
            seen.add(chapter_url)
            title_text = anchor.get_text(" ", strip=True)
            number_match = re.search(r"chapter\s+(\d+)", title_text, flags=re.IGNORECASE) or re.search(r"/chapter-(\d+)", chapter_url)
            number = parse_int(number_match.group(1)) if number_match else len(chapters) + 1
            chapters.append({"number": number, "title": title_text or f"Chapter {number}", "url": chapter_url, "source_chapter_id": str(number)})
        chapters.sort(key=lambda item: int(item.get("number") or 0))

    return {
        "source": "novelbin",
        "story_url": story_url,
        "slug": safe_slug(urlparse(story_url).path.rstrip("/").rsplit("/", 1)[-1]),
        "title": title,
        "author": author,
        "total_chapters": latest_chapter or len(chapters),
        "chapters": chapters,
    }


def freewebnovel_slug(url: str) -> str:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if "novel" in parts:
        index = parts.index("novel")
        if len(parts) > index + 1:
            return parts[index + 1]
    return parts[0] if parts else "story"


def parse_freewebnovel_catalog(story_url: str, args: argparse.Namespace) -> dict[str, Any]:
    slug = freewebnovel_slug(story_url)
    base_url = f"https://freewebnovel.com/novel/{slug}"
    latest_chapter = args.to_chapter
    if latest_chapter <= 0:
        latest_chapter = args.latest_chapter
    if latest_chapter <= 0:
        latest_chapter = 782
        log("[WARN] freewebnovel latest chapter is not discoverable from chapter pages; default latest_chapter=782. Override with --latest-chapter.")
    start = max(1, args.from_chapter or 1)
    if args.max_chapters:
        latest_chapter = min(latest_chapter, start + args.max_chapters - 1)
    chapters = [
        {
            "number": number,
            "title": f"Chapter {number}",
            "url": f"{base_url}/chapter-{number}",
            "source_chapter_id": str(number),
        }
        for number in range(start, latest_chapter + 1)
    ]
    return {
        "source": "freewebnovel",
        "story_url": base_url,
        "slug": slug,
        "title": slug.replace("-", " ").title(),
        "author": "",
        "total_chapters": latest_chapter,
        "chapters": chapters,
    }


def lightnovelpub_slug(url: str) -> str:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if "novel" in parts:
        index = parts.index("novel")
        if len(parts) > index + 1:
            return parts[index + 1]
    return parts[0] if parts else "story"


def parse_lightnovelpub_catalog(story_url: str, args: argparse.Namespace) -> dict[str, Any]:
    return parse_lightnovelpub_site_catalog(
        story_url,
        timeout=args.timeout,
        retries=args.retries,
        retry_sleep=args.retry_sleep,
        max_catalog_pages=args.max_catalog_pages,
        from_chapter=args.from_chapter,
        to_chapter=args.to_chapter or args.latest_chapter,
        max_chapters=args.max_chapters,
    )


def novelhub_slug(url: str) -> str:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if "novel" in parts:
        index = parts.index("novel")
        if len(parts) > index + 1:
            return parts[index + 1]
    return parts[0] if parts else "story"


def parse_novelhub_catalog(story_url: str, args: argparse.Namespace) -> dict[str, Any]:
    slug = novelhub_slug(story_url)
    base_url = f"https://novelhub.net/novel/{slug}"
    html = fetch_html(base_url, args.timeout, args.retries, args.retry_sleep)
    soup = BeautifulSoup(html, "html.parser")
    title_node = soup.select_one("h1") or soup.select_one("title")
    title = title_node.get_text(" ", strip=True) if title_node else slug.replace("-", " ").title()
    page_text = soup.get_text(" ", strip=True)
    latest_chapter = args.to_chapter or args.latest_chapter
    if latest_chapter <= 0:
        matches = [
            parse_int(match.group(1))
            for match in re.finditer(r"\bChapter\s+(\d+)\b", page_text, flags=re.IGNORECASE)
        ]
        matches.extend(
            parse_int(match.group(1))
            for match in re.finditer(r"/chapter-(\d+)", " ".join(a.get("href") or "" for a in soup.select("a[href]")))
        )
        latest_chapter = max(matches, default=0)
    if latest_chapter <= 0:
        raise RuntimeError("Cannot discover latest chapter for NovelHub. Pass --latest-chapter or --to-chapter.")
    start = max(1, args.from_chapter or 1)
    if args.max_chapters:
        latest_chapter = min(latest_chapter, start + args.max_chapters - 1)
    chapters = [
        {
            "number": number,
            "title": f"Chapter {number}",
            "url": f"{base_url}/chapter-{number}",
            "source_chapter_id": str(number),
        }
        for number in range(start, latest_chapter + 1)
    ]
    return {
        "source": "novelhub",
        "story_url": base_url,
        "slug": slug,
        "title": title,
        "author": "",
        "total_chapters": latest_chapter,
        "chapters": chapters,
    }


def extract_novelbin_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    node = (
        soup.select_one("#chr-content")
        or soup.select_one(".chr-c")
        or soup.select_one(".chapter-c")
        or soup.select_one(".chapter-content")
        or soup.select_one("article")
    )
    if node is None:
        for selector in ["script", "style", "noscript", "nav", "header", "footer", ".comments", ".chapter-nav"]:
            for removable in soup.select(selector):
                removable.decompose()
        text = soup.get_text("\n", strip=True)
    else:
        for removable in node.select("script, style, noscript, iframe, .ads, .advertisement"):
            removable.decompose()
        text = node.get_text("\n", strip=True)
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    lines = [
        line
        for line in lines
        if line
        and "Novel Bin Read light novel" not in line
        and "Report chapter" not in line
        and "Tip: You can use" not in line
    ]
    return "\n\n".join(lines).strip()


def extract_freewebnovel_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for selector in [
        "script",
        "style",
        "noscript",
        "nav",
        "header",
        "footer",
        ".comments",
        ".chapter-nav",
        ".chapter-control",
        ".m-read",
    ]:
        for removable in soup.select(selector):
            removable.decompose()
    node = (
        soup.select_one("#article")
        or soup.select_one(".chapter-content")
        or soup.select_one(".chapter-c")
        or soup.select_one(".chr-c")
        or soup.select_one("article")
    )
    text = node.get_text("\n", strip=True) if node is not None else soup.get_text("\n", strip=True)
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    drop_markers = (
        "Free Web Novel",
        "Novel list",
        "Login/Signup",
        "Prev Chapter",
        "Next Chapter",
        "Background",
        "Font family",
        "Text to Speech",
        "Add to Library",
        "Use arrow keys",
    )
    lines = [line for line in lines if line and not any(marker in line for marker in drop_markers)]
    # Keep the actual prose after the first repeated chapter heading if present.
    for index, line in enumerate(lines):
        if re.fullmatch(r"Chapter\s+\d+", line, flags=re.IGNORECASE):
            return "\n\n".join(lines[index + 1 :]).strip()
    return "\n\n".join(lines).strip()


def extract_lightnovelpub_text(html: str) -> str:
    from scripts.story_pipeline.crawl_lightnovelpub_chapters import extract_chapter_text

    return extract_chapter_text(html)


def extract_novelhub_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one("article#chapter-content") or soup.select_one("#chapter-content") or soup.select_one("article")
    if node is None:
        return ""
    for selector in [
        "script",
        "style",
        "noscript",
        "iframe",
        ".ads",
        ".advertisement",
        ".chapter-nav",
        ".nav-row",
        ".keyboard-hint",
        ".comments",
    ]:
        for removable in node.select(selector):
            removable.decompose()
    lines = [re.sub(r"\s+", " ", line).strip() for line in node.get_text("\n", strip=True).splitlines()]
    drop_markers = (
        "Prev Chapter",
        "Next Chapter",
        "Report chapter",
        "Tip:",
        "Use arrow keys",
    )
    lines = [line for line in lines if line and not any(marker in line for marker in drop_markers)]
    if lines and re.fullmatch(r"Chapter\s+\d+(?:\s*-\s*.+)?", lines[0], flags=re.IGNORECASE):
        lines = lines[1:]
    return "\n\n".join(lines).strip()


def parse_catalog_for_source(source_code: str, url: str, args: argparse.Namespace) -> dict[str, Any]:
    if source_code == "truyenfull_today":
        return parse_truyenfull_today_catalog(url, args.timeout, args.retries, args.retry_sleep, args.max_catalog_pages)
    if source_code == "truyenyy":
        return parse_truyenyy_catalog(url, args.timeout, args.retries, args.retry_sleep, args.max_catalog_pages)
    if source_code == "docln":
        return parse_docln_catalog(url, args.timeout, args.retries, args.retry_sleep)
    if source_code == "hako":
        return parse_hako_catalog(url, args.timeout)
    if source_code == "wattpad_vn":
        chapters = parse_wattpad_catalog(url, args.timeout, args.retries, args.retry_sleep)
        return {
            "source": "wattpad_vn",
            "story_url": url,
            "slug": safe_slug(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]),
            "title": safe_slug(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]),
            "total_chapters": len(chapters),
            "chapters": [asdict(chapter) if is_dataclass(chapter) else dict(chapter) for chapter in chapters],
        }
    if source_code == "manhwatv":
        return parse_manhwatv_catalog(url, args.timeout, args.retries, args.retry_sleep)
    if source_code in {"sttruyen", "truyenchuhay", "truyenhoangdung"}:
        return parse_generic_vn_catalog(url, args.timeout, args.retries, args.retry_sleep, args.max_catalog_pages)
    if source_code == "royalroad":
        return parse_royalroad_catalog(url, args.timeout, args.retries, args.retry_sleep)
    if source_code == "qidian":
        return parse_qidian_catalog(url, args.timeout, args.retries, args.retry_sleep)
    if source_code == "novelbin":
        return parse_novelbin_catalog(url, args)
    if source_code == "freewebnovel":
        return parse_freewebnovel_catalog(url, args)
    if source_code == "lightnovelpub":
        return parse_lightnovelpub_catalog(url, args)
    if source_code == "novelhub":
        return parse_novelhub_catalog(url, args)
    raise ValueError(f"Unsupported alternate source: {source_code}")


def fetch_text_for_source(source_code: str, chapter_url: str, args: argparse.Namespace) -> str:
    if source_code == "truyenfull_today":
        return fetch_truyenfull_today_text(chapter_url, timeout=args.timeout, retries=args.retries, retry_sleep=args.retry_sleep)
    if source_code == "truyenyy":
        return fetch_truyenyy_text(chapter_url, timeout=args.timeout, retries=args.retries, retry_sleep=args.retry_sleep)
    if source_code == "docln":
        return fetch_docln_text(chapter_url, timeout=args.timeout, retries=args.retries, retry_sleep=args.retry_sleep)
    if source_code == "hako":
        _, content = fetch_hako_chapter(chapter_url, args.timeout, args.retries, args.retry_sleep)
        if hako_looks_locked(content):
            raise PermissionError("Hako chapter looks locked.")
        return content
    if source_code == "wattpad_vn":
        return fetch_wattpad_text(chapter_url, timeout=args.timeout, retries=args.retries, retry_sleep=args.retry_sleep)
    if source_code == "manhwatv":
        return fetch_manhwatv_text(chapter_url, timeout=args.timeout, retries=args.retries, retry_sleep=args.retry_sleep)
    if source_code in {"sttruyen", "truyenchuhay", "truyenhoangdung"}:
        return fetch_generic_vn_text(chapter_url, timeout=args.timeout, retries=args.retries, retry_sleep=args.retry_sleep)
    if source_code == "royalroad":
        return fetch_royalroad_text(chapter_url, timeout=args.timeout, retries=args.retries, retry_sleep=args.retry_sleep)
    if source_code == "qidian":
        return extract_qidian_text(
            fetch_qidian_html(chapter_url, timeout=args.timeout, retries=args.retries, retry_sleep=args.retry_sleep)
        )
    if source_code == "novelbin":
        return extract_novelbin_text(fetch_html(chapter_url, args.timeout, args.retries, args.retry_sleep))
    if source_code == "freewebnovel":
        return extract_freewebnovel_text(fetch_html(chapter_url, args.timeout, args.retries, args.retry_sleep))
    if source_code == "lightnovelpub":
        return fetch_lightnovelpub_text(chapter_url, timeout=args.timeout, retries=args.retries, retry_sleep=args.retry_sleep)
    if source_code == "novelhub":
        return extract_novelhub_text(fetch_html(chapter_url, args.timeout, args.retries, args.retry_sleep))
    raise ValueError(f"Unsupported alternate source: {source_code}")


def target_story_from_args(args: argparse.Namespace) -> dict[str, Any]:
    if args.target_story_id:
        return repo.get_story_by_id(args.target_story_id)
    matches = repo.find_stories(
        title_contains=args.target_title or None,
        source_url=args.target_story_url or None,
        source_codes=args.target_source or None,
        limit=2,
    )
    if not matches:
        raise SystemExit("Không tìm thấy target story trong DB.")
    if len(matches) > 1:
        raise SystemExit(
            "Target story không đủ cụ thể, tìm thấy nhiều hơn 1 kết quả. "
            "Hãy dùng --target-story-id hoặc --target-story-url."
        )
    return matches[0]


def create_target_from_alternate(args: argparse.Namespace) -> dict[str, Any]:
    alt_url = args.alternate_url[0]
    source_code, source_name, base_url = detect_source(alt_url)
    raw_language = args.raw_language or SOURCE_LANGUAGES.get(source_code, "vi")
    repo.upsert_source(source_code, source_name, base_url)
    catalog = parse_catalog_for_source(source_code, alt_url, args)
    slug = safe_slug(args.target_slug or catalog.get("slug") or catalog.get("title") or source_code)
    source_author = catalog.get("author") or ""
    source_description = catalog.get("description") or ""
    story = repo.upsert_story(
        source_code,
        {
            "source_story_id": catalog.get("source_story_id") or slug,
            "title": args.target_title or catalog.get("title") or slug,
            "original_title": catalog.get("title") or args.target_title or slug,
            "author": source_author,
            "category": ", ".join(catalog.get("tags") or []) if isinstance(catalog.get("tags"), list) else catalog.get("category"),
            "status": catalog.get("status"),
            "language": raw_language,
            "source_url": alt_url,
            "catalog_url": alt_url,
            "description": source_description,
            "cover_image_url": catalog.get("cover_image_url"),
            "total_chapters": catalog.get("total_chapters") or len(catalog.get("chapters") or []),
            "metadata": {
                "slug": slug,
                "source": source_code,
                "created_from_alternate_source": True,
                "source_author": source_author,
                "source_description": source_description,
            },
            "touch_catalog_checked_at": False,
        },
    )
    log(f"[CREATE] target story {story['id']} | {source_code} | {story['title']} | {alt_url}")
    return story


def build_job_args(args: argparse.Namespace) -> SimpleNamespace:
    return SimpleNamespace(
        polished_output_root=args.polished_output_root,
        vi_model=args.vi_model,
        translate_model=args.translate_model,
        polish_max_attempts=args.polish_max_attempts,
        requeue_done=args.requeue_done,
    )


def build_polish_worker_args(args: argparse.Namespace) -> SimpleNamespace:
    return SimpleNamespace(
        overwrite=args.overwrite_polish,
        translated_output_root=args.translated_output_root,
        ollama_url=args.ollama_url,
        vi_model=args.vi_model,
        translate_model=args.translate_model,
        temperature=args.temperature,
        num_ctx=args.num_ctx,
        timeout=args.ollama_timeout,
        retries=args.ollama_retries,
        keep_alive=args.keep_alive,
        prompt_profile=args.prompt_profile,
        polish_mode=args.polish_mode,
        post_translate=args.post_translate,
        min_output_ratio=args.min_output_ratio,
        polish_max_chars_per_chunk=args.polish_max_chars_per_chunk,
        translate_max_chars_per_chunk=args.translate_max_chars_per_chunk,
    )


def maybe_process_polish_inline(job: dict[str, Any] | None, args: argparse.Namespace) -> None:
    if not job or not args.polish_inline:
        return
    log(f"[POLISH] inline start job={job.get('id')} input={job.get('input_path')}")
    process_polish_job(job, build_polish_worker_args(args))
    log(f"[POLISH] inline done job={job.get('id')} output={job.get('output_path')}")


def mapped_chapter_number(source_number: int, args: argparse.Namespace) -> int:
    if args.target_start and args.source_start:
        return args.target_start + (source_number - args.source_start)
    return source_number + args.chapter_offset


def apply_next_missing_start(target_story: dict[str, Any], args: argparse.Namespace) -> None:
    if not args.from_next_missing:
        return
    progress = repo.get_story_chapter_progress(target_story["id"])
    if args.resume_from == "polished":
        next_chapter = progress["first_tail_unpolished_chapter"] or (progress["max_polished_chapter"] + 1)
    elif args.resume_from == "downloaded":
        next_chapter = progress["max_downloaded_chapter"] + 1
    elif args.resume_from == "unpolished":
        next_chapter = progress["first_unpolished_chapter"] or (progress["max_chapter"] + 1)
    else:
        next_chapter = progress["max_chapter"] + 1
    if next_chapter <= 1:
        log(f"[NEXT] target has no chapters yet; crawl from source start")
        return
    if args.source_start and args.target_start:
        source_next = args.source_start + (next_chapter - args.target_start)
        if source_next > 0:
            args.from_chapter = max(args.from_chapter, next_chapter)
            log(
                f"[NEXT] resume_from={args.resume_from} target max={progress['max_chapter']} "
                f"downloaded={progress['max_downloaded_chapter']} polished={progress['max_polished_chapter']} "
                f"first_unpolished={progress['first_unpolished_chapter']} "
                f"tail_unpolished={progress['first_tail_unpolished_chapter']} count={progress['chapter_count']} "
                f"-> target_from={args.from_chapter} mapped_source_from={source_next}"
            )
        return
    args.from_chapter = max(args.from_chapter, next_chapter)
    log(
        f"[NEXT] resume_from={args.resume_from} target max={progress['max_chapter']} "
        f"downloaded={progress['max_downloaded_chapter']} polished={progress['max_polished_chapter']} "
        f"first_unpolished={progress['first_unpolished_chapter']} "
        f"tail_unpolished={progress['first_tail_unpolished_chapter']} count={progress['chapter_count']} "
        f"-> from_chapter={args.from_chapter}"
    )


def crawl_alternate_source(target_story: dict[str, Any], alt_url: str, args: argparse.Namespace) -> dict[str, Any]:
    source_code, source_name, base_url = detect_source(alt_url)
    raw_language = args.raw_language or SOURCE_LANGUAGES.get(source_code, "vi")
    repo.upsert_source(source_code, source_name, base_url)
    log(f"[ALT] catalog source={source_code} lang={raw_language} url={alt_url}")
    catalog = parse_catalog_for_source(source_code, alt_url, args)
    source_slug = safe_slug(catalog.get("slug") or catalog.get("title") or source_code)
    target_slug = safe_slug(args.target_slug or target_story.get("metadata", {}).get("slug") or target_story["title"])
    output_root = Path(args.text_output_root)
    if raw_language in {"zh", "cn"}:
        output_root = Path(args.raw_zh_output_root)
    elif raw_language in {"en"}:
        output_root = Path(args.raw_en_output_root)
    elif raw_language in {"ko", "kr"}:
        output_root = Path(args.raw_ko_output_root)
    output_dir = output_root / target_slug / f"from_{source_code}_{source_slug}"
    manifest_path = Path(args.catalog_output_root) / "alternate_sources" / target_slug / source_code / source_slug / "chapters.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    chapters = catalog.get("chapters") or []
    if args.from_chapter:
        chapters = [chapter for chapter in chapters if mapped_chapter_number(int(chapter.get("number") or 0), args) >= args.from_chapter]
    if args.to_chapter:
        chapters = [chapter for chapter in chapters if mapped_chapter_number(int(chapter.get("number") or 0), args) <= args.to_chapter]
    if args.max_chapters:
        chapters = chapters[: args.max_chapters]

    imported = 0
    skipped_existing = 0
    failed = 0
    job_args = build_job_args(args)
    chapter_workers = getattr(args, "chapter_workers", 1)

    # --- Parallel prefetch ---
    # Pre-download chapter text for all pending chapters using a thread pool.
    # DB writes and file writes remain sequential after the prefetch phase.
    prefetched: dict[str, str | Exception] = {}
    if chapter_workers > 1:
        pending_to_fetch = [
            ch for ch in chapters
            if args.overwrite
            or not (output_dir / f"chapter{mapped_chapter_number(int(ch.get('number') or 0), args):04d}.txt").exists()
        ]
        if pending_to_fetch:
            log(f"[PREFETCH] {len(pending_to_fetch)} chapters, workers={chapter_workers} source={source_code}")

            def _prefetch_one(chapter: dict) -> tuple[str, str | Exception]:
                url = chapter.get("url") or ""
                if not url:
                    return url, ValueError(f"chapter {chapter.get('number')} has no URL")
                try:
                    return url, fetch_text_for_source(source_code, url, args)
                except Exception as exc:
                    return url, exc

            with ThreadPoolExecutor(max_workers=chapter_workers) as _pool:
                _futures = {_pool.submit(_prefetch_one, ch): ch for ch in pending_to_fetch}
                _done = 0
                for _fut in as_completed(_futures):
                    _url, _result = _fut.result()
                    if _url:
                        prefetched[_url] = _result
                    _done += 1
                    if _done % 20 == 0 or _done == len(pending_to_fetch):
                        log(f"[PREFETCH] {_done}/{len(pending_to_fetch)} done")

    for index, chapter in enumerate(chapters, start=1):
        source_number = int(chapter.get("number") or index)
        chapter_number = mapped_chapter_number(source_number, args)
        title = chapter.get("title") or f"Chương {chapter_number}"
        raw_path = output_dir / f"chapter{chapter_number:04d}.txt"
        if raw_path.exists() and not args.overwrite:
            skipped_existing += 1
            db_chapter = upsert_downloaded_chapter(
                target_story,
                source_chapter_id=f"{source_code}:{chapter.get('source_chapter_id') or source_number}",
                chapter_number=chapter_number,
                title=title,
                source_url=chapter.get("url") or alt_url,
                raw_language=raw_language,
                raw_path=raw_path,
                raw_text_content=raw_path.read_text(encoding="utf-8"),
                volume=chapter.get("volume"),
            )
            job = enqueue_polish_for_args(source_code, target_story, db_chapter, target_slug, raw_path, raw_language, job_args)
            maybe_process_polish_inline(job, args)
            continue
        try:
            chapter_url = chapter.get("url") or ""
            if not chapter_url:
                raise ValueError("chapter has no URL")

            # Use prefetched result if available, else fetch now (sequential fallback)
            if chapter_url in prefetched:
                _pre = prefetched[chapter_url]
                if isinstance(_pre, Exception):
                    raise _pre
                content = _pre
            else:
                content = fetch_text_for_source(source_code, chapter_url, args)

            if not content or len(content) < args.min_text_chars:
                log(f"[SKIP] short/empty source={source_code} chapter={chapter_number} url={chapter_url}")
                continue
            text = f"{title}\n\n{content}".strip() + "\n"
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(text, encoding="utf-8")
            db_chapter = upsert_downloaded_chapter(
                target_story,
                source_chapter_id=f"{source_code}:{chapter.get('source_chapter_id') or source_number}",
                chapter_number=chapter_number,
                title=title,
                source_url=chapter_url,
                raw_language=raw_language,
                raw_path=raw_path,
                raw_text_content=text,
                volume=chapter.get("volume"),
            )
            job = enqueue_polish_for_args(source_code, target_story, db_chapter, target_slug, raw_path, raw_language, job_args)
            maybe_process_polish_inline(job, args)
            imported += 1
            log(f"[OK] {target_slug} chapter={chapter_number} from={source_code} source_chapter={source_number}")
        except Exception as exc:
            failed += 1
            log(f"[WARN] failed chapter target={chapter_number} source={source_code} url={chapter.get('url')}: {type(exc).__name__}: {exc}")
            if args.stop_on_error:
                raise
        if chapter_workers <= 1:
            time.sleep(args.chapter_delay)

    return {
        "source_code": source_code,
        "source_url": alt_url,
        "raw_language": raw_language,
        "catalog_path": manifest_path.as_posix(),
        "imported": imported,
        "skipped_existing": skipped_existing,
        "failed": failed,
        "crawled_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Crawl chapter từ alternate source và merge vào một target story trong DB."
    )
    parser.add_argument("--target-story-id", default="")
    parser.add_argument("--target-story-url", default="")
    parser.add_argument("--target-title", default="")
    parser.add_argument("--target-source", nargs="*", default=[])
    parser.add_argument("--target-slug", default="")
    parser.add_argument("--create-target", action="store_true", help="Tạo target story từ alternate URL đầu tiên nếu chưa có story trong DB.")
    parser.add_argument("--alternate-url", nargs="+", required=True)
    parser.add_argument("--raw-language", default="", help="Override language, ví dụ vi/en/ko/zh.")
    parser.add_argument("--source-start", type=int, default=0)
    parser.add_argument("--target-start", type=int, default=0)
    parser.add_argument("--chapter-offset", type=int, default=0)
    parser.add_argument("--from-chapter", type=int, default=0)
    parser.add_argument(
        "--from-next-missing",
        action="store_true",
        help="Đọc tiến độ target story trong DB và crawl từ chapter kế tiếp theo --resume-from.",
    )
    parser.add_argument(
        "--resume-from",
        choices=["polished", "downloaded", "row", "unpolished"],
        default="polished",
        help=(
            "Cách tính điểm resume cho --from-next-missing. "
            "polished=chapter chưa polish gần cuối, nếu không có thì max polished + 1; downloaded=max downloaded + 1; "
            "row=max row + 1; unpolished=chapter chưa polish đầu tiên."
        ),
    )
    parser.add_argument("--to-chapter", type=int, default=0)
    parser.add_argument("--max-chapters", type=int, default=0)
    parser.add_argument("--latest-chapter", type=int, default=0, help="Fallback latest chapter cho nguồn không expose catalog rõ ràng.")
    parser.add_argument("--max-catalog-pages", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--retry-sleep", type=float, default=2.0)
    parser.add_argument("--chapter-delay", type=float, default=1.5)
    parser.add_argument(
        "--chapter-workers",
        type=int,
        default=1,
        help="Parallel workers để prefetch chapter text (default 1 = tuần tự). "
             "Dùng 2–4 để tăng tốc; quá cao có thể bị rate-limit.",
    )
    parser.add_argument("--min-text-chars", type=int, default=80)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--requeue-done", action="store_true")
    parser.add_argument("--stop-on-error", action="store_true")
    parser.add_argument("--catalog-output-root", default="story_data/catalogs")
    parser.add_argument("--text-output-root", default="story_data/text")
    parser.add_argument("--raw-zh-output-root", default="story_data/raw_zh")
    parser.add_argument("--raw-en-output-root", default="story_data/raw_en")
    parser.add_argument("--raw-ko-output-root", default="story_data/raw_ko")
    parser.add_argument("--polished-output-root", default="story_data/polished")
    parser.add_argument("--translated-output-root", default="story_data/translated")
    parser.add_argument("--vi-model", default="qwen3:14b")
    parser.add_argument("--translate-model", default="translategemma:12b")
    parser.add_argument("--polish-max-attempts", type=int, default=3)
    parser.add_argument(
        "--polish-inline",
        action="store_true",
        help="Sau khi crawl mỗi chapter, xử lý luôn job translate/polish trong cùng process.",
    )
    parser.add_argument("--overwrite-polish", action="store_true")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--temperature", type=float, default=0.25)
    parser.add_argument("--num-ctx", type=int, default=4096)
    parser.add_argument("--ollama-timeout", type=int, default=300)
    parser.add_argument("--ollama-retries", type=int, default=3)
    parser.add_argument("--keep-alive", default="24h")
    parser.add_argument("--prompt-profile", choices=("fast", "full"), default="full")
    parser.add_argument("--polish-mode", choices=("llm", "clean"), default="llm")
    parser.add_argument(
        "--post-translate",
        choices=("polish", "copy"),
        default="polish",
        help="Sau khi dịch raw khác tiếng Việt: polish bằng LLM, hoặc copy bản dịch sang polished output.",
    )
    parser.add_argument("--polish-max-chars-per-chunk", type=int, default=5000)
    parser.add_argument("--translate-max-chars-per-chunk", type=int, default=2500)
    parser.add_argument(
        "--min-output-ratio",
        type=float,
        default=0.70,
        help="Nếu output polish của một chunk ngắn hơn tỷ lệ này so với input, fallback clean-only để tránh mất ý.",
    )
    args = parser.parse_args()

    if args.create_target and not (args.target_story_id or args.target_story_url):
        target_story = create_target_from_alternate(args)
    else:
        target_story = target_story_from_args(args)
    log(f"[TARGET] {target_story['id']} | {target_story['source_code']} | {target_story['title']} | {target_story['source_url']}")
    apply_next_missing_start(target_story, args)
    results = [crawl_alternate_source(target_story, alt_url, args) for alt_url in args.alternate_url]
    metadata = target_story.get("metadata") or {}
    previous = metadata.get("alternate_sources") or []
    repo.update_story_metadata(
        target_story["id"],
        {
            "alternate_sources": [*previous, *results],
            "alternate_sources_updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    log("[DONE] alternate sources merged:")
    log(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
