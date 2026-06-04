#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.story_pipeline.crawl_story_alternate_sources import safe_slug  # noqa: E402
from scripts.story_pipeline.translate_chapters_from_db import (  # noqa: E402
    translate_story_author,
    translate_story_description,
    translate_story_title,
    update_story_translation,
)
from scripts.story_pipeline.translate_chapter_texts_ollama import translate_file  # noqa: E402
from scripts.story_pipeline.crawl_stories_from_db import enqueue_polish_for_args, upsert_downloaded_chapter  # noqa: E402
from scripts.story_pipeline.genre_prompts import find_char_map_file  # noqa: E402
from scripts.story_pipeline.crawl_utils import looks_blocked  # noqa: E402
from story_db.story_pipeline_db import repository as repo  # noqa: E402


DROP_EXACT = {
    "Advertisement",
    "Reading Settings",
    "Reset to Default",
    "Show Comments",
    "Home",
    "Privacy",
    "DMCA",
    "FAQ",
}
START_MARKERS = (
    "Tap the text to show or hide reading controls.",
    "Click or tap inside the chapter body to show/hide the bottom settings",
)
END_MARKERS = (
    "Next Episode",
    "Previous Episode",
    "Seeking Korean Translators",
    "Your support helps keep our chapters free.",
    "Enjoying the series?",
    "Do not post a spoiler",
    "Reading Settings",
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


def chapter_number_from_url(url: str) -> int:
    parts = [part for part in urlparse(url).path.split("/") if part]
    if not parts:
        return 0
    match = re.match(r"(\d+)(?:-|$)", parts[-1])
    return int(match.group(1)) if match else 0


def clean_lines(lines: list[str]) -> list[str]:
    cleaned: list[str] = []
    for line in lines:
        line = re.sub(r"\s+", " ", line).strip()
        if not line or line in DROP_EXACT:
            continue
        if re.fullmatch(r"(Inter|Lora|Mono|Comic|Default)", line):
            continue
        if re.fullmatch(r"(Compact|Normal|Relaxed)", line):
            continue
        cleaned.append(line)
    return cleaned


def extract_chapter(page: Any) -> tuple[int, str, str, str | None]:
    url = page.url
    html = page.content()
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.select_one("h1")
    heading_text = heading.get_text(" ", strip=True) if heading else ""
    number = chapter_number_from_url(url)
    if not number:
        match = re.search(r"\bEpisode\s+(\d+)\b", heading_text, flags=re.IGNORECASE)
        number = int(match.group(1)) if match else 0

    body_text = page.locator("body").inner_text(timeout=10_000)
    lines = clean_lines(body_text.splitlines())

    start = 0
    for index, line in enumerate(lines):
        if line in START_MARKERS or line == "Advertisement":
            start = index + 1
            break
    if start == 0:
        for index, line in enumerate(lines):
            if heading_text and line == heading_text:
                start = index + 1
                break

    end = len(lines)
    for index in range(start, len(lines)):
        if any(lines[index].startswith(marker) for marker in END_MARKERS):
            end = index
            break

    content_lines = lines[start:end]
    while content_lines and re.fullmatch(r"(Series|BL|Products|Subscriptions|Discord|Login|Register|SFW|18\+)", content_lines[0]):
        content_lines.pop(0)
    content = "\n\n".join(content_lines).strip()

    next_url: str | None = None
    for anchor in soup.select("a[href]"):
        label = anchor.get_text(" ", strip=True).lower()
        href = anchor.get("href") or ""
        if "next episode" in label or re.fullmatch(r"next(?:\s+ch\.\s+\d+)?", label):
            next_url = urljoin(url, href).split("#", 1)[0]
            break

    title = heading_text or f"Episode {number}"
    return number, title, content, next_url


def build_translate_args(args: argparse.Namespace) -> SimpleNamespace:
    return SimpleNamespace(
        ollama_url=args.ollama_url,
        model=args.translate_model,
        temperature=args.temperature,
        num_ctx=args.num_ctx,
        timeout=args.ollama_timeout,
        retries=args.ollama_retries,
        keep_alive=args.keep_alive,
        max_chars_per_chunk=args.translate_max_chars_per_chunk,
        char_map_file=getattr(args, "char_map_file", "") or find_char_map_file(story_id=getattr(args, "story_id", ""), slug=getattr(args, "target_slug", "")),
    )


def build_story_metadata_args(args: argparse.Namespace) -> SimpleNamespace:
    return SimpleNamespace(
        ollama_url=args.ollama_url,
        story_model=args.story_model,
        translate_model=args.translate_model,
        temperature=args.temperature,
        num_ctx=args.num_ctx,
        ollama_timeout=args.ollama_timeout,
        ollama_retries=args.ollama_retries,
        keep_alive=args.keep_alive,
    )


def first_meta_content(soup: BeautifulSoup, selectors: list[str]) -> str:
    for selector in selectors:
        node = soup.select_one(selector)
        if node and node.get("content"):
            return str(node.get("content") or "").strip()
    return ""


def compact_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def label_value(page_text: str, labels: list[str]) -> str:
    for label in labels:
        match = re.search(rf"\b{re.escape(label)}\b\s*:?\s*(.+?)(?:\s{{2,}}|$)", page_text, flags=re.IGNORECASE)
        if match:
            value = compact_text(match.group(1))
            if value and len(value) <= 120:
                return value
    return ""


def extract_author(soup: BeautifulSoup) -> str:
    author_node = (
        soup.select_one("[class*='author'] a")
        or soup.select_one("[class*='author']")
        or soup.select_one("a[href*='author']")
        or soup.select_one("a[href*='artist']")
        or soup.select_one("a[href*='profile']")
    )
    if author_node:
        author = compact_text(author_node.get_text(" ", strip=True))
        if author and author.lower() not in {"author", "artist"}:
            return author
    return label_value(soup.get_text(" ", strip=True), ["Author", "Artist", "Writer", "Translator"])


def extract_cover_image_url(soup: BeautifulSoup, base_url: str, title: str) -> str:
    cover = first_meta_content(
        soup,
        [
            'meta[property="og:image"]',
            'meta[name="twitter:image"]',
            'meta[property="twitter:image"]',
        ],
    )
    if cover:
        return urljoin(base_url, cover)

    title_norm = safe_slug(title)
    candidates = []
    for image in soup.select("img"):
        src = image.get("data-src") or image.get("src") or ""
        if not src:
            continue
        alt = compact_text(image.get("alt"))
        class_text = " ".join(image.get("class") or [])
        score = 0
        if alt and title_norm and title_norm in safe_slug(alt):
            score += 4
        if re.search(r"cover|poster|thumbnail|project", class_text, flags=re.IGNORECASE):
            score += 3
        if re.search(r"cover|poster|thumbnail|project", src, flags=re.IGNORECASE):
            score += 2
        candidates.append((score, src))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[0], reverse=True)
    return urljoin(base_url, candidates[0][1])


