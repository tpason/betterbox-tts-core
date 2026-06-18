#!/usr/bin/env python3
"""Xóa duplicate stories — cùng source_url (sau khi strip trailing slash), khác ID.

Strategy:
  - "Primary" = story có nhiều polished chapters hơn; tie-break = story cũ hơn.
  - "Duplicate" = story còn lại (thường 0 polished, mới hơn, URL có thêm trailing slash).
  - Mặc định chỉ xóa story_jobs của duplicate (an toàn, có thể rollback).
  - --delete-stories: xóa thêm story + chapters (nguy hiểm — dùng sau khi verify).

Usage:
  # Xem danh sách duplicates (dry-run mặc định)
  python cleanup_duplicate_stories.py

  # Xóa jobs của duplicates
  python cleanup_duplicate_stories.py --apply

  # Xóa cả story + chapters + jobs (cần --apply)
  python cleanup_duplicate_stories.py --apply --delete-stories
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from story_db.story_pipeline_db.db import connect  # noqa: E402


def find_duplicate_groups(conn) -> list[list[dict]]:
    """Return groups of stories sharing the same normalized source_url."""
    rows = conn.execute(
        """
        WITH normalized AS (
          SELECT s.id, s.title, s.source_url,
                 REGEXP_REPLACE(s.source_url, '/+$', '') AS url_norm,
                 s.source_id, s.total_chapters, s.created_at
          FROM stories s
        ),
        dup_urls AS (
          SELECT url_norm, source_id
          FROM normalized
          GROUP BY url_norm, source_id
          HAVING COUNT(*) > 1
        )
        SELECT n.id, n.title, n.source_url, n.url_norm, n.total_chapters, n.created_at,
               sc.code AS source_code
        FROM normalized n
        JOIN dup_urls du ON du.url_norm = n.url_norm AND du.source_id = n.source_id
        JOIN sources sc ON sc.id = n.source_id
        ORDER BY n.url_norm, n.created_at
        """
    ).fetchall()

    # Group by (url_norm, source_id)
    from collections import defaultdict
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        key = row["url_norm"]
        groups[key].append(dict(row))

    # For each group fetch polished counts
    story_ids = [r["id"] for r in rows]
    if not story_ids:
        return []

    placeholders = ",".join(["%s"] * len(story_ids))
    polished = conn.execute(
        f"""
        SELECT story_id,
               COUNT(*) FILTER (WHERE is_polished) AS polished_count,
               COUNT(*) FILTER (WHERE status = 'pending') AS pending_jobs
        FROM chapters c
        LEFT JOIN story_jobs j ON j.story_id = c.story_id
        WHERE c.story_id IN ({placeholders})
        GROUP BY c.story_id
        """,
        story_ids,
    ).fetchall()
    polished_map = {r["story_id"]: dict(r) for r in polished}

    result = []
    for url_norm, stories in groups.items():
        for s in stories:
            info = polished_map.get(s["id"], {})
            s["polished_count"] = info.get("polished_count", 0)
            s["pending_jobs"] = info.get("pending_jobs", 0)

        # Primary = most polished; tie-break = oldest
        stories.sort(key=lambda s: (-s["polished_count"], s["created_at"]))
        result.append(stories)

    return result


def run(args: argparse.Namespace) -> None:
    dry_run = not args.apply

    with connect() as conn:
        groups = find_duplicate_groups(conn)

    n_dupes = sum(len(g) - 1 for g in groups)
    print(f"[DUPES] {len(groups)} duplicate groups, {n_dupes} stories to clean", flush=True)

    total_jobs = 0
    total_stories = 0

    with connect() as conn:
        for group in groups:
            primary = group[0]
            duplicates = group[1:]

            print(
                f"\n[GROUP] {primary['title']!r} [{primary['source_code']}]"
                f"\n  primary: {primary['id'][:8]} polished={primary['polished_count']}"
                f" url={primary['source_url']!r}"
            )

            for dup in duplicates:
                label = (
                    f"  [DUP] {dup['id'][:8]} polished={dup['polished_count']}"
                    f" pending_jobs={dup['pending_jobs']} url={dup['source_url']!r}"
                )
                print(label)

                if dup["polished_count"] > 0 and dup["polished_count"] >= primary["polished_count"]:
                    print(f"    → SKIP: dup có {dup['polished_count']} polished ≥ primary — verify thủ công")
                    continue

                if not dry_run:
                    deleted = conn.execute(
                        "DELETE FROM story_jobs WHERE story_id = %s RETURNING id",
                        (dup["id"],),
                    ).fetchall()
                    n_jobs = len(deleted)
                    print(f"    → deleted {n_jobs} jobs")
                    total_jobs += n_jobs
                else:
                    print(f"    → [DRY] would delete all story_jobs (pending={dup['pending_jobs']})")

                if args.delete_stories and not dry_run:
                    conn.execute("DELETE FROM chapters WHERE story_id = %s", (dup["id"],))
                    conn.execute("DELETE FROM stories WHERE id = %s", (dup["id"],))
                    print(f"    → deleted story + chapters")
                    total_stories += 1
                elif args.delete_stories:
                    print(f"    → [DRY] would delete story + chapters")

        if not dry_run:
            conn.commit()

    print(f"\n[DONE] jobs_deleted={total_jobs} stories_deleted={total_stories}")
    if dry_run:
        print("[DRY-RUN] Re-run với --apply để áp dụng.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Cleanup duplicate stories (same source_url, different ID)")
    parser.add_argument("--apply", action="store_true", help="Áp dụng (mặc định: dry-run)")
    parser.add_argument("--delete-stories", action="store_true",
                        help="Xóa luôn story + chapters của duplicate (cần --apply)")
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
