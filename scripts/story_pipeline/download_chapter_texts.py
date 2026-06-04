#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from argparse import Namespace
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from genre_prompts import find_char_map_file, resolve_genre_from_context
from polish_chapter_texts_ollama import polish_file

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "vi,en-US;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


def story_slug(story_url: str) -> str:
    parsed = urlparse(story_url)
    slug = Path(parsed.path).parts[-1]
    return slug or "story"


def chapter_number_from_url(url: str) -> str | None:
    match = re.search(r"/chuong-(\d+)", url)
    return match.group(1) if match else None


def chapter_number_from_title(title: str) -> str | None:
    match = re.search(r"(\d+)", title)
    return match.group(1) if match else None


def resolve_chapter_number(title: str, url: str, fallback_index: int) -> str:
    return (
        chapter_number_from_url(url)
        or chapter_number_from_title(title)
        or str(fallback_index + 1)
    )


def fetch_chapter_text(
    url: str,
    *,
    timeout: int = 30,
    retries: int = 5,
    retry_sleep: float = 3.0,
) -> str:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, headers=HEADERS, timeout=timeout)
            if response.status_code in {429, 500, 502, 503, 504}:
                print(f"[WARN] Wattpad HTTP {response.status_code} attempt {attempt}/{retries}: {url}")
                if attempt < retries:
                    time.sleep(retry_sleep * attempt)
                    continue
                return ""
            response.raise_for_status()
            break
        except requests.RequestException as exc:
            last_error = exc
            print(f"[WARN] Wattpad network error attempt {attempt}/{retries}: {url} ({exc})")
            if attempt < retries:
                time.sleep(retry_sleep * attempt)
                continue
            raise
    else:
        raise RuntimeError(f"Cannot fetch Wattpad chapter: {last_error}")

    soup = BeautifulSoup(response.text, "html.parser")
    node = soup.select_one("#vungdoc > div.truyen")
    if node is None:
        raise ValueError(f"Không tìm thấy vùng nội dung #vungdoc > div.truyen: {url}")
    return node.get_text("\n", strip=True)


