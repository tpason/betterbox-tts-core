#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from story_db.story_pipeline_db import repository as repo  # noqa: E402


HEADERS = {
    "User-Agent": "Mozilla/5.0 BetterBox-TTS system-villain discovery",
    "Accept-Language": "vi,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
}

SOURCE_BY_HOST = {
    "metruyencv.com": ("metruyencv", "Mê Truyện CV", "https://metruyencv.com"),
    "truyenyy.co": ("truyenyy", "TruyenYY", "https://truyenyy.co"),
    "truyenchu.com.vn": ("truyenchu_com_vn", "TruyenChu", "https://truyenchu.com.vn"),
    "truyenchuhay.vn": ("truyenchuhay", "TruyenChuHay", "https://truyenchuhay.vn"),
    "truyenchuhay.org": ("truyenchuhay", "TruyenChuHay", "https://truyenchuhay.org"),
    "ntruyen.biz": ("ntruyen", "nTruyen", "https://ntruyen.biz"),
    "sttruyen.com": ("sttruyen", "STTruyen", "https://sttruyen.com"),
    "truyenhoangdung.xyz": ("truyenhoangdung", "TruyenHoangDung", "https://www.truyenhoangdung.xyz"),
    "www.truyenhoangdung.xyz": ("truyenhoangdung", "TruyenHoangDung", "https://www.truyenhoangdung.xyz"),
    "docln.net": ("docln", "DocLN", "https://docln.net"),
    "ln.hako.vn": ("hako", "Hako", "https://ln.hako.vn"),
    "manhwatv6.com": ("manhwatv", "ManhwaTV", "https://manhwatv6.com"),
    "manhwatv5.com": ("manhwatv", "ManhwaTV", "https://manhwatv6.com"),
    "manhwatv4.com": ("manhwatv", "ManhwaTV", "https://manhwatv6.com"),
    "lightnovelpub.org": ("lightnovelpub", "LightNovelPub", "https://lightnovelpub.org"),
}

SUPPORTED_DB_SOURCES = {
    "docln",
    "hako",
    "manhwatv",
    "sttruyen",
    "truyenchuhay",
    "truyenhoangdung",
    "wattpad_vn",
    "truyenfull_today",
    "truyenyy",
    "qidian",
    "royalroad",
    "lightnovelpub",
}

DEFAULT_DB_UPSERT_SOURCES = {
    "sttruyen",
    "truyenfull_today",
    "truyenchuhay",
    "truyenhoangdung",
    "truyenyy",
    "wattpad_vn",
    "lightnovelpub",
}

DEFAULT_SEED_URLS = [
    "https://truyenyy.co/he-thong",
    "https://truyenyy.co/truyen/dich-ta-thien-menh-dai-nhan-vat-phan-phai",
    "https://truyenyy.co/truyen/toan-tri-doc-gia",
    "https://docln.net/truyen/21204-toan-tri-doc-gia",
    "https://ln.hako.vn/truyen/166-remonster",
    "https://manhwatv6.com/truyen-tranh/snvtnovel-thien-ma-phi-thang-truyen.html",
    "https://sttruyen.com/story/hoa-son-tai-khoi",
    "https://www.truyenhoangdung.xyz/truyen/hoa-son-tai-khoi-dich.html",
    "https://metruyencv.com/truyen/phan-phai-khong-he-thong-lam-sao-thang-a",
    "https://metruyencv.com/truyen/huyen-huyen-dai-phan-phai-he-thong",
    "https://metruyencv.com/truyen/phan-phai-ai-noi-la-ta-toi-tu-hon",
    "https://metruyencv.com/truyen/phan-phai-bat-dau-nam-lay-so-mot-nu-chinh",
    "https://metruyencv.com/truyen/phan-phai-lat-ban-khong-dua",
    "https://metruyencv.com/truyen/ma-dao-nu-de-vuong-phu-ta-dua-vao-kich-ban-quet-ngang-chu-thien",
    "https://metruyencv.com/truyen/nguoi-trong-sach-trung-sinh-con-nho-ma-ton-hac-hoa",
    "https://metruyencv.com/truyen/ta-dinh-cap-de-toc-phan-phai-tran-sat-thien-menh-chi-nu",
    "https://truyenchu.com.vn/truyen/phan-phai-the-tu-bat-dau-cuong-chiem-thien-menh-chi-tu-than-ty.html",
    "https://truyenchuhay.org/ta-dinh-cap-de-toc-phan-phai-tran-sat-thien-menh-chi-nu",
    "https://truyenchuhay.vn/phan-phai-tu-hon-nguoi-xach-hien-tai-nguoi-khoc-cai-gi",
]

