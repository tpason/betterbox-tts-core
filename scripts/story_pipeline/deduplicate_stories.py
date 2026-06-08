#!/usr/bin/env python3
"""
Tìm và xóa story trùng nhau trong DB.

Tiêu chí trùng (theo thứ tự độ tin cậy):
  1. URL sau khi normalize (strip trailing slash, lowercase) giống nhau
     → cùng trang, chỉ khác format URL (ví dụ royalroad trailing slash)
  2. Cùng source + cùng source_story_id (fiction ID)

Mode --cross-source: tìm duplicate cross-source theo title normalized.
  - Group by: normalize_title(title) khi có >= 2 story từ source khác nhau
  - KHÔNG dùng title matching cho same-source (too many false positives
    ví dụ naver_series "시리즈" — nhiều story khác nhau cùng label).
  - Chỉ match khi title length > 5 để tránh generic labels.

Khi có duplicate: giữ story có nhiều polished chapter nhất.
Tie-break: translated → downloaded → total rows → created_at cũ hơn.

Usage:
  # Investigate only (không xóa gì)
  python scripts/story_pipeline/deduplicate_stories.py

  # Cross-source mode (khác source, cùng title)
  python scripts/story_pipeline/deduplicate_stories.py --cross-source

  # Chỉ xem source cụ thể
  python scripts/story_pipeline/deduplicate_stories.py --source-code royalroad

  # Xóa (có confirm)
  python scripts/story_pipeline/deduplicate_stories.py --delete

  # Xóa không hỏi
  python scripts/story_pipeline/deduplicate_stories.py --delete --yes

  # Dry-run (hiện sẽ xóa gì nhưng không xóa)
  python scripts/story_pipeline/deduplicate_stories.py --delete --dry-run
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from story_db.story_pipeline_db.db import connect


def normalize_url(url: str) -> str:
    """Strip trailing slashes (kể cả double //) và lowercase để so sánh URL."""
    u = (url or "").strip().lower()
    while u.endswith("/"):
        u = u[:-1]
    return u


_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_title(title: str) -> str:
    """Lowercase, strip punctuation/whitespace để so sánh title cross-source."""
    t = (title or "").strip().lower()
    t = _PUNCT_RE.sub(" ", t)
    return " ".join(t.split())


def _has_lang_title_columns() -> bool:
    """Return True if migration 015 (title_en/ko/zh) has been applied to this DB."""
    with connect() as conn:
        row = conn.execute("""
            SELECT COUNT(*) AS n FROM information_schema.columns
            WHERE table_name = 'stories' AND column_name = 'title_en'
        """).fetchone()
        return bool(row and row["n"])


def fetch_all_stories(source_codes: list[str]) -> list[dict]:
    has_lang_cols = _has_lang_title_columns()
    lang_cols = "s.title_en, s.title_ko, s.title_zh," if has_lang_cols else "NULL AS title_en, NULL AS title_ko, NULL AS title_zh,"
    query = f"""
        SELECT
            s.id,
            s.title,
            s.original_title,
            {lang_cols}
            s.author,
            s.source_url,
            s.source_story_id,
            s.total_chapters,
            s.is_active,
            s.created_at,
            src.code AS source_code,
            COUNT(c.id) FILTER (WHERE c.is_downloaded) AS downloaded_chapters,
            COUNT(c.id) FILTER (WHERE c.is_translated) AS translated_chapters,
            COUNT(c.id) FILTER (WHERE c.is_polished)   AS polished_chapters,
            COUNT(c.id) AS total_chapter_rows
        FROM stories s
        JOIN sources src ON src.id = s.source_id
        LEFT JOIN chapters c ON c.story_id = s.id
    """
    params: list = []
    if source_codes:
        query += " WHERE src.code = ANY(%s)"
        params.append(source_codes)
    query += " GROUP BY s.id, src.code ORDER BY src.code, s.title, s.created_at"
    if not has_lang_cols:
        print("NOTE: title_en/ko/zh columns not found — run migration 015 for language-specific title matching.")

    with connect() as conn:
        return [dict(r) for r in conn.execute(query, params).fetchall()]


def find_duplicates(stories: list[dict]) -> list[list[dict]]:
    """
    Trả về list các nhóm duplicate (same-source, same URL).
    Chỉ dùng normalized URL — đây là tiêu chí duy nhất đủ đáng tin cậy.
    source_story_id bị bỏ qua vì có false positive (ví dụ truyenfull_today
    assign sai ID cho nhiều story khác nhau).
    """
    by_norm_url: dict[tuple, list[dict]] = defaultdict(list)
    for s in stories:
        norm = normalize_url(s["source_url"])
        by_norm_url[(s["source_code"], norm)].append(s)

    return [group for group in by_norm_url.values() if len(group) > 1]


def find_cross_source_duplicates(stories: list[dict]) -> list[list[dict]]:
    """
    Tìm duplicate cross-source với 2 match levels:

    Strong (title_en/title_ko/title_zh, len >= 10):
      Language-specific columns — reliably populated by crawlers.
      Two stories from different sources sharing the same title_en are almost certainly duplicates.

    Weak (title only, len > 5, >= 2 different sources):
      Lower confidence — may include false positives from generic titles.
      Requires --confirmed gate before deletion.

    Groups containing stories from only one source are excluded (handled by URL mode).
    """
    _STRONG_MIN = 10

    by_lang_title: dict[str, list[dict]] = defaultdict(list)
    by_title: dict[str, list[dict]] = defaultdict(list)

    for s in stories:
        # Strong: group by any language-specific title column
        for col, label in [("title_en", "en"), ("title_ko", "ko"), ("title_zh", "zh")]:
            norm = normalize_title(s.get(col) or "")
            if len(norm) >= _STRONG_MIN:
                by_lang_title[f"{label}:{norm}"].append(s)

        norm_title = normalize_title(s["title"] or "")
        if len(norm_title) > 5:
            by_title[norm_title].append(s)

    seen_ids: set[frozenset] = set()
    groups: list[list[dict]] = []

    # Strong groups first (title_en/ko/zh match)
    for key_str, group in by_lang_title.items():
        lang_label, _ = key_str.split(":", 1)
        sources = {s["source_code"] for s in group}
        if len(sources) < 2:
            continue
        key = frozenset(s["id"] for s in group)
        if key in seen_ids:
            continue
        seen_ids.add(key)
        for s in group:
            s["_match_type"] = f"strong(title_{lang_label})"
        groups.append(group)

    # Weak groups (title only, not already covered by strong)
    for group in by_title.values():
        sources = {s["source_code"] for s in group}
        if len(sources) < 2:
            continue
        key = frozenset(s["id"] for s in group)
        if key in seen_ids:
            continue
        seen_ids.add(key)
        for s in group:
            s["_match_type"] = "weak(title_only)"
        groups.append(group)

    return groups


def pick_winner(group: list[dict]) -> dict:
    """Giữ story có nhiều polished chapter nhất; tie-break: translated → downloaded → rows → older."""
    return max(
        group,
        key=lambda s: (
            int(s["polished_chapters"] or 0),
            int(s["translated_chapters"] or 0),
            int(s["downloaded_chapters"] or 0),
            int(s["total_chapter_rows"] or 0),
            -s["created_at"].timestamp(),
        ),
    )


def detect_reason(group: list[dict]) -> str:
    return "same_url_normalized"


def print_group(group: list[dict], winner_id: str, reason: str) -> None:
    title = group[0]["title"]
    author = group[0].get("author") or ""
    match_type = group[0].get("_match_type", reason)
    sources = ", ".join(sorted({s["source_code"] for s in group}))
    lang_titles = []
    for col, label in [("title_en", "EN"), ("title_ko", "KO"), ("title_zh", "ZH")]:
        val = group[0].get(col) or ""
        if val and val != title:
            lang_titles.append(f"{label}={val!r}")
    meta_parts = []
    if author:
        meta_parts.append(f"author={author!r}")
    if lang_titles:
        meta_parts.append("  ".join(lang_titles))
    meta = ("  " + "  ".join(meta_parts)) if meta_parts else ""
    print(f"\n  [{sources}] {title}{meta}  ({match_type})")
    for s in sorted(group, key=lambda x: (
        int(x["polished_chapters"] or 0),
        int(x["translated_chapters"] or 0),
        int(x["downloaded_chapters"] or 0),
    ), reverse=True):
        tag = "KEEP " if s["id"] == winner_id else "DEL  "
        active = "active" if s["is_active"] else "inactive"
        s_author = s.get("author") or ""
        s_en = s.get("title_en") or ""
        s_ko = s.get("title_ko") or ""
        s_zh = s.get("title_zh") or ""
        lang_info = "  ".join(
            f"{lbl}={v!r}" for lbl, v in [("EN", s_en), ("KO", s_ko), ("ZH", s_zh)] if v and v != s["title"]
        )
        print(
            f"    {tag} [{s['source_code']}]"
            f"  po={s['polished_chapters']}"
            f"  tr={s['translated_chapters']}"
            f"  dl={s['downloaded_chapters']}"
            f"  rows={s['total_chapter_rows']}"
            f"  {active}"
            f"  created={str(s['created_at'])[:10]}"
            + (f"  author={s_author!r}" if s_author else "")
            + (f"  {lang_info}" if lang_info else "")
            + f"  id={s['id']}"
        )
        print(f"         {s['source_url']}")


def delete_story(story_id: str) -> int:
    """Xóa story và cascade chapters/jobs. Trả về số chapter rows đã xóa."""
    with connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM chapters WHERE story_id = %s", (story_id,)
        ).fetchone()["n"]
        conn.execute("DELETE FROM stories WHERE id = %s", (story_id,))
    return int(n or 0)


