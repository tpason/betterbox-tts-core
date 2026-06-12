#!/usr/bin/env python3
"""Crawl JadeScrolls public novel metadata, catalog, and free chapter text.

JadeScrolls exposes a public JSON API used by its Next.js reader. This crawler
only imports chapters that are explicitly accessible without payment:

- chapter ``type`` must be ``FREE`` by default;
- chapter detail must include non-empty ``content``;
- ``user_metadata.has_access`` must not be false when present;
- premium/coin/scroll/subscription chapters are skipped unless a caller
  intentionally changes the allowed type filter.

Example:
  viterbox/venv/bin/python scripts/story_pipeline/crawl_jadescrolls_chapters.py \
    --story-url https://jadescrolls.com/novel/reincarnators-stream \
    --upsert-db --download-text --from-chapter 1 --to-chapter 20

By default this crawler stores chapter text in the DB only. Raw ``chapterXXXX.txt``
files are opt-in via ``--write-files`` so the audio pipeline does not consume
raw crawl files as source artifacts.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
for path in (ROOT, SCRIPT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scripts.story_pipeline.crawl_stories_from_db import enqueue_polish_for_args, upsert_downloaded_chapter  # noqa: E402
from scripts.story_pipeline.crawl_utils import clean_text, safe_slug  # noqa: E402
from story_db.story_pipeline_db import repository as repo  # noqa: E402


BASE_URL = "https://jadescrolls.com"
API_BASE_URL = "https://api.jadescrolls.com/api"
SOURCE_CODE = "jadescrolls"
SOURCE_NAME = "JadeScrolls"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9,vi;q=0.8",
    "Origin": BASE_URL,
    "Referer": BASE_URL + "/",
}


@dataclass
class JadeScrollsChapter:
    number: int
    title: str
    url: str
    source_chapter_id: str
    slug: str
    chapter_type: str = ""
    word_count: int = 0
    volume: str = ""
    publish_at: str = ""


def compact_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def parse_story_slug(url_or_slug: str) -> str:
    value = (url_or_slug or "").strip()
    if not value:
        return ""
    if "://" not in value:
        return safe_slug(value, "jadescrolls-story")
    parts = [part for part in urlparse(value).path.split("/") if part]
    if "novel" in parts:
        index = parts.index("novel")
        if len(parts) > index + 1:
            return parts[index + 1]
    if "novels" in parts:
        index = parts.index("novels")
        if len(parts) > index + 1:
            return parts[index + 1]
    return parts[-1] if parts else ""


def canonical_story_url(slug: str) -> str:
    return f"{BASE_URL}/novel/{slug}"


def canonical_chapter_url(story_slug: str, chapter_slug: str) -> str:
    return f"{BASE_URL}/novels/{story_slug}/chapter/{chapter_slug}"


def api_get(path: str, *, params: dict[str, Any] | None = None, timeout: int = 30, retries: int = 3, retry_sleep: float = 1.5) -> Any:
    url = path if path.startswith("http") else API_BASE_URL.rstrip("/") + "/" + path.lstrip("/")
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            if response.status_code in {408, 429, 500, 502, 503, 504}:
                response.raise_for_status()
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(retry_sleep * attempt)
    raise RuntimeError(f"Cannot fetch JadeScrolls API after {retries} attempts: {url} | {last_error}") from last_error


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for removable in soup.select("script, style, noscript, iframe"):
        removable.decompose()
    lines: list[str] = []
    for line in soup.get_text("\n", strip=True).splitlines():
        text = compact_text(line)
        if text:
            lines.append(text)
    return clean_text("\n".join(lines))


def chapter_is_public(chapter: dict[str, Any], allowed_types: set[str]) -> tuple[bool, str]:
    chapter_type = str(chapter.get("type") or "").upper()
    if chapter_type and chapter_type not in allowed_types:
        return False, f"type={chapter_type.lower()}"
    if chapter.get("coin_value") or chapter.get("scroll_value"):
        return False, "coin_or_scroll_required"
    if chapter.get("subscriptions"):
        return False, "subscription_required"
    user_meta = chapter.get("user_metadata") or {}
    if user_meta.get("has_access") is False:
        return False, "has_access_false"
    return True, ""


def looks_jadescrolls_blocked(text: str) -> bool:
    lowered = text.casefold()
    markers = (
        "unlock with",
        "buy chapter",
        "purchase chapter",
        "subscribe to read",
        "login to read",
        "please log in",
        "coin_value",
        "scroll_value",
    )
    return any(marker in lowered for marker in markers)


def extract_story_metadata(slug: str, payload: dict[str, Any]) -> dict[str, Any]:
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    genres = [
        compact_text(item.get("name"))
        for item in [*(payload.get("genres") or []), *(payload.get("sub_genres") or [])]
        if isinstance(item, dict) and compact_text(item.get("name"))
    ]
    return {
        "source": SOURCE_CODE,
        "source_story_id": str(payload.get("id") or slug),
        "story_url": canonical_story_url(slug),
        "slug": slug,
        "title": compact_text(payload.get("title")) or slug.replace("-", " ").title(),
        "original_title": compact_text(payload.get("title")) or slug.replace("-", " ").title(),
        "author": compact_text(payload.get("author_name")) or compact_text((payload.get("author") or {}).get("username")),
        "translator": compact_text(payload.get("translator_name")),
        "tags": list(dict.fromkeys(genres)),
        "category": ", ".join(dict.fromkeys(genres)),
        "status": compact_text(metadata.get("release_status") or metadata.get("status")),
        "language": "en",
        "source_language": compact_text(metadata.get("language")) or "Korean",
        "description": html_to_text(str(payload.get("synopsis") or "")),
        "cover_image_url": str(payload.get("cover_image") or ""),
        "total_chapters": int(payload.get("chapters_count") or 0),
        "chapters_word_count": int(payload.get("chapters_word_count") or 0),
        "metadata": metadata,
    }


def get_story_by_slug(slug: str, args: argparse.Namespace) -> dict[str, Any]:
    payload = api_get("/novels", params={"slug": slug}, timeout=args.timeout, retries=args.retries, retry_sleep=args.retry_sleep)
    if not isinstance(payload, dict) or not payload.get("id"):
        raise RuntimeError(f"JadeScrolls novel not found for slug={slug!r}")
    return payload


def fetch_all_chapters(novel_id: str, story_slug: str, args: argparse.Namespace) -> list[JadeScrollsChapter]:
    chapters: list[JadeScrollsChapter] = []
    page = 1
    seen_ids: set[str] = set()
    page_size = int(getattr(args, "page_size", 100) or 100)
    max_catalog_pages = int(getattr(args, "max_catalog_pages", 0) or 0)
    catalog_delay = float(getattr(args, "catalog_delay", 0.2) or 0.0)
    while True:
        payload = api_get(
            f"/novels-chapter/{novel_id}/chapters/list",
            params={
                "limit": page_size,
                "page": page,
                "status": "PUBLISHED",
                "isDeleted": "false",
                "sortOrder": "asc",
            },
            timeout=args.timeout,
            retries=args.retries,
            retry_sleep=args.retry_sleep,
        )
        rows = payload.get("data") if isinstance(payload, dict) else []
        if not rows:
            break
        for row in rows:
            if not isinstance(row, dict):
                continue
            chapter_id = str(row.get("id") or "")
            if not chapter_id or chapter_id in seen_ids:
                continue
            seen_ids.add(chapter_id)
            number = int(row.get("chapter_number") or len(chapters) + 1)
            chapter_slug = str(row.get("slug") or f"chapter-{number}")
            volume = row.get("volume") if isinstance(row.get("volume"), dict) else {}
            chapters.append(
                JadeScrollsChapter(
                    number=number,
                    title=compact_text(row.get("title")) or f"Chapter {number}",
                    url=canonical_chapter_url(story_slug, chapter_slug),
                    source_chapter_id=chapter_id,
                    slug=chapter_slug,
                    chapter_type=str(row.get("type") or ""),
                    word_count=int(row.get("word_count") or 0),
                    volume=str(volume.get("title") or volume.get("number") or ""),
                    publish_at=str(row.get("publish_at") or ""),
                )
            )
        meta = payload.get("meta") if isinstance(payload, dict) else {}
        total_pages = int(meta.get("total_pages") or page)
        if max_catalog_pages and page >= max_catalog_pages:
            break
        if page >= total_pages:
            break
        page += 1
        time.sleep(catalog_delay)
    return sorted(chapters, key=lambda chapter: chapter.number)


def parse_catalog(story_url: str, args: argparse.Namespace | None = None) -> dict[str, Any]:
    if args is None:
        args = argparse.Namespace(timeout=30, retries=3, retry_sleep=1.5, page_size=100, max_catalog_pages=0, catalog_delay=0.2)
    slug = parse_story_slug(story_url)
    if not slug:
        raise ValueError(f"Cannot derive JadeScrolls slug from URL: {story_url}")
    story_payload = get_story_by_slug(slug, args)
    metadata = extract_story_metadata(slug, story_payload)
    chapters = fetch_all_chapters(str(story_payload["id"]), slug, args)
    return {
        **metadata,
        "api_url": f"{API_BASE_URL}/novels?slug={slug}",
        "catalog_url": canonical_story_url(slug),
        "chapters": [asdict(chapter) for chapter in chapters],
        "crawled_at": datetime.now(timezone.utc).isoformat(),
    }


def fetch_chapter_text_by_id(novel_id: str, chapter_id: str, args: argparse.Namespace, *, allowed_types: set[str] | None = None) -> str:
    allowed_types = allowed_types or {"FREE"}
    payload = api_get(
        f"/novels-chapter/{novel_id}/chapters/{chapter_id}",
        timeout=args.timeout,
        retries=args.retries,
        retry_sleep=args.retry_sleep,
    )
    ok, reason = chapter_is_public(payload, allowed_types)
    if not ok:
        raise RuntimeError(f"JadeScrolls chapter is not public/free: {reason}")
    text = html_to_text(str(payload.get("content") or ""))
    if not text:
        raise RuntimeError("JadeScrolls chapter has empty content")
    if looks_jadescrolls_blocked(text):
        raise RuntimeError("JadeScrolls chapter looks locked/paywalled")
    return text


def fetch_chapter_text(chapter_url: str, args: argparse.Namespace | None = None) -> str:
    if args is None:
        args = argparse.Namespace(timeout=30, retries=3, retry_sleep=1.5, allowed_chapter_types=["FREE"])
    if not hasattr(args, "allowed_chapter_types"):
        setattr(args, "allowed_chapter_types", ["FREE"])
    parts = [part for part in urlparse(chapter_url).path.split("/") if part]
    if len(parts) >= 4 and parts[0] == "novels" and parts[2] == "chapter":
        story_slug = parts[1]
        chapter_slug = parts[3]
    else:
        raise ValueError(f"Unsupported JadeScrolls chapter URL: {chapter_url}")
    story_payload = get_story_by_slug(story_slug, args)
    catalog_args = argparse.Namespace(**vars(args), page_size=100, max_catalog_pages=0, catalog_delay=0.0)
    chapters = fetch_all_chapters(str(story_payload["id"]), story_slug, catalog_args)
    match = next((chapter for chapter in chapters if chapter.slug == chapter_slug), None)
    if match is None:
        raise RuntimeError(f"Cannot resolve JadeScrolls chapter slug={chapter_slug!r}")
    return fetch_chapter_text_by_id(str(story_payload["id"]), match.source_chapter_id, args, allowed_types=set(args.allowed_chapter_types))


def upsert_catalog_to_db(catalog: dict[str, Any]) -> dict[str, Any]:
    repo.upsert_source(SOURCE_CODE, SOURCE_NAME, BASE_URL)
    metadata = dict(catalog.get("metadata") or {})
    metadata.update(
        {
            "slug": catalog.get("slug"),
            "source": SOURCE_CODE,
            "source_author": catalog.get("author") or "",
            "source_translator": catalog.get("translator") or "",
            "source_description": catalog.get("description") or "",
            "source_language": catalog.get("source_language") or "Korean",
            "chapters_word_count": catalog.get("chapters_word_count") or 0,
        }
    )
    return repo.upsert_story(
        SOURCE_CODE,
        {
            "source_story_id": catalog.get("source_story_id") or catalog.get("slug"),
            "title": catalog.get("title") or catalog.get("slug") or "JadeScrolls Story",
            "original_title": catalog.get("original_title") or catalog.get("title"),
            "author": catalog.get("author"),
            "category": catalog.get("category"),
            "status": catalog.get("status"),
            "language": "en",
            "source_url": catalog.get("story_url"),
            "catalog_url": catalog.get("catalog_url"),
            "description": catalog.get("description"),
            "cover_image_url": catalog.get("cover_image_url"),
            "total_chapters": catalog.get("total_chapters") or len(catalog.get("chapters") or []),
            "is_completed": str(catalog.get("status") or "").lower() in {"completed", "complete"},
            "metadata": metadata,
        },
    )


def upsert_catalog_chapters_to_db(story: dict[str, Any], catalog: dict[str, Any], *, allowed_types: set[str]) -> int:
    count = 0
    for chapter in catalog.get("chapters") or []:
        number = int(chapter.get("number") or 0)
        if not number:
            continue
        is_free = str(chapter.get("chapter_type") or "").upper() in allowed_types
        repo.upsert_chapter(
            story["id"],
            {
                "source_chapter_id": str(chapter.get("source_chapter_id") or number),
                "chapter_number": number,
                "title": chapter.get("title") or f"Chapter {number}",
                "source_url": chapter.get("url") or "",
                "volume": chapter.get("volume"),
                "raw_language": "en",
                "is_downloaded": False,
                "is_locked": not is_free,
                "lock_reason": "" if is_free else f"type={chapter.get('chapter_type') or 'unknown'}",
            },
        )
        count += 1
    return count


def chapter_path(root: Path, slug: str, chapter_number: int) -> Path:
    return root / slug / f"chapter{chapter_number:04d}.txt"


def write_if_needed(path: Path, text: str, overwrite: bool, *, persist: bool = True) -> bool:
    if not persist:
        return True
    if path.exists() and not overwrite:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return True


def should_write_files(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "write_files", False)) and not bool(getattr(args, "no_write_files", False))


def download_chapters_to_db(story: dict[str, Any], catalog: dict[str, Any], args: argparse.Namespace) -> dict[str, int]:
    slug = safe_slug(catalog.get("slug") or catalog.get("title") or "jadescrolls-story")
    novel_id = str(catalog.get("source_story_id") or "")
    allowed_types = {str(value).upper() for value in args.allowed_chapter_types}
    chapters = list(catalog.get("chapters") or [])
    if args.from_chapter:
        chapters = [chapter for chapter in chapters if int(chapter.get("number") or 0) >= args.from_chapter]
    if args.to_chapter:
        chapters = [chapter for chapter in chapters if int(chapter.get("number") or 0) <= args.to_chapter]
    if args.max_chapters:
        chapters = chapters[: args.max_chapters]
    persist_files = should_write_files(args)

    saved = skipped = failed = jobs = locked = 0
    for chapter in chapters:
        number = int(chapter.get("number") or 0)
        if not number:
            skipped += 1
            continue
        title = chapter.get("title") or f"Chapter {number}"
        raw_path = chapter_path(Path(args.raw_en_output_root), slug, number)
        chapter_type = str(chapter.get("chapter_type") or "").upper()
        if chapter_type and chapter_type not in allowed_types:
            locked += 1
            if args.upsert_db:
                upsert_downloaded_chapter(
                    story,
                    source_chapter_id=str(chapter.get("source_chapter_id") or number),
                    chapter_number=number,
                    title=title,
                    source_url=chapter.get("url") or "",
                    raw_language="en",
                    raw_path=None,
                    raw_text_content=None,
                    volume=chapter.get("volume"),
                    is_locked=True,
                    lock_reason=f"type={chapter_type.lower()}",
                )
            print(f"[SKIP] locked/paywall jadescrolls {slug}/chapter{number:04d} type={chapter_type}")
            continue
        if persist_files and raw_path.exists() and not args.overwrite:
            text = raw_path.read_text(encoding="utf-8")
            db_chapter = upsert_downloaded_chapter(
                story,
                source_chapter_id=str(chapter.get("source_chapter_id") or number),
                chapter_number=number,
                title=title,
                source_url=chapter.get("url") or "",
                raw_language="en",
                raw_path=raw_path,
                raw_text_content=text,
                volume=chapter.get("volume"),
            )
            skipped += 1
            if args.enqueue_polish:
                job = enqueue_polish_for_args(SOURCE_CODE, story, db_chapter, slug, raw_path, "en", args)
                jobs += 1 if job else 0
            continue
        try:
            content = fetch_chapter_text_by_id(novel_id, str(chapter.get("source_chapter_id") or ""), args, allowed_types=allowed_types)
            if not content or len(content) < args.min_text_chars:
                print(f"[SKIP] short/empty jadescrolls {slug}/chapter{number:04d}")
                skipped += 1
                continue
            text = f"{title}\n\n{content}".strip() + "\n"
            if not persist_files:
                print(f"[TEXT] db-only {slug}/chapter{number:04d}: {len(text)} chars")
                write_path: Path | None = None
            else:
                write_if_needed(raw_path, text, args.overwrite)
                write_path = raw_path
                print(f"[TEXT] saved {slug}/chapter{number:04d}: {raw_path}")
            db_chapter = upsert_downloaded_chapter(
                story,
                source_chapter_id=str(chapter.get("source_chapter_id") or number),
                chapter_number=number,
                title=title,
                source_url=chapter.get("url") or "",
                raw_language="en",
                raw_path=write_path,
                raw_text_content=text,
                volume=chapter.get("volume"),
            )
            saved += 1
            if args.enqueue_polish:
                job = enqueue_polish_for_args(SOURCE_CODE, story, db_chapter, slug, write_path, "en", args)
                jobs += 1 if job else 0
        except Exception as exc:
            failed += 1
            print(f"[WARN] jadescrolls chapter failed {chapter.get('url')}: {type(exc).__name__}: {exc}")
            if args.stop_on_error:
                raise
        time.sleep(args.chapter_delay)
    return {"saved": saved, "skipped": skipped, "locked": locked, "failed": failed, "jobs": jobs}


def main() -> None:
    parser = argparse.ArgumentParser(description="Crawl JadeScrolls public catalog/free chapter text.")
    parser.add_argument("--story-url", default="https://jadescrolls.com/novel/reincarnators-stream")
    parser.add_argument("--from-chapter", type=int, default=0)
    parser.add_argument("--to-chapter", type=int, default=0)
    parser.add_argument("--max-chapters", type=int, default=0)
    parser.add_argument("--max-catalog-pages", type=int, default=0)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-sleep", type=float, default=1.5)
    parser.add_argument("--catalog-delay", type=float, default=0.2)
    parser.add_argument("--chapter-delay", type=float, default=0.5)
    parser.add_argument("--output-root", default="story_data/catalogs/jadescrolls")
    parser.add_argument("--raw-en-output-root", default="story_data/raw_en")
    parser.add_argument("--translated-output-root", default="story_data/translated")
    parser.add_argument("--polished-output-root", default="story_data/polished")
    parser.add_argument("--upsert-db", action="store_true")
    parser.add_argument("--upsert-chapters", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--download-text", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--write-files",
        action="store_true",
        help="Opt-in: persist raw chapterXXXX.txt files under --raw-en-output-root. Default is DB-only.",
    )
    parser.add_argument("--no-write-files", action="store_true", help="Force DB-only mode; kept for compatibility.")
    parser.add_argument("--min-text-chars", type=int, default=200)
    parser.add_argument("--allowed-chapter-types", nargs="*", default=["FREE"])
    parser.add_argument("--enqueue-polish", action="store_true")
    parser.add_argument("--vi-model", default="qwen3:14b")
    parser.add_argument("--translate-model", default="qwen3:14b")
    parser.add_argument("--polish-max-attempts", type=int, default=3)
    parser.add_argument("--requeue-done", action="store_true")
    parser.add_argument("--post-translate", choices=("polish", "copy"), default="copy")
    parser.add_argument("--stop-on-error", action="store_true")
    args = parser.parse_args()

    catalog = parse_catalog(args.story_url, args)
    slug = safe_slug(catalog.get("slug") or catalog.get("title") or "jadescrolls-story")
    catalog_path = Path(args.output_root) / slug / "chapters.json"
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[CATALOG] {catalog.get('title')} chapters={len(catalog.get('chapters') or [])} path={catalog_path}")

    story: dict[str, Any] | None = None
    allowed_types = {str(value).upper() for value in args.allowed_chapter_types}
    if args.upsert_db:
        story = upsert_catalog_to_db(catalog)
        if args.upsert_chapters:
            count = upsert_catalog_chapters_to_db(story, catalog, allowed_types=allowed_types)
            print(f"[DB] upserted story_id={story['id']} chapters={count}")

    if args.download_text:
        if story is None:
            story = upsert_catalog_to_db(catalog)
        stats = download_chapters_to_db(story, catalog, args)
        print(f"[DONE] {catalog.get('title')} | {stats}")
    else:
        print(f"[DONE] {catalog.get('title')} | catalog only")


if __name__ == "__main__":
    main()
