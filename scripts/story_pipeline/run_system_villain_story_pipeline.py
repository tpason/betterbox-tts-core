#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DISCOVER_SCRIPT = ROOT / "scripts/story_pipeline/discover_system_villain_stories.py"
CRAWL_SCRIPT = ROOT / "scripts/story_pipeline/crawl_stories_from_db.py"

ALL_DB_CRAWLER_SOURCES = {
    "docln",
    "hako",
    "manhwatv",
    "qidian",
    "royalroad",
    "sttruyen",
    "truyenfull_today",
    "truyenchuhay",
    "truyenhoangdung",
    "truyenyy",
    "wattpad_vn",
}

DEFAULT_TEXT_CRAWL_SOURCES = {
    "sttruyen",
    "truyenfull_today",
    "truyenchuhay",
    "truyenhoangdung",
    "truyenyy",
    "wattpad_vn",
}

DEFAULT_PRODUCTION_SEED_URLS = [
    "https://truyenyy.co/he-thong",
    "https://truyenyy.co/truyen/dich-ta-thien-menh-dai-nhan-vat-phan-phai",
    "https://truyenyy.co/truyen/toan-tri-doc-gia",
    "https://www.truyenhoangdung.xyz/truyen/hoa-son-tai-khoi-dich.html",
    "https://truyenchuhay.org/ta-dinh-cap-de-toc-phan-phai-tran-sat-thien-menh-chi-nu",
    "https://truyenchuhay.vn/phan-phai-tu-hon-nguoi-xach-hien-tai-nguoi-khoc-cai-gi",
]


def run_command(command: list[str], *, dry_run: bool = False) -> None:
    print("[RUN] " + " ".join(command))
    if dry_run:
        return
    subprocess.run(command, cwd=ROOT, check=True)


def read_candidates(path: Path) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"Discovery output does not exist: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    candidates = payload.get("candidates") or []
    if not isinstance(candidates, list):
        raise SystemExit(f"Invalid discovery output: {path}")
    return candidates


def chunked(values: list[str], size: int) -> list[list[str]]:
    if size <= 0:
        return [values]
    return [values[index : index + size] for index in range(0, len(values), size)]


def build_discovery_command(args: argparse.Namespace, output: Path) -> list[str]:
    command = [
        sys.executable,
        str(DISCOVER_SCRIPT),
        "--output",
        str(output),
        "--max-links-per-list",
        str(args.max_links_per_list),
        "--max-stories",
        str(args.max_stories),
        "--timeout",
        str(args.timeout),
        "--retries",
        str(args.discovery_retries),
        "--retry-sleep",
        str(args.retry_sleep),
        "--sleep",
        str(args.discovery_sleep),
    ]
    if not args.all_default_seeds:
        command.extend(["--urls", *DEFAULT_PRODUCTION_SEED_URLS])
    if not args.skip_db:
        command.append("--upsert-db")
        db_sources = args.source if args.source else sorted(DEFAULT_TEXT_CRAWL_SOURCES)
        command.extend(["--db-sources", *db_sources])
    for url in args.extra_url:
        command.extend(["--extra-url", url])
    return command


def build_crawl_base_command(args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable,
        str(CRAWL_SCRIPT),
        "--workers",
        str(args.workers),
        "--timeout",
        str(args.timeout),
        "--retries",
        str(args.crawl_retries),
        "--retry-sleep",
        str(args.retry_sleep),
        "--chapter-delay",
        str(args.chapter_delay),
        "--min-text-chars",
        str(args.min_text_chars),
        "--max-consecutive-content-misses",
        str(args.max_consecutive_content_misses),
        "--catalog-output-root",
        args.catalog_output_root,
        "--text-output-root",
        args.text_output_root,
        "--raw-zh-output-root",
        args.raw_zh_output_root,
        "--raw-en-output-root",
        args.raw_en_output_root,
        "--polished-output-root",
        args.polished_output_root,
        "--vi-model",
        args.vi_model,
        "--translate-model",
        args.translate_model,
        "--polish-max-attempts",
        str(args.polish_max_attempts),
    ]
    if args.max_chapters:
        command.extend(["--max-chapters", str(args.max_chapters)])
    if args.overwrite:
        command.append("--overwrite")
    if args.overwrite_catalog:
        command.append("--overwrite-catalog")
    if args.requeue_done:
        command.append("--requeue-done")
    if args.stop_on_error:
        command.append("--stop-on-error")
    return command


