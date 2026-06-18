#!/usr/bin/env python3
"""
Backfill script: sửa author/title bị lấy sai do các bugs trong crawl pipeline.

Modes:
  --fix-display-title  Backfill stories.display_title từ title (strip crawl artifacts).
                       Chỉ update stories có display_title IS NULL.
  --fix-chapter-urls   Bug 1: xóa 859 stories truyenfull.today có chapter URL làm
                       source_url (title là "Quyển N - Chương M"), upsert lại đúng
                       story URL cha.
  --fix-null-authors   Bug 2–5: re-fetch story page và cập nhật author cho các
                       stories đang null (royalroad, truyenyy, hako, truyenfull).

Run với --dry-run trước để xem tác động, rồi bỏ flag để apply.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from story_db.story_pipeline_db import db  # noqa: E402
from story_db.story_pipeline_db import repository as repo  # noqa: E402
from scripts.story_pipeline.crawl_utils import fetch_html, compact_text  # noqa: E402
from scripts.story_pipeline.crawl_truyenfull_today_chapters import (  # noqa: E402
    parse_catalog as crawl_truyenfull_catalog,
)
from scripts.story_pipeline.crawl_royalroad_chapters import (  # noqa: E402
    parse_catalog as crawl_royalroad_catalog,
)
from scripts.story_pipeline.crawl_truyenyy_chapters import (  # noqa: E402
    parse_catalog as crawl_truyenyy_catalog,
)
from scripts.story_pipeline.crawl_hako_chapters import (  # noqa: E402
    crawl_catalog as crawl_hako_catalog,
)


CHAPTER_URL_PATTERN = re.compile(r"/(?:quyen-\d+-)?chuong-\d+/?$")

# ── display_title backfill ─────────────────────────────────────────────────────
# Các artifact crawl phổ biến cần loại khỏi title để tạo display_title sạch.
_LEADING_ARTIFACT_RE = re.compile(
    r"^\s*\[Dịch(?:\s+Vip)?\]\s*", re.IGNORECASE
)
_TRAILING_ARTIFACT_RES = [
    # " - Truyện Chữ" / " – Truyện Chữ"
    re.compile(r"\s*[-–]\s*Truyện\s*Chữ\s*$", re.IGNORECASE),
    # " - Full" / "(Full)"
    re.compile(r"\s*[-–]\s*Full\s*$", re.IGNORECASE),
    re.compile(r"\s*\(Full\)\s*$", re.IGNORECASE),
    # "(Dịch)" / "(Dịch Vip)" — chỉ dạng có ngoặc, tránh false-positive compound words như "Giao Dịch"
    re.compile(r"\s*\(Dịch(?:\s+Vip)?\)\s*$", re.IGNORECASE),
    # "(Cải Biên)" / " Cải Biên"
    re.compile(r"\s*\(Cải\s*Biên\)\s*$", re.IGNORECASE),
    re.compile(r"\s+Cải\s*Biên\s*$", re.IGNORECASE),
    # "(Convert)" / " Convert"
    re.compile(r"\s*\(Convert\)\s*$", re.IGNORECASE),
    re.compile(r"\s+Convert\s*$", re.IGNORECASE),
    # " - Truyện Chữ" duplicate guard
    re.compile(r"\s*-\s*Truyện\s*Chữ\s*$", re.IGNORECASE),
]


def clean_title_for_display(title: str) -> str:
    """Strip crawl artifacts from title → clean display_title."""
    t = title.strip()
    t = _LEADING_ARTIFACT_RE.sub("", t).strip()
    changed = True
    while changed:
        prev = t
        for pattern in _TRAILING_ARTIFACT_RES:
            t = pattern.sub("", t).strip()
        changed = t != prev
    return t


def fix_display_titles(args: argparse.Namespace) -> None:
    """Backfill display_title cho stories có display_title IS NULL.

    Chỉ update nếu cleaned title khác với raw title (tức là có artifact để strip).
    Stories không có artifact thì set display_title = title (canonical clean copy).
    """
    dry_run = args.dry_run
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, title, language
            FROM stories
            WHERE display_title IS NULL
            ORDER BY total_chapters DESC, title
            """
        ).fetchall()

    rows = [dict(r) for r in rows]
    print(f"[DISPLAY-TITLE] {len(rows)} stories without display_title", flush=True)

    updated = skipped = 0
    with db.connect() as conn:
        for row in rows:
            title = row["title"] or ""
            cleaned = clean_title_for_display(title)
            if not cleaned:
                skipped += 1
                continue

            label = f"{title!r} → {cleaned!r}" if cleaned != title else f"{title!r} (no change)"
            if args.verbose or cleaned != title:
                print(f"  [{'DRY' if dry_run else 'SET'}] {label}", flush=True)

            if not dry_run:
                conn.execute(
                    "UPDATE stories SET display_title = %s, updated_at = now() WHERE id = %s",
                    (cleaned, row["id"]),
                )
            updated += 1

        if not dry_run:
            conn.commit()

    print(f"\n[DISPLAY-TITLE] DONE updated={updated} skipped={skipped}", flush=True)
    if dry_run:
        print("[DRY-RUN] Re-run sans --dry-run để apply.", flush=True)