def extract_project_metadata(page: Any) -> dict[str, str]:
    soup = BeautifulSoup(page.content(), "html.parser")
    title = (
        (soup.select_one("h1").get_text(" ", strip=True) if soup.select_one("h1") else "")
        or first_meta_content(soup, ['meta[property="og:title"]', 'meta[name="twitter:title"]'])
        or page.title()
    )
    description = first_meta_content(
        soup,
        [
            'meta[property="og:description"]',
            'meta[name="description"]',
            'meta[name="twitter:description"]',
        ],
    )
    if not description:
        paragraphs = [
            re.sub(r"\s+", " ", p.get_text(" ", strip=True)).strip()
            for p in soup.select("p")
            if len(p.get_text(" ", strip=True)) > 120
        ]
        description = max(paragraphs, key=len, default="")
    if any(marker in description for marker in ["[Please select an option]", "Restart from beginning", "Restart from save point"]):
        description = ""
    author = extract_author(soup)
    cover_image_url = extract_cover_image_url(soup, page.url, title)
    return {
        "title": title.strip(),
        "description": description.strip(),
        "author": author,
        "cover_image_url": cover_image_url,
    }


def maybe_translate_story_metadata(page: Any, args: argparse.Namespace, project_slug: str) -> None:
    if not args.translate_story_metadata:
        return
    metadata = extract_project_metadata(page)
    source_title = metadata.get("title") or project_slug.replace("-", " ").title()
    source_description = metadata.get("description") or ""
    author = metadata.get("author") or ""
    cover_image_url = metadata.get("cover_image_url") or ""
    story_args = build_story_metadata_args(args)

    print(f"[STORY] translating title: {source_title}", flush=True)
    vi_title = translate_story_title(source_title, story_args)
    vi_description = ""
    if source_description:
        print(f"[STORY] translating description chars={len(source_description)}", flush=True)
        vi_description = translate_story_description(source_description, story_args)
    vi_author = translate_story_author(author, story_args) if author else ""

    output_path = (
        Path(args.story_metadata_output)
        if args.story_metadata_output
        else Path(args.catalog_output_root) / "skydemonorder" / args.target_slug / "story_metadata.vi.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            {
                "source": "skydemonorder",
                "project_url": args.project_url,
                "source_title": source_title,
                "title_vi": vi_title,
                "source_description": source_description,
                "description_vi": vi_description,
                "author": author,
                "author_vi": vi_author,
                "cover_image_url": cover_image_url,
                "model": args.story_model or args.translate_model,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"[STORY] metadata saved: {output_path}", flush=True)

    if args.story_id:
        update_story_translation(
            args.story_id,
            display_title=vi_title,
            author=vi_author or None,
            description=vi_description or None,
            original_description=source_description or None,
            model=args.story_model or args.translate_model,
        )
        print(f"[STORY] DB updated story_id={args.story_id}", flush=True)


