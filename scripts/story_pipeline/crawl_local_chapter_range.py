#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.story_pipeline.crawl_story_alternate_sources import (  # noqa: E402
    SOURCE_LANGUAGES,
    detect_source,
    fetch_text_for_source,
    parse_catalog_for_source,
    safe_slug,
)


def output_root_for_language(args: argparse.Namespace, raw_language: str) -> Path:
    if raw_language in {"zh", "cn"}:
        return Path(args.raw_zh_output_root)
    if raw_language == "en":
        return Path(args.raw_en_output_root)
    if raw_language in {"ko", "kr"}:
        return Path(args.raw_ko_output_root)
    return Path(args.text_output_root)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Crawl a chapter range from a supported alternate source into local text files without touching DB."
    )
    parser.add_argument("--alternate-url", required=True)
    parser.add_argument("--target-slug", required=True)
    parser.add_argument("--from-chapter", type=int, required=True)
    parser.add_argument("--to-chapter", type=int, required=True)
    parser.add_argument("--max-chapters", type=int, default=0)
    parser.add_argument("--latest-chapter", type=int, default=0)
    parser.add_argument("--raw-language", default="")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-sleep", type=float, default=2.0)
    parser.add_argument("--chapter-delay", type=float, default=1.0)
    parser.add_argument("--min-text-chars", type=int, default=500)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--catalog-output-root", default="story_data/catalogs")
    parser.add_argument("--text-output-root", default="story_data/text")
    parser.add_argument("--raw-zh-output-root", default="story_data/raw_zh")
    parser.add_argument("--raw-en-output-root", default="story_data/raw_en")
    parser.add_argument("--raw-ko-output-root", default="story_data/raw_ko")
    args = parser.parse_args()

    source_code, _, _ = detect_source(args.alternate_url)
    raw_language = args.raw_language or SOURCE_LANGUAGES.get(source_code, "vi")
    catalog = parse_catalog_for_source(source_code, args.alternate_url, args)
    source_slug = safe_slug(catalog.get("slug") or catalog.get("title") or source_code)

    manifest_path = (
        Path(args.catalog_output_root)
        / "alternate_sources"
        / args.target_slug
        / source_code
        / source_slug
        / "chapters.json"
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    output_dir = output_root_for_language(args, raw_language) / args.target_slug / f"from_{source_code}_{source_slug}"
    output_dir.mkdir(parents=True, exist_ok=True)

    imported = 0
    skipped = 0
    failed = 0
    for chapter in catalog.get("chapters") or []:
        number = int(chapter.get("number") or 0)
        if args.from_chapter and number < args.from_chapter:
            continue
        if args.to_chapter and number > args.to_chapter:
            continue
        output_path = output_dir / f"chapter{number:04d}.txt"
        if output_path.exists() and output_path.stat().st_size > 0 and not args.overwrite:
            skipped += 1
            print(f"[SKIP] exists chapter={number} path={output_path}", flush=True)
            continue

        try:
            text = fetch_text_for_source(source_code, chapter.get("url") or "", args)
            if len(text) < args.min_text_chars:
                skipped += 1
                print(f"[SKIP] short chapter={number} chars={len(text)} url={chapter.get('url')}", flush=True)
                continue
            title = chapter.get("title") or f"Chapter {number}"
            output_path.write_text(f"{title}\n\n{text.strip()}\n", encoding="utf-8")
            imported += 1
            print(f"[OK] chapter={number} chars={len(text)} path={output_path}", flush=True)
        except Exception as exc:
            failed += 1
            print(f"[WARN] failed chapter={number} url={chapter.get('url')}: {type(exc).__name__}: {exc}", flush=True)
        if args.chapter_delay > 0:
            time.sleep(args.chapter_delay)

    print(
        f"[DONE] source={source_code} raw_language={raw_language} imported={imported} "
        f"skipped={skipped} failed={failed} manifest={manifest_path} output_dir={output_dir}",
        flush=True,
    )


if __name__ == "__main__":
    main()
