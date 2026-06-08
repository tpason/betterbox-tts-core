#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from story_db.story_pipeline_db import repository as repo


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Enqueue audio_chapter jobs có chọn lọc cho các chapter đã polish."
    )
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--audio-output-root", default="story_audio")
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--source", nargs="*", default=[], help="Filter source: hako wattpad_vn qidian.")
    parser.add_argument("--story-id", default="")
    parser.add_argument("--story-url", default="")
    parser.add_argument("--story-slug", default="")
    parser.add_argument("--story-title", default="", help="ILIKE search theo title.")
    parser.add_argument("--chapter", type=int, default=0)
    parser.add_argument("--from-chapter", type=int, default=0)
    parser.add_argument("--to-chapter", type=int, default=0)
    parser.add_argument("--include-existing-audio", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    rows = repo.list_polished_chapters_for_audio(
        story_id=args.story_id or None,
        story_url=args.story_url or None,
        story_slug=args.story_slug or None,
        story_title=args.story_title or None,
        source_codes=args.source or None,
        chapter_number=args.chapter or None,
        from_chapter=args.from_chapter or None,
        to_chapter=args.to_chapter or None,
        include_existing_audio=args.include_existing_audio,
        limit=args.limit,
    )
    if not rows:
        print("No matching polished chapters pending audio.")
        return

    for row in rows:
        raw_polished_path = row.get("polished_text_path")
        polished_path = Path(raw_polished_path) if raw_polished_path else None
        if polished_path and polished_path.exists():
            story_slug = polished_path.parent.name
            chapter_stem = polished_path.stem
        else:
            # File không còn trên disk — dùng metadata từ DB
            story_slug = row.get("story_metadata", {}).get("slug") or str(row["story_id"])
            chapter_stem = f"chapter{row['chapter_number']:04d}"

        output_path = Path(args.audio_output_root) / story_slug / f"{chapter_stem}.wav"
        if args.dry_run:
            src = raw_polished_path if (polished_path and polished_path.exists()) else "db"
            print(
                f"[DRY] {row['source_code']} | {row['story_title']} | "
                f"chapter{row['chapter_number']:04d} [{src}] -> {output_path}"
            )
            continue
        job = repo.enqueue_chapter_job(
            "audio_chapter",
            row["id"],
            story_id=row["story_id"],
            source_code=row["source_code"],
            input_path=raw_polished_path or "",
            output_path=output_path.as_posix(),
            payload={
                "story_slug": story_slug,
                "chapter_number": row["chapter_number"],
                "polished_text_path": raw_polished_path,
            },
            max_attempts=args.max_attempts,
        )
        print(f"[JOB] audio_chapter {job['status']}: {output_path}")


if __name__ == "__main__":
    main()