def story_url_from_chapter_url(chapter_url: str) -> str:
    """Strip chapter segment → parent story URL."""
    parsed = urlparse(chapter_url)
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 2:
        parts = parts[:-1]
    parent_path = "/" + "/".join(parts) + "/"
    return parsed._replace(path=parent_path, params="", query="", fragment="").geturl()


def fetch_bad_truyenfull_stories(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, title, author, source_url, source_id
        FROM stories
        WHERE source_url ~ '/(?:quyen-[0-9]+-)?chuong-[0-9]+'
          AND source_url LIKE '%truyenfull%'
        ORDER BY source_url
        """
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_null_author_stories(conn, source_domain: str | None = None) -> list[dict]:
    query = """
        SELECT s.id, s.title, s.author, s.source_url, sc.code as source_code
        FROM stories s
        JOIN sources sc ON sc.id = s.source_id
        WHERE (s.author IS NULL OR s.author = '')
    """
    params: list = []
    if source_domain:
        query += " AND s.source_url LIKE %s"
        params.append(f"%{source_domain}%")
    query += " ORDER BY sc.code, s.source_url"
    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def delete_story_by_id(conn, story_id: str) -> None:
    conn.execute("DELETE FROM stories WHERE id = %s", (story_id,))


def update_story_fields(conn, story_id: str, *, title: str | None = None, author: str | None = None, source_url: str | None = None) -> None:
    parts = []
    vals: list = []
    if title is not None:
        parts.append("title = %s")
        vals.append(title)
    if author is not None:
        parts.append("author = %s")
        vals.append(author or None)
    if source_url is not None:
        parts.append("source_url = %s")
        parts.append("catalog_url = %s")
        vals.extend([source_url, source_url])
    if not parts:
        return
    parts.append("updated_at = now()")
    vals.append(story_id)
    conn.execute(f"UPDATE stories SET {', '.join(parts)} WHERE id = %s", vals)


def fix_chapter_urls(args: argparse.Namespace) -> None:
    """
    Bug 1: stories truyenfull.today với chapter URL làm source_url.
    Chiến lược:
    - Tính parent story URL từ chapter URL
    - Nếu parent đã tồn tại trong DB → xóa bad story
    - Nếu chưa → fetch parent page, update title/author/source_url
    """
    with db.connect() as conn:
        bad_stories = fetch_bad_truyenfull_stories(conn)
    print(f"[BUG1] Tìm thấy {len(bad_stories)} stories với chapter URL", flush=True)

    deleted = fixed = skipped = 0
    for story in bad_stories:
        bad_url = story["source_url"]
        parent_url = story_url_from_chapter_url(bad_url)
        print(f"[BUG1] {bad_url}\n       → parent: {parent_url}", flush=True)

        # Check nếu parent story đã tồn tại trong DB
        with db.connect() as conn:
            existing = conn.execute(
                "SELECT id, title, author FROM stories WHERE source_url = %s OR source_url = %s",
                (parent_url, parent_url.rstrip("/") + "/"),
            ).fetchone()

        if existing:
            print(f"       [EXIST] parent id={existing['id']} title={existing['title']}", flush=True)
            if not args.dry_run:
                with db.connect() as conn:
                    delete_story_by_id(conn, story["id"])
                print(f"       [DELETE] xóa bad story id={story['id']}", flush=True)
            else:
                print(f"       [DRY] sẽ xóa bad story id={story['id']}", flush=True)
            deleted += 1
            time.sleep(0.05)
            continue

        # Parent chưa tồn tại → fetch và upsert
        try:
            catalog = crawl_truyenfull_catalog(
                parent_url,
                timeout=args.timeout,
                retries=args.retries,
                retry_sleep=args.retry_sleep,
            )
        except Exception as exc:
            print(f"       [WARN] fetch failed: {exc}", flush=True)
            skipped += 1
            time.sleep(args.retry_sleep)
            continue

        new_title = catalog.get("title") or ""
        new_author = catalog.get("author") or ""
        if not new_title or new_title == story["title"]:
            # Page vẫn trả chapter title (redirect) → xóa và skip
            print(f"       [SKIP] fetch vẫn trả sai title={new_title!r}", flush=True)
            skipped += 1
            time.sleep(args.retry_sleep)
            continue

        print(f"       [FIX] title={new_title!r} author={new_author!r}", flush=True)
        if not args.dry_run:
            with db.connect() as conn:
                update_story_fields(conn, story["id"],
                                    title=new_title,
                                    author=new_author or None,
                                    source_url=parent_url)
        fixed += 1
        time.sleep(args.request_delay)

    print(f"\n[BUG1] DONE deleted={deleted} fixed={fixed} skipped={skipped}", flush=True)


_GARBAGE_AUTHOR_PATTERN = re.compile(
    r"^(tác giả\s*:?\s*|author\s*:?\s*|start reading|sky demon order)$",
    re.IGNORECASE,
)


def _is_valid_author(author: str) -> bool:
    if not author or len(author) < 2:
        return False
    if _GARBAGE_AUTHOR_PATTERN.match(author.strip()):
        return False
    return True


def _try_extract_author_from_url(source_code: str, source_url: str, args: argparse.Namespace) -> str:
    """Re-fetch story page và extract author theo source."""
    try:
        if source_code == "truyenfull_today":
            catalog = crawl_truyenfull_catalog(source_url, timeout=args.timeout, retries=args.retries, retry_sleep=args.retry_sleep)
            author = catalog.get("author") or ""
            return author if _is_valid_author(author) else ""
        if source_code == "royalroad":
            catalog = crawl_royalroad_catalog(source_url, timeout=args.timeout, retries=args.retries, retry_sleep=args.retry_sleep)
            return catalog.get("author") or ""
        if source_code == "truyenyy":
            catalog = crawl_truyenyy_catalog(source_url, timeout=args.timeout, retries=args.retries, retry_sleep=args.retry_sleep)
            return catalog.get("author") or ""
        if source_code == "hako":
            catalog = crawl_hako_catalog(source_url, timeout=args.timeout, retries=args.retries, retry_sleep=args.retry_sleep)
            return catalog.get("author") or ""
    except Exception as exc:
        print(f"       [WARN] fetch error ({source_code}): {exc}", flush=True)
    return ""


SUPPORTED_SOURCES = {"truyenfull_today", "royalroad", "truyenyy", "hako"}


def fix_null_authors(args: argparse.Namespace) -> None:
    """Bug 2–5: re-fetch và update author cho stories đang null."""
    domain_filter = args.source_domain or None
    with db.connect() as conn:
        stories = fetch_null_author_stories(conn, domain_filter)

    supported = [s for s in stories if s.get("source_code") in SUPPORTED_SOURCES]
    print(f"[NULL-AUTHOR] {len(stories)} stories null author → {len(supported)} có thể fix", flush=True)

    by_source: dict[str, list] = {}
    for s in supported:
        by_source.setdefault(s["source_code"], []).append(s)
    for src, items in by_source.items():
        print(f"  {src}: {len(items)}", flush=True)

    updated = skipped = errors = 0
    for story in supported:
        src = story["source_code"]
        print(f"[NULL-AUTHOR] {src} {story['source_url']}", flush=True)

        author = _try_extract_author_from_url(src, story["source_url"], args)
        if not author:
            print(f"       [SKIP] không tìm thấy author", flush=True)
            skipped += 1
            time.sleep(args.request_delay)
            continue

        print(f"       [FOUND] author={author!r}", flush=True)
        if not args.dry_run:
            try:
                repo.update_story_author(story["id"], author)
            except Exception as exc:
                print(f"       [ERROR] update failed: {exc}", flush=True)
                errors += 1
                continue
        updated += 1
        time.sleep(args.request_delay)

    print(f"\n[NULL-AUTHOR] DONE updated={updated} skipped={skipped} errors={errors}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill story metadata (author/title).")
    parser.add_argument("--fix-display-title", action="store_true",
                        help="Backfill display_title từ title (strip crawl artifacts)")
    parser.add_argument("--fix-chapter-urls", action="store_true",
                        help="Bug 1: xóa/sửa stories truyenfull có chapter URL làm source_url")
    parser.add_argument("--fix-null-authors", action="store_true",
                        help="Bug 2–5: re-fetch và update author null")
    parser.add_argument("--source-domain", default="",
                        help="Lọc theo domain (vd: royalroad, truyenyy). Mặc định: tất cả.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Hiện tác động mà không thực sự thay đổi DB")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-sleep", type=float, default=2.0)
    parser.add_argument("--request-delay", type=float, default=1.5,
                        help="Delay giữa các request (giây)")
    args = parser.parse_args()

    if not args.fix_display_title and not args.fix_chapter_urls and not args.fix_null_authors:
        parser.error("Cần chọn ít nhất một mode: --fix-display-title, --fix-chapter-urls hoặc --fix-null-authors")

    if args.dry_run:
        print("[DRY RUN] Sẽ không thay đổi DB", flush=True)

    if args.fix_display_title:
        fix_display_titles(args)

    if args.fix_chapter_urls:
        fix_chapter_urls(args)

    if args.fix_null_authors:
        fix_null_authors(args)


if __name__ == "__main__":
    main()
