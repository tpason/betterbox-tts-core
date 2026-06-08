#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from argparse import Namespace
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from genre_prompts import find_char_map_file, resolve_genre_from_context
from polish_chapter_texts_ollama import polish_file
from translate_chapter_texts_ollama import translate_file

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def safe_slug(value: str) -> str:
    value = clean_text(value)
    value = re.sub(r"[\\/:*?\"<>|]+", "_", value)
    value = re.sub(r"\s+", "_", value)
    return value.strip("._") or "qidian_book"


def fetch_html(url: str, timeout: int = 30, retries: int = 3, retry_sleep: float = 2.0) -> str:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, headers=HEADERS, timeout=timeout)
            if response.status_code in {401, 403, 451}:
                raise PermissionError(f"Không có quyền truy cập public: HTTP {response.status_code}")
            if response.status_code in {408, 425, 429, 500, 502, 503, 504}:
                response.raise_for_status()
            response.raise_for_status()
            return response.text
        except PermissionError:
            raise
        except requests.RequestException as exc:
            last_error = exc
            if attempt < retries:
                print(f"[WARN] qidian public retry {attempt}/{retries}: {url} | {exc}", flush=True)
                time.sleep(retry_sleep * attempt)
    raise RuntimeError(f"Cannot fetch Qidian public URL after {retries} attempts: {url} | {last_error}") from last_error


def extract_chapter_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    for selector in (
        ".chapter-content",
        ".read-content",
        ".main-text-wrap .text-wrap",
        ".content-wrap",
        "article",
    ):
        node = soup.select_one(selector)
        if node is None:
            continue
        paragraphs = [clean_text(p.get_text(" ", strip=True)) for p in node.select("p")]
        paragraphs = [p for p in paragraphs if p]
        if paragraphs:
            return "\n\n".join(paragraphs)

    paragraphs = [
        clean_text(p.get_text(" ", strip=True))
        for p in soup.select("p")
        if len(clean_text(p.get_text(" ", strip=True))) > 20
    ]
    if paragraphs:
        return "\n\n".join(paragraphs)

    page_text = clean_text(soup.get_text(" ", strip=True))
    locked_markers = ("订阅", "付费", "VIP", "登录", "本章未完")
    if any(marker in page_text for marker in locked_markers):
        raise PermissionError("Chapter có vẻ bị khóa/VIP/login, skip.")

    raise ValueError("Không tìm thấy nội dung chapter public trong HTML.")


