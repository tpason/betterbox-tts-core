#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests import HTTPError

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from story_db.story_pipeline_db import repository as repo
from scripts.story_pipeline.genre_prompts import detect_genre, find_char_map_file, resolve_genre_from_context
from scripts.story_pipeline.crawl_hako_chapters import crawl_catalog as crawl_hako_catalog
from scripts.story_pipeline.crawl_lightnovelpub_chapters import (
    fetch_chapter_text as fetch_lightnovelpub_text,
    fetch_html as fetch_lightnovelpub_html,
    extract_chapter_text as extract_lightnovelpub_text,
    extract_chapter_title as extract_lightnovelpub_title,
    parse_catalog as crawl_lightnovelpub_catalog,
    safe_slug as lightnovelpub_safe_slug,
)
from scripts.story_pipeline.crawl_docln_chapters import (
    fetch_chapter_text as fetch_docln_text,
    parse_catalog as crawl_docln_catalog,
)
from scripts.story_pipeline.crawl_manhwatv_chapters import (
    fetch_chapter_text as fetch_manhwatv_text,
    parse_catalog as crawl_manhwatv_catalog,
)
from scripts.story_pipeline.crawl_generic_vn_chapters import (
    fetch_chapter_text as fetch_generic_vn_text,
    parse_catalog as crawl_generic_vn_catalog,
)
from scripts.story_pipeline.crawl_wattpad_chapters import (
    collect_story_chapters as crawl_wattpad_catalog,
    story_slug as wattpad_slug,
)
from scripts.story_pipeline.crawl_qidian_catalog import parse_catalog as crawl_qidian_catalog
from scripts.story_pipeline.crawl_royalroad_chapters import (
    fetch_chapter_text as fetch_royalroad_text,
    parse_catalog as crawl_royalroad_catalog,
    safe_slug as royalroad_safe_slug,
)
from scripts.story_pipeline.crawl_truyenfull_today_chapters import (
    fetch_chapter_text as fetch_truyenfull_today_text,
    parse_catalog as crawl_truyenfull_today_catalog,
)
from scripts.story_pipeline.crawl_truyenyy_chapters import (
    fetch_chapter_text as fetch_truyenyy_text,
    parse_catalog as crawl_truyenyy_catalog,
)
from scripts.story_pipeline.download_chapter_texts import fetch_chapter_text as fetch_wattpad_text
from scripts.story_pipeline.download_hako_chapter_texts import (
    chapter_filename as hako_chapter_filename,
    fetch_chapter as fetch_hako_chapter,
    looks_locked as hako_looks_locked,
)
from scripts.story_pipeline.download_qidian_public_chapters import (
    extract_chapter_text as extract_qidian_text,
    fetch_html as fetch_qidian_html,
    safe_slug as qidian_slug,
)
from scripts.story_pipeline.crawl_utils import looks_blocked
from scripts.story_pipeline.crawl_fanmtl_chapters import (
    fetch_chapter_text as fetch_fanmtl_text,
    parse_catalog as crawl_fanmtl_catalog,
    safe_slug as fanmtl_safe_slug,
    upsert_catalog_to_db as fanmtl_upsert_story,
    upsert_catalog_chapters_to_db as fanmtl_upsert_chapters,
    chapter_path as fanmtl_chapter_path,
    write_if_needed as fanmtl_write_if_needed,
)

PUBLIC_EN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,vi;q=0.8",
}


def host_from_url(url: str | None) -> str:
    if not url:
        return ""
    return urlparse(url).netloc.lower()


def is_source_unavailable_error(exc: Exception) -> bool:
    if isinstance(exc, RuntimeError) and exc.__cause__ is not None:
        return is_source_unavailable_error(exc.__cause__)
    if isinstance(exc, HTTPError) and exc.response is not None:
        return exc.response.status_code in {502, 503, 504}
    if isinstance(exc, requests.ConnectionError):
        return True
    text = str(exc).lower()
    return any(
        marker in text
        for marker in [
            "failed to resolve",
            "name resolution",
            "no address associated with hostname",
            "connection reset by peer",
            "connection aborted",
            "temporary failure in name resolution",
        ]
    )


def chapter_path(root: Path, slug: str, chapter_number: int) -> Path:
    return root / slug / f"chapter{chapter_number:04d}.txt"


def parse_int(value: str) -> int:
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    return int(digits) if digits else 0


def fetch_public_en_html(url: str, timeout: int, retries: int, retry_sleep: float) -> str:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, headers=PUBLIC_EN_HEADERS, timeout=timeout)
            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            last_error = exc
            if attempt < retries:
                print(f"[WARN] retry {attempt}/{retries}: {url} | {exc}")
                time.sleep(retry_sleep * attempt)
    raise RuntimeError(f"Cannot fetch URL after {retries} attempts: {url} | {last_error}") from last_error


def public_source_slug(url: str, fallback: str = "story") -> str:
    parts = [part for part in urlparse(url).path.split("/") if part]
    if "novel" in parts:
        index = parts.index("novel")
        if len(parts) > index + 1:
            return parts[index + 1]
    if "b" in parts:
        index = parts.index("b")
        if len(parts) > index + 1:
            return parts[index + 1]
    return parts[-1] if parts else fallback


def parse_novelbin_catalog(story_url: str, args: argparse.Namespace) -> dict[str, Any]:
    html = fetch_public_en_html(story_url, args.timeout, args.retries, args.retry_sleep)
    soup = BeautifulSoup(html, "html.parser")
    title_node = soup.select_one("h1, h3")
    title = title_node.get_text(" ", strip=True) if title_node else public_source_slug(story_url, "novelbin-story")
    author_node = soup.select_one("a[href*='/a/']")
    author = author_node.get_text(" ", strip=True) if author_node else ""
    page_text = soup.get_text(" ", strip=True)
    latest_match = re.search(r"Latest chapter\s+Chapter\s+(\d+)", page_text, flags=re.IGNORECASE)
    latest_chapter = parse_int(latest_match.group(1)) if latest_match else 0
    if not latest_chapter:
        latest_chapter = max(
            [
                parse_int(match.group(1))
                for match in re.finditer(
                    r"/chapter-(\d+)",
                    " ".join(anchor.get("href") or "" for anchor in soup.select("a[href]")),
                )
            ],
            default=0,
        )
    chapters: list[dict[str, Any]] = []
    if latest_chapter:
        for number in range(1, latest_chapter + 1):
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
            chapter_url = urljoin(story_url, anchor.get("href") or "").split("#", 1)[0]
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
        "slug": public_source_slug(story_url, "novelbin-story"),
        "title": title,
        "author": author,
        "total_chapters": latest_chapter or len(chapters),
        "chapters": chapters,
    }


