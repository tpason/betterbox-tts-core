#!/usr/bin/env python3
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
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from requests import Session
from requests.adapters import HTTPAdapter

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from genre_prompts import find_char_map_file, resolve_genre_from_context  # noqa: E402
from crawl_utils import looks_blocked  # noqa: E402


BASE_URL = "https://lightnovelpub.org"
DEFAULT_STORY_URLS = [
    "https://lightnovelpub.org/novel/shadow-slave/",
    "https://lightnovelpub.org/novel/reverend-insanity/",
    "https://lightnovelpub.org/novel/lord-of-the-mysteries/",
    "https://lightnovelpub.org/novel/genetic-ascension/",
    "https://lightnovelpub.org/novel/a-regressors-tale-of-cultivation/",
    "https://lightnovelpub.org/novel/the-authors-pov/",
]
DEFAULT_DISCOVERY_URLS = [
    "https://lightnovelpub.org/ranking/",
    "https://lightnovelpub.org/genre-all/?order=popular",
]
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,vi;q=0.8",
}


@dataclass
class LightNovelPubChapter:
    number: int
    title: str
    url: str
    source_chapter_id: str
    released_at_text: str = ""


def compact_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def parse_int(value: str) -> int:
    digits = re.sub(r"[^\d]", "", value or "")
    return int(digits) if digits else 0


def safe_slug(value: str) -> str:
    value = re.sub(r"\s+", "-", (value or "").strip().lower())
    value = re.sub(r"[^a-z0-9-]+", "", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value or "lightnovelpub-story"


def story_slug(url: str) -> str:
    parts = [part for part in urlparse(url).path.split("/") if part]
    if "novel" in parts:
        index = parts.index("novel")
        if len(parts) > index + 1:
            return safe_slug(parts[index + 1])
    return safe_slug(parts[-1] if parts else "story")


def canonical_story_url(url: str) -> str:
    return f"{BASE_URL}/novel/{story_slug(url)}/"


def add_query_param(url: str, **params: Any) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    for key, value in params.items():
        if value is None:
            query.pop(key, None)
        else:
            query[key] = str(value)
    return urlunparse(parsed._replace(query=urlencode(query)))


def _make_session() -> Session:
    sess = Session()
    sess.headers.update(HEADERS)
    adapter = HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0)
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    return sess


def fetch_html(url: str, timeout: int = 30, retries: int = 5, retry_sleep: float = 2.0, *, session: Session | None = None) -> str:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            if session is not None:
                response = session.get(url, timeout=timeout)
            else:
                response = requests.get(url, headers=HEADERS, timeout=timeout)
            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(retry_sleep * attempt)
    raise RuntimeError(f"Cannot fetch LightNovelPub URL after {retries} attempts: {url} | {last_error}") from last_error


def extract_json_ld_book(soup: BeautifulSoup) -> dict[str, Any]:
    for script in soup.select("script[type='application/ld+json']"):
        text = script.string or script.get_text("", strip=True)
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        entries = payload if isinstance(payload, list) else [payload]
        for entry in entries:
            if isinstance(entry, dict) and entry.get("@type") == "Book":
                return entry
    return {}


