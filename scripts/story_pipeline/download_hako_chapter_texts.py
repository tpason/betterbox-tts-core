#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import html
import json
import re
import sys
import time
from argparse import Namespace
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

from genre_prompts import find_char_map_file, resolve_genre_from_context
from polish_chapter_texts_ollama import polish_file

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


CONTENT_SELECTORS = [
    "#chapter-content",
    ".chapter-content",
    ".reading-content",
    ".rdtext",
    ".chapter-c",
    ".chr-c",
    ".chapter-content-inner",
    "article",
]

LOCK_PATTERNS = [
    "cần đăng nhập",
    "vui lòng đăng nhập",
    "chương này bị khóa",
    "chương đã bị khóa",
    "locked",
    "login",
]


def clean_text(value: str) -> str:
    value = value.replace("\xa0", " ")
    lines = []
    for line in value.splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line:
            lines.append(line)
    return "\n\n".join(lines).strip()


def looks_locked(text: str) -> bool:
    lowered = text.lower()
    return any(pattern in lowered for pattern in LOCK_PATTERNS)


def decode_hako_protected_content(node: Any) -> str:
    scheme = node.get("data-s") or "none"
    key = node.get("data-k") or ""
    try:
        chunks = json.loads(html.unescape(node.get("data-c") or "[]"))
    except json.JSONDecodeError:
        return ""
    if not isinstance(chunks, list):
        return ""

    decoded_chunks: list[str] = []
    for chunk in sorted(chunks, key=lambda item: int(str(item)[:4] or 0)):
        chunk = str(chunk)
        payload = chunk[4:]
        if scheme == "base64_reverse":
            payload = payload[::-1]
        raw = base64.b64decode(payload)
        if scheme == "xor_shuffle" and key:
            key_bytes = key.encode("utf-8")
            raw = bytes(value ^ key_bytes[index % len(key_bytes)] for index, value in enumerate(raw))
        decoded_chunks.append(raw.decode("utf-8", errors="replace"))

    fragment = BeautifulSoup("".join(decoded_chunks), "html.parser")
    return clean_text(fragment.get_text("\n", strip=True))


def pick_content(soup: BeautifulSoup) -> str:
    for tag_name in ["script", "style", "noscript", "nav", "header", "footer", "form"]:
        for tag in soup.select(tag_name):
            tag.decompose()

    protected = soup.select_one("#chapter-c-protected[data-c]")
    if protected:
        text = decode_hako_protected_content(protected)
        if len(text) >= 200:
            return text

    for selector in CONTENT_SELECTORS:
        node = soup.select_one(selector)
        if node:
            protected = node.select_one("#chapter-c-protected[data-c]")
            if protected:
                text = decode_hako_protected_content(protected)
                if len(text) >= 200:
                    return text
            text = clean_text(node.get_text("\n", strip=True))
            if len(text) >= 200:
                return text

    candidates: list[str] = []
    for node in soup.select("main, .container, .content, .section, div"):
        text = clean_text(node.get_text("\n", strip=True))
        if len(text) >= 200:
            candidates.append(text)
    return max(candidates, key=len, default="")


def fetch_chapter(url: str, timeout: int, retries: int, retry_sleep: float) -> tuple[str, str]:
    headers = {"User-Agent": "Mozilla/5.0 BetterBox-TTS story crawler"}
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                try:
                    sleep_seconds = float(retry_after) if retry_after else retry_sleep * attempt * 5
                except ValueError:
                    sleep_seconds = retry_sleep * attempt * 5
                print(f"rate limited 429: sleep {sleep_seconds:.1f}s then retry {attempt}/{retries - 1}: {url}")
                time.sleep(sleep_seconds)
                continue
            response.raise_for_status()
            break
        except requests.RequestException as exc:
            last_error = exc
            if attempt >= retries:
                raise
            sleep_seconds = retry_sleep * attempt
            print(f"retry {attempt}/{retries - 1} after network error: {url} ({exc})")
            time.sleep(sleep_seconds)
    else:
        raise RuntimeError(f"Cannot fetch chapter: {last_error}")

    soup = BeautifulSoup(response.text, "html.parser")
    title_node = soup.select_one("h1, .chapter-title, .title")
    title = clean_text(title_node.get_text(" ", strip=True)) if title_node else ""
    content = pick_content(soup)
    return title, content


