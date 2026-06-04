#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from crawl_qidian_catalog import parse_catalog  # noqa: E402
from crawl_qidian_rankings import DEFAULT_RANK_URLS, parse_rank_page  # noqa: E402


def load_books_from_rank(rank: str, limit: int) -> list[dict]:
    rank_url = DEFAULT_RANK_URLS[rank]
    books = parse_rank_page(rank, rank_url, limit)
    return [
        {
            "rank": book.rank_name,
            "rank_position": book.position,
            "title": book.title,
            "author": book.author,
            "category": book.category,
            "status": book.status,
            "intro": book.intro,
            "book_url": book.book_url,
        }
        for book in books
    ]


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Khảo sát Qidian free lists: crawl book list, crawl catalog từng book, "
            "đếm total/free/vip chapters để chọn truyện đáng clone public chapters."
        )
    )
    parser.add_argument(
        "--ranks",
        nargs="+",
        default=["free", "free_all", "free_completed"],
        choices=sorted(DEFAULT_RANK_URLS),
    )
    parser.add_argument("--limit-per-rank", type=int, default=20)
    parser.add_argument("--delay", type=float, default=1.5)
    parser.add_argument("--output-dir", default="story_data/qidian/free_survey")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    candidates: list[dict] = []
    seen_urls: set[str] = set()
    for rank in args.ranks:
        print(f"\n=== Crawl rank {rank} ===")
        try:
            books = load_books_from_rank(rank, args.limit_per_rank)
        except Exception as exc:
            print(f"[WARN] Không crawl được rank {rank}: {exc}")
            continue

        for book in books:
            if book["book_url"] in seen_urls:
                continue
            seen_urls.add(book["book_url"])
            candidates.append(book)

    print(f"\nTổng candidate book: {len(candidates)}")

    results: list[dict] = []
    for index, book in enumerate(candidates, start=1):
        print(f"\n[{index}/{len(candidates)}] Catalog: {book['title']} ({book['book_url']})")
        try:
            catalog = parse_catalog(book["book_url"])
            result = {
                **book,
                "book_id": catalog["book_id"],
                "catalog_url": catalog["catalog_url"],
                "catalog_title": catalog["title"],
                "total_chapters": catalog["total_chapters"],
                "free_chapters": catalog["free_chapters"],
                "vip_chapters": catalog["vip_chapters"],
                "free_ratio": round(catalog["free_chapters"] / max(catalog["total_chapters"], 1), 4),
                "catalog_manifest": f"story_data/qidian/catalogs/{catalog['book_id']}/chapters.json",
                "download_command": (
                    "python scripts/story_pipeline/download_qidian_public_chapters.py "
                    f"--manifest story_data/qidian/catalogs/{catalog['book_id']}/chapters.json"
                ),
            }
            results.append(result)

            manifest_path = Path(result["catalog_manifest"])
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
            print(
                f"free={result['free_chapters']} / total={result['total_chapters']} "
                f"vip={result['vip_chapters']}"
            )
        except Exception as exc:
            print(f"[WARN] Catalog lỗi: {exc}")
        time.sleep(args.delay)

    results.sort(key=lambda item: (item["free_chapters"], item["free_ratio"]), reverse=True)

    json_path = output_dir / "books.json"
    csv_path = output_dir / "books.csv"
    json_path.write_text(json.dumps({"total": len(results), "books": results}, ensure_ascii=False, indent=2), encoding="utf-8")

    fieldnames = [
        "rank",
        "rank_position",
        "title",
        "author",
        "category",
        "status",
        "book_id",
        "total_chapters",
        "free_chapters",
        "vip_chapters",
        "free_ratio",
        "book_url",
        "catalog_manifest",
        "download_command",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for result in results:
            writer.writerow({key: result.get(key, "") for key in fieldnames})

    print(f"\nHoàn tất khảo sát: {json_path}")
    print(f"CSV: {csv_path}")
    print("\nTop candidates:")
    for item in results[:10]:
        print(
            f"- {item['title']} | free={item['free_chapters']}/{item['total_chapters']} "
            f"| {item['book_url']}"
        )


if __name__ == "__main__":
    main()