def extract_story_metadata(story_url: str, html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    book = extract_json_ld_book(soup)
    slug = story_slug(story_url)
    title_node = soup.select_one("h1.novel-title") or soup.select_one("h1")
    title = compact_text(str(book.get("name") or "")) or (compact_text(title_node.get_text(" ", strip=True)) if title_node else "")
    author_payload = book.get("author") if isinstance(book.get("author"), dict) else {}
    author = compact_text(str(author_payload.get("name") or ""))
    if not author:
        author_node = soup.select_one(".novel-author a, a.author-link, a[href*='/author/']")
        author = compact_text(author_node.get_text(" ", strip=True)) if author_node else ""
    genres = book.get("genre") if isinstance(book.get("genre"), list) else []
    tags = [compact_text(str(item)) for item in genres if compact_text(str(item))]
    if not tags:
        tags = [compact_text(node.get_text(" ", strip=True)).title() for node in soup.select(".genre-tag") if compact_text(node.get_text(" ", strip=True))]
    description = compact_text(str(book.get("description") or ""))
    if not description:
        desc_node = soup.select_one(".novel-description")
        description = compact_text(desc_node.get_text(" ", strip=True)) if desc_node else ""
    cover = str(book.get("image") or "")
    cover_node = soup.select_one("img.novel-cover, .novel-cover-container img, meta[property='og:image']")
    if not cover and cover_node:
        cover = str(cover_node.get("content") or cover_node.get("src") or "")
    total_chapters = parse_int(str(book.get("numberOfPages") or ""))
    if not total_chapters:
        total_node = soup.select_one("input#chapterInput[max], meta[property='og:title']")
        total_chapters = parse_int(str(total_node.get("max") if total_node else ""))
    if not total_chapters:
        total_chapters = max([parse_int(match.group(1)) for match in re.finditer(r"(\d[\d,]*)\s+chapters?", soup.get_text(" ", strip=True), flags=re.IGNORECASE)], default=0)
    status = compact_text(str(book.get("status") or ""))
    return {
        "source": "lightnovelpub",
        "source_story_id": str(soup.select_one("meta[name='novel-id']").get("content")) if soup.select_one("meta[name='novel-id']") else slug,
        "story_url": canonical_story_url(story_url),
        "slug": slug,
        "title": title or slug.replace("-", " ").title(),
        "author": author,
        "tags": tags,
        "category": ", ".join(tags),
        "status": status,
        "description": description,
        "cover_image_url": urljoin(BASE_URL, cover) if cover else "",
        "total_chapters": total_chapters,
    }


def parse_max_page(soup: BeautifulSoup) -> int:
    values = [parse_int(option.get("value") or option.get_text(" ", strip=True)) for option in soup.select("select#pageSelect option")]
    page_selector = soup.select_one(".page-selector")
    text = page_selector.get_text(" ", strip=True) if page_selector else ""
    match = re.search(r"\bof\s+(\d+)\b", text, flags=re.IGNORECASE)
    if match:
        values.append(parse_int(match.group(1)))
    return max(values, default=1)


def parse_chapters_from_page(html: str, page_url: str) -> list[LightNovelPubChapter]:
    soup = BeautifulSoup(html, "html.parser")
    chapters: list[LightNovelPubChapter] = []
    seen: set[str] = set()
    for card in soup.select(".chapter-card"):
        onclick = card.get("onclick") or ""
        href_match = re.search(r"location\.href=['\"]([^'\"]+)['\"]", onclick)
        href = href_match.group(1) if href_match else ""
        anchor = card.select_one("a[href*='/chapter/']")
        if not href and anchor:
            href = anchor.get("href") or ""
        if not href:
            continue
        chapter_url = urljoin(page_url, href).split("#", 1)[0]
        if chapter_url in seen:
            continue
        seen.add(chapter_url)
        number = parse_int(card.select_one(".chapter-number").get_text(" ", strip=True) if card.select_one(".chapter-number") else "")
        if not number:
            match = re.search(r"/chapter/(\d+)/?", chapter_url)
            number = parse_int(match.group(1)) if match else len(chapters) + 1
        title_node = card.select_one(".chapter-title")
        title = compact_text(title_node.get_text(" ", strip=True)) if title_node else f"Chapter {number}"
        time_node = card.select_one(".chapter-time")
        chapters.append(
            LightNovelPubChapter(
                number=number,
                title=title or f"Chapter {number}",
                url=chapter_url,
                source_chapter_id=str(number),
                released_at_text=compact_text(time_node.get_text(" ", strip=True)) if time_node else "",
            )
        )
    return chapters


def parse_catalog(
    story_url: str,
    timeout: int = 30,
    retries: int = 5,
    retry_sleep: float = 2.0,
    max_catalog_pages: int = 0,
    from_chapter: int = 0,
    to_chapter: int = 0,
    max_chapters: int = 0,
) -> dict[str, Any]:
    story_url = canonical_story_url(story_url)
    session = _make_session()
    try:
        story_html = fetch_html(story_url, timeout, retries, retry_sleep, session=session)
        metadata = extract_story_metadata(story_url, story_html)
        chapters_url = urljoin(story_url, "chapters/")
        first_html = fetch_html(chapters_url, timeout, retries, retry_sleep, session=session)
        first_soup = BeautifulSoup(first_html, "html.parser")
        max_page = parse_max_page(first_soup)
        first_page_chapters = parse_chapters_from_page(first_html, chapters_url)
        page_size = max(1, len(first_page_chapters))

        start_page = max(1, ((from_chapter - 1) // page_size) + 1) if from_chapter and page_size else 1
        end_page = max_page
        if to_chapter and page_size:
            end_page = min(end_page, max(1, ((to_chapter - 1) // page_size) + 1))
        if max_chapters and page_size:
            end_page = min(end_page, start_page + max(1, ((max_chapters - 1) // page_size) + 1) - 1)
        if max_catalog_pages:
            end_page = min(end_page, start_page + max_catalog_pages - 1)

        chapters: list[LightNovelPubChapter] = []
        if start_page <= 1:
            chapters.extend(first_page_chapters)
            page_start = 2
        else:
            page_start = start_page
        for page in range(page_start, end_page + 1):
            page_html = fetch_html(add_query_param(chapters_url, page=page), timeout, retries, retry_sleep, session=session)
            chapters.extend(parse_chapters_from_page(page_html, add_query_param(chapters_url, page=page)))

        deduped = {chapter.number: chapter for chapter in chapters}
        chapters = [deduped[number] for number in sorted(deduped)]
        if from_chapter:
            chapters = [chapter for chapter in chapters if chapter.number >= from_chapter]
        if to_chapter:
            chapters = [chapter for chapter in chapters if chapter.number <= to_chapter]
        if max_chapters:
            chapters = chapters[:max_chapters]

        return {
            **metadata,
            "catalog_url": chapters_url,
            "total_chapters": metadata.get("total_chapters") or (chapters[-1].number if chapters else 0),
            "catalog_pages": max_page,
            "crawled_catalog_pages": max(0, end_page - start_page + 1),
            "chapters": [asdict(chapter) for chapter in chapters],
            "crawled_at": datetime.now(timezone.utc).isoformat(),
        }
    finally:
        session.close()


def extract_chapter_title(html: str) -> str:
    """Extract chapter title from the chapter page (e.g. 'Chapter 527 - The Ordinary Finn')."""
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one(".chapter-title") or soup.select_one("h1") or soup.select_one(".reading-detail h2")
    if node is None:
        return ""
    text = compact_text(node.get_text(" ", strip=True))
    # Ignore if it's just the site name or story title
    if any(marker in text for marker in ("Light Novel Pub", "LIGHTNOVELPUB", "lightnovelpub")):
        return ""
    return text


def extract_chapter_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one("#chapterText") or soup.select_one(".chapter-text") or soup.select_one(".chapter-content")
    if node is None:
        return ""
    for selector in [
        "script",
        "style",
        "noscript",
        "iframe",
        ".chapter-ad-container",
        ".ad-unit",
        ".ads",
        ".advertisement",
    ]:
        for removable in node.select(selector):
            removable.decompose()
    lines = [compact_text(line) for line in node.get_text("\n", strip=True).splitlines()]
    drop_markers = (
        "Prev Chapter",
        "Next Chapter",
        "Text to Speech",
        "LIGHTNOVELPUB",
        "Light Novel Pub",
    )
    lines = [line for line in lines if line and not any(marker in line for marker in drop_markers)]
    return "\n\n".join(lines).strip()


def fetch_chapter_text(url: str, timeout: int = 30, retries: int = 5, retry_sleep: float = 2.0) -> str:
    return extract_chapter_text(fetch_html(url, timeout, retries, retry_sleep))


def discover_story_urls(
    urls: list[str],
    *,
    timeout: int = 30,
    retries: int = 5,
    retry_sleep: float = 2.0,
    limit: int = 0,
    max_pages: int = 1,
) -> list[str]:
    discovered: list[str] = []
    seen: set[str] = set()
    for seed_url in urls:
        for page in range(1, max(1, max_pages) + 1):
            url = seed_url if page == 1 else add_query_param(seed_url, page=page)
            html = fetch_html(url, timeout, retries, retry_sleep)
            soup = BeautifulSoup(html, "html.parser")
            for anchor in soup.select("a[href*='/novel/']"):
                href = anchor.get("href") or ""
                if "/chapter/" in href or "/characters/" in href or "/chapters/" in href:
                    continue
                story_url = canonical_story_url(urljoin(url, href))
                if story_url in seen:
                    continue
                seen.add(story_url)
                discovered.append(story_url)
                if limit and len(discovered) >= limit:
                    return discovered
    return discovered


def upsert_catalog_to_db(catalog: dict[str, Any]) -> dict[str, Any]:
    from story_db.story_pipeline_db import repository as repo

    repo.upsert_source("lightnovelpub", "LightNovelPub", BASE_URL)
    story = repo.upsert_story(
        "lightnovelpub",
        {
            "source_story_id": catalog.get("source_story_id") or catalog.get("slug"),
            "title": catalog.get("title") or catalog.get("slug") or "LightNovelPub Story",
            "original_title": catalog.get("title") or catalog.get("slug"),
            "author": catalog.get("author"),
            "category": ", ".join(catalog.get("tags") or []) or catalog.get("category"),
            "status": catalog.get("status"),
            "language": "en",
            "source_url": catalog.get("story_url"),
            "catalog_url": catalog.get("catalog_url"),
            "description": catalog.get("description"),
            "cover_image_url": catalog.get("cover_image_url"),
            "total_chapters": catalog.get("total_chapters") or len(catalog.get("chapters") or []),
            "is_completed": str(catalog.get("status") or "").lower() in {"completed", "complete"},
            "metadata": {
                "slug": catalog.get("slug"),
                "source": "lightnovelpub",
                "tags": catalog.get("tags") or [],
                "catalog_pages": catalog.get("catalog_pages"),
                "source_author": catalog.get("author") or "",
                "source_description": catalog.get("description") or "",
            },
        },
    )
    return story


def chapter_path(root: Path, slug: str, chapter_number: int) -> Path:
    return root / slug / f"chapter{chapter_number:04d}.txt"


def write_if_needed(path: Path, text: str, overwrite: bool) -> bool:
    if path.exists() and not overwrite:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return True


def upsert_catalog_chapters_to_db(story: dict[str, Any], catalog: dict[str, Any]) -> int:
    from story_db.story_pipeline_db import repository as repo

    count = 0
    for index, chapter in enumerate(catalog.get("chapters") or [], start=1):
        number = int(chapter.get("number") or index)
        repo.upsert_chapter(
            story["id"],
            {
                "source_chapter_id": str(chapter.get("source_chapter_id") or number),
                "chapter_number": number,
                "title": chapter.get("title") or f"Chapter {number}",
                "source_url": chapter.get("url") or "",
                "raw_language": "en",
                "is_downloaded": False,
            },
        )
        count += 1
    return count


def enqueue_polish_job(
    *,
    story: dict[str, Any],
    db_chapter: dict[str, Any],
    slug: str,
    raw_path: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    from story_db.story_pipeline_db import repository as repo

    polished_path = Path(args.polished_output_root) / slug / raw_path.name
    category = str(story.get("category") or " ".join((story.get("metadata") or {}).get("tags") or []))
    char_map_file = find_char_map_file(story_id=str(story.get("id") or ""), slug=slug)
    return repo.enqueue_chapter_job(
        "polish_chapter",
        db_chapter["id"],
        story_id=story["id"],
        source_code="lightnovelpub",
        model=args.translate_model,
        input_path=raw_path.as_posix(),
        output_path=polished_path.as_posix(),
        payload={
            "raw_language": "en",
            "story_slug": slug,
            "chapter_number": db_chapter["chapter_number"],
            "chapter_title": db_chapter.get("title") or raw_path.stem,
            "source_chapter_title": db_chapter.get("title") or raw_path.stem,
            "translate_story_metadata": True,
            "source_story_title": story.get("original_title") or story.get("title") or "",
            "source_story_author": (story.get("metadata") or {}).get("source_author") or story.get("author") or "",
            "source_story_description": (story.get("metadata") or {}).get("source_description")
            or story.get("description")
            or "",
            "post_translate": args.post_translate,
            "translated_text_path": (Path(args.translated_output_root) / slug / raw_path.name).as_posix(),
            "genre": resolve_genre_from_context(
                category,
                raw_language="en",
                source_code="lightnovelpub",
                char_map_file=char_map_file,
            ),
            "char_map_file": char_map_file,
        },
        max_attempts=args.polish_max_attempts,
    )


def download_chapters_to_db(story: dict[str, Any], catalog: dict[str, Any], args: argparse.Namespace) -> dict[str, int]:
    from story_db.story_pipeline_db import repository as repo

    slug = str(catalog.get("slug") or story_slug(catalog.get("story_url") or story.get("source_url") or ""))
    saved = 0
    skipped = 0
    failed = 0
    jobs = 0
    for index, chapter in enumerate(catalog.get("chapters") or [], start=1):
        number = int(chapter.get("number") or index)
        raw_path = chapter_path(Path(args.raw_en_output_root), slug, number)
        title = chapter.get("title") or f"Chapter {number}"
        if raw_path.exists() and not args.overwrite:
            content = raw_path.read_text(encoding="utf-8")
            db_chapter = repo.upsert_chapter(
                story["id"],
                {
                    "source_chapter_id": str(chapter.get("source_chapter_id") or number),
                    "chapter_number": number,
                    "title": title,
                    "source_url": chapter.get("url") or "",
                    "raw_language": "en",
                    "raw_text_path": raw_path.as_posix(),
                    "raw_text_content": content,
                    "is_downloaded": True,
                },
            )
            skipped += 1
            if args.enqueue_polish:
                enqueue_polish_job(story=story, db_chapter=db_chapter, slug=slug, raw_path=raw_path, args=args)
                jobs += 1
            continue

        try:
            raw_html = fetch_html(
                chapter["url"],
                timeout=args.timeout,
                retries=args.retries,
                retry_sleep=args.retry_sleep,
            )
            content = extract_chapter_text(raw_html)
            if not content or len(content) < args.min_text_chars:
                print(f"[SKIP] short/empty lightnovelpub {slug}/chapter{number:04d}")
                skipped += 1
                continue
            if looks_blocked(content):
                print(f"[SKIP] locked/paywall lightnovelpub {slug}/chapter{number:04d}")
                skipped += 1
                continue
            # Use page title if catalog only has bare "Chapter N" (no subtitle)
            if re.match(r"^Chapter\s+\d+$", title, re.IGNORECASE):
                page_title = extract_chapter_title(raw_html)
                if page_title:
                    title = page_title
            text = f"{title}\n\n{content}".strip() + "\n"
            write_if_needed(raw_path, text, args.overwrite)
            db_chapter = repo.upsert_chapter(
                story["id"],
                {
                    "source_chapter_id": str(chapter.get("source_chapter_id") or number),
                    "chapter_number": number,
                    "title": title,
                    "source_url": chapter.get("url") or "",
                    "raw_language": "en",
                    "raw_text_path": raw_path.as_posix(),
                    "raw_text_content": text,
                    "is_downloaded": True,
                },
            )
            saved += 1
            if args.enqueue_polish:
                enqueue_polish_job(story=story, db_chapter=db_chapter, slug=slug, raw_path=raw_path, args=args)
                jobs += 1
            print(f"[TEXT] saved {slug}/chapter{number:04d}: {raw_path}")
        except Exception as exc:
            failed += 1
            print(f"[WARN] lightnovelpub chapter failed {chapter.get('url')}: {type(exc).__name__}: {exc}")
            if args.stop_on_error:
                raise
        time.sleep(args.chapter_delay)
    return {"saved": saved, "skipped": skipped, "failed": failed, "jobs": jobs}


def main() -> None:
    parser = argparse.ArgumentParser(description="Crawl LightNovelPub story catalog/chapter text.")
    parser.add_argument("story_url", nargs="*", default=[])
    parser.add_argument("--discover-url", nargs="*", default=[], help="Ranking/genre page để lấy story URL.")
    parser.add_argument("--use-default-stories", action="store_true", help="Dùng 6 story LightNovelPub đang được cấu hình sẵn.")
    parser.add_argument("--use-default-discovery", action="store_true", help="Dùng ranking và genre-all popular làm discovery seed.")
    parser.add_argument("--discover-limit", type=int, default=0)
    parser.add_argument("--discover-pages", type=int, default=1)
    parser.add_argument("--from-chapter", type=int, default=0)
    parser.add_argument("--to-chapter", type=int, default=0)
    parser.add_argument("--max-chapters", type=int, default=0)
    parser.add_argument("--max-catalog-pages", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--retry-sleep", type=float, default=2.0)
    parser.add_argument("--output-root", default="story_data/catalogs/lightnovelpub")
    parser.add_argument("--upsert-db", action="store_true", help="Upsert story metadata vào story_db sau khi crawl catalog.")
    parser.add_argument(
        "--upsert-chapters",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Khi --upsert-db, tạo/cập nhật chapter rows trong DB. Mặc định bật.",
    )
    parser.add_argument("--download-text", action="store_true", help="Tải raw English chapter text vào --raw-en-output-root và cập nhật DB.")
    parser.add_argument("--raw-en-output-root", default="story_data/raw_en")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--min-text-chars", type=int, default=80)
    parser.add_argument("--chapter-delay", type=float, default=1.5)
    parser.add_argument("--stop-on-error", action="store_true")
    parser.add_argument("--enqueue-polish", action="store_true", help="Sau khi tải raw text, enqueue job dịch/polish sang tiếng Việt.")
    parser.add_argument("--polished-output-root", default="story_data/polished")
    parser.add_argument("--translated-output-root", default="story_data/translated")
    parser.add_argument("--translate-model", default="translategemma:12b")
    parser.add_argument("--polish-max-attempts", type=int, default=3)
    parser.add_argument(
        "--post-translate",
        choices=("polish", "copy"),
        default="copy",
        help="Mode cho worker sau khi dịch raw khác tiếng Việt: polish tiếp hoặc copy bản dịch sang polished.",
    )
    args = parser.parse_args()

    story_urls = list(args.story_url)
    if args.use_default_stories:
        story_urls.extend(DEFAULT_STORY_URLS)
    discover_urls = list(args.discover_url)
    if args.use_default_discovery:
        discover_urls.extend(DEFAULT_DISCOVERY_URLS)
    if discover_urls:
        story_urls.extend(
            discover_story_urls(
                discover_urls,
                timeout=args.timeout,
                retries=args.retries,
                retry_sleep=args.retry_sleep,
                limit=args.discover_limit,
                max_pages=args.discover_pages,
            )
        )
    story_urls = list(dict.fromkeys(canonical_story_url(url) for url in story_urls))
    if not story_urls:
        story_urls = DEFAULT_STORY_URLS

    for story_url in story_urls:
        catalog = parse_catalog(
            story_url,
            timeout=args.timeout,
            retries=args.retries,
            retry_sleep=args.retry_sleep,
            max_catalog_pages=args.max_catalog_pages,
            from_chapter=args.from_chapter,
            to_chapter=args.to_chapter,
            max_chapters=args.max_chapters,
        )
        output_path = Path(args.output_root) / str(catalog["slug"]) / "chapters.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        db_suffix = ""
        story: dict[str, Any] | None = None
        if args.upsert_db:
            story = upsert_catalog_to_db(catalog)
            chapter_count = upsert_catalog_chapters_to_db(story, catalog) if args.upsert_chapters else 0
            db_suffix = f" | db_story={story['id']} db_chapters={chapter_count}"
        download_suffix = ""
        if args.download_text:
            if story is None:
                story = upsert_catalog_to_db(catalog)
                if args.upsert_chapters:
                    upsert_catalog_chapters_to_db(story, catalog)
            stats = download_chapters_to_db(story, catalog, args)
            download_suffix = (
                f" | raw_saved={stats['saved']} raw_skipped={stats['skipped']} "
                f"raw_failed={stats['failed']} jobs={stats['jobs']}"
            )
        print(f"[OK] {catalog['title']} | chapters={len(catalog.get('chapters') or [])}/{catalog.get('total_chapters') or 0} | {output_path}")
        if db_suffix:
            print(f"[DB] {catalog['title']}{db_suffix}")
        if download_suffix:
            print(f"[TEXT] {catalog['title']}{download_suffix}")


if __name__ == "__main__":
    main()