def run_dedup(
    stories: list[dict],
    groups: list[list[dict]],
    reason_label: str,
    args: argparse.Namespace,
) -> None:
    if not groups:
        print(f"No {reason_label} duplicates found.")
        return

    total_to_delete = sum(len(g) - 1 for g in groups)
    total_chapter_rows_drop = 0

    print(f"\nFound {len(groups)} {reason_label} duplicate groups ({total_to_delete} stories to remove):\n")
    print("=" * 80)

    deletions: list[dict] = []
    for group in sorted(groups, key=lambda g: g[0].get("source_code", "")):
        winner = pick_winner(group)
        print_group(group, winner["id"], reason_label)
        for s in group:
            if s["id"] != winner["id"]:
                total_chapter_rows_drop += int(s["total_chapter_rows"] or 0)
                deletions.append(s)

    print("\n" + "=" * 80)
    print(
        f"Summary: {len(groups)} groups, {total_to_delete} stories to delete, "
        f"{total_chapter_rows_drop} chapter rows will be cascade-deleted."
    )

    if not args.delete:
        print("\nRun with --delete to remove duplicates.")
        if getattr(args, "cross_source", False):
            print(
                "NOTE: Cross-source grouping uses title/original_title matching.\n"
                "      Review all groups above, then add --confirmed to acknowledge before deleting."
            )
        return

    if args.dry_run:
        print(f"\n[DRY RUN] Would delete {len(deletions)} stories:")
        for s in deletions:
            mt = s.get("_match_type", "")
            print(
                f"  [{s['source_code']}] {s['title']}  po={s['polished_chapters']}"
                f"  dl={s['downloaded_chapters']}  match={mt}  id={s['id']}"
            )
        return

    # ALL cross-source deletions require --confirmed — original_title alone is not
    # a safe automatic merge signal (crawlers may store display title as original_title).
    if getattr(args, "cross_source", False) and not getattr(args, "confirmed", False):
        print(
            f"\n[SAFETY] Cross-source --delete requires --confirmed.\n"
            "  Grouping is based on title/original_title which can produce false positives.\n"
            "  Review all groups above, then re-run with --delete --confirmed."
        )
        return

    if not args.yes:
        answer = input(f"\nDelete {len(deletions)} stories and their chapters? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted.")
            return

    deleted_stories = 0
    deleted_chapters = 0
    for s in deletions:
        n = delete_story(s["id"])
        deleted_stories += 1
        deleted_chapters += n
        print(f"  DELETED [{s['source_code']}] {s['title']}  po={s['polished_chapters']}  dl={s['downloaded_chapters']}  chapters_deleted={n}")

    print(f"\nDone. Deleted {deleted_stories} stories, {deleted_chapters} chapters.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Tìm và xóa story trùng trong DB.")
    parser.add_argument("--source-code", action="append", default=[])
    parser.add_argument("--cross-source", action="store_true",
                        help="Tìm duplicate cross-source theo title normalized (thay vì URL).")
    parser.add_argument("--delete", action="store_true")
    parser.add_argument("--yes", "-y", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--confirmed",
        action="store_true",
        help="Required for --cross-source --delete: confirms you have reviewed all groups.",
    )
    args = parser.parse_args()

    print("Fetching stories...")
    stories = fetch_all_stories(args.source_code)
    print(f"Total stories fetched: {len(stories)}")

    if args.cross_source:
        groups = find_cross_source_duplicates(stories)
        run_dedup(stories, groups, "cross-source title", args)
    else:
        groups = find_duplicates(stories)
        run_dedup(stories, groups, "same_url_normalized", args)


if __name__ == "__main__":
    main()