def chapter_filename(chapter: dict[str, Any], fallback: int) -> str:
    number = chapter.get("number") or fallback
    try:
        number_int = int(number)
    except (TypeError, ValueError):
        number_int = fallback
    return f"chapter{number_int:04d}.txt"


def has_existing_text(path: Path) -> bool:
    return path.exists() and path.is_file() and path.stat().st_size > 0


def emit_polish_job(
    catalog: dict[str, Any],
    chapter: dict[str, Any],
    raw_path: Path,
    polished_path: Path,
    model: str,
    max_attempts: int,
    overwrite: bool,
    char_map_file: str = "",
    raw_text_content: str | None = None,
) -> None:
    from story_db.story_pipeline_db import repository as repo

    chapter_number = int(chapter.get("number") or 0)
    chapter_stem = f"chapter{chapter_number:04d}"
    repo.upsert_source("hako", "Hako", "https://ln.hako.vn")
    story = repo.upsert_story(
        "hako",
        {
            "source_story_id": catalog.get("slug"),
            "title": catalog.get("title") or catalog.get("slug") or "hako_story",
            "author": catalog.get("author"),
            "language": "vi",
            "source_url": catalog.get("story_url"),
            "catalog_url": catalog.get("story_url"),
            "total_chapters": catalog.get("chapter_count") or len(catalog.get("chapters") or []),
            "cover_image_url": catalog.get("cover_image_url"),
            "metadata": {"slug": catalog.get("slug"), "source": "hako"},
        },
    )
    content = raw_text_content
    db_chapter = repo.upsert_chapter(
        story["id"],
        {
            "source_chapter_id": str(chapter.get("number") or ""),
            "chapter_number": chapter_number,
            "title": chapter.get("title") or chapter_stem,
            "source_url": chapter.get("url") or "",
            "is_locked": bool(chapter.get("is_locked")),
            "raw_language": "vi",
            "raw_text_path": None,
            "raw_text_content": content,
            "is_downloaded": bool(content),
        },
    )

    if db_chapter.get("is_polished") and not overwrite:
        print(f"[SKIP] DB polish job, chapter already polished: {chapter_stem}")
        return

    effective_char_map = char_map_file or find_char_map_file(story_id=str(story["id"]), slug=str(catalog.get("slug") or ""))
    category = str(catalog.get("category") or catalog.get("genre") or " ".join(catalog.get("tags") or []))
    job = repo.enqueue_chapter_job(
        "polish_chapter",
        db_chapter["id"],
        story_id=story["id"],
        source_code="hako",
        model=model,
        input_path=None,
        output_path=None,
        payload={
            "raw_language": "vi",
            "story_slug": catalog.get("slug"),
            "chapter_number": chapter.get("number"),
            "genre": resolve_genre_from_context(
                category,
                raw_language="vi",
                source_code="hako",
                char_map_file=effective_char_map,
            ),
            "char_map_file": effective_char_map,
        },
        max_attempts=max_attempts,
    )
    print(f"[JOB] polish_chapter {job['status']}: {chapter_stem}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download public Hako chapter text from a chapters.json catalog."
    )
    parser.add_argument("catalog_json", help="Path to story_data/hako/<slug>/chapters.json")
    parser.add_argument("--output-root", default="story_data/text")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--retry-sleep", type=float, default=2.0, help="Base seconds. Backoff is retry_sleep * attempt.")
    parser.add_argument("--delay", type=float, default=1.5, help="Seconds to sleep between downloaded chapter requests.")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--max-chapters", type=int, default=0, help="0 means all chapters.")
    parser.add_argument(
        "--polish-with-ollama",
        action="store_true",
        help="After downloading each raw chapter, create a polished copy for TTS.",
    )
    parser.add_argument("--polish-output-root", default="story_data/polished")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--polish-model", default="qwen3:14b")
    parser.add_argument("--polish-temperature", type=float, default=0.25)
    parser.add_argument("--polish-num-ctx", type=int, default=8192)
    parser.add_argument("--polish-timeout", type=int, default=300)
    parser.add_argument("--polish-retries", type=int, default=3)
    parser.add_argument("--polish-max-chars-per-chunk", type=int, default=3500)
    parser.add_argument("--char-map-file", default="", help="Override character map file; mặc định tự tìm theo story slug/DB story id.")
    parser.add_argument(
        "--emit-polish-job",
        action="store_true",
        help="Only enqueue polish job in Postgres after saving raw chapter. Worker handles Ollama.",
    )
    parser.add_argument("--db-polish-output-root", default="story_data/polished")
    parser.add_argument("--db-polish-model", default="qwen3:14b")
    parser.add_argument("--db-polish-max-attempts", type=int, default=3)
    args = parser.parse_args()

    catalog_path = Path(args.catalog_json)
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    slug = catalog.get("slug") or catalog_path.parent.name
    output_dir = Path(args.output_root) / slug
    output_dir.mkdir(parents=True, exist_ok=True)
    effective_char_map = args.char_map_file or find_char_map_file(slug=slug)

    chapters = catalog.get("chapters") or []
    report: list[dict[str, Any]] = []
    downloaded = 0
    skipped = 0

    for index, chapter in enumerate(chapters, start=1):
        if args.max_chapters and downloaded >= args.max_chapters:
            break

        output_path = output_dir / chapter_filename(chapter, index)
        polished_path = Path(args.db_polish_output_root) / slug / output_path.name

        url = chapter.get("url")
        if not url:
            skipped += 1
            report.append({"path": output_path.as_posix(), "status": "missing_url"})
            continue

        try:
            title, content = fetch_chapter(url, args.timeout, args.retries, args.retry_sleep)
        except Exception as exc:
            skipped += 1
            report.append({"url": url, "path": output_path.as_posix(), "status": "error", "error": str(exc)})
            print(f"skip {url}: {exc}")
            continue

        if not content or len(content) < 200 or looks_locked(content):
            skipped += 1
            report.append({"url": url, "path": output_path.as_posix(), "status": "locked_or_empty"})
            print(f"skip locked/empty: {url}")
            continue

        heading = title or chapter.get("title") or f"Chapter {chapter.get('number') or index}"
        full_content = f"{heading}\n\n{content}\n"
        downloaded += 1
        report.append({"url": url, "status": "downloaded"})
        print(f"saved chapter to DB: {output_path.name}")

        if args.emit_polish_job:
            emit_polish_job(
                catalog,
                chapter,
                output_path,
                polished_path,
                args.db_polish_model,
                args.db_polish_max_attempts,
                args.overwrite,
                effective_char_map,
                raw_text_content=full_content,
            )

        if args.polish_with_ollama:
            polished_path = Path(args.polish_output_root) / slug / output_path.name
            if polished_path.exists() and not args.overwrite:
                print(f"[SKIP] Polished đã tồn tại: {polished_path}")
            else:
                polish_args = Namespace(
                    ollama_url=args.ollama_url,
                    model=args.polish_model,
                    temperature=args.polish_temperature,
                    num_ctx=args.polish_num_ctx,
                    timeout=args.polish_timeout,
                    retries=args.polish_retries,
                    max_chars_per_chunk=args.polish_max_chars_per_chunk,
                    char_map_file=effective_char_map,
                )
                try:
                    polish_file(output_path, polished_path, polish_args)
                    report.append(
                        {
                            "url": url,
                            "path": polished_path.as_posix(),
                            "status": "polished",
                            "model": args.polish_model,
                        }
                    )
                except Exception as exc:
                    report.append(
                        {
                            "url": url,
                            "path": polished_path.as_posix(),
                            "status": "polish_error",
                            "error": str(exc),
                        }
                    )
                    print(f"[WARN] polish failed {output_path}: {exc}")

        if args.delay > 0:
            time.sleep(args.delay)

    report_path = output_dir / "_download_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nDone. downloaded={downloaded}, skipped={skipped}, report={report_path}")


if __name__ == "__main__":
    main()