def parse_freewebnovel_catalog(story_row: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    slug = public_source_slug(story_row["source_url"], "freewebnovel-story")
    base_url = f"https://freewebnovel.com/novel/{slug}"
    html = fetch_public_en_html(base_url, args.timeout, args.retries, args.retry_sleep)
    soup = BeautifulSoup(html, "html.parser")
    title_node = soup.select_one("h1, h2, title")
    title = title_node.get_text(" ", strip=True) if title_node else slug.replace("-", " ").title()
    latest_chapter = int(story_row.get("total_chapters") or 0)
    if not latest_chapter:
        latest_chapter = max(
            [
                parse_int(match.group(1))
                for match in re.finditer(
                    r"/chapter-(\d+)",
                    " ".join(anchor.get("href") or "" for anchor in soup.select("a[href]")),
                )
            ],
            default=0,
        )
    chapters = [
        {
            "number": number,
            "title": f"Chapter {number}",
            "url": f"{base_url}/chapter-{number}",
            "source_chapter_id": str(number),
        }
        for number in range(1, latest_chapter + 1)
    ]
    return {
        "source": "freewebnovel",
        "story_url": base_url,
        "slug": slug,
        "title": title,
        "author": story_row.get("author") or "",
        "total_chapters": latest_chapter,
        "chapters": chapters,
    }


def parse_novelhub_catalog(story_url: str, args: argparse.Namespace) -> dict[str, Any]:
    slug = public_source_slug(story_url, "novelhub-story")
    base_url = f"https://novelhub.net/novel/{slug}"
    html = fetch_public_en_html(base_url, args.timeout, args.retries, args.retry_sleep)
    soup = BeautifulSoup(html, "html.parser")
    title_node = soup.select_one("h1") or soup.select_one("title")
    title = title_node.get_text(" ", strip=True) if title_node else slug.replace("-", " ").title()
    page_text = soup.get_text(" ", strip=True)
    latest_chapter = max(
        [
            parse_int(match.group(1))
            for match in re.finditer(r"\bChapter\s+(\d+)\b", page_text, flags=re.IGNORECASE)
        ]
        + [
            parse_int(match.group(1))
            for match in re.finditer(
                r"/chapter-(\d+)",
                " ".join(anchor.get("href") or "" for anchor in soup.select("a[href]")),
            )
        ],
        default=0,
    )
    chapters = [
        {
            "number": number,
            "title": f"Chapter {number}",
            "url": f"{base_url}/chapter-{number}",
            "source_chapter_id": str(number),
        }
        for number in range(1, latest_chapter + 1)
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
    for selector in ["script", "style", "noscript", "nav", "header", "footer", ".comments", ".chapter-nav", ".chapter-control", ".m-read"]:
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
    for index, line in enumerate(lines):
        if re.fullmatch(r"Chapter\s+\d+", line, flags=re.IGNORECASE):
            return "\n\n".join(lines[index + 1 :]).strip()
    return "\n\n".join(lines).strip()


def extract_novelhub_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one("article#chapter-content") or soup.select_one("#chapter-content") or soup.select_one("article")
    if node is None:
        return ""
    for selector in ["script", "style", "noscript", "iframe", ".ads", ".advertisement", ".chapter-nav", ".nav-row", ".keyboard-hint", ".comments"]:
        for removable in node.select(selector):
            removable.decompose()
    lines = [re.sub(r"\s+", " ", line).strip() for line in node.get_text("\n", strip=True).splitlines()]
    return "\n\n".join(line for line in lines if line).strip()


def enqueue_polish(
    *,
    source_code: str,
    story: dict[str, Any],
    chapter: dict[str, Any],
    slug: str,
    raw_path: Path | None,
    raw_language: str,
    polished_root: Path,
    vi_model: str,
    translate_model: str,
    max_attempts: int,
    post_translate: str = "polish",
) -> dict[str, Any]:
    chapter_stem = raw_path.stem if raw_path else f"chapter{chapter['chapter_number']:04d}"
    polished_path = polished_root / slug / f"{chapter_stem}.txt"
    model = vi_model if raw_language == "vi" else translate_model
    char_map_file = find_char_map_file(story_id=str(story.get("id") or ""), slug=slug)
    job = repo.enqueue_chapter_job(
        "polish_chapter",
        chapter["id"],
        story_id=story["id"],
        source_code=source_code,
        model=model,
        input_path=raw_path.as_posix() if raw_path else "",
        output_path=polished_path.as_posix(),
        payload={
            "raw_language": raw_language,
            "story_slug": slug,
            "chapter_number": chapter["chapter_number"],
            "chapter_title": chapter.get("title") or chapter_stem,
            "source_chapter_title": chapter.get("title") or chapter_stem,
            "translate_story_metadata": raw_language.lower() not in {"vi"},
            "source_story_title": story.get("original_title") or story.get("title") or "",
            "source_story_author": (story.get("metadata") or {}).get("source_author") or story.get("author") or "",
            "source_story_description": (story.get("metadata") or {}).get("source_description")
            or story.get("description")
            or "",
            "post_translate": post_translate,
            "genre": resolve_genre_from_context(
                story.get("category") or "",
                raw_language=raw_language,
                source_code=source_code,
                char_map_file=char_map_file,
            ),
            "char_map_file": char_map_file,
        },
        max_attempts=max_attempts,
    )
    mode = post_translate if raw_language.lower() not in {"vi"} else "polish"
    print(f"[JOB] polish_chapter {job['status']} mode={mode}: {slug}/{chapter_stem}.txt")
    return job


def upsert_downloaded_chapter(
    story: dict[str, Any],
    *,
    source_chapter_id: str,
    chapter_number: int,
    title: str,
    source_url: str,
    raw_language: str,
    raw_path: Path | None,
    raw_text_content: str | None = None,
    volume: str | None = None,
    is_locked: bool = False,
    lock_reason: str | None = None,
) -> dict[str, Any]:
    title_fallback = raw_path.stem if raw_path else f"chapter{chapter_number:04d}"
    return repo.upsert_chapter(
        story["id"],
        {
            "source_chapter_id": source_chapter_id,
            "chapter_number": chapter_number,
            "title": title or title_fallback,
            "source_url": source_url,
            "volume": volume,
            "is_locked": is_locked,
            "lock_reason": lock_reason,
            "raw_language": raw_language,
            "raw_text_path": raw_path.as_posix() if raw_path else None,
            "raw_text_content": raw_text_content,
            "is_downloaded": bool(raw_text_content) or (raw_path.exists() if raw_path else False),
        },
    )


def write_if_needed(path: Path, text: str, overwrite: bool, persist: bool = True) -> bool:
    if not persist:
        return True
    if path.exists() and not overwrite:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return True


def _prefetch_chapters(
    chapters: list[dict],
    raw_path_fn: Callable[[int], Path],
    fetch_fn: Callable[..., str],
    args: argparse.Namespace,
    *,
    is_eligible: Callable[[dict], bool] | None = None,
) -> dict[str, str | Exception]:
    """Parallel-fetch chapter texts for chapters not yet on disk.

    Returns {url: text_or_exception}. Returns {} (no-op) when
    chapter_workers <= 1 or all chapters are already downloaded.

    Args:
        chapters: list of chapter dicts from catalog
        raw_path_fn: takes chapter_number -> Path to raw text file
        fetch_fn: chapter text fetcher, called as fetch_fn(url, timeout, retries, retry_sleep)
        args: namespace with chapter_workers, timeout, retries, retry_sleep, overwrite
        is_eligible: optional extra filter — return False to skip prefetching a chapter
    """
    chapter_workers: int = getattr(args, "chapter_workers", 1)
    if chapter_workers <= 1:
        return {}

    pending = [
        (i, ch) for i, ch in enumerate(chapters, start=1)
        if (is_eligible is None or is_eligible(ch))
        and (args.overwrite or not raw_path_fn(int(ch.get("number") or i)).exists())
    ]
    if not pending:
        return {}

    print(f"[PREFETCH] {len(pending)} chapters, workers={chapter_workers}", flush=True)
    results: dict[str, str | Exception] = {}

    def _fetch(item: tuple[int, dict]) -> tuple[str, str | Exception]:
        _idx, ch = item
        url = ch.get("url") or ""
        if not url:
            return url, ValueError(f"chapter {ch.get('number')} has no URL")
        try:
            return url, fetch_fn(
                url,
                timeout=args.timeout,
                retries=args.retries,
                retry_sleep=args.retry_sleep,
            )
        except Exception as exc:
            return url, exc

    with ThreadPoolExecutor(max_workers=chapter_workers) as _pool:
        _futures = {_pool.submit(_fetch, item): item for item in pending}
        _done = 0
        for _fut in as_completed(_futures):
            _url, _result = _fut.result()
            if _url:
                results[_url] = _result
            _done += 1
            if _done % 20 == 0 or _done == len(pending):
                print(f"[PREFETCH] {_done}/{len(pending)} done", flush=True)

    return results


def crawl_hako_story(story_row: dict[str, Any], args: argparse.Namespace) -> None:
    catalog = crawl_hako_catalog(story_row["source_url"], args.timeout, args.retries, args.retry_sleep)
    slug = catalog.get("slug") or Path(story_row["source_url"]).name
    story = repo.upsert_story(
        "hako",
        {
            "source_story_id": slug,
            "title": catalog.get("title") or story_row["title"],
            "author": catalog.get("author") or story_row.get("author"),
            "language": "vi",
            "source_url": story_row["source_url"],
            "catalog_url": story_row.get("catalog_url") or story_row["source_url"],
            "cover_image_url": catalog.get("cover_image_url") or story_row.get("cover_image_url"),
            "total_chapters": catalog.get("chapter_count") or len(catalog.get("chapters") or []),
            "is_completed": bool(story_row.get("is_completed")),
            "metadata": {**(story_row.get("metadata") or {}), "slug": slug, "source": "hako"},
        },
    )
    manifest_path = Path(args.catalog_output_root) / "hako" / slug / "chapters.json"
    write_if_needed(manifest_path, json.dumps(catalog, ensure_ascii=False, indent=2), args.overwrite_catalog)

    chapters = catalog.get("chapters") or []
    if args.max_chapters:
        chapters = chapters[: args.max_chapters]
    for index, chapter in enumerate(chapters, start=1):
        number = int(chapter.get("number") or index)
        raw_path = chapter_path(Path(args.text_output_root), slug, number)
        if raw_path.exists() and not args.overwrite:
            db_chapter = upsert_downloaded_chapter(
                story,
                source_chapter_id=str(number),
                chapter_number=number,
                title=chapter.get("title") or raw_path.stem,
                source_url=chapter.get("url") or "",
                raw_language="vi",
                raw_path=raw_path,
                raw_text_content=raw_path.read_text(encoding="utf-8"),
                is_locked=bool(chapter.get("is_locked")),
            )
            enqueue_polish_for_args("hako", story, db_chapter, slug, raw_path, "vi", args)
            continue

        try:
            title, content = fetch_hako_chapter(chapter["url"], args.timeout, args.retries, args.retry_sleep)
            if not content or len(content) < args.min_text_chars or hako_looks_locked(content):
                upsert_downloaded_chapter(
                    story,
                    source_chapter_id=str(number),
                    chapter_number=number,
                    title=chapter.get("title") or title or raw_path.stem,
                    source_url=chapter.get("url") or "",
                    raw_language="vi",
                    raw_path=raw_path,
                    is_locked=True,
                    lock_reason="locked_or_empty",
                )
                print(f"[SKIP] locked/empty hako {slug}/chapter{number:04d}")
                continue
            heading = title or chapter.get("title") or f"Chapter {number}"
            write_if_needed(raw_path, f"{heading}\n\n{content}", args.overwrite, persist=not getattr(args, "no_persist_files", False))
            db_chapter = upsert_downloaded_chapter(
                story,
                source_chapter_id=str(number),
                chapter_number=number,
                title=heading,
                source_url=chapter.get("url") or "",
                raw_language="vi",
                raw_path=raw_path,
                raw_text_content=f"{heading}\n\n{content}".strip() + "\n",
            )
            enqueue_polish_for_args("hako", story, db_chapter, slug, raw_path, "vi", args)
        except Exception as exc:
            print(f"[WARN] hako skip {chapter.get('url')}: {exc}")
        time.sleep(args.chapter_delay)


def crawl_wattpad_story(story_row: dict[str, Any], args: argparse.Namespace) -> None:
    chapters = list(
        crawl_wattpad_catalog(
            story_row["source_url"],
            timeout=args.timeout,
            retries=args.retries,
            retry_sleep=args.retry_sleep,
        )
    )
    slug = wattpad_slug(story_row["source_url"])
    story = repo.upsert_story(
        "wattpad_vn",
        {
            "source_story_id": slug,
            "title": story_row["title"] or slug,
            "author": story_row.get("author"),
            "language": "vi",
            "source_url": story_row["source_url"],
            "catalog_url": story_row.get("catalog_url") or story_row["source_url"],
            "total_chapters": len(chapters),
            "is_completed": bool(story_row.get("is_completed")),
            "metadata": {**(story_row.get("metadata") or {}), "slug": slug, "source": "wattpad_vn"},
        },
    )
    manifest = {
        "source": "wattpad_vn",
        "story_url": story_row["source_url"],
        "slug": slug,
        "total_chapters": len(chapters),
        "chapters": [{"title": item.title, "url": item.url} for item in chapters],
    }
    manifest_path = Path(args.catalog_output_root) / "wattpad_vn" / slug / "chapters.json"
    write_if_needed(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2), args.overwrite_catalog)

    if args.max_chapters:
        chapters = chapters[: args.max_chapters]
    for index, chapter in enumerate(chapters, start=1):
        raw_path = chapter_path(Path(args.text_output_root), slug, index)
        if raw_path.exists() and not args.overwrite:
            db_chapter = upsert_downloaded_chapter(
                story,
                source_chapter_id=str(index),
                chapter_number=index,
                title=chapter.title or raw_path.stem,
                source_url=chapter.url,
                raw_language="vi",
                raw_path=raw_path,
                raw_text_content=raw_path.read_text(encoding="utf-8"),
            )
            enqueue_polish_for_args("wattpad_vn", story, db_chapter, slug, raw_path, "vi", args)
            continue
        try:
            content = fetch_wattpad_text(
                chapter.url,
                timeout=args.timeout,
                retries=args.retries,
                retry_sleep=args.retry_sleep,
            )
            if not content or len(content) < args.min_text_chars:
                print(f"[SKIP] empty wattpad {slug}/chapter{index:04d}")
                continue
            write_if_needed(raw_path, content, args.overwrite, persist=not getattr(args, "no_persist_files", False))
            db_chapter = upsert_downloaded_chapter(
                story,
                source_chapter_id=str(index),
                chapter_number=index,
                title=chapter.title or raw_path.stem,
                source_url=chapter.url,
                raw_language="vi",
                raw_path=raw_path,
                raw_text_content=content.strip() + "\n",
            )
            enqueue_polish_for_args("wattpad_vn", story, db_chapter, slug, raw_path, "vi", args)
        except Exception as exc:
            print(f"[WARN] wattpad_vn skip {chapter.url}: {exc}")
        time.sleep(args.chapter_delay)


def crawl_truyenfull_today_story(story_row: dict[str, Any], args: argparse.Namespace) -> None:
    catalog = crawl_truyenfull_today_catalog(
        story_row["source_url"],
        timeout=args.timeout,
        retries=args.retries,
        retry_sleep=args.retry_sleep,
    )
    slug = catalog.get("slug") or Path(story_row["source_url"]).name
    repo.upsert_source("truyenfull_today", "TruyenFull Today", "https://truyenfull.today")
    story = repo.upsert_story(
        "truyenfull_today",
        {
            "source_story_id": catalog.get("source_story_id") or slug,
            "title": catalog.get("title") or story_row["title"] or slug,
            "author": catalog.get("author") or story_row.get("author"),
            "category": ", ".join(catalog.get("tags") or []) or story_row.get("category"),
            "status": catalog.get("status") or story_row.get("status"),
            "language": "vi",
            "source_url": story_row["source_url"],
            "catalog_url": story_row.get("catalog_url") or story_row["source_url"],
            "description": catalog.get("description") or story_row.get("description"),
            "cover_image_url": catalog.get("cover_image_url") or story_row.get("cover_image_url"),
            "total_chapters": catalog.get("total_chapters") or len(catalog.get("chapters") or []),
            "is_completed": (catalog.get("status") or story_row.get("status") or "").lower()
            in {"hoàn thành", "full", "completed", "complete"},
            "metadata": {
                **(story_row.get("metadata") or {}),
                "slug": slug,
                "source": "truyenfull_today",
                "tags": catalog.get("tags") or [],
            },
        },
    )
    manifest_path = Path(args.catalog_output_root) / "truyenfull_today" / slug / "chapters.json"
    write_if_needed(manifest_path, json.dumps(catalog, ensure_ascii=False, indent=2), args.overwrite_catalog)

    chapters = catalog.get("chapters") or []
    if args.max_chapters:
        chapters = chapters[: args.max_chapters]
    _text_root = Path(args.text_output_root)
    prefetched = _prefetch_chapters(
        chapters,
        raw_path_fn=lambda n: chapter_path(_text_root, slug, n),
        fetch_fn=fetch_truyenfull_today_text,
        args=args,
    )
    for index, chapter in enumerate(chapters, start=1):
        number = int(chapter.get("number") or index)
        raw_path = chapter_path(_text_root, slug, number)
        if raw_path.exists() and not args.overwrite:
            db_chapter = upsert_downloaded_chapter(
                story,
                source_chapter_id=str(chapter.get("source_chapter_id") or number),
                chapter_number=number,
                title=chapter.get("title") or raw_path.stem,
                source_url=chapter.get("url") or "",
                raw_language="vi",
                raw_path=raw_path,
                raw_text_content=raw_path.read_text(encoding="utf-8"),
            )
            enqueue_polish_for_args("truyenfull_today", story, db_chapter, slug, raw_path, "vi", args)
            continue

        try:
            chapter_url = chapter.get("url") or ""
            if chapter_url in prefetched:
                _r = prefetched[chapter_url]
                if isinstance(_r, Exception):
                    raise _r
                content = _r
            else:
                content = fetch_truyenfull_today_text(
                    chapter_url,
                    timeout=args.timeout,
                    retries=args.retries,
                    retry_sleep=args.retry_sleep,
                )
            if not content or len(content) < args.min_text_chars:
                print(f"[SKIP] empty truyenfull_today {slug}/chapter{number:04d}")
                continue
            title = chapter.get("title") or f"Chương {number}"
            text = f"{title}\n\n{content}".strip() + "\n"
            write_if_needed(raw_path, text, args.overwrite, persist=not getattr(args, "no_persist_files", False))
            db_chapter = upsert_downloaded_chapter(
                story,
                source_chapter_id=str(chapter.get("source_chapter_id") or number),
                chapter_number=number,
                title=title,
                source_url=chapter_url,
                raw_language="vi",
                raw_path=raw_path,
                raw_text_content=text,
            )
            enqueue_polish_for_args("truyenfull_today", story, db_chapter, slug, raw_path, "vi", args)
        except Exception as exc:
            print(f"[WARN] truyenfull_today skip {chapter.get('url')}: {exc}")
        if not prefetched:
            time.sleep(args.chapter_delay)


def crawl_truyenyy_story(story_row: dict[str, Any], args: argparse.Namespace) -> None:
    catalog = crawl_truyenyy_catalog(
        story_row["source_url"],
        timeout=args.timeout,
        retries=args.retries,
        retry_sleep=args.retry_sleep,
    )
    slug = catalog.get("slug") or Path(urlparse(story_row["source_url"]).path).name
    repo.upsert_source("truyenyy", "TruyenYY", "https://truyenyy.co")
    story = repo.upsert_story(
        "truyenyy",
        {
            "source_story_id": catalog.get("source_story_id") or slug,
            "title": catalog.get("title") or story_row["title"] or slug,
            "author": catalog.get("author") or story_row.get("author"),
            "category": ", ".join(catalog.get("tags") or []) or story_row.get("category"),
            "status": catalog.get("status") or story_row.get("status"),
            "language": "vi",
            "source_url": catalog.get("story_url") or story_row["source_url"],
            "catalog_url": story_row.get("catalog_url") or story_row["source_url"],
            "description": catalog.get("description") or story_row.get("description"),
            "cover_image_url": catalog.get("cover_image_url") or story_row.get("cover_image_url"),
            "total_chapters": catalog.get("total_chapters") or len(catalog.get("chapters") or []),
            "is_completed": (catalog.get("status") or story_row.get("status") or "").lower()
            in {"hoàn thành", "full", "completed", "complete"},
            "metadata": {
                **(story_row.get("metadata") or {}),
                "slug": slug,
                "source": "truyenyy",
                "tags": catalog.get("tags") or [],
            },
        },
    )
    manifest_path = Path(args.catalog_output_root) / "truyenyy" / slug / "chapters.json"
    write_if_needed(manifest_path, json.dumps(catalog, ensure_ascii=False, indent=2), args.overwrite_catalog)

    chapters = catalog.get("chapters") or []
    if not chapters:
        print(
            "[WARN] truyenyy catalog has no concrete chapter URLs. "
            "Story metadata was updated, but chapter download is skipped."
        )
        return
    if args.max_chapters:
        chapters = chapters[: args.max_chapters]
    _text_root = Path(args.text_output_root)
    prefetched = _prefetch_chapters(
        chapters,
        raw_path_fn=lambda n: chapter_path(_text_root, slug, n),
        fetch_fn=fetch_truyenyy_text,
        args=args,
    )
    for index, chapter in enumerate(chapters, start=1):
        number = int(chapter.get("number") or index)
        raw_path = chapter_path(_text_root, slug, number)
        if raw_path.exists() and not args.overwrite:
            db_chapter = upsert_downloaded_chapter(
                story,
                source_chapter_id=str(chapter.get("source_chapter_id") or number),
                chapter_number=number,
                title=chapter.get("title") or raw_path.stem,
                source_url=chapter.get("url") or "",
                raw_language="vi",
                raw_path=raw_path,
                raw_text_content=raw_path.read_text(encoding="utf-8"),
            )
            enqueue_polish_for_args("truyenyy", story, db_chapter, slug, raw_path, "vi", args)
            continue

        try:
            chapter_url = chapter.get("url") or ""
            if chapter_url in prefetched:
                _r = prefetched[chapter_url]
                if isinstance(_r, Exception):
                    raise _r
                content = _r
            else:
                content = fetch_truyenyy_text(chapter_url, timeout=args.timeout, retries=args.retries, retry_sleep=args.retry_sleep)
            if not content or len(content) < args.min_text_chars:
                print(f"[SKIP] empty truyenyy {slug}/chapter{number:04d}")
                continue
            title = chapter.get("title") or f"Chương {number}"
            text = f"{title}\n\n{content}".strip() + "\n"
            write_if_needed(raw_path, text, args.overwrite, persist=not getattr(args, "no_persist_files", False))
            db_chapter = upsert_downloaded_chapter(
                story,
                source_chapter_id=str(chapter.get("source_chapter_id") or number),
                chapter_number=number,
                title=title,
                source_url=chapter_url,
                raw_language="vi",
                raw_path=raw_path,
                raw_text_content=text,
            )
            enqueue_polish_for_args("truyenyy", story, db_chapter, slug, raw_path, "vi", args)
        except Exception as exc:
            print(f"[WARN] truyenyy skip {chapter.get('url')}: {exc}")
        if not prefetched:
            time.sleep(args.chapter_delay)


def crawl_docln_story(story_row: dict[str, Any], args: argparse.Namespace) -> None:
    catalog = crawl_docln_catalog(
        story_row["source_url"],
        timeout=args.timeout,
        retries=args.retries,
        retry_sleep=args.retry_sleep,
    )
    slug = catalog.get("slug") or Path(urlparse(story_row["source_url"]).path).name
    repo.upsert_source("docln", "DocLN", "https://docln.net")
    story = repo.upsert_story(
        "docln",
        {
            "source_story_id": catalog.get("source_story_id") or slug,
            "title": catalog.get("title") or story_row["title"] or slug,
            "author": catalog.get("author") or story_row.get("author"),
            "category": ", ".join(catalog.get("tags") or []) or story_row.get("category"),
            "status": catalog.get("status") or story_row.get("status"),
            "language": "vi",
            "source_url": catalog.get("story_url") or story_row["source_url"],
            "catalog_url": story_row.get("catalog_url") or story_row["source_url"],
            "description": catalog.get("description") or story_row.get("description"),
            "cover_image_url": catalog.get("cover_image_url") or story_row.get("cover_image_url"),
            "total_chapters": catalog.get("total_chapters") or len(catalog.get("chapters") or []),
            "locked_chapters": sum(1 for chapter in catalog.get("chapters") or [] if chapter.get("is_locked")),
            "is_completed": (catalog.get("status") or story_row.get("status") or "").lower()
            in {"hoàn thành", "full", "completed", "complete"},
            "metadata": {
                **(story_row.get("metadata") or {}),
                "slug": slug,
                "source": "docln",
                "tags": catalog.get("tags") or [],
            },
        },
    )
    manifest_path = Path(args.catalog_output_root) / "docln" / slug / "chapters.json"
    write_if_needed(manifest_path, json.dumps(catalog, ensure_ascii=False, indent=2), args.overwrite_catalog)

    chapters = catalog.get("chapters") or []
    if not chapters:
        print(
            "[WARN] docln catalog has no concrete chapter URLs. "
            "Story metadata was updated, but chapter download is skipped."
        )
        return
    if args.max_chapters:
        chapters = chapters[: args.max_chapters]
    _text_root = Path(args.text_output_root)
    prefetched = _prefetch_chapters(
        chapters,
        raw_path_fn=lambda n: chapter_path(_text_root, slug, n),
        fetch_fn=fetch_docln_text,
        args=args,
        is_eligible=lambda ch: not ch.get("is_locked"),
    )
    for index, chapter in enumerate(chapters, start=1):
        number = int(chapter.get("number") or index)
        raw_path = chapter_path(_text_root, slug, number)
        if chapter.get("is_locked"):
            upsert_downloaded_chapter(
                story,
                source_chapter_id=str(chapter.get("source_chapter_id") or number),
                chapter_number=number,
                title=chapter.get("title") or raw_path.stem,
                source_url=chapter.get("url") or "",
                raw_language="vi",
                raw_path=raw_path,
                is_locked=True,
                lock_reason=chapter.get("lock_reason") or "locked_or_premium",
            )
            print(f"[SKIP] locked docln {slug}/chapter{number:04d}")
            continue
        if raw_path.exists() and not args.overwrite:
            db_chapter = upsert_downloaded_chapter(
                story,
                source_chapter_id=str(chapter.get("source_chapter_id") or number),
                chapter_number=number,
                title=chapter.get("title") or raw_path.stem,
                source_url=chapter.get("url") or "",
                raw_language="vi",
                raw_path=raw_path,
                raw_text_content=raw_path.read_text(encoding="utf-8"),
            )
            enqueue_polish_for_args("docln", story, db_chapter, slug, raw_path, "vi", args)
            continue

        try:
            chapter_url = chapter.get("url") or ""
            if chapter_url in prefetched:
                _r = prefetched[chapter_url]
                if isinstance(_r, Exception):
                    raise _r
                content = _r
            else:
                content = fetch_docln_text(chapter_url, timeout=args.timeout, retries=args.retries, retry_sleep=args.retry_sleep)
            if not content or len(content) < args.min_text_chars:
                print(f"[SKIP] empty docln {slug}/chapter{number:04d}")
                continue
            title = chapter.get("title") or f"Chương {number}"
            text = f"{title}\n\n{content}".strip() + "\n"
            write_if_needed(raw_path, text, args.overwrite, persist=not getattr(args, "no_persist_files", False))
            db_chapter = upsert_downloaded_chapter(
                story,
                source_chapter_id=str(chapter.get("source_chapter_id") or number),
                chapter_number=number,
                title=title,
                source_url=chapter_url,
                raw_language="vi",
                raw_path=raw_path,
                raw_text_content=text,
            )
            enqueue_polish_for_args("docln", story, db_chapter, slug, raw_path, "vi", args)
        except PermissionError as exc:
            upsert_downloaded_chapter(
                story,
                source_chapter_id=str(chapter.get("source_chapter_id") or number),
                chapter_number=number,
                title=chapter.get("title") or raw_path.stem,
                source_url=chapter.get("url") or "",
                raw_language="vi",
                raw_path=raw_path,
                is_locked=True,
                lock_reason=str(exc)[:200],
            )
            print(f"[SKIP] locked docln {slug}/chapter{number:04d}: {exc}")
        except Exception as exc:
            print(f"[WARN] docln skip {chapter.get('url')}: {exc}")
        if not prefetched:
            time.sleep(args.chapter_delay)


def crawl_manhwatv_story(story_row: dict[str, Any], args: argparse.Namespace) -> None:
    catalog = crawl_manhwatv_catalog(
        story_row["source_url"],
        timeout=args.timeout,
        retries=args.retries,
        retry_sleep=args.retry_sleep,
    )
    slug = catalog.get("slug") or Path(urlparse(story_row["source_url"]).path).stem
    repo.upsert_source("manhwatv", "ManhwaTV", "https://manhwatv6.com")
    story = repo.upsert_story(
        "manhwatv",
        {
            "source_story_id": catalog.get("source_story_id") or slug,
            "title": catalog.get("title") or story_row["title"] or slug,
            "author": catalog.get("author") or story_row.get("author"),
            "category": ", ".join(catalog.get("tags") or []) or story_row.get("category"),
            "status": catalog.get("status") or story_row.get("status"),
            "language": "vi",
            "source_url": story_row["source_url"].rstrip("/"),
            "catalog_url": story_row.get("catalog_url") or story_row["source_url"],
            "description": catalog.get("description") or story_row.get("description"),
            "cover_image_url": catalog.get("cover_image_url") or story_row.get("cover_image_url"),
            "total_chapters": catalog.get("total_chapters") or len(catalog.get("chapters") or []),
            "locked_chapters": catalog.get("locked_chapters")
            or sum(1 for chapter in catalog.get("chapters") or [] if chapter.get("is_locked")),
            "is_completed": (catalog.get("status") or story_row.get("status") or "").lower()
            in {"hoàn thành", "full", "completed", "complete"},
            "metadata": {
                **(story_row.get("metadata") or {}),
                "slug": slug,
                "source": "manhwatv",
                "tags": catalog.get("tags") or [],
                "chapter_content_note": "Chapters may be locked or image-based; crawler only saves public text.",
            },
        },
    )
    manifest_path = Path(args.catalog_output_root) / "manhwatv" / slug / "chapters.json"
    write_if_needed(manifest_path, json.dumps(catalog, ensure_ascii=False, indent=2), args.overwrite_catalog)

    chapters = catalog.get("chapters") or []
    if not chapters:
        print(
            "[WARN] manhwatv catalog has no concrete chapter URLs. "
            "Story metadata was updated, but chapter download is skipped."
        )
        return
    if args.max_chapters:
        chapters = chapters[: args.max_chapters]
    _text_root = Path(args.text_output_root)
    prefetched = _prefetch_chapters(
        chapters,
        raw_path_fn=lambda n: chapter_path(_text_root, slug, n),
        fetch_fn=fetch_manhwatv_text,
        args=args,
        is_eligible=lambda ch: not ch.get("is_locked"),
    )
    for index, chapter in enumerate(chapters, start=1):
        number = int(chapter.get("number") or index)
        raw_path = chapter_path(_text_root, slug, number)
        if chapter.get("is_locked"):
            upsert_downloaded_chapter(
                story,
                source_chapter_id=str(chapter.get("source_chapter_id") or number),
                chapter_number=number,
                title=chapter.get("title") or raw_path.stem,
                source_url=chapter.get("url") or "",
                raw_language="vi",
                raw_path=raw_path,
                is_locked=True,
                lock_reason=chapter.get("lock_reason") or "locked_or_premium",
            )
            print(f"[SKIP] locked manhwatv {slug}/chapter{number:04d}")
            continue
        if raw_path.exists() and not args.overwrite:
            db_chapter = upsert_downloaded_chapter(
                story,
                source_chapter_id=str(chapter.get("source_chapter_id") or number),
                chapter_number=number,
                title=chapter.get("title") or raw_path.stem,
                source_url=chapter.get("url") or "",
                raw_language="vi",
                raw_path=raw_path,
                raw_text_content=raw_path.read_text(encoding="utf-8"),
            )
            enqueue_polish_for_args("manhwatv", story, db_chapter, slug, raw_path, "vi", args)
            continue

        try:
            chapter_url = chapter.get("url") or ""
            if chapter_url in prefetched:
                _r = prefetched[chapter_url]
                if isinstance(_r, Exception):
                    raise _r
                content = _r
            else:
                content = fetch_manhwatv_text(
                    chapter_url,
                    timeout=args.timeout,
                    retries=args.retries,
                    retry_sleep=args.retry_sleep,
                )
            if not content or len(content) < args.min_text_chars:
                print(f"[SKIP] empty manhwatv {slug}/chapter{number:04d}")
                continue
            title = chapter.get("title") or f"Chương {number}"
            text = f"{title}\n\n{content}".strip() + "\n"
            write_if_needed(raw_path, text, args.overwrite, persist=not getattr(args, "no_persist_files", False))
            db_chapter = upsert_downloaded_chapter(
                story,
                source_chapter_id=str(chapter.get("source_chapter_id") or number),
                chapter_number=number,
                title=title,
                source_url=chapter_url,
                raw_language="vi",
                raw_path=raw_path,
                raw_text_content=text,
            )
            enqueue_polish_for_args("manhwatv", story, db_chapter, slug, raw_path, "vi", args)
        except PermissionError as exc:
            upsert_downloaded_chapter(
                story,
                source_chapter_id=str(chapter.get("source_chapter_id") or number),
                chapter_number=number,
                title=chapter.get("title") or raw_path.stem,
                source_url=chapter.get("url") or "",
                raw_language="vi",
                raw_path=raw_path,
                is_locked=True,
                lock_reason=str(exc)[:200],
            )
            print(f"[SKIP] locked manhwatv {slug}/chapter{number:04d}: {exc}")
        except Exception as exc:
            print(f"[WARN] manhwatv skip {chapter.get('url')}: {exc}")
        if not prefetched:
            time.sleep(args.chapter_delay)


def crawl_generic_vn_story(
    story_row: dict[str, Any],
    args: argparse.Namespace,
    *,
    source_code: str,
    source_name: str,
    base_url: str,
) -> None:
    catalog = crawl_generic_vn_catalog(
        story_row["source_url"],
        timeout=args.timeout,
        retries=args.retries,
        retry_sleep=args.retry_sleep,
    )
    slug = catalog.get("slug") or Path(urlparse(story_row["source_url"]).path).stem
    repo.upsert_source(source_code, source_name, base_url)
    story = repo.upsert_story(
        source_code,
        {
            "source_story_id": catalog.get("source_story_id") or slug,
            "title": catalog.get("title") or story_row["title"] or slug,
            "author": catalog.get("author") or story_row.get("author"),
            "category": ", ".join(catalog.get("tags") or []) or story_row.get("category"),
            "status": catalog.get("status") or story_row.get("status"),
            "language": "vi",
            "source_url": story_row["source_url"].rstrip("/"),
            "catalog_url": story_row.get("catalog_url") or story_row["source_url"],
            "description": catalog.get("description") or story_row.get("description"),
            "cover_image_url": catalog.get("cover_image_url") or story_row.get("cover_image_url"),
            "total_chapters": catalog.get("total_chapters") or len(catalog.get("chapters") or []),
            "is_completed": (catalog.get("status") or story_row.get("status") or "").lower()
            in {"hoàn thành", "full", "completed", "complete"},
            "metadata": {
                **(story_row.get("metadata") or {}),
                "slug": slug,
                "source": source_code,
                "tags": catalog.get("tags") or [],
            },
        },
    )
    manifest_path = Path(args.catalog_output_root) / source_code / slug / "chapters.json"
    write_if_needed(manifest_path, json.dumps(catalog, ensure_ascii=False, indent=2), args.overwrite_catalog)

    chapters = catalog.get("chapters") or []
    if not chapters:
        print(
            f"[WARN] {source_code} catalog has no concrete chapter URLs. "
            "Story metadata was updated, but chapter download is skipped."
        )
        return
    if args.max_chapters:
        chapters = chapters[: args.max_chapters]
    consecutive_content_misses = 0
    max_content_misses = max(1, int(getattr(args, "max_consecutive_content_misses", 1) or 1))
    _text_root = Path(args.text_output_root)
    prefetched = _prefetch_chapters(
        chapters,
        raw_path_fn=lambda n: chapter_path(_text_root, slug, n),
        fetch_fn=fetch_generic_vn_text,
        args=args,
    )
    for index, chapter in enumerate(chapters, start=1):
        number = int(chapter.get("number") or index)
        raw_path = chapter_path(_text_root, slug, number)
        if raw_path.exists() and not args.overwrite:
            consecutive_content_misses = 0
            db_chapter = upsert_downloaded_chapter(
                story,
                source_chapter_id=str(chapter.get("source_chapter_id") or number),
                chapter_number=number,
                title=chapter.get("title") or raw_path.stem,
                source_url=chapter.get("url") or "",
                raw_language="vi",
                raw_path=raw_path,
                raw_text_content=raw_path.read_text(encoding="utf-8"),
            )
            enqueue_polish_for_args(source_code, story, db_chapter, slug, raw_path, "vi", args)
            continue

        try:
            chapter_url = chapter.get("url") or ""
            if chapter_url in prefetched:
                _r = prefetched[chapter_url]
                if isinstance(_r, Exception):
                    raise _r
                content = _r
            else:
                content = fetch_generic_vn_text(
                    chapter_url,
                    timeout=args.timeout,
                    retries=args.retries,
                    retry_sleep=args.retry_sleep,
                )
            if not content or len(content) < args.min_text_chars:
                print(f"[SKIP] empty {source_code} {slug}/chapter{number:04d}")
                consecutive_content_misses += 1
                if consecutive_content_misses >= max_content_misses:
                    print(
                        f"[SKIP] stop {source_code} {slug}: "
                        f"{consecutive_content_misses} consecutive chapter content misses "
                        f"at chapter{number:04d}"
                    )
                    return
                continue
            consecutive_content_misses = 0
            title = chapter.get("title") or f"Chương {number}"
            text = f"{title}\n\n{content}".strip() + "\n"
            write_if_needed(raw_path, text, args.overwrite, persist=not getattr(args, "no_persist_files", False))
            db_chapter = upsert_downloaded_chapter(
                story,
                source_chapter_id=str(chapter.get("source_chapter_id") or number),
                chapter_number=number,
                title=title,
                source_url=chapter_url,
                raw_language="vi",
                raw_path=raw_path,
                raw_text_content=text,
            )
            enqueue_polish_for_args(source_code, story, db_chapter, slug, raw_path, "vi", args)
        except Exception as exc:
            print(f"[WARN] {source_code} skip {chapter.get('url')}: {exc}")
            if "Cannot find VN chapter content" in str(exc):
                consecutive_content_misses += 1
                if consecutive_content_misses >= max_content_misses:
                    print(
                        f"[SKIP] stop {source_code} {slug}: "
                        f"{consecutive_content_misses} consecutive chapter content misses "
                        f"at chapter{number:04d}"
                    )
                    return
        if not prefetched:
            time.sleep(args.chapter_delay)


def crawl_qidian_story(story_row: dict[str, Any], args: argparse.Namespace) -> None:
    catalog = crawl_qidian_catalog(story_row["source_url"], args.timeout, args.retries, args.retry_sleep)
    slug = qidian_slug(catalog.get("title") or catalog.get("book_id") or story_row["title"])
    story = repo.upsert_story(
        "qidian",
        {
            "source_story_id": catalog.get("book_id"),
            "title": catalog.get("title") or story_row["title"],
            "original_title": catalog.get("title") or story_row.get("original_title"),
            "author": catalog.get("author") or story_row.get("author"),
            "language": "zh",
            "source_url": story_row["source_url"],
            "catalog_url": catalog.get("catalog_url") or story_row.get("catalog_url"),
            "total_chapters": catalog.get("total_chapters") or len(catalog.get("chapters") or []),
            "free_chapters": catalog.get("free_chapters") or 0,
            "locked_chapters": catalog.get("vip_chapters") or 0,
            "is_completed": (story_row.get("status") or "").lower() in {"完结", "completed", "full", "hoàn thành"},
            "metadata": {**(story_row.get("metadata") or {}), "book_id": catalog.get("book_id"), "slug": slug},
        },
    )
    manifest_path = Path(args.catalog_output_root) / "qidian" / str(catalog.get("book_id") or slug) / "chapters.json"
    write_if_needed(manifest_path, json.dumps(catalog, ensure_ascii=False, indent=2), args.overwrite_catalog)

    chapters = catalog.get("chapters") or []
    if args.max_chapters:
        chapters = chapters[: args.max_chapters]
    for chapter in chapters:
        number = int(chapter.get("position") or 0)
        if not number:
            continue
        raw_path = chapter_path(Path(args.raw_zh_output_root), slug, number)
        if chapter.get("is_vip") and not args.include_qidian_vip_marked:
            repo.upsert_chapter(
                story["id"],
                {
                    "source_chapter_id": str(number),
                    "chapter_number": number,
                    "title": chapter.get("title") or f"chapter{number}",
                    "source_url": chapter.get("url") or "",
                    "volume": chapter.get("volume"),
                    "is_locked": True,
                    "lock_reason": "vip",
                    "raw_language": "zh",
                },
            )
            continue
        if raw_path.exists() and not args.overwrite:
            db_chapter = upsert_downloaded_chapter(
                story,
                source_chapter_id=str(number),
                chapter_number=number,
                title=chapter.get("title") or raw_path.stem,
                source_url=chapter.get("url") or "",
                raw_language="zh",
                raw_path=raw_path,
                raw_text_content=raw_path.read_text(encoding="utf-8"),
                volume=chapter.get("volume"),
            )
            enqueue_polish_for_args("qidian", story, db_chapter, slug, raw_path, "zh", args)
            continue
        try:
            html = fetch_qidian_html(
                chapter["url"],
                timeout=args.timeout,
                retries=args.retries,
                retry_sleep=args.retry_sleep,
            )
            content = extract_qidian_text(html)
            if not content or len(content) < args.min_text_chars:
                print(f"[SKIP] empty qidian {slug}/chapter{number:04d}")
                continue
            title = chapter.get("title") or f"chapter{number}"
            write_if_needed(raw_path, f"{title}\n\n{content}", args.overwrite, persist=not getattr(args, "no_persist_files", False))
            db_chapter = upsert_downloaded_chapter(
                story,
                source_chapter_id=str(number),
                chapter_number=number,
                title=title,
                source_url=chapter.get("url") or "",
                raw_language="zh",
                raw_path=raw_path,
                raw_text_content=f"{title}\n\n{content}".strip() + "\n",
                volume=chapter.get("volume"),
            )
            enqueue_polish_for_args("qidian", story, db_chapter, slug, raw_path, "zh", args)
        except Exception as exc:
            print(f"[WARN] qidian skip {chapter.get('url')}: {exc}")
        time.sleep(args.chapter_delay)


def crawl_royalroad_story(story_row: dict[str, Any], args: argparse.Namespace) -> None:
    catalog = crawl_royalroad_catalog(
        story_row["source_url"],
        timeout=args.timeout,
        retries=args.retries,
        retry_sleep=args.retry_sleep,
    )
    slug = catalog.get("slug") or royalroad_safe_slug(catalog.get("title") or story_row["title"])
    repo.upsert_source("royalroad", "Royal Road", "https://www.royalroad.com")
    story = repo.upsert_story(
        "royalroad",
        {
            "source_story_id": catalog.get("source_story_id"),
            "title": catalog.get("title") or story_row["title"],
            "original_title": catalog.get("title") or story_row.get("original_title"),
            "author": catalog.get("author") or story_row.get("author"),
            "category": ", ".join(catalog.get("tags") or []) or story_row.get("category"),
            "status": catalog.get("status") or story_row.get("status"),
            "language": "en",
            "source_url": story_row["source_url"],
            "catalog_url": story_row.get("catalog_url") or story_row["source_url"],
            "description": catalog.get("description") or story_row.get("description"),
            "cover_image_url": catalog.get("cover_image_url") or story_row.get("cover_image_url"),
            "total_chapters": catalog.get("total_chapters") or len(catalog.get("chapters") or []),
            "is_completed": (catalog.get("status") or story_row.get("status") or "").lower() in {"completed", "complete"},
            "metadata": {**(story_row.get("metadata") or {}), "slug": slug, "source": "royalroad", "tags": catalog.get("tags") or []},
        },
    )
    manifest_path = Path(args.catalog_output_root) / "royalroad" / slug / "chapters.json"
    write_if_needed(manifest_path, json.dumps(catalog, ensure_ascii=False, indent=2), args.overwrite_catalog)

    chapters = catalog.get("chapters") or []
    if args.max_chapters:
        chapters = chapters[: args.max_chapters]
    _en_root = Path(args.raw_en_output_root)
    prefetched = _prefetch_chapters(
        chapters,
        raw_path_fn=lambda n: chapter_path(_en_root, slug, n),
        fetch_fn=fetch_royalroad_text,
        args=args,
    )
    for index, chapter in enumerate(chapters, start=1):
        number = int(chapter.get("number") or index)
        raw_path = chapter_path(_en_root, slug, number)
        if raw_path.exists() and not args.overwrite:
            db_chapter = upsert_downloaded_chapter(
                story,
                source_chapter_id=str(chapter.get("source_chapter_id") or number),
                chapter_number=number,
                title=chapter.get("title") or raw_path.stem,
                source_url=chapter.get("url") or "",
                raw_language="en",
                raw_path=raw_path,
                raw_text_content=raw_path.read_text(encoding="utf-8"),
            )
            enqueue_polish_for_args("royalroad", story, db_chapter, slug, raw_path, "en", args)
            continue

        try:
            chapter_url = chapter.get("url") or ""
            if chapter_url in prefetched:
                _r = prefetched[chapter_url]
                if isinstance(_r, Exception):
                    raise _r
                content = _r
            else:
                content = fetch_royalroad_text(chapter_url, timeout=args.timeout, retries=args.retries, retry_sleep=args.retry_sleep)
            if not content or len(content) < args.min_text_chars:
                print(f"[SKIP] empty royalroad {slug}/chapter{number:04d}")
                continue
            if looks_blocked(content):
                print(f"[SKIP] locked/paywall royalroad {slug}/chapter{number:04d}")
                upsert_downloaded_chapter(
                    story,
                    source_chapter_id=str(chapter.get("source_chapter_id") or number),
                    chapter_number=number,
                    title=chapter.get("title") or f"Chapter {number}",
                    source_url=chapter_url,
                    raw_language="en",
                    raw_path=raw_path,
                    is_locked=True,
                    lock_reason="locked_or_paywall",
                )
                continue
            title = chapter.get("title") or f"Chapter {number}"
            text = f"{title}\n\n{content}".strip() + "\n"
            write_if_needed(raw_path, text, args.overwrite, persist=not getattr(args, "no_persist_files", False))
            db_chapter = upsert_downloaded_chapter(
                story,
                source_chapter_id=str(chapter.get("source_chapter_id") or number),
                chapter_number=number,
                title=title,
                source_url=chapter_url,
                raw_language="en",
                raw_path=raw_path,
                raw_text_content=text,
            )
            enqueue_polish_for_args("royalroad", story, db_chapter, slug, raw_path, "en", args)
        except Exception as exc:
            print(f"[WARN] royalroad skip {chapter.get('url')}: {exc}")
        if not prefetched:
            time.sleep(args.chapter_delay)


def crawl_lightnovelpub_story(story_row: dict[str, Any], args: argparse.Namespace) -> None:
    catalog = crawl_lightnovelpub_catalog(
        story_row["source_url"],
        timeout=args.timeout,
        retries=args.retries,
        retry_sleep=args.retry_sleep,
        max_catalog_pages=args.max_catalog_pages,
        max_chapters=args.max_chapters,
    )
    slug = catalog.get("slug") or lightnovelpub_safe_slug(catalog.get("title") or story_row["title"])
    existing_metadata = story_row.get("metadata") or {}
    source_author = catalog.get("author") or existing_metadata.get("source_author") or story_row.get("author")
    story_author = (
        story_row.get("author")
        if existing_metadata.get("story_author_translated_to") == "vi" and story_row.get("author")
        else source_author
    )
    source_description = catalog.get("description") or existing_metadata.get("source_description") or story_row.get("description")
    repo.upsert_source("lightnovelpub", "LightNovelPub", "https://lightnovelpub.org")
    story = repo.upsert_story(
        "lightnovelpub",
        {
            "source_story_id": catalog.get("source_story_id") or slug,
            "title": catalog.get("title") or story_row["title"] or slug,
            "original_title": catalog.get("title") or story_row.get("original_title"),
            "author": story_author,
            "category": ", ".join(catalog.get("tags") or []) or story_row.get("category"),
            "status": catalog.get("status") or story_row.get("status"),
            "language": "en",
            "source_url": catalog.get("story_url") or story_row["source_url"],
            "catalog_url": catalog.get("catalog_url") or story_row.get("catalog_url") or story_row["source_url"],
            "description": source_description,
            "cover_image_url": catalog.get("cover_image_url") or story_row.get("cover_image_url"),
            "total_chapters": catalog.get("total_chapters") or len(catalog.get("chapters") or []),
            "is_completed": (catalog.get("status") or story_row.get("status") or "").lower() in {"completed", "complete"},
            "metadata": {
                **existing_metadata,
                "slug": slug,
                "source": "lightnovelpub",
                "tags": catalog.get("tags") or [],
                "catalog_pages": catalog.get("catalog_pages"),
                "source_author": source_author or "",
                "source_description": source_description or "",
            },
        },
    )
    manifest_path = Path(args.catalog_output_root) / "lightnovelpub" / slug / "chapters.json"
    write_if_needed(manifest_path, json.dumps(catalog, ensure_ascii=False, indent=2), args.overwrite_catalog)

    chapters = catalog.get("chapters") or []
    if not chapters:
        print(
            "[WARN] lightnovelpub catalog has no concrete chapter URLs. "
            "Story metadata was updated, but chapter download is skipped."
        )
        return
    if args.max_chapters:
        chapters = chapters[: args.max_chapters]
    _en_root = Path(args.raw_en_output_root)
    prefetched = _prefetch_chapters(
        chapters,
        raw_path_fn=lambda n: chapter_path(_en_root, slug, n),
        fetch_fn=fetch_lightnovelpub_text,
        args=args,
    )
    for index, chapter in enumerate(chapters, start=1):
        number = int(chapter.get("number") or index)
        raw_path = chapter_path(_en_root, slug, number)
        if raw_path.exists() and not args.overwrite:
            db_chapter = upsert_downloaded_chapter(
                story,
                source_chapter_id=str(chapter.get("source_chapter_id") or number),
                chapter_number=number,
                title=chapter.get("title") or raw_path.stem,
                source_url=chapter.get("url") or "",
                raw_language="en",
                raw_path=raw_path,
                raw_text_content=raw_path.read_text(encoding="utf-8"),
            )
            enqueue_polish_for_args("lightnovelpub", story, db_chapter, slug, raw_path, "en", args)
            continue

        try:
            chapter_url = chapter.get("url") or ""
            catalog_title = chapter.get("title") or f"Chapter {number}"
            if chapter_url in prefetched:
                _r = prefetched[chapter_url]
                if isinstance(_r, Exception):
                    raise _r
                content = _r
                raw_html = None
            else:
                raw_html = fetch_lightnovelpub_html(chapter_url, timeout=args.timeout, retries=args.retries, retry_sleep=args.retry_sleep)
                content = extract_lightnovelpub_text(raw_html)
            if not content or len(content) < args.min_text_chars:
                print(f"[SKIP] empty lightnovelpub {slug}/chapter{number:04d}")
                continue
            if looks_blocked(content):
                print(f"[SKIP] locked/paywall lightnovelpub {slug}/chapter{number:04d}")
                upsert_downloaded_chapter(
                    story,
                    source_chapter_id=str(chapter.get("source_chapter_id") or number),
                    chapter_number=number,
                    title=catalog_title,
                    source_url=chapter_url,
                    raw_language="en",
                    raw_path=raw_path,
                    is_locked=True,
                    lock_reason="locked_or_paywall",
                )
                continue
            # Supplement bare "Chapter N" title with the richer title from the chapter page
            title = catalog_title
            if re.match(r"^Chapter\s+\d+$", title, re.IGNORECASE):
                page_title = extract_lightnovelpub_title(raw_html) if raw_html else ""
                if page_title:
                    title = page_title
            text = f"{title}\n\n{content}".strip() + "\n"
            write_if_needed(raw_path, text, args.overwrite, persist=not getattr(args, "no_persist_files", False))
            db_chapter = upsert_downloaded_chapter(
                story,
                source_chapter_id=str(chapter.get("source_chapter_id") or number),
                chapter_number=number,
                title=title,
                source_url=chapter_url,
                raw_language="en",
                raw_path=raw_path,
                raw_text_content=text,
            )
            enqueue_polish_for_args("lightnovelpub", story, db_chapter, slug, raw_path, "en", args)
        except Exception as exc:
            print(f"[WARN] lightnovelpub skip {chapter.get('url')}: {exc}")
        if not prefetched:
            time.sleep(args.chapter_delay)


def fetch_novelbin_text(chapter_url: str, timeout: int, retries: int, retry_sleep: float) -> str:
    return extract_novelbin_text(fetch_public_en_html(chapter_url, timeout, retries, retry_sleep))


def fetch_freewebnovel_text(chapter_url: str, timeout: int, retries: int, retry_sleep: float) -> str:
    return extract_freewebnovel_text(fetch_public_en_html(chapter_url, timeout, retries, retry_sleep))


def fetch_novelhub_text(chapter_url: str, timeout: int, retries: int, retry_sleep: float) -> str:
    return extract_novelhub_text(fetch_public_en_html(chapter_url, timeout, retries, retry_sleep))


def crawl_english_public_story(
    story_row: dict[str, Any],
    args: argparse.Namespace,
    *,
    source_code: str,
    source_name: str,
    base_url: str,
    catalog_fn: Callable[[], dict[str, Any]],
    fetch_fn: Callable[..., str],
) -> None:
    catalog = catalog_fn()
    slug = catalog.get("slug") or public_source_slug(story_row["source_url"], f"{source_code}-story")
    repo.upsert_source(source_code, source_name, base_url)
    story = repo.upsert_story(
        source_code,
        {
            "source_story_id": catalog.get("source_story_id") or slug,
            "title": catalog.get("title") or story_row["title"] or slug,
            "original_title": catalog.get("title") or story_row.get("original_title"),
            "author": catalog.get("author") or story_row.get("author"),
            "category": ", ".join(catalog.get("tags") or []) or story_row.get("category"),
            "status": catalog.get("status") or story_row.get("status"),
            "language": "en",
            "source_url": catalog.get("story_url") or story_row["source_url"],
            "catalog_url": catalog.get("catalog_url") or story_row.get("catalog_url") or story_row["source_url"],
            "description": catalog.get("description") or story_row.get("description"),
            "cover_image_url": catalog.get("cover_image_url") or story_row.get("cover_image_url"),
            "total_chapters": catalog.get("total_chapters") or len(catalog.get("chapters") or []),
            "is_completed": (catalog.get("status") or story_row.get("status") or "").lower() in {"completed", "complete"},
            "metadata": {
                **(story_row.get("metadata") or {}),
                "slug": slug,
                "source": source_code,
                "tags": catalog.get("tags") or [],
                "source_author": catalog.get("author") or story_row.get("author") or "",
                "source_description": catalog.get("description") or story_row.get("description") or "",
            },
        },
    )
    manifest_path = Path(args.catalog_output_root) / source_code / slug / "chapters.json"
    write_if_needed(manifest_path, json.dumps(catalog, ensure_ascii=False, indent=2), args.overwrite_catalog)

    chapters = catalog.get("chapters") or []
    if not chapters:
        print(
            f"[WARN] {source_code} catalog has no concrete chapter URLs. "
            "Story metadata was updated, but chapter download is skipped."
        )
        return
    if args.max_chapters:
        chapters = chapters[: args.max_chapters]
    _en_root = Path(args.raw_en_output_root)
    prefetched = _prefetch_chapters(
        chapters,
        raw_path_fn=lambda n: chapter_path(_en_root, slug, n),
        fetch_fn=fetch_fn,
        args=args,
    )
    for index, chapter in enumerate(chapters, start=1):
        number = int(chapter.get("number") or index)
        raw_path = chapter_path(_en_root, slug, number)
        if raw_path.exists() and not args.overwrite:
            db_chapter = upsert_downloaded_chapter(
                story,
                source_chapter_id=str(chapter.get("source_chapter_id") or number),
                chapter_number=number,
                title=chapter.get("title") or raw_path.stem,
                source_url=chapter.get("url") or "",
                raw_language="en",
                raw_path=raw_path,
                raw_text_content=raw_path.read_text(encoding="utf-8"),
            )
            enqueue_polish_for_args(source_code, story, db_chapter, slug, raw_path, "en", args)
            continue

        try:
            chapter_url = chapter.get("url") or ""
            if chapter_url in prefetched:
                _r = prefetched[chapter_url]
                if isinstance(_r, Exception):
                    raise _r
                content = _r
            else:
                content = fetch_fn(chapter_url, timeout=args.timeout, retries=args.retries, retry_sleep=args.retry_sleep)
            if not content or len(content) < args.min_text_chars:
                print(f"[SKIP] empty {source_code} {slug}/chapter{number:04d}")
                continue
            title = chapter.get("title") or f"Chapter {number}"
            text = f"{title}\n\n{content}".strip() + "\n"
            write_if_needed(raw_path, text, args.overwrite, persist=not getattr(args, "no_persist_files", False))
            db_chapter = upsert_downloaded_chapter(
                story,
                source_chapter_id=str(chapter.get("source_chapter_id") or number),
                chapter_number=number,
                title=title,
                source_url=chapter_url,
                raw_language="en",
                raw_path=raw_path,
                raw_text_content=text,
            )
            enqueue_polish_for_args(source_code, story, db_chapter, slug, raw_path, "en", args)
        except Exception as exc:
            print(f"[WARN] {source_code} skip {chapter.get('url')}: {exc}")
        if not prefetched:
            time.sleep(args.chapter_delay)


def crawl_novelbin_story(story_row: dict[str, Any], args: argparse.Namespace) -> None:
    crawl_english_public_story(
        story_row,
        args,
        source_code="novelbin",
        source_name="NovelBin",
        base_url="https://novelbin.com",
        catalog_fn=lambda: parse_novelbin_catalog(story_row["source_url"], args),
        fetch_fn=fetch_novelbin_text,
    )


def crawl_freewebnovel_story(story_row: dict[str, Any], args: argparse.Namespace) -> None:
    crawl_english_public_story(
        story_row,
        args,
        source_code="freewebnovel",
        source_name="FreeWebNovel",
        base_url="https://freewebnovel.com",
        catalog_fn=lambda: parse_freewebnovel_catalog(story_row, args),
        fetch_fn=fetch_freewebnovel_text,
    )


def crawl_novelhub_story(story_row: dict[str, Any], args: argparse.Namespace) -> None:
    crawl_english_public_story(
        story_row,
        args,
        source_code="novelhub",
        source_name="NovelHub",
        base_url="https://novelhub.net",
        catalog_fn=lambda: parse_novelhub_catalog(story_row["source_url"], args),
        fetch_fn=fetch_novelhub_text,
    )


def crawl_skydemonorder_story(story_row: dict[str, Any], args: argparse.Namespace) -> None:
    metadata = story_row.get("metadata") or {}
    slug = metadata.get("slug") or Path(urlparse(story_row["source_url"]).path.rstrip("/")).name
    command = [
        sys.executable,
        str(ROOT / "scripts/story_pipeline/crawl_skydemonorder_chapters.py"),
        "--project-url",
        story_row["source_url"],
        "--target-slug",
        slug,
        "--story-id",
        str(story_row["id"]),
        "--from-chapter",
        "1",
        "--profile-dir",
        args.skydemonorder_profile_dir,
        "--timeout",
        str(args.timeout),
        "--wait-ms",
        str(args.skydemonorder_wait_ms),
        "--chapter-delay",
        str(args.chapter_delay),
        "--min-text-chars",
        str(args.min_text_chars),
        "--raw-en-output-root",
        args.raw_en_output_root,
        "--polished-output-root",
        args.polished_output_root,
        "--enqueue-polish",
        "--vi-model",
        args.vi_model,
        "--translate-model",
        args.translate_model,
        "--catalog-output-root",
        args.catalog_output_root,
        "--post-translate",
        args.post_translate,
        "--polish-max-attempts",
        str(args.polish_max_attempts),
    ]
    if args.max_chapters:
        command.extend(["--max-chapters", str(args.max_chapters)])
    if args.overwrite:
        command.append("--overwrite")
    if args.skydemonorder_headful:
        command.append("--headful")
    if args.skydemonorder_manual_wait:
        command.extend(["--manual-wait", str(args.skydemonorder_manual_wait)])
    subprocess.run(command, check=True)


def crawl_fanmtl_story(story_row: dict[str, Any], args: argparse.Namespace) -> None:
    catalog = crawl_fanmtl_catalog(
        story_row["source_url"],
        timeout=args.timeout,
        retries=args.retries,
        retry_sleep=args.retry_sleep,
        max_catalog_pages=args.max_catalog_pages,
        max_chapters=args.max_chapters,
    )
    slug = catalog.get("slug") or fanmtl_safe_slug(catalog.get("title") or story_row["title"])
    existing_metadata = story_row.get("metadata") or {}
    story = fanmtl_upsert_story(catalog)
    fanmtl_upsert_chapters(story, catalog)

    manifest_path = Path(args.catalog_output_root) / "fanmtl" / slug / "chapters.json"
    fanmtl_write_if_needed(
        manifest_path,
        __import__("json").dumps(catalog, ensure_ascii=False, indent=2),
        args.overwrite_catalog,
    )

    chapters = catalog.get("chapters") or []
    if not chapters:
        print("[WARN] fanmtl catalog has no chapters.")
        return
    _en_root = Path(args.raw_en_output_root)
    prefetched = _prefetch_chapters(
        chapters,
        raw_path_fn=lambda n: fanmtl_chapter_path(_en_root, slug, n),
        fetch_fn=fetch_fanmtl_text,
        args=args,
    )
    for index, chapter in enumerate(chapters, start=1):
        number = int(chapter.get("number") or index)
        raw_path = fanmtl_chapter_path(_en_root, slug, number)
        if raw_path.exists() and not args.overwrite:
            db_chapter = upsert_downloaded_chapter(
                story,
                source_chapter_id=str(chapter.get("source_chapter_id") or number),
                chapter_number=number,
                title=chapter.get("title") or f"Chapter {number}",
                source_url=chapter.get("url") or "",
                raw_language="en",
                raw_path=raw_path,
                raw_text_content=raw_path.read_text(encoding="utf-8"),
            )
            enqueue_polish_for_args("fanmtl", story, db_chapter, slug, raw_path, "en", args)
            continue
        try:
            chapter_url = chapter.get("url") or ""
            if chapter_url in prefetched:
                _r = prefetched[chapter_url]
                if isinstance(_r, Exception):
                    raise _r
                content = _r
            else:
                content = fetch_fanmtl_text(chapter_url, timeout=args.timeout, retries=args.retries, retry_sleep=args.retry_sleep)
            if not content or len(content) < args.min_text_chars:
                print(f"[SKIP] empty fanmtl {slug}/chapter{number:04d}")
                continue
            if looks_blocked(content):
                print(f"[SKIP] locked/paywall fanmtl {slug}/chapter{number:04d}")
                continue
            title = chapter.get("title") or f"Chapter {number}"
            text = f"{title}\n\n{content}".strip() + "\n"
            fanmtl_write_if_needed(raw_path, text, args.overwrite, persist=not getattr(args, "no_persist_files", False))
            db_chapter = upsert_downloaded_chapter(
                story,
                source_chapter_id=str(chapter.get("source_chapter_id") or number),
                chapter_number=number,
                title=title,
                source_url=chapter_url,
                raw_language="en",
                raw_path=raw_path,
                raw_text_content=text,
            )
            enqueue_polish_for_args("fanmtl", story, db_chapter, slug, raw_path, "en", args)
        except Exception as exc:
            print(f"[WARN] fanmtl skip {chapter.get('url')}: {exc}")
        if not prefetched:
            time.sleep(args.chapter_delay)


def crawl_novelfire_story(story_row: dict[str, Any], args: argparse.Namespace) -> None:
    metadata = story_row.get("metadata") or {}
    command = [
        sys.executable,
        str(ROOT / "scripts/story_pipeline/crawl_novelfire_chapters.py"),
        "--story-url", story_row["source_url"],
        "--story-id", str(story_row["id"]),
        "--profile-dir", args.novelfire_profile_dir,
        "--timeout", str(args.timeout),
        "--wait-ms", str(args.novelfire_wait_ms),
        "--chapter-delay", str(args.chapter_delay),
        "--min-text-chars", str(args.min_text_chars),
        "--raw-en-output-root", args.raw_en_output_root,
        "--polished-output-root", args.polished_output_root,
        "--catalog-output-root", args.catalog_output_root,
        "--translate-model", args.translate_model,
        "--polish-max-attempts", str(args.polish_max_attempts),
        "--post-translate", args.post_translate,
        "--download-text",
        "--enqueue-polish",
    ]
    if args.max_chapters:
        command.extend(["--max-chapters", str(args.max_chapters)])
    if args.overwrite:
        command.append("--overwrite")
    if getattr(args, "novelfire_headful", False):
        command.append("--headful")
    if getattr(args, "novelfire_manual_wait", 0):
        command.extend(["--manual-wait", str(args.novelfire_manual_wait)])
    subprocess.run(command, check=True)


def enqueue_polish_for_args(
    source_code: str,
    story: dict[str, Any],
    db_chapter: dict[str, Any],
    slug: str,
    raw_path: Path,
    raw_language: str,
    args: argparse.Namespace,
) -> dict[str, Any] | None:
    if db_chapter.get("is_polished") and not args.requeue_done:
        return None
    return enqueue_polish(
        source_code=source_code,
        story=story,
        chapter=db_chapter,
        slug=slug,
        raw_path=raw_path,
        raw_language=raw_language,
        polished_root=Path(args.polished_output_root),
        vi_model=args.vi_model,
        translate_model=args.translate_model,
        max_attempts=args.polish_max_attempts,
        post_translate=getattr(args, "post_translate", "polish"),
    )


def process_story(
    story: dict[str, Any],
    args: argparse.Namespace,
    unavailable_hosts: set[str],
    host_lock: Lock,
) -> tuple[str, str]:
    source_code = story["source_code"]
    host = host_from_url(story.get("source_url"))
    if host:
        with host_lock:
            if host in unavailable_hosts:
                print(
                    f"\n[SKIP] host unavailable in this run: {source_code}: "
                    f"{story.get('title') or '<untitled>'} | {story.get('source_url') or ''}"
                )
                mark_story_needs_alternate_source(story)
                return source_code, "skipped"

    print(f"\n[STORY] {source_code}: {story['title']} | {story['source_url']}")
    try:
        if source_code == "hako":
            crawl_hako_story(story, args)
        elif source_code == "wattpad_vn":
            crawl_wattpad_story(story, args)
        elif source_code == "truyenfull_today":
            crawl_truyenfull_today_story(story, args)
        elif source_code == "truyenyy":
            crawl_truyenyy_story(story, args)
        elif source_code == "docln":
            crawl_docln_story(story, args)
        elif source_code == "manhwatv":
            crawl_manhwatv_story(story, args)
        elif source_code == "sttruyen":
            crawl_generic_vn_story(
                story,
                args,
                source_code="sttruyen",
                source_name="STTruyen",
                base_url="https://sttruyen.com",
            )
        elif source_code == "truyenchuhay":
            crawl_generic_vn_story(
                story,
                args,
                source_code="truyenchuhay",
                source_name="TruyenChuHay",
                base_url="https://truyenchuhay.vn",
            )
        elif source_code == "truyenhoangdung":
            crawl_generic_vn_story(
                story,
                args,
                source_code="truyenhoangdung",
                source_name="TruyenHoangDung",
                base_url="https://www.truyenhoangdung.xyz",
            )
        elif source_code == "qidian":
            crawl_qidian_story(story, args)
        elif source_code == "royalroad":
            crawl_royalroad_story(story, args)
        elif source_code == "lightnovelpub":
            crawl_lightnovelpub_story(story, args)
        elif source_code == "novelbin":
            crawl_novelbin_story(story, args)
        elif source_code == "freewebnovel":
            crawl_freewebnovel_story(story, args)
        elif source_code == "novelhub":
            crawl_novelhub_story(story, args)
        elif source_code == "skydemonorder":
            crawl_skydemonorder_story(story, args)
        elif source_code == "fanmtl":
            crawl_fanmtl_story(story, args)
        elif source_code == "novelfire":
            crawl_novelfire_story(story, args)
        else:
            print(f"[SKIP] unsupported source: {source_code}")
            return source_code, "unsupported"
        return source_code, "ok"
    except Exception as exc:
        print(
            "[ERROR] story failed "
            f"{source_code}: {story.get('title') or '<untitled>'} | "
            f"{story.get('source_url') or ''} | {type(exc).__name__}: {exc}"
        )
        if host and is_source_unavailable_error(exc):
            with host_lock:
                unavailable_hosts.add(host)
            print(f"[WARN] mark host unavailable for this run: {host}")
            mark_story_needs_alternate_source(story)
        if args.stop_on_error:
            raise
        return source_code, "failed"


def mark_story_needs_alternate_source(story: dict[str, Any]) -> None:
    try:
        repo.update_story_metadata(
            story["id"],
            {
                "source_host_unavailable": True,
                "source_host_unavailable_at": datetime.now(timezone.utc).isoformat(),
                "needs_alternate_source": True,
            },
        )
        print(f"[DB] marked source_host_unavailable + needs_alternate_source: id={story['id']}")
    except Exception as db_exc:
        print(f"[WARN] failed to update story metadata: {db_exc}")


def story_matches_runtime_filters(story: dict[str, Any], args: argparse.Namespace) -> bool:
    if args.story_url:
        wanted_urls = {url.rstrip("/") for url in args.story_url}
        if (story.get("source_url") or "").rstrip("/") not in wanted_urls:
            return False
    if args.title_contains:
        needle = args.title_contains.casefold()
        if needle not in (story.get("title") or "").casefold():
            return False
    return True


def process_claimed_story(
    story: dict[str, Any],
    args: argparse.Namespace,
    unavailable_hosts: set[str],
    host_lock: Lock,
) -> tuple[str, str]:
    try:
        source_code, status = process_story(story, args, unavailable_hosts, host_lock)
        repo.release_story_claim(story["id"], worker_id=args.worker_id, status=status)
        return source_code, status
    except Exception:
        repo.release_story_claim(story["id"], worker_id=args.worker_id, status="failed")
        raise


def count_status(
    source_counts: dict[str, dict[str, int]],
    source_code: str,
    status: str,
) -> None:
    source_counts.setdefault(source_code, {"ok": 0, "failed": 0, "skipped": 0, "unsupported": 0})
    source_counts[source_code][status] = source_counts[source_code].get(status, 0) + 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Crawl tất cả stories trong DB, ghi chapter text, rồi enqueue polish/audio pipeline."
    )
    parser.add_argument(
        "--sources",
        nargs="*",
        default=[],
        help=(
            "Ví dụ: hako wattpad_vn truyenfull_today truyenyy docln manhwatv "
            "sttruyen truyenchuhay truyenhoangdung qidian royalroad lightnovelpub "
            "novelbin freewebnovel novelhub skydemonorder. "
            "Bỏ trống = tất cả."
        ),
    )
    parser.add_argument("--story-url", nargs="*", default=[], help="Chỉ crawl các story có source_url đúng các URL này.")
    parser.add_argument("--title-contains", default="", help="Chỉ crawl story có title chứa chuỗi này, không phân biệt hoa thường.")
    parser.add_argument("--limit-stories", type=int, default=0)
    parser.add_argument("--max-chapters", type=int, default=0, help="0 = toàn bộ chapter của mỗi story.")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument(
        "--claim-batch-size",
        type=int,
        default=0,
        help="Số story mỗi lần claim từ DB. 0 = bằng --workers.",
    )
    parser.add_argument(
        "--claim-ttl-minutes",
        type=int,
        default=240,
        help="Thời gian giữ claim story. Nếu process crash, story được terminal khác claim lại sau TTL này.",
    )
    parser.add_argument(
        "--claim-finished-cooldown-minutes",
        type=int,
        default=240,
        help="Không claim lại story đã crawl xong trong N phút gần đây. 0 = tắt cooldown.",
    )
    parser.add_argument(
        "--no-story-claims",
        action="store_true",
        help="Tắt cơ chế claim story, quay về cách lấy list một lần như cũ.",
    )
    parser.add_argument(
        "--worker-id",
        default="",
        help="ID ghi vào DB claim. Mặc định tự sinh theo host/process/time.",
    )
    parser.add_argument(
        "--only-incomplete",
        action="store_true",
        help="Chỉ check lại story chưa được đánh dấu full/completed trong DB.",
    )
    parser.add_argument(
        "--min-catalog-check-hours",
        type=int,
        default=0,
        help="Chỉ check story chưa check trong N giờ gần đây. 0 = không lọc theo thời gian.",
    )
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--retry-sleep", type=float, default=2.0)
    parser.add_argument("--max-catalog-pages", type=int, default=0, help="Giới hạn số trang catalog mỗi story nếu source có phân trang.")
    parser.add_argument("--chapter-delay", type=float, default=1.5)
    parser.add_argument(
        "--chapter-workers",
        type=int,
        default=1,
        help="Parallel workers để prefetch chapter text trước khi ghi DB (default 1 = tuần tự). "
             "Dùng 2–4 để tăng tốc; quá cao có thể bị rate-limit.",
    )
    parser.add_argument("--min-text-chars", type=int, default=80)
    parser.add_argument(
        "--max-consecutive-content-misses",
        type=int,
        default=1,
        help=(
            "Dừng crawl phần chapter còn lại của một story sau N lần liên tiếp không extract được content. "
            "Mặc định 1 để tránh spam request với source có layout không hỗ trợ."
        ),
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--overwrite-catalog", action="store_true")
    parser.add_argument("--requeue-done", action="store_true")
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Dừng toàn bộ script khi một story lỗi. Mặc định: log lỗi và crawl tiếp story khác.",
    )
    parser.add_argument("--include-qidian-vip-marked", action="store_true")
    parser.add_argument("--catalog-output-root", default="story_data/catalogs")
    parser.add_argument("--text-output-root", default="story_data/text")
    parser.add_argument("--raw-zh-output-root", default="story_data/raw_zh")
    parser.add_argument("--raw-en-output-root", default="story_data/raw_en")
    parser.add_argument("--polished-output-root", default="story_data/polished")
    parser.add_argument(
        "--no-persist-files",
        action="store_true",
        help="Không ghi file txt ra disk; lưu nội dung trực tiếp vào DB. Job polish sẽ dùng DB content.",
    )
    parser.add_argument("--skydemonorder-profile-dir", default=".browser/skydemonorder")
    parser.add_argument("--skydemonorder-headful", action="store_true", help="Mở browser thật để xử lý Cloudflare/login.")
    parser.add_argument("--skydemonorder-manual-wait", type=int, default=0)
    parser.add_argument("--skydemonorder-wait-ms", type=int, default=1500)
    parser.add_argument("--novelfire-profile-dir", default=".browser/novelfire")
    parser.add_argument("--novelfire-headful", action="store_true", help="Mở browser thật để xử lý Cloudflare (NovelFire).")
    parser.add_argument("--novelfire-manual-wait", type=int, default=0)
    parser.add_argument("--novelfire-wait-ms", type=int, default=2000)
    parser.add_argument("--vi-model", default="qwen3:14b")
    parser.add_argument("--translate-model", default="qwen3:14b")
    parser.add_argument(
        "--post-translate",
        choices=("polish", "copy"),
        default="polish",
        help="Sau khi dịch raw khác tiếng Việt: enqueue polish bằng LLM, hoặc copy bản dịch sang polished output.",
    )
    parser.add_argument("--polish-max-attempts", type=int, default=3)
    args = parser.parse_args()
    if "skydemonorder" in args.sources and args.workers > 1:
        print("[INFO] force --workers 1 for skydemonorder because the browser profile is shared.")
        args.workers = 1
    args.worker_id = args.worker_id or f"{socket.gethostname()}:{Path(sys.argv[0]).name}:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"

    ok_count = 0
    failed_count = 0
    skipped_count = 0
    unsupported_count = 0
    source_counts: dict[str, dict[str, int]] = {}
    unavailable_hosts: set[str] = set()
    host_lock = Lock()

    if not args.no_story_claims and not args.story_url and not args.title_contains:
        batch_size = args.claim_batch_size or max(1, args.workers)
        print(
            f"worker_id={args.worker_id}, workers={args.workers}, claim_batch_size={batch_size}, "
            f"claim_ttl_minutes={args.claim_ttl_minutes}, worker_host={socket.gethostname()}"
        )
        processed = 0
        while True:
            current_batch_size = batch_size
            if args.limit_stories:
                remaining = args.limit_stories - processed
                if remaining <= 0:
                    break
                current_batch_size = min(current_batch_size, remaining)
            stories = repo.claim_active_stories(
                worker_id=args.worker_id,
                source_codes=args.sources or None,
                only_incomplete=args.only_incomplete,
                min_catalog_check_hours=args.min_catalog_check_hours,
                limit=current_batch_size,
                claim_ttl_minutes=args.claim_ttl_minutes,
                finished_cooldown_minutes=args.claim_finished_cooldown_minutes,
            )
            stories = [story for story in stories if story_matches_runtime_filters(story, args)]
            if not stories:
                break
            print(f"[CLAIM] claimed {len(stories)} stories for worker_id={args.worker_id}")
            with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
                futures = [
                    executor.submit(process_claimed_story, story, args, unavailable_hosts, host_lock)
                    for story in stories
                ]
                for future in as_completed(futures):
                    try:
                        source_code, status = future.result()
                        count_status(source_counts, source_code, status)
                        if status == "ok":
                            ok_count += 1
                        elif status == "skipped":
                            skipped_count += 1
                        elif status == "unsupported":
                            unsupported_count += 1
                        else:
                            failed_count += 1
                    except Exception as exc:
                        failed_count += 1
                        print(f"[ERROR] worker crashed: {type(exc).__name__}: {exc}")
                        if args.stop_on_error:
                            raise
            processed += len(stories)
            if args.limit_stories and processed >= args.limit_stories:
                break

        print(
            "\nDone. Chapter text đã được lưu và polish_chapter jobs đã được enqueue. "
            f"ok={ok_count}, failed={failed_count}, skipped={skipped_count}, unsupported={unsupported_count}"
        )
        for source_code, counts in sorted(source_counts.items()):
            print(f"[SUMMARY] {source_code}: {counts}")
        return

    stories = repo.list_active_stories(
        source_codes=args.sources or None,
        only_incomplete=args.only_incomplete,
        min_catalog_check_hours=args.min_catalog_check_hours,
        limit=args.limit_stories,
    )
    if not stories:
        raise SystemExit("Không có story active trong DB.")

    if args.story_url:
        wanted_urls = {url.rstrip("/") for url in args.story_url}
        stories = [story for story in stories if (story.get("source_url") or "").rstrip("/") in wanted_urls]
    if args.title_contains:
        needle = args.title_contains.casefold()
        stories = [story for story in stories if needle in (story.get("title") or "").casefold()]
    if not stories:
        raise SystemExit("Không có story active trong DB sau khi áp dụng filter --story-url/--title-contains.")

    print(f"stories={len(stories)}, workers={args.workers}, worker_host={socket.gethostname()}")
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = [executor.submit(process_story, story, args, unavailable_hosts, host_lock) for story in stories]
        for future in as_completed(futures):
            try:
                source_code, status = future.result()
                count_status(source_counts, source_code, status)
                if status == "ok":
                    ok_count += 1
                elif status == "skipped":
                    skipped_count += 1
                elif status == "unsupported":
                    unsupported_count += 1
                else:
                    failed_count += 1
            except Exception as exc:
                failed_count += 1
                print(f"[ERROR] worker crashed: {type(exc).__name__}: {exc}")
                if args.stop_on_error:
                    raise

    print(
        "\nDone. Chapter text đã được lưu và polish_chapter jobs đã được enqueue. "
        f"ok={ok_count} failed={failed_count} skipped={skipped_count} unsupported={unsupported_count}"
    )
    for source_code in sorted(source_counts):
        counts = source_counts[source_code]
        print(
            "[SUMMARY] "
            f"{source_code} ok={counts.get('ok', 0)} failed={counts.get('failed', 0)} "
            f"skipped={counts.get('skipped', 0)} unsupported={counts.get('unsupported', 0)}"
        )


if __name__ == "__main__":
    main()