def emit_polish_job(
    manifest: dict,
    chapter: dict,
    story_dir_name: str,
    raw_path: Path,
    polished_path: Path,
    model: str,
    max_attempts: int,
    overwrite: bool,
    char_map_file: str = "",
) -> None:
    from story_db.story_pipeline_db import repository as repo

    repo.upsert_source("qidian", "Qidian", "https://www.qidian.com")
    story = repo.upsert_story(
        "qidian",
        {
            "source_story_id": manifest.get("book_id"),
            "title": manifest.get("title") or story_dir_name,
            "original_title": manifest.get("title"),
            "author": manifest.get("author"),
            "language": "zh",
            "source_url": manifest.get("book_url") or manifest.get("source_url") or manifest.get("catalog_url"),
            "catalog_url": manifest.get("catalog_url"),
            "total_chapters": manifest.get("total_chapters") or len(manifest.get("chapters") or []),
            "free_chapters": manifest.get("free_chapters") or 0,
            "locked_chapters": manifest.get("vip_chapters") or 0,
            "metadata": {"book_id": manifest.get("book_id"), "source": "qidian"},
        },
    )
    position = int(chapter.get("position") or 0)
    db_chapter = repo.upsert_chapter(
        story["id"],
        {
            "source_chapter_id": str(position),
            "chapter_number": position,
            "title": chapter.get("title") or raw_path.stem,
            "source_url": chapter.get("url") or "",
            "volume": chapter.get("volume"),
            "is_locked": False,
            "raw_language": "zh",
            "raw_text_path": raw_path.as_posix(),
            "raw_text_content": raw_path.read_text(encoding="utf-8") if raw_path.exists() else None,
            "is_downloaded": True,
        },
    )
    if polished_path.exists() and not overwrite:
        repo.update_chapter_text_outputs(
            db_chapter["id"],
            translated_text_path=polished_path.as_posix(),
            polished_text_path=polished_path.as_posix(),
            translated_text_content=polished_path.read_text(encoding="utf-8"),
            polished_text_content=polished_path.read_text(encoding="utf-8"),
        )
        print(f"[SKIP] DB polish job, polished exists: {polished_path}")
        return
    if db_chapter.get("is_polished") and not overwrite:
        print(f"[SKIP] DB polish job, chapter already polished: {raw_path.name}")
        return
    effective_char_map = char_map_file or find_char_map_file(story_id=str(story["id"]), slug=story_dir_name)
    category = str(manifest.get("category") or manifest.get("genre") or " ".join(manifest.get("tags") or []))
    job = repo.enqueue_chapter_job(
        "polish_chapter",
        db_chapter["id"],
        story_id=story["id"],
        source_code="qidian",
        model=model,
        input_path=raw_path.as_posix(),
        output_path=polished_path.as_posix(),
        payload={
            "raw_language": "zh",
            "story_slug": story_dir_name,
            "chapter_number": position,
            "genre": resolve_genre_from_context(
                category,
                raw_language="zh",
                source_code="qidian",
                char_map_file=effective_char_map,
            ),
            "char_map_file": effective_char_map,
        },
        max_attempts=max_attempts,
    )
    print(f"[JOB] polish_chapter {job['status']}: {raw_path.name}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Tải các chapter public từ Qidian catalog manifest. "
            "Script skip VIP/locked chapters và không bypass paywall."
        )
    )
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output-root", default="story_data/raw_zh")
    parser.add_argument("--limit", type=int, default=0, help="0 nghĩa là thử toàn bộ chapter public.")
    parser.add_argument("--delay", type=float, default=1.5)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--include-vip-marked", action="store_true", help="Chỉ thử nếu chapter bị mark VIP nhưng vẫn public.")
    parser.add_argument("--translate-with-ollama", action="store_true")
    parser.add_argument("--translate-output-root", default="story_data/translated")
    parser.add_argument("--translate-model", default="qwen3:14b")
    parser.add_argument("--translate-temperature", type=float, default=0.2)
    parser.add_argument("--translate-num-ctx", type=int, default=8192)
    parser.add_argument("--translate-timeout", type=int, default=300)
    parser.add_argument("--translate-retries", type=int, default=3)
    parser.add_argument("--translate-max-chars-per-chunk", type=int, default=2500)
    parser.add_argument("--polish-with-ollama", action="store_true")
    parser.add_argument("--polish-output-root", default="story_data/polished")
    parser.add_argument("--polish-model", default="qwen3:14b")
    parser.add_argument("--polish-temperature", type=float, default=0.25)
    parser.add_argument("--polish-num-ctx", type=int, default=8192)
    parser.add_argument("--polish-timeout", type=int, default=300)
    parser.add_argument("--polish-retries", type=int, default=3)
    parser.add_argument("--polish-max-chars-per-chunk", type=int, default=3500)
    parser.add_argument("--char-map-file", default="", help="Override character map file; mặc định tự tìm theo story slug/DB story id.")
    parser.add_argument("--emit-polish-job", action="store_true")
    parser.add_argument("--db-polish-output-root", default="story_data/polished")
    parser.add_argument("--db-polish-model", default="qwen3:14b")
    parser.add_argument("--db-polish-max-attempts", type=int, default=3)
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    chapters = manifest.get("chapters") or []
    if not chapters:
        raise SystemExit(f"Manifest không có chapter: {manifest_path}")

    story_dir_name = safe_slug(manifest.get("title") or manifest.get("book_id") or "qidian_book")
    output_dir = Path(args.output_root) / story_dir_name
    output_dir.mkdir(parents=True, exist_ok=True)
    effective_char_map = args.char_map_file or find_char_map_file(slug=story_dir_name)

    candidates = [
        chapter for chapter in chapters
        if args.include_vip_marked or not chapter.get("is_vip")
    ]
    if args.limit:
        candidates = candidates[: args.limit]

    saved = 0
    skipped = 0
    report: dict = {
        "manifest": str(manifest_path),
        "book_id": manifest.get("book_id"),
        "title": manifest.get("title"),
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "output_dir": str(output_dir),
        "saved": [],
        "skipped": [],
    }
    for chapter in candidates:
        position = int(chapter.get("position") or saved + skipped + 1)
        target_path = output_dir / f"chapter{position}.txt"
        polished_path = Path(args.db_polish_output_root) / story_dir_name / target_path.name
        if target_path.exists() and not args.overwrite:
            print(f"[SKIP] Đã tồn tại: {target_path}")
            if args.emit_polish_job:
                emit_polish_job(
                    manifest,
                    chapter,
                    story_dir_name,
                    target_path,
                    polished_path,
                    args.db_polish_model,
                    args.db_polish_max_attempts,
                    args.overwrite,
                    effective_char_map,
                )
            report["skipped"].append({
                "position": position,
                "title": chapter.get("title"),
                "url": chapter.get("url"),
                "reason": "exists",
                "target_path": str(target_path),
            })
            skipped += 1
            continue

        title = chapter.get("title") or f"chapter{position}"
        url = chapter.get("url")
        if not url:
            skipped += 1
            continue

        print(f"Đang tải chapter{position}: {title}")
        try:
            html = fetch_html(url)
            content = extract_chapter_text(html)
            text = f"{title}\n\n{content.strip()}\n"
            target_path.write_text(text, encoding="utf-8")
            if args.emit_polish_job:
                emit_polish_job(
                    manifest,
                    chapter,
                    story_dir_name,
                    target_path,
                    polished_path,
                    args.db_polish_model,
                    args.db_polish_max_attempts,
                    args.overwrite,
                    effective_char_map,
                )
            saved_item = {
                "position": position,
                "title": title,
                "url": url,
                "target_path": str(target_path),
            }

            next_text_path = target_path
            if args.translate_with_ollama:
                translated_path = Path(args.translate_output_root) / story_dir_name / target_path.name
                if translated_path.exists() and not args.overwrite:
                    print(f"[SKIP] Translated đã tồn tại: {translated_path}")
                else:
                    translate_file(
                        target_path,
                        translated_path,
                        Namespace(
                            ollama_url=args.ollama_url,
                            model=args.translate_model,
                            temperature=args.translate_temperature,
                            num_ctx=args.translate_num_ctx,
                            timeout=args.translate_timeout,
                            retries=args.translate_retries,
                            max_chars_per_chunk=args.translate_max_chars_per_chunk,
                            char_map_file=effective_char_map,
                        ),
                    )
                next_text_path = translated_path
                saved_item["translated_path"] = str(translated_path)

            if args.polish_with_ollama:
                polished_path = Path(args.polish_output_root) / story_dir_name / target_path.name
                if polished_path.exists() and not args.overwrite:
                    print(f"[SKIP] Polished đã tồn tại: {polished_path}")
                else:
                    polish_file(
                        next_text_path,
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
                saved_item["polished_path"] = str(polished_path)

            report["saved"].append(saved_item)
            saved += 1
            time.sleep(args.delay)
        except Exception as exc:
            print(f"[SKIP] chapter{position}: {exc}")
            report["skipped"].append({
                "position": position,
                "title": title,
                "url": url,
                "reason": str(exc),
                "target_path": str(target_path),
            })
            skipped += 1

    report["saved_count"] = saved
    report["skipped_count"] = skipped
    report_path = output_dir / "_download_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Hoàn tất. saved={saved}, skipped={skipped}, output={output_dir}")
    print(f"Report: {report_path}")


if __name__ == "__main__":
    main()