def source_allowed(candidate: dict, active_sources: set[str]) -> bool:
    source_code = str(candidate.get("source_code") or "")
    if source_code not in ALL_DB_CRAWLER_SOURCES:
        return False
    return source_code in active_sources


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Production-style pipeline: discover Vietnamese system/villain/murim stories, "
            "upsert them into DB, then crawl discovered story URLs."
        )
    )
    parser.add_argument("--output", type=Path, default=Path("story_data/discovery/system_villain_stories.json"))
    parser.add_argument("--extra-url", action="append", default=[], help="Add extra seed URL to discovery.")
    parser.add_argument(
        "--all-default-seeds",
        action="store_true",
        help="Use every seed from discover_system_villain_stories.py, including audit-only sources that may be locked/503.",
    )
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        help=(
            "Restrict crawl to source_code. Repeatable. "
            "Default production sources skip currently locked/503 sources such as hako/docln/manhwatv."
        ),
    )
    parser.add_argument("--max-links-per-list", type=int, default=80)
    parser.add_argument("--max-stories", type=int, default=160)
    parser.add_argument("--max-crawl-stories", type=int, default=0, help="0 = crawl all discovered DB-supported stories.")
    parser.add_argument("--max-chapters", type=int, default=0, help="0 = all chapters.")
    parser.add_argument("--story-url-chunk-size", type=int, default=40)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--discovery-retries", type=int, default=2)
    parser.add_argument("--crawl-retries", type=int, default=5)
    parser.add_argument("--retry-sleep", type=float, default=2.0)
    parser.add_argument("--discovery-sleep", type=float, default=0.4)
    parser.add_argument("--chapter-delay", type=float, default=1.5)
    parser.add_argument("--min-text-chars", type=int, default=80)
    parser.add_argument(
        "--max-consecutive-content-misses",
        type=int,
        default=1,
        help="Stop crawling remaining chapters in a story after N consecutive content extraction misses.",
    )
    parser.add_argument("--skip-db", action="store_true", help="Do not upsert discovery results before crawling.")
    parser.add_argument("--discovery-only", action="store_true")
    parser.add_argument("--crawl-only", action="store_true", help="Read existing --output and crawl those candidates.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--overwrite-catalog", action="store_true")
    parser.add_argument("--requeue-done", action="store_true")
    parser.add_argument("--stop-on-error", action="store_true")
    parser.add_argument("--catalog-output-root", default="story_data/catalogs")
    parser.add_argument("--text-output-root", default="story_data/text")
    parser.add_argument("--raw-zh-output-root", default="story_data/raw_zh")
    parser.add_argument("--raw-en-output-root", default="story_data/raw_en")
    parser.add_argument("--polished-output-root", default="story_data/polished")
    parser.add_argument("--vi-model", default="qwen3:14b")
    parser.add_argument("--translate-model", default="qwen3:14b")
    parser.add_argument("--polish-max-attempts", type=int, default=3)
    args = parser.parse_args()

    output = args.output
    if not args.crawl_only:
        run_command(build_discovery_command(args, output), dry_run=args.dry_run)
    if args.discovery_only:
        return
    if args.dry_run and not output.exists():
        print("[DRY-RUN] skip reading discovery output because it does not exist yet.")
        return

    selected_sources = {source.strip() for source in args.source if source.strip()}
    active_sources = selected_sources or DEFAULT_TEXT_CRAWL_SOURCES
    candidates = [candidate for candidate in read_candidates(output) if source_allowed(candidate, active_sources)]
    candidates.sort(key=lambda item: (-(int(item.get("score") or 0)), str(item.get("source_code") or ""), str(item.get("title") or "")))
    if args.max_crawl_stories:
        candidates = candidates[: args.max_crawl_stories]
    if not candidates:
        raise SystemExit("No discovered candidates are supported by crawl_stories_from_db.py.")

    urls_by_source: dict[str, list[str]] = defaultdict(list)
    seen_urls: set[str] = set()
    for candidate in candidates:
        url = str(candidate.get("source_url") or "").rstrip("/")
        source_code = str(candidate.get("source_code") or "")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        urls_by_source[source_code].append(url)

    print(
        "[INFO] crawl candidates="
        f"{sum(len(urls) for urls in urls_by_source.values())} "
        f"sources={', '.join(sorted(urls_by_source))} "
        f"generated_at={datetime.now(timezone.utc).isoformat()}"
    )

    base_command = build_crawl_base_command(args)
    for source_code in sorted(urls_by_source):
        urls = urls_by_source[source_code]
        for batch in chunked(urls, args.story_url_chunk_size):
            command = [*base_command, "--sources", source_code, "--story-url", *batch]
            run_command(command, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