DEFAULT_INCLUDE_KEYWORDS = [
    "hệ thống",
    "system",
    "phản phái",
    "nhân vật phản diện",
    "đại phản phái",
    "trùm phản diện",
    "tà đạo",
    "ma đạo",
    "ma giáo",
    "ma tôn",
    "thiên mệnh chi tử",
    "khí vận chi tử",
    "thiên mệnh",
    "khí vận",
    "thiên đạo",
    "xuyên sách",
    "xuyên thư",
    "kịch bản",
    "nhân sinh kịch bản",
    "không hệ thống",
    "không có hệ thống",
    "vô địch lưu",
    "sát phạt",
    "toàn trí độc giả",
    "omniscient reader",
    "re:monster",
    "remonster",
    "goblin",
    "goblem",
    "quái vật",
    "hoa sơn tái khởi",
    "thiên ma phi thăng",
    "murim",
    "võ thuật",
    "hồi quy",
    "trọng sinh",
    "chuyển sinh",
]

DEFAULT_EXCLUDE_KEYWORDS = [
    "đam mỹ",
    "bách hợp",
    "bl",
    "gl",
    "boy love",
    "girl love",
    "yaoi",
    "yuri",
    "hentai",
    "smut",
]

VIBE_RULES = {
    "main_co_he_thong": ["hệ thống", "system", "đánh dấu", "rút thẻ", "mô phỏng khí"],
    "phan_phai_he_thong": ["phản phái hệ thống", "trùm phản diện", "đại phản phái", "nhân vật phản diện"],
    "chong_thien_menh_chi_tu": ["thiên mệnh chi tử", "khí vận chi tử", "khí vận chi nữ", "thiên đạo khí vận"],
    "main_khong_he_thong": ["không hệ thống", "không có hệ thống", "không phải khí vận chi tử"],
    "ta_dao_ma_dao": ["tà đạo", "ma đạo", "ma giáo", "ma tôn", "hợp hoan", "hắc hóa"],
    "xuyen_sach_kich_ban": ["xuyên sách", "xuyên thư", "kịch bản", "nhân sinh kịch bản", "người trong sách"],
    "favorite_seed": ["toàn trí độc giả", "re:monster", "remonster", "hoa sơn tái khởi", "thiên ma phi thăng"],
    "murim_chuyen_sinh": ["murim", "võ thuật", "hồi quy", "trọng sinh", "chuyển sinh", "goblin", "quái vật"],
}

KNOWN_CATEGORY_SEGMENTS = {
    "ngon-tinh",
    "kiem-hiep",
    "tien-hiep",
    "huyen-huyen",
    "di-gioi",
    "do-thi",
    "quan-su",
    "lich-su",
    "xuyen-khong",
    "trong-sinh",
    "he-thong",
    "mat-the",
    "vong-du",
    "sac",
    "dam-my",
    "bach-hop",
}


@dataclass
class StoryCandidate:
    source_code: str
    source_name: str
    source_url: str
    title: str
    author: str = ""
    category: str = ""
    status: str = ""
    description: str = ""
    total_chapters: int = 0
    language: str = "vi"
    discovered_from: str = ""
    tags: list[str] = field(default_factory=list)
    matched_keywords: list[str] = field(default_factory=list)
    matched_vibes: list[str] = field(default_factory=list)
    score: int = 0
    crawled_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.strip().lower())
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-") or "story"


def split_csv(value: str, defaults: list[str]) -> list[str]:
    if not value:
        return defaults
    return [part.strip() for part in value.split(",") if part.strip()]


def parse_int(value: str | None) -> int:
    digits = re.sub(r"[^\d]", "", value or "")
    return int(digits) if digits else 0


