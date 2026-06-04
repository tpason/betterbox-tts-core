#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from story_db.story_pipeline_db.db import connect


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "vi,en;q=0.9,ko;q=0.8,zh;q=0.8",
}

IMAGE_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
}


@dataclass
class StoryRow:
    id: str
    source_code: str
    title: str
    original_title: str | None
    display_title: str | None
    author: str | None
    cover_image_url: str | None
    source_url: str
    catalog_url: str | None


@dataclass
class CoverCandidate:
    image_url: str
    page_url: str
    method: str


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_for_match(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    text = re.sub(r"[\W_]+", " ", text, flags=re.UNICODE)
    return clean_text(text)


def title_tokens(value: str | None) -> set[str]:
    stopwords = {
        "a",
        "an",
        "and",
        "book",
        "cover",
        "full",
        "novel",
        "read",
        "story",
        "the",
        "truyen",
        "truyenfull",
        "truyen chu",
    }
    return {token for token in normalize_for_match(value).split() if len(token) >= 3 and token not in stopwords}


def unique_non_empty(values: list[str | None]) -> list[str]:
    seen: set[str] = set()
    results: list[str] = []
    for value in values:
        cleaned = clean_text(value)
        key = normalize_for_match(cleaned)
        if cleaned and key and key not in seen:
            seen.add(key)
            results.append(cleaned)
    return results


def clean_url(value: str | None, base_url: str) -> str:
    url = (value or "").strip()
    if not url or url.startswith("data:"):
        return ""
    return urljoin(base_url, url)


def first_attr(soup: BeautifulSoup, base_url: str, selectors: list[tuple[str, list[str]]]) -> str:
    for selector, attrs in selectors:
        for node in soup.select(selector):
            for attr in attrs:
                url = clean_url(str(node.get(attr) or ""), base_url)
                if url:
                    return url
    return ""


def extract_cover_url(html: str, page_url: str, source_code: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    generic = first_attr(
        soup,
        page_url,
        [
            ("meta[property='og:image:secure_url']", ["content"]),
            ("meta[property='og:image']", ["content"]),
            ("meta[name='twitter:image']", ["content"]),
            ("meta[name='twitter:image:src']", ["content"]),
            ("link[rel='image_src']", ["href"]),
        ],
    )
    if generic:
        return generic

    source_selectors: dict[str, list[tuple[str, list[str]]]] = {
        "hako": [
            (".series-cover img", ["data-src", "src"]),
            (".cover img", ["data-src", "src"]),
            ("img[src*='cover']", ["data-src", "src"]),
        ],
        "royalroad": [
            ("img.thumbnail", ["data-src", "src"]),
            (".fiction-info img", ["data-src", "src"]),
            ("img[src*='covers']", ["data-src", "src"]),
        ],
        "wattpad_vn": [
            (".story-detail img", ["data-src", "src"]),
            (".book img", ["data-src", "src"]),
            (".cover img", ["data-src", "src"]),
            ("img[src*='cover']", ["data-src", "src"]),
        ],
        "qidian": [
            (".book-img img", ["data-src", "src"]),
            (".book-photo img", ["data-src", "src"]),
            ("img[src*='bookcover']", ["data-src", "src"]),
        ],
        "naver_series": [
            (".end_head img", ["data-src", "src"]),
            (".poster img", ["data-src", "src"]),
            ("img[src*='book']", ["data-src", "src"]),
        ],
        "truyenfull_today": [
            (".book img", ["data-src", "src"]),
            (".truyen img", ["data-src", "src"]),
            (".cover img", ["data-src", "src"]),
        ],
    }

    specific = first_attr(soup, page_url, source_selectors.get(source_code, []))
    if specific:
        return specific

    return first_attr(
        soup,
        page_url,
        [
            ("img[alt*='cover' i]", ["data-src", "src"]),
            ("img[src*='cover' i]", ["data-src", "src"]),
            ("img[data-src]", ["data-src"]),
            ("img[src]", ["src"]),
        ],
    )


def looks_like_image_url(url: str) -> bool:
    lower = url.lower().split("?", 1)[0]
    return lower.endswith((".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif", ".svg"))


def validate_image_url(url: str, timeout: int) -> bool:
    if not url:
        return False
    if looks_like_image_url(url):
        return True
    try:
        response = requests.head(url, headers=IMAGE_HEADERS, timeout=timeout, allow_redirects=True)
        if response.status_code >= 400 or response.status_code == 405:
            response = requests.get(url, headers=IMAGE_HEADERS, timeout=timeout, stream=True)
        content_type = response.headers.get("content-type", "").lower()
        return response.status_code < 400 and content_type.startswith("image/")
    except requests.RequestException:
        return False


def unwrap_search_url(url: str) -> str:
    parsed = urlparse(url)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        if target:
            return unquote(target)
    return url


def same_hostname(url_a: str, url_b: str) -> bool:
    host_a = urlparse(url_a).netloc.lower().removeprefix("www.")
    host_b = urlparse(url_b).netloc.lower().removeprefix("www.")
    return bool(host_a and host_b and host_a == host_b)


def page_matches_story(html: str, story: StoryRow) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    page_title = clean_text(
        " ".join(
            part
            for part in [
                soup.title.get_text(" ", strip=True) if soup.title else "",
                first_text(soup, ["meta[property='og:title']", "h1", ".title", "[class*='title']"]),
            ]
            if part
        )
    )
    haystack = normalize_for_match(
        " ".join(
            part
            for part in [
                page_title,
                first_text(soup, ["meta[name='description']", "meta[property='og:description']", ".author", "[class*='author']"]),
            ]
            if part
        )
    )
    story_names = unique_non_empty([story.display_title, story.original_title, story.title])
    if not story_names:
        return False

    for name in story_names:
        normalized_name = normalize_for_match(name)
        if normalized_name and normalized_name in haystack:
            return True

        tokens = title_tokens(name)
        if tokens:
            matched = sum(1 for token in tokens if token in haystack)
            if matched >= max(2, int(len(tokens) * 0.6)):
                return True

    author = normalize_for_match(story.author)
    if author and author in haystack:
        return bool(set().union(*(title_tokens(name) for name in story_names)) & set(haystack.split()))

    return False


def first_text(soup: BeautifulSoup, selectors: list[str]) -> str:
    for selector in selectors:
        node = soup.select_one(selector)
        if not node:
            continue
        if node.name == "meta":
            text = clean_text(str(node.get("content") or ""))
        else:
            text = clean_text(node.get_text(" ", strip=True))
        if text:
            return text
    return ""


def fetch_html(url: str, timeout: int, retries: int, retry_sleep: float) -> str:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, headers=HEADERS, timeout=timeout)
            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            last_error = exc
            if attempt < retries:
                print(f"[WARN] retry {attempt}/{retries}: {url} | {exc}", flush=True)
                time.sleep(retry_sleep)
    raise RuntimeError(f"Cannot fetch story page: {url} | {last_error}") from last_error


def fetch_search_results(query: str, timeout: int, retries: int, retry_sleep: float, max_links: int) -> list[str]:
    url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
    html = fetch_html(url, timeout, retries, retry_sleep)
    soup = BeautifulSoup(html, "html.parser")
    links: list[str] = []
    seen: set[str] = set()
    for node in soup.select("a.result__a, a.result-link, a[href]"):
        href = clean_url(str(node.get("href") or ""), url)
        href = unwrap_search_url(href)
        parsed = urlparse(href)
        if parsed.scheme not in {"http", "https"}:
            continue
        if "duckduckgo.com" in parsed.netloc:
            continue
        key = href.split("#", 1)[0]
        if key in seen:
            continue
        seen.add(key)
        links.append(key)
        if len(links) >= max_links:
            break
    return links


def external_queries(story: StoryRow) -> list[str]:
    story_names = unique_non_empty([story.original_title, story.title, story.display_title])
    queries: list[str] = []
    for name in story_names:
        if story.author:
            queries.append(f'"{name}" "{story.author}" cover')
            queries.append(f'"{name}" "{story.author}" truyện')
        queries.append(f'"{name}" novel cover')
        queries.append(f'"{name}" truyện bìa')
    return unique_non_empty(queries)


def find_external_cover(
    story: StoryRow,
    timeout: int,
    retries: int,
    retry_sleep: float,
    max_queries: int,
    max_results_per_query: int,
    skip_validate: bool,
    source_page_url: str,
) -> CoverCandidate | None:
    for query in external_queries(story)[:max_queries]:
        try:
            result_urls = fetch_search_results(query, timeout, retries, retry_sleep, max_results_per_query)
        except Exception as exc:
            print(f"[WARN] external search failed | {story.title} | {query} | {exc}", flush=True)
            continue

        for result_url in result_urls:
            if source_page_url and same_hostname(result_url, source_page_url):
                continue

            if looks_like_image_url(result_url):
                if skip_validate or validate_image_url(result_url, timeout):
                    return CoverCandidate(result_url, result_url, "external_search_direct_image")
                continue

            try:
                html = fetch_html(result_url, timeout, retries, retry_sleep)
            except Exception as exc:
                print(f"[WARN] external page failed | {story.title} | {result_url} | {exc}", flush=True)
                continue

            if not page_matches_story(html, story):
                continue

            cover_url = extract_cover_url(html, result_url, story.source_code)
            if not cover_url:
                continue
            if skip_validate or validate_image_url(cover_url, timeout):
                return CoverCandidate(cover_url, result_url, "external_search")

    return None


def has_cover(value: str | None) -> bool:
    return bool(clean_text(value))


def list_cover_candidate_stories(source_codes: list[str], limit: int, replace_broken: bool) -> list[StoryRow]:
    where = ["s.is_active = TRUE"]
    if not replace_broken:
        where.append("(s.cover_image_url IS NULL OR btrim(s.cover_image_url) = '')")
    params: list[Any] = []
    if source_codes:
        params.append(source_codes)
        where.append(f"src.code = ANY(%s)")
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
                s.id::text,
                src.code AS source_code,
                s.title,
                s.original_title,
                s.display_title,
                s.author,
                s.cover_image_url,
                s.source_url,
                s.catalog_url
            FROM stories s
            JOIN sources src ON src.id = s.source_id
            WHERE {" AND ".join(where)}
            ORDER BY
                CASE WHEN s.cover_image_url IS NULL OR btrim(s.cover_image_url) = '' THEN 0 ELSE 1 END,
                s.updated_at DESC,
                s.created_at DESC
            LIMIT %s
            """,
            params,
        ).fetchall()
    return [StoryRow(**dict(row)) for row in rows]


def update_story_cover(story_id: str, cover: CoverCandidate, replace_broken: bool) -> None:
    with connect() as conn:
        cover_predicate = "(cover_image_url IS NULL OR btrim(cover_image_url) = '')"
        if replace_broken:
            cover_predicate = "TRUE"
        conn.execute(
            f"""
            UPDATE stories
            SET cover_image_url = %s,
                metadata = COALESCE(metadata, '{{}}'::jsonb) || jsonb_build_object(
                    'cover_backfilled_at', now(),
                    'cover_backfill_method', %s,
                    'cover_backfill_page_url', %s
                ),
                updated_at = now()
            WHERE id = %s
              AND {cover_predicate}
            """,
            (cover.image_url, cover.method, cover.page_url, story_id),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill cover_image_url cho stories đang thiếu ảnh.")
    parser.add_argument("--sources", nargs="*", default=[], help="Ví dụ: hako wattpad_vn qidian naver_series royalroad. Bỏ trống = tất cả.")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--retry-sleep", type=float, default=1.5)
    parser.add_argument("--delay", type=float, default=0.8)
    parser.add_argument("--write", action="store_true", help="Ghi cover_image_url vào DB. Mặc định chỉ dry-run.")
    parser.add_argument("--skip-validate", action="store_true", help="Không HEAD/GET validate image URL.")
    parser.add_argument("--replace-broken", action="store_true", help="Nếu cover_image_url hiện tại không truy cập được, thay bằng ảnh tìm được từ nguồn khác.")
    parser.add_argument("--no-external-search", action="store_true", help="Chỉ thử trang gốc, không search ảnh từ nguồn khác.")
    parser.add_argument("--external-max-queries", type=int, default=4, help="Số query search ngoài tối đa cho mỗi story.")
    parser.add_argument("--external-max-results", type=int, default=6, help="Số kết quả search ngoài tối đa cho mỗi query.")
    args = parser.parse_args()

    stories = list_cover_candidate_stories(args.sources, args.limit, args.replace_broken)
    if not stories:
        print("No stories need cover backfill.", flush=True)
        return

    print(
        f"stories_to_check={len(stories)} "
        f"sources={','.join(args.sources) if args.sources else 'all'} "
        f"write={args.write} external_search={not args.no_external_search} "
        f"replace_broken={args.replace_broken}",
        flush=True,
    )
    updated = 0
    failed = 0
    skipped = 0

    for index, story in enumerate(stories, start=1):
        page_url = story.catalog_url or story.source_url
        cover: CoverCandidate | None = None
        try:
            if has_cover(story.cover_image_url):
                if not args.replace_broken:
                    skipped += 1
                    continue
                if args.skip_validate or validate_image_url(str(story.cover_image_url), args.timeout):
                    skipped += 1
                    print(f"[SKIP] {index}/{len(stories)} {story.source_code} | {story.title} | existing image is valid", flush=True)
                    time.sleep(args.delay)
                    continue
                print(f"[BROKEN] {index}/{len(stories)} {story.source_code} | {story.title} | {story.cover_image_url}", flush=True)

            if page_url:
                try:
                    html = fetch_html(page_url, args.timeout, args.retries, args.retry_sleep)
                    source_cover_url = extract_cover_url(html, page_url, story.source_code)
                    if source_cover_url and (args.skip_validate or validate_image_url(source_cover_url, args.timeout)):
                        cover = CoverCandidate(source_cover_url, page_url, "source_page")
                    elif source_cover_url:
                        print(f"[MISS] {index}/{len(stories)} {story.source_code} | {story.title} | invalid source image: {source_cover_url}", flush=True)
                except Exception as exc:
                    print(f"[WARN] source page failed | {index}/{len(stories)} {story.source_code} | {story.title} | {type(exc).__name__}: {exc}", flush=True)

            if cover is None and not args.no_external_search:
                cover = find_external_cover(
                    story=story,
                    timeout=args.timeout,
                    retries=args.retries,
                    retry_sleep=args.retry_sleep,
                    max_queries=max(0, args.external_max_queries),
                    max_results_per_query=max(0, args.external_max_results),
                    skip_validate=args.skip_validate,
                    source_page_url=page_url,
                )

            if cover is None:
                skipped += 1
                print(f"[MISS] {index}/{len(stories)} {story.source_code} | {story.title} | no image found", flush=True)
                continue

            if args.write:
                update_story_cover(story.id, cover, args.replace_broken)
                updated += 1
                print(f"[UPDATE] {index}/{len(stories)} {story.source_code} | {story.title} | {cover.method} | {cover.image_url}", flush=True)
            else:
                print(f"[DRY] {index}/{len(stories)} {story.source_code} | {story.title} | {cover.method} | {cover.image_url}", flush=True)
        except Exception as exc:
            failed += 1
            print(f"[ERROR] {index}/{len(stories)} {story.source_code} | {story.title} | {type(exc).__name__}: {exc}", flush=True)
        time.sleep(args.delay)

    print(f"Done. updated={updated} skipped={skipped} failed={failed} dry_run={not args.write}", flush=True)


if __name__ == "__main__":
    main()