def upsert_target_story(page: Any, args: argparse.Namespace, project_slug: str) -> dict[str, Any]:
    if args.story_id:
        story = repo.get_story_by_id(args.story_id)
        if args.translate_story_metadata:
            maybe_translate_story_metadata(page, args, project_slug)
        print(f"[DB] using existing story_id={story['id']}", flush=True)
        return story

    repo.upsert_source("skydemonorder", "Sky Demon Order", "https://skydemonorder.com")
    metadata = extract_project_metadata(page)
    source_title = metadata.get("title") or project_slug.replace("-", " ").title()
    source_description = metadata.get("description") or None
    source_author = metadata.get("author") or None
    author = source_author
    cover_image_url = metadata.get("cover_image_url") or None
    display_title = None
    description = source_description

    if args.translate_story_metadata:
        story_args = build_story_metadata_args(args)
        print(f"[STORY] translating title: {source_title}", flush=True)
        display_title = translate_story_title(source_title, story_args)
        if source_author:
            author = translate_story_author(source_author, story_args)
        if source_description:
            print(f"[STORY] translating description chars={len(source_description)}", flush=True)
            description = translate_story_description(source_description, story_args)

    story = repo.upsert_story(
        "skydemonorder",
        {
            "source_story_id": project_slug,
            "title": source_title,
            "original_title": source_title,
            "display_title": display_title,
            "author": author,
            "category": None,
            "status": None,
            "language": "en",
            "source_url": args.project_url,
            "catalog_url": args.project_url,
            "description": description,
            "cover_image_url": cover_image_url,
            "metadata": {
                "slug": args.target_slug,
                "source": "skydemonorder",
                "source_description": source_description,
                "source_author": source_author,
                "story_author_translated_to": "vi" if args.translate_story_metadata and source_author else None,
                "source_cover_image_url": cover_image_url,
                "story_metadata_translated_to": "vi" if args.translate_story_metadata else None,
                "story_metadata_translation_model": (args.story_model or args.translate_model)
                if args.translate_story_metadata
                else None,
            },
        },
    )
    print(f"[DB] upserted story_id={story['id']} title={story['title']}", flush=True)

    if args.translate_story_metadata:
        output_path = (
            Path(args.story_metadata_output)
            if args.story_metadata_output
            else Path(args.catalog_output_root) / "skydemonorder" / args.target_slug / "story_metadata.vi.json"
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(
                {
                    "source": "skydemonorder",
                    "project_url": args.project_url,
                    "source_title": source_title,
                    "title_vi": display_title,
                    "source_description": source_description or "",
                    "description_vi": description or "",
                    "source_author": source_author or "",
                    "author_vi": author or "",
                    "cover_image_url": cover_image_url or "",
                    "model": args.story_model or args.translate_model,
                    "story_id": str(story["id"]),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"[STORY] metadata saved: {output_path}", flush=True)
    return story


def write_outputs(raw_path: Path, chapter_number: int, args: argparse.Namespace) -> tuple[Path | None, Path | None]:
    if not args.translate_inline:
        return None, None
    translated_path = Path(args.translated_output_root) / args.target_slug / f"chapter{chapter_number:04d}.txt"
    if translated_path.exists() and not args.overwrite_translation:
        print(f"[SKIP] translated exists: {translated_path}", flush=True)
    else:
        translate_file(raw_path, translated_path, build_translate_args(args))

    if args.post_translate == "copy":
        polished_path = Path(args.polished_output_root) / args.target_slug / translated_path.name
        polished_path.parent.mkdir(parents=True, exist_ok=True)
        polished_path.write_text(translated_path.read_text(encoding="utf-8").strip() + "\n", encoding="utf-8")
        print(f"[COPY] translated -> polished: {polished_path}", flush=True)
        return translated_path, polished_path
    return translated_path, None


def maybe_update_chapter_db(
    target_story: dict[str, Any] | None,
    args: argparse.Namespace,
    *,
    chapter_number: int,
    title: str,
    source_url: str,
    raw_path: Path,
    translated_path: Path | None,
    polished_path: Path | None,
) -> None:
    if target_story is None:
        return
    raw_text = raw_path.read_text(encoding="utf-8")
    db_chapter = upsert_downloaded_chapter(
        target_story,
        source_chapter_id=f"skydemonorder:{chapter_number}",
        chapter_number=chapter_number,
        title=title,
        source_url=source_url,
        raw_language="en",
        raw_path=raw_path,
        raw_text_content=raw_text,
        volume=None,
    )
    repo.update_chapter_text_outputs(
        db_chapter["id"],
        translated_text_path=translated_path.as_posix() if translated_path else None,
        polished_text_path=polished_path.as_posix() if polished_path else None,
        raw_text_content=raw_text,
        translated_text_content=translated_path.read_text(encoding="utf-8") if translated_path and translated_path.exists() else None,
        polished_text_content=polished_path.read_text(encoding="utf-8") if polished_path and polished_path.exists() else None,
    )
    print(f"[DB] updated chapter={chapter_number} story_id={target_story['id']}", flush=True)
    if args.enqueue_polish:
        enqueue_polish_for_args("skydemonorder", target_story, db_chapter, args.target_slug, raw_path, "en", args)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Crawl Sky Demon Order chapters with Playwright and optionally translate them with Ollama."
    )
    parser.add_argument("--project-url", required=True)
    parser.add_argument("--target-slug", default="")
    parser.add_argument("--from-chapter", type=int, default=1)
    parser.add_argument("--to-chapter", type=int, default=0)
    parser.add_argument("--max-chapters", type=int, default=0)
    parser.add_argument("--start-url", default="", help="Optional chapter URL to start from instead of project start link.")
    parser.add_argument("--profile-dir", default=".browser/skydemonorder")
    parser.add_argument("--headful", action="store_true")
    parser.add_argument("--manual-wait", type=int, default=0, help="Seconds to wait on first page for manual Cloudflare/login handling.")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--wait-ms", type=int, default=1500)
    parser.add_argument("--chapter-delay", type=float, default=0.5)
    parser.add_argument("--min-text-chars", type=int, default=500)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--raw-en-output-root", default="story_data/raw_en")
    parser.add_argument("--enqueue-polish", action="store_true", help="Enqueue polish_chapter jobs after saving raw chapters.")
    parser.add_argument("--translate-inline", action="store_true")
    parser.add_argument("--translated-output-root", default="story_data/translated")
    parser.add_argument("--polished-output-root", default="story_data/polished")
    parser.add_argument("--vi-model", default="qwen3:14b")
    parser.add_argument("--translate-model", default="translategemma:12b")
    parser.add_argument("--story-model", default="", help="Model riêng cho title/description; mặc định dùng --translate-model.")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--num-ctx", type=int, default=8192)
    parser.add_argument("--ollama-timeout", type=int, default=300)
    parser.add_argument("--ollama-retries", type=int, default=3)
    parser.add_argument("--keep-alive", default="24h")
    parser.add_argument("--translate-max-chars-per-chunk", type=int, default=2500)
    parser.add_argument("--char-map-file", default="", help="Override character map file; mặc định tự tìm theo story id/target slug.")
    parser.add_argument("--overwrite-translation", action="store_true")
    parser.add_argument("--post-translate", choices=("polish", "copy", "none"), default="copy")
    parser.add_argument("--polish-max-attempts", type=int, default=3)
    parser.add_argument("--translate-story-metadata", action="store_true")
    parser.add_argument("--story-id", default="", help="Nếu có, update display_title/description vào DB story này.")
    parser.add_argument("--catalog-output-root", default="story_data/catalogs")
    parser.add_argument("--story-metadata-output", default="")
    args = parser.parse_args()

    project_slug = safe_slug(urlparse(args.project_url).path.rstrip("/").rsplit("/", 1)[-1])
    if not args.target_slug:
        args.target_slug = project_slug
    output_dir = Path(args.raw_en_output_root) / args.target_slug / f"from_skydemonorder_{project_slug}"
    output_dir.mkdir(parents=True, exist_ok=True)
    target_story: dict[str, Any] | None = None

    sync_playwright, PlaywrightTimeoutError = import_playwright()
    profile_dir = Path(args.profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=profile_dir.as_posix(),
            headless=not args.headful,
            viewport={"width": 1366, "height": 900},
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            args=["--disable-blink-features=AutomationControlled", "--lang=en-US"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        next_url = args.start_url or args.project_url
        imported = 0
        skipped = 0
        failed = 0
        visited: set[str] = set()
        first_page = True

        while next_url:
            if next_url in visited:
                print(f"[STOP] repeated URL: {next_url}", flush=True)
                break
            visited.add(next_url)

            try:
                page.goto(next_url, wait_until="domcontentloaded", timeout=args.timeout * 1000)
                if first_page and args.manual_wait > 0:
                    print(f"[WAIT] manual wait {args.manual_wait}s: {page.url}", flush=True)
                    page.wait_for_timeout(args.manual_wait * 1000)
                first_page = False
                if args.wait_ms > 0:
                    page.wait_for_timeout(args.wait_ms)

                if "/projects/" in page.url and chapter_number_from_url(page.url) == 0:
                    if target_story is None:
                        target_story = upsert_target_story(page, args, project_slug)
                    start_link = page.get_by_text("Start Reading", exact=True)
                    if start_link.count() > 0:
                        start_link.first.click(timeout=10_000)
                        page.wait_for_load_state("domcontentloaded", timeout=args.timeout * 1000)
                        if args.wait_ms > 0:
                            page.wait_for_timeout(args.wait_ms)
                elif target_story is None:
                    if args.story_id:
                        target_story = repo.get_story_by_id(args.story_id)
                    else:
                        repo.upsert_source("skydemonorder", "Sky Demon Order", "https://skydemonorder.com")
                        target_story = repo.upsert_story(
                            "skydemonorder",
                            {
                                "source_story_id": project_slug,
                                "title": project_slug.replace("-", " ").title(),
                                "original_title": project_slug.replace("-", " ").title(),
                                "language": "en",
                                "source_url": args.project_url,
                                "catalog_url": args.project_url,
                                "metadata": {"slug": args.target_slug, "source": "skydemonorder"},
                            },
                        )

                number, title, content, found_next = extract_chapter(page)
                next_url = found_next
                if not number:
                    failed += 1
                    print(f"[WARN] cannot detect chapter number url={page.url}", flush=True)
                    break
                if args.to_chapter and number > args.to_chapter:
                    break
                if number < args.from_chapter:
                    print(f"[SKIP] before range chapter={number}", flush=True)
                    time.sleep(args.chapter_delay)
                    continue

                raw_path = output_dir / f"chapter{number:04d}.txt"
                if raw_path.exists() and raw_path.stat().st_size > 0 and not args.overwrite:
                    skipped += 1
                    print(f"[SKIP] exists chapter={number} path={raw_path}", flush=True)
                    translated_path = Path(args.translated_output_root) / args.target_slug / raw_path.name
                    polished_path = Path(args.polished_output_root) / args.target_slug / raw_path.name
                    maybe_update_chapter_db(
                        target_story,
                        args,
                        chapter_number=number,
                        title=title,
                        source_url=page.url,
                        raw_path=raw_path,
                        translated_path=translated_path if translated_path.exists() else None,
                        polished_path=polished_path if polished_path.exists() else None,
                    )
                elif len(content) < args.min_text_chars:
                    skipped += 1
                    print(f"[SKIP] short chapter={number} chars={len(content)} url={page.url}", flush=True)
                elif looks_blocked(content):
                    skipped += 1
                    print(f"[SKIP] locked/paywall chapter={number} url={page.url}", flush=True)
                else:
                    raw_path.write_text(f"{title}\n\n{content}\n", encoding="utf-8")
                    imported += 1
                    print(f"[OK] chapter={number} chars={len(content)} path={raw_path}", flush=True)
                    translated_path, polished_path = write_outputs(raw_path, number, args)
                    maybe_update_chapter_db(
                        target_story,
                        args,
                        chapter_number=number,
                        title=title,
                        source_url=page.url,
                        raw_path=raw_path,
                        translated_path=translated_path,
                        polished_path=polished_path,
                    )

                if args.max_chapters and imported >= args.max_chapters:
                    break
                time.sleep(args.chapter_delay)
            except PlaywrightTimeoutError as exc:
                failed += 1
                print(f"[WARN] timeout url={next_url}: {exc}", flush=True)
                break
            except Exception as exc:
                failed += 1
                print(f"[WARN] failed url={next_url}: {type(exc).__name__}: {exc}", flush=True)
                break

        context.close()

    print(
        f"[DONE] imported={imported} skipped={skipped} failed={failed} output_dir={output_dir}",
        flush=True,
    )


if __name__ == "__main__":
    main()