def source_info(url: str) -> tuple[str, str, str]:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    if host in SOURCE_BY_HOST:
        return SOURCE_BY_HOST[host]
    code = re.sub(r"[^a-z0-9]+", "_", host).strip("_") or "unknown"
    return code, host, f"{urlparse(url).scheme}://{host}"


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
                print(f"[WARN] retry {attempt}/{retries}: {url} | {exc}")
                time.sleep(retry_sleep)
    raise RuntimeError(f"Không fetch được URL sau {retries} lần: {url} | {last_error}") from last_error


def looks_like_story_url(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    parts = [part for part in path.split("/") if part]
    if not parts:
        return False
    if any(part in {"the-loai", "danh-sach", "cat", "tag", "search", "tim-kiem", "tac-gia", "author"} for part in parts):
        return False
    if any(part in {"truyen", "story"} for part in parts):
        return True
    host = parsed.netloc.lower().removeprefix("www.")
    if host.startswith("manhwatv") and "truyen-tranh" in parts and path.endswith(".html"):
        return True
    if len(parts) == 1 and host in {"truyenchuhay.vn", "truyenchuhay.org", "truyenfull.today", "wattpad.com.vn"}:
        return parts[0] not in KNOWN_CATEGORY_SEGMENTS
    return False


def extract_story_links(base_url: str, html: str, max_links: int) -> list[str]:
    host = urlparse(base_url).netloc.lower()
    soup = BeautifulSoup(html, "html.parser")
    links: list[str] = []
    seen: set[str] = set()
    for link in soup.select("a[href]"):
        href = link.get("href") or ""
        url = urljoin(base_url, href).split("#", 1)[0].rstrip("/")
        parsed = urlparse(url)
        if parsed.netloc.lower() != host:
            continue
        if not looks_like_story_url(url):
            continue
        if re.search(r"/(chuong|chapter|chap)-?\d+", parsed.path, flags=re.IGNORECASE):
            continue
        title = clean_text(link.get_text(" ", strip=True))
        if len(title) < 3 or url in seen:
            continue
        seen.add(url)
        links.append(url)
        if max_links > 0 and len(links) >= max_links:
            break
    return links


def label_value(text: str, labels: Iterable[str]) -> str:
    for label in labels:
        match = re.search(rf"{re.escape(label)}\s*[:：]\s*(.*?)(?:\s{{2,}}| Thể loại| Trạng thái| Số chương|$)", text, re.I)
        if match:
            return clean_text(match.group(1))
    return ""


def parse_story_page(url: str, html: str, discovered_from: str) -> StoryCandidate | None:
    source_code, source_name, _ = source_info(url)
    soup = BeautifulSoup(html, "html.parser")
    page_text = clean_text(soup.get_text(" ", strip=True))

    title_node = soup.select_one("h1")
    title = clean_text(title_node.get_text(" ", strip=True)) if title_node else ""
    if not title:
        og_title = soup.select_one("meta[property='og:title'], meta[name='title']")
        title = clean_text(og_title.get("content") if og_title else "")
    if not title:
        return None
    title = re.sub(r"\s*[-|]\s*(Mê Truyện CV|Truyện Chữ|TruyenYY|STTRUYEN).*$", "", title, flags=re.I).strip()

    author = label_value(page_text, ["Tác giả", "Author"])
    author_node = soup.select_one("a[href*='tac-gia'], a[href*='author']")
    if author_node:
        author = clean_text(author_node.get_text(" ", strip=True)) or author

    tags = [
        clean_text(tag.get_text(" ", strip=True))
        for tag in soup.select("a[href*='the-loai'], a[href*='the-loai'], a[href*='cat/'], a[href*='tag']")
        if clean_text(tag.get_text(" ", strip=True))
    ]
    category = ", ".join(dict.fromkeys(tags))
    if not category:
        category = label_value(page_text, ["Thể loại", "Thể Loại", "Category"])

    status = label_value(page_text, ["Trạng thái", "Tình trạng", "Status"])
    total_chapters = 0
    for pattern in [
        r"(\d[\d.,]*)\s*(?:Chương|chương|chap|chapter)",
        r"Tổng\s*số\s*chương\s*[:：]?\s*(\d[\d.,]*)",
        r"Số\s*chương\s*[:：]?\s*(\d[\d.,]*)",
    ]:
        match = re.search(pattern, page_text, re.I)
        if match:
            total_chapters = max(total_chapters, parse_int(match.group(1)))

    description = ""
    description_node = soup.select_one(
        ".description, .desc, .summary, .intro, .book-intro, "
        ".story-detail-info, .js-truncate, [class*='desc'], [class*='intro']"
    )
    if description_node:
        description = clean_text(description_node.get_text(" ", strip=True))
    if not description:
        meta_desc = soup.select_one("meta[name='description'], meta[property='og:description']")
        description = clean_text(meta_desc.get("content") if meta_desc else "")
    if not description:
        marker_match = re.search(r"(?:GIỚI THIỆU|Giới thiệu|tóm tắt nội dung truyện)(.*?)(?:Danh sách chương|CHƯƠNG MỚI|CÙNG TÁC GIẢ|$)", page_text, re.I)
        description = clean_text(marker_match.group(1) if marker_match else page_text[:700])

    return StoryCandidate(
        source_code=source_code,
        source_name=source_name,
        source_url=url,
        title=title,
        author=author,
        category=category,
        status=status,
        description=description[:1200],
        total_chapters=total_chapters,
        discovered_from=discovered_from,
        tags=list(dict.fromkeys(tags)),
    )


def score_candidate(candidate: StoryCandidate, include_keywords: list[str], exclude_keywords: list[str]) -> bool:
    text = " ".join(
        [candidate.title, candidate.author, candidate.category, candidate.status, candidate.description, " ".join(candidate.tags)]
    ).lower()
    excluded = [keyword for keyword in exclude_keywords if keyword.lower() in text]
    if excluded:
        return False

    candidate.matched_keywords = [keyword for keyword in include_keywords if keyword.lower() in text]
    candidate.matched_vibes = [
        vibe for vibe, terms in VIBE_RULES.items() if any(term.lower() in text for term in terms)
    ]
    candidate.score = len(candidate.matched_keywords) * 10 + len(candidate.matched_vibes) * 15
    if "thiên mệnh chi tử" in text or "khí vận chi tử" in text:
        candidate.score += 20
    if "không hệ thống" in text or "không có hệ thống" in text:
        candidate.score += 15
    if "phản phái" in text and "hệ thống" in text:
        candidate.score += 15
    return bool(candidate.matched_keywords)


def dedupe_candidates(candidates: Iterable[StoryCandidate]) -> list[StoryCandidate]:
    best_by_url: dict[str, StoryCandidate] = {}
    for candidate in candidates:
        key = candidate.source_url.rstrip("/")
        current = best_by_url.get(key)
        if current is None or candidate.score > current.score:
            best_by_url[key] = candidate
    return sorted(best_by_url.values(), key=lambda item: (-item.score, item.source_code, item.title.lower()))


def write_outputs(candidates: list[StoryCandidate], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(candidates),
        "candidates": [asdict(candidate) for candidate in candidates],
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    jsonl_path = output.with_suffix(".jsonl")
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for candidate in candidates:
            handle.write(json.dumps(asdict(candidate), ensure_ascii=False) + "\n")
    print(f"[OK] wrote {len(candidates)} candidates: {output}")
    print(f"[OK] wrote JSONL: {jsonl_path}")


def upsert_candidates(candidates: list[StoryCandidate], allowed_sources: set[str]) -> None:
    seen_sources: set[str] = set()
    for candidate in candidates:
        if candidate.source_code not in SUPPORTED_DB_SOURCES:
            print(
                "[SKIP-DB] unsupported by crawl_stories_from_db.py: "
                f"{candidate.source_code} | {candidate.title} | {candidate.source_url}"
            )
            continue
        if candidate.source_code not in allowed_sources:
            print(
                "[SKIP-DB] source is not in production text-crawl list: "
                f"{candidate.source_code} | {candidate.title} | {candidate.source_url}"
            )
            continue
        if candidate.source_code not in seen_sources:
            _, _, base_url = source_info(candidate.source_url)
            repo.upsert_source(candidate.source_code, candidate.source_name, base_url)
            seen_sources.add(candidate.source_code)
        story = repo.upsert_story(
            candidate.source_code,
            {
                "source_story_id": slugify(urlparse(candidate.source_url).path.rstrip("/").rsplit("/", 1)[-1] or candidate.title),
                "title": candidate.title,
                "author": candidate.author,
                "category": candidate.category,
                "status": candidate.status,
                "language": candidate.language,
                "source_url": candidate.source_url,
                "catalog_url": candidate.source_url,
                "description": candidate.description,
                "total_chapters": candidate.total_chapters,
                "metadata": {
                    "tags": candidate.tags,
                    "matched_keywords": candidate.matched_keywords,
                    "matched_vibes": candidate.matched_vibes,
                    "system_villain_score": candidate.score,
                    "discovered_from": candidate.discovered_from,
                },
            },
        )
        print(f"[DB] upsert {story['title']} | {candidate.source_code} | score={candidate.score}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Discover Vietnamese web novel metadata matching system/villain/heavenly-fate vibes."
    )
    parser.add_argument("--urls", nargs="*", default=DEFAULT_SEED_URLS, help="Seed list/story URLs.")
    parser.add_argument("--extra-url", action="append", default=[], help="Add one seed URL without replacing defaults.")
    parser.add_argument("--include-keywords", default=",".join(DEFAULT_INCLUDE_KEYWORDS))
    parser.add_argument("--exclude-keywords", default=",".join(DEFAULT_EXCLUDE_KEYWORDS))
    parser.add_argument("--max-links-per-list", type=int, default=80)
    parser.add_argument("--max-stories", type=int, default=120)
    parser.add_argument("--timeout", type=int, default=12)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--retry-sleep", type=float, default=1.5)
    parser.add_argument("--sleep", type=float, default=0.4, help="Delay between story detail requests.")
    parser.add_argument("--output", type=Path, default=Path("story_data/discovery/system_villain_stories.json"))
    parser.add_argument("--upsert-db", action="store_true", help="Upsert matched story metadata into story_db.")
    parser.add_argument(
        "--db-sources",
        nargs="*",
        default=sorted(DEFAULT_DB_UPSERT_SOURCES),
        help=(
            "Source codes allowed for DB upsert. Default skips sources that currently do not "
            "produce public Vietnamese text chapters in production."
        ),
    )
    args = parser.parse_args()

    include_keywords = split_csv(args.include_keywords, DEFAULT_INCLUDE_KEYWORDS)
    exclude_keywords = split_csv(args.exclude_keywords, DEFAULT_EXCLUDE_KEYWORDS)
    seed_urls = list(dict.fromkeys([*args.urls, *args.extra_url]))

    story_urls: list[tuple[str, str]] = []
    for seed_url in seed_urls:
        try:
            html = fetch_html(seed_url, args.timeout, args.retries, args.retry_sleep)
        except Exception as exc:
            print(f"[WARN] skip seed {seed_url}: {exc}")
            continue
        if looks_like_story_url(seed_url):
            story_urls.append((seed_url.rstrip("/"), seed_url))
            continue
        links = extract_story_links(seed_url, html, args.max_links_per_list)
        print(f"[INFO] {seed_url}: found {len(links)} story links")
        story_urls.extend((url, seed_url) for url in links)

    candidates: list[StoryCandidate] = []
    seen_story_urls: set[str] = set()
    for story_url, discovered_from in story_urls:
        if story_url in seen_story_urls:
            continue
        seen_story_urls.add(story_url)
        if args.max_stories > 0 and len(seen_story_urls) > args.max_stories:
            break
        try:
            html = fetch_html(story_url, args.timeout, args.retries, args.retry_sleep)
            candidate = parse_story_page(story_url, html, discovered_from)
        except Exception as exc:
            print(f"[WARN] skip story {story_url}: {exc}")
            continue
        if candidate and score_candidate(candidate, include_keywords, exclude_keywords):
            candidates.append(candidate)
            print(f"[HIT] score={candidate.score:03d} {candidate.title} | {candidate.source_url}")
        time.sleep(args.sleep)

    candidates = dedupe_candidates(candidates)
    write_outputs(candidates, args.output)
    if args.upsert_db:
        upsert_candidates(candidates, {source.strip() for source in args.db_sources if source.strip()})


if __name__ == "__main__":
    main()