def emit_polish_job(
    manifest: dict,
    chapter: dict,
    chapter_number: int,
    raw_path: Path,
    polished_path: Path,
    model: str,
    max_attempts: int,
    overwrite: bool,
    char_map_file: str = "",
) -> None:
    from story_db.story_pipeline_db import repository as repo

    story_url = manifest["story_url"]
    slug = story_slug(story_url)
    repo.upsert_source("wattpad_vn", "Wattpad VN", "https://wattpad.com.vn")
    story = repo.upsert_story(
        "wattpad_vn",
        {
            "source_story_id": slug,
            "title": manifest.get("title") or slug,
            "author": manifest.get("author"),
            "language": "vi",
            "source_url": story_url,
            "catalog_url": story_url,
            "total_chapters": len(manifest.get("chapters") or []),
            "metadata": {"slug": slug, "source": "wattpad_vn"},
        },
    )
    db_chapter = repo.upsert_chapter(
        story["id"],
        {
            "source_chapter_id": str(chapter_number),
            "chapter_number": chapter_number,
            "title": chapter.get("title") or raw_path.stem,
            "source_url": chapter.get("url") or "",
            "raw_language": "vi",
            "raw_text_path": raw_path.as_posix(),
            "raw_text_content": raw_path.read_text(encoding="utf-8") if raw_path.exists() else None,
            "is_downloaded": True,
        },
    )
    if polished_path.exists() and not overwrite:
        repo.update_chapter_text_outputs(
            db_chapter["id"],
            polished_text_path=polished_path.as_posix(),
            polished_text_content=polished_path.read_text(encoding="utf-8"),
        )
        print(f"[SKIP] DB polish job, polished exists: {polished_path}")
        return
    if db_chapter.get("is_polished") and not overwrite:
        print(f"[SKIP] DB polish job, chapter already polished: {raw_path.name}")
        return
    effective_char_map = char_map_file or find_char_map_file(story_id=str(story["id"]), slug=slug)
    category = str(manifest.get("category") or manifest.get("genre") or " ".join(manifest.get("tags") or []))
    job = repo.enqueue_chapter_job(
        "polish_chapter",
        db_chapter["id"],
        story_id=story["id"],
        source_code="wattpad_vn",
        model=model,
        input_path=raw_path.as_posix(),
        output_path=polished_path.as_posix(),
        payload={
            "raw_language": "vi",
            "story_slug": slug,
            "chapter_number": chapter_number,
            "genre": resolve_genre_from_context(
                category,
                raw_language="vi",
                source_code="wattpad_vn",
                char_map_file=effective_char_map,
            ),
            "char_map_file": effective_char_map,
        },
        max_attempts=max_attempts,
    )
    print(f"[JOB] polish_chapter {job['status']}: {raw_path.name}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Tải text từng chapter từ file chapters.json.")
    parser.add_argument("--manifest", default="story_data/chapters.json")
    parser.add_argument("--output-root", default="story_data/text")
    parser.add_argument("--limit", type=int, default=0, help="0 nghĩa là tải toàn bộ.")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--polish-with-ollama", action="store_true")
    parser.add_argument("--polish-output-root", default="story_data/polished")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--polish-model", default="translategemma:12b")
    parser.add_argument("--polish-temperature", type=float, default=0.25)
    parser.add_argument("--polish-num-ctx", type=int, default=8192)
    parser.add_argument("--polish-timeout", type=int, default=300)
    parser.add_argument("--polish-retries", type=int, default=3)
    parser.add_argument("--polish-max-chars-per-chunk", type=int, default=3500)
    parser.add_argument("--char-map-file", default="", help="Override character map file; mặc định tự tìm theo story slug/DB story id.")
    parser.add_argument("--emit-polish-job", action="store_true")
    parser.add_argument("--db-polish-output-root", default="story_data/polished")
    parser.add_argument("--db-polish-model", default="qwen3:8b")
    parser.add_argument("--db-polish-max-attempts", type=int, default=3)
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    chapters = manifest.get("chapters") or []
    if not chapters:
        raise SystemExit(f"Manifest không có chapter: {manifest_path}")

    output_dir = Path(args.output_root) / story_slug(manifest["story_url"])
    output_dir.mkdir(parents=True, exist_ok=True)
    slug = output_dir.name
    effective_char_map = args.char_map_file or find_char_map_file(slug=slug)

    selected = chapters[: args.limit] if args.limit else chapters
    saved_count = 0

    for idx, chapter in enumerate(selected):
        url = chapter["url"]
        title = chapter.get("title", f"Chapter {idx + 1}")
        number = resolve_chapter_number(title, url, idx)
        target_path = output_dir / f"chapter{number}.txt"
        chapter_number = int(number)
        polished_path = Path(args.db_polish_output_root) / slug / target_path.name

        if target_path.exists() and not args.overwrite:
            print(f"[SKIP] {target_path}")
            if args.emit_polish_job:
                emit_polish_job(
                    manifest,
                    chapter,
                    chapter_number,
                    target_path,
                    polished_path,
                    args.db_polish_model,
                    args.db_polish_max_attempts,
                    args.overwrite,
                    effective_char_map,
                )
            continue

        print(f"Đang tải {title}: {url}")
        try:
            content = fetch_chapter_text(url)
            if not content:
                print(f"[WARN] Nội dung rỗng, bỏ qua chapter {number}")
                continue
            target_path.write_text(content, encoding="utf-8")
            saved_count += 1
            if args.emit_polish_job:
                emit_polish_job(
                    manifest,
                    chapter,
                    chapter_number,
                    target_path,
                    polished_path,
                    args.db_polish_model,
                    args.db_polish_max_attempts,
                    args.overwrite,
                    effective_char_map,
                )
            if args.polish_with_ollama:
                polished_path = Path(args.polish_output_root) / slug / target_path.name
                if polished_path.exists() and not args.overwrite:
                    print(f"[SKIP] Polished đã tồn tại: {polished_path}")
                else:
                    polish_file(
                        target_path,
                        polished_path,
                        Namespace(
                            ollama_url=args.ollama_url,
                            model=args.polish_model,
                            temperature=args.polish_temperature,
                            num_ctx=args.polish_num_ctx,
                            timeout=args.polish_timeout,
                            retries=args.polish_retries,
                            max_chars_per_chunk=args.polish_max_chars_per_chunk,
                            char_map_file=effective_char_map,
                        ),
                    )
        except Exception as exc:
            print(f"[ERROR] Chapter {number}: {exc}")

    print(f"Hoàn tất. Đã lưu {saved_count} file vào {output_dir}")


if __name__ == "__main__":
    main()
