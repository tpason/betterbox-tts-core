#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from requests import HTTPError
from requests.exceptions import ConnectionError as RequestsConnectionError

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from datetime import timedelta  # noqa: E402 — used by UrlSkipCache

from scripts.story_pipeline.crawl_qidian_rankings import (  # noqa: E402
    DEFAULT_RANK_URLS as QIDIAN_RANK_URLS,
    parse_rank_page,
)
from story_db.story_pipeline_db.repository import story_priority_sort_key  # noqa: E402


SOURCES = {
    "qidian": {
        "name": "Qidian",
        "base_url": "https://www.qidian.com",
        "language": "zh",
    },
    "wattpad_vn": {
        "name": "Wattpad VN",
        "base_url": "https://wattpad.com.vn",
        "language": "vi",
    },
    "truyenfull_today": {
        "name": "TruyenFull Today",
        "base_url": "https://truyenfull.today",
        "language": "vi",
    },
    "hako": {
        "name": "Hako",
        "base_url": "https://ln.hako.vn",
        "language": "vi",
    },
    "naver_series": {
        "name": "Naver Series",
        "base_url": "https://series.naver.com",
        "language": "ko",
    },
    "royalroad": {
        "name": "Royal Road",
        "base_url": "https://www.royalroad.com",
        "language": "en",
    },
    "skydemonorder": {
        "name": "Sky Demon Order",
        "base_url": "https://skydemonorder.com",
        "language": "en",
    },
}

DEFAULT_WATTPAD_URLS = [
    "https://wattpad.com.vn/truyen-hot",
    "https://wattpad.com.vn/the-loai/tien-hiep",
    "https://wattpad.com.vn/the-loai/kiem-hiep",
    "https://wattpad.com.vn/the-loai/huyen-huyen",
    "https://wattpad.com.vn/the-loai/he-thong",
    "https://wattpad.com.vn/the-loai/di-gioi",
    "https://wattpad.com.vn/the-loai/di-nang",
    "https://wattpad.com.vn/the-loai/khoa-huyen",
    "https://wattpad.com.vn/the-loai/do-thi",
    "https://wattpad.com.vn/the-loai/xuyen-khong",
    "https://wattpad.com.vn/the-loai/xuyen-nhanh",
    "https://wattpad.com.vn/the-loai/trong-sinh",
    "https://wattpad.com.vn/the-loai/dong-phuong",
    "https://wattpad.com.vn/the-loai/mat-the",
    "https://wattpad.com.vn/the-loai/vong-du",
]

DEFAULT_HAKO_URLS = [
    # Hako keeps direct story pages public, but list pages currently return "Không có truyện nào"
    # to non-browser requests in this environment. Pass --hako-urls explicitly if a working list
    # URL becomes available again.
]

DEFAULT_TRUYENFULL_TODAY_URLS = [
    "https://truyenfull.today/danh-sach/truyen-hot/",
    "https://truyenfull.today/danh-sach/truyen-full/",
    "https://truyenfull.today/danh-sach/tien-hiep-hay/",
    "https://truyenfull.today/danh-sach/kiem-hiep-hay/",
    "https://truyenfull.today/the-loai/tien-hiep/",
    "https://truyenfull.today/the-loai/huyen-huyen/",
    "https://truyenfull.today/the-loai/he-thong/",
    "https://truyenfull.today/the-loai/kiem-hiep/",
    "https://truyenfull.today/the-loai/di-gioi/",
    "https://truyenfull.today/the-loai/khoa-huyen/",
    "https://truyenfull.today/the-loai/do-thi/",
    "https://truyenfull.today/the-loai/xuyen-khong/",
    "https://truyenfull.today/the-loai/xuyen-nhanh/",
    "https://truyenfull.today/the-loai/trong-sinh/",
    "https://truyenfull.today/the-loai/mat-the/",
    "https://truyenfull.today/the-loai/vong-du/",
]

DEFAULT_TRUYENFULL_TODAY_AUTHOR_URLS = [
    "https://truyenfull.today/tac-gia/nhi-can/",
    "https://truyenfull.today/tac-gia/vong-ngu/",
    "https://truyenfull.today/tac-gia/thien-tam-tho-dau/",
    "https://truyenfull.today/tac-gia/than-dong/",
    "https://truyenfull.today/tac-gia/mong-nhap-than-co/",
    "https://truyenfull.today/tac-gia/yem-but-tieu-sinh/",
    "https://truyenfull.today/tac-gia/phong-hoa-hi-chu-hau/",
]

TRUYENFULL_TODAY_AUTHOR_SLUGS = {
    "nhi-can": "Nhĩ Căn",
    "vong-ngu": "Vong Ngữ",
    "thien-tam-tho-dau": "Thiên Tàm Thổ Đậu",
    "than-dong": "Thần Đông",
    "mong-nhap-than-co": "Mộng Nhập Thần Cơ",
    "yem-but-tieu-sinh": "Yếm Bút Tiêu Sinh",
    "phong-hoa-hi-chu-hau": "Phong Hỏa Hí Chư Hầu",
}

DEFAULT_TRUYENFULL_TODAY_CLASSIC_STORY_URLS = [
    "https://truyenfull.today/tien-nghich/",
    "https://truyenfull.today/pham-nhan-tu-tien/",
    "https://truyenfull.today/nhat-niem-vinh-hang/",
    "https://truyenfull.today/cau-ma/",
    "https://truyenfull.today/nga-duc-phong-thien/",
    "https://truyenfull.today/quang-am-chi-ngoai/",
    "https://truyenfull.today/dau-pha-thuong-khung/",
    "https://truyenfull.today/the-gioi-hoan-my/",
    "https://truyenfull.today/de-ba/",
]

DEFAULT_NAVER_SERIES_URLS = [
    "https://series.naver.com/novel/top100List.series?categoryCode=ALL&rankingTypeCode=DAILY",
    "https://series.naver.com/novel/top100List.series?categoryCode=ALL&rankingTypeCode=WEEKLY",
    "https://series.naver.com/novel/top100List.series?categoryCode=ALL&rankingTypeCode=MONTHLY",
]

DEFAULT_ROYALROAD_URLS = [
    "https://www.royalroad.com/fictions/best-rated",
    "https://www.royalroad.com/fictions/trending",
    "https://www.royalroad.com/fictions/weekly-popular",
    "https://www.royalroad.com/fictions/rising-stars",
    "https://www.royalroad.com/fictions/complete",
    "https://www.royalroad.com/fictions/active-popular",
]

DEFAULT_SKYDEMONORDER_URLS = [
    "https://skydemonorder.com/projects?sort=trending&pp=48",
    "https://skydemonorder.com/projects?sort=popular&pp=48",
    "https://skydemonorder.com/projects?sort=hot&pp=48",
    "https://skydemonorder.com/projects?pp=48",
]

DEFAULT_PRODUCTION_SOURCES = ["truyenfull_today", "royalroad"]

DEFAULT_INCLUDE_KEYWORDS = [
    "tiên hiệp",
    "huyền huyễn",
    "tu tiên",
    "tu luyện",
    "hệ thống",
    "đô thị tu tiên",
    "trọng sinh",
    "xuyên không",
    "xuyên nhanh",
    "kiếm hiệp",
    "dị giới",
    "dị năng",
    "khoa huyễn",
    "đông phương",
    "mạt thế",
    "võng du",
    "xianxia",
    "xuanhuan",
    "cultivation",
    "martial arts",
    "wuxia",
    "murim",
    "sword",
    "system",
    "litrpg",
    "progression",
    "progression fantasy",
    "dungeon",
    "isekai",
    "portal fantasy",
    "magic academy",
    "reincarnation",
    "regression",
    "仙侠",
    "玄幻",
    "修仙",
    "修真",
    "系统",
    "重生",
    "穿越",
    "都市",
    "판타지",
    "무협",
    "현판",
    "로판",
    "회귀",
    "빙의",
    "환생",
    "헌터",
    "탑",
    "마법",
    "이세계",
    "웹소설",
]

DEFAULT_EXCLUDE_KEYWORDS = [
    "18+",
    "adult",
    "mature",
    "nsfw",
    "sắc",
    "cao h",
    "h văn",
    "hentai",
    "ecchi",
    "smut",
    "đam mỹ",
    "bách hợp",
    "boys love",
    "girls love",
    "boy's love",
    "girl's love",
    "yaoi",
    "yuri",
    "bl",
    "gl",
    "lgbt",
    "adult",
    "耽美",
    "百合",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 BetterBox-TTS story discovery",
    "Accept-Language": "vi,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
}


_URL_TYPE_CONFIG: dict[str, dict[str, int]] = {
    "author":   {"base_days": 7,  "cap_days": 30},
    "category": {"base_days": 1,  "cap_days": 7},
    "ranking":  {"base_days": 1,  "cap_days": 7},
    "default":  {"base_days": 1,  "cap_days": 14},
}


def _infer_url_type(url: str) -> str:
    path = urlparse(url).path
    if "/tac-gia/" in path or "/author/" in path:
        return "author"
    if "ranking" in path or "best-rated" in path or "top100" in path:
        return "ranking"
    if "/the-loai/" in path or "/danh-sach/" in path or "/genre/" in path:
        return "category"
    return "default"


class UrlSkipCache:
    """File-backed per-URL exponential backoff cache for discovery runs.

    Single-writer assumption: only one discovery process should write to the
    same file at a time. Safe for the default single-replica Docker setup.

    Schema version is stored at top level so future migrations are detectable.
    """

    _SCHEMA_VERSION = 1

    def __init__(self, path: Path, *, enabled: bool = True) -> None:
        self.path = path
        self.enabled = enabled
        self._state: dict = {}
        if enabled:
            self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self._state = {"schema_version": self._SCHEMA_VERSION, "updated_at": "", "urls": {}}
            return
        try:
            self._state = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(self._state.get("urls"), dict):
                raise ValueError("bad schema")
        except Exception as exc:
            bak = self.path.with_suffix(".json.bak")
            print(f"[SKIP-CACHE] corrupt state file ({exc}), moving to {bak}", flush=True)
            try:
                self.path.rename(bak)
            except OSError:
                pass
            self._state = {"schema_version": self._SCHEMA_VERSION, "updated_at": "", "urls": {}}

    def _save(self) -> None:
        self._state["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def should_skip(self, url: str) -> bool:
        if not self.enabled:
            return False
        entry = self._state.get("urls", {}).get(url, {})
        skip_until = entry.get("skip_until")
        if not skip_until:
            return False
        try:
            return datetime.fromisoformat(skip_until) > datetime.now(timezone.utc)
        except ValueError:
            return False

    def skip_until_str(self, url: str) -> str:
        return self._state.get("urls", {}).get(url, {}).get("skip_until", "")

    def record_result(self, url: str, new_count: int, url_type: str | None = None) -> None:
        if not self.enabled:
            return
        resolved_type = url_type or _infer_url_type(url)
        urls = self._state.setdefault("urls", {})
        entry = urls.setdefault(url, {})
        entry["url_type"] = resolved_type
        entry["last_checked_at"] = datetime.now(timezone.utc).isoformat()
        if new_count > 0:
            entry["consecutive_empty"] = 0
            entry.pop("skip_until", None)
            entry["last_new_at"] = datetime.now(timezone.utc).isoformat()
        else:
            cfg = _URL_TYPE_CONFIG.get(resolved_type, _URL_TYPE_CONFIG["default"])
            n = entry.get("consecutive_empty", 0) + 1
            entry["consecutive_empty"] = n
            delay_days = min(cfg["cap_days"], cfg["base_days"] * (2 ** max(n - 1, 0)))
            entry["skip_until"] = (
                datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=delay_days)
            ).isoformat()
        self._save()


@dataclass
class StoryCandidate:
    source_code: str
    source_url: str
    title: str
    author: str = ""
    category: str = ""
    status: str = ""
    description: str = ""
    cover_image_url: str = ""
    rank_name: str = ""
    rank_position: int = 0
    total_chapters: int = 0
    views: int = 0
    language: str = "vi"
    discovered_from: str = ""
    tags: list[str] = field(default_factory=list)
    matched_include_keywords: list[str] = field(default_factory=list)
    matched_exclude_keywords: list[str] = field(default_factory=list)


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def slugify_vietnamese(value: str) -> str:
    import unicodedata

    normalized = unicodedata.normalize("NFKD", value.strip().lower())
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")
    return slug


def split_terms(value: str, defaults: list[str]) -> list[str]:
    if not value:
        return defaults
    return [term.strip() for term in value.split(",") if term.strip()]


def parse_int(value: str) -> int:
    digits = re.sub(r"[^\d]", "", value or "")
    return int(digits) if digits else 0


def stable_source_story_id(url: str) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    for key in ("bookId", "productNo", "novelId"):
        if query.get(key):
            return query[key]
    path = parsed.path.rstrip("/")
    return path.rsplit("/", 1)[-1] or url


def canonical_story_url(url: str) -> str:
    parsed = urlparse(url.strip())
    path = parsed.path.rstrip("/")
    if path:
        path = f"{path}/"
    return urlunparse(parsed._replace(path=path, params="", query="", fragment=""))


def candidate_search_text(candidate: StoryCandidate) -> str:
    return " ".join(
        [
            candidate.title,
            candidate.author,
            candidate.category,
            candidate.status,
            candidate.description,
            " ".join(candidate.tags),
        ]
    ).lower()


def candidate_exclude_text(candidate: StoryCandidate) -> str:
    return " ".join(
        [
            candidate.title,
            candidate.author,
            candidate.category,
            candidate.status,
            " ".join(candidate.tags),
        ]
    ).lower()


def matches_exclude_keyword(text: str, keyword: str) -> bool:
    keyword = keyword.lower()
    if len(keyword) <= 3 and re.fullmatch(r"[a-z0-9]+", keyword):
        return bool(re.search(rf"\b{re.escape(keyword)}\b", text))
    return bool(re.search(rf"\b{re.escape(keyword)}\b", text)) or keyword in text


def apply_filters(
    candidates: Iterable[StoryCandidate],
    include_keywords: list[str],
    exclude_keywords: list[str],
    require_include: bool,
    min_chapters: int,
) -> list[StoryCandidate]:
    accepted: list[StoryCandidate] = []
    for candidate in candidates:
        text = candidate_search_text(candidate)
        exclude_text = candidate_exclude_text(candidate)
        candidate.matched_include_keywords = [
            keyword for keyword in include_keywords if keyword.lower() in text
        ]
        candidate.matched_exclude_keywords = [
            keyword for keyword in exclude_keywords if matches_exclude_keyword(exclude_text, keyword)
        ]
        if candidate.matched_exclude_keywords:
            continue
        if require_include and not candidate.matched_include_keywords:
            continue
        if min_chapters and candidate.total_chapters and candidate.total_chapters < min_chapters:
            continue
        accepted.append(candidate)
    return accepted


def probe_host(url: str, timeout: int = 8) -> bool:
    """Quick reachability check for a single URL — tries HEAD then GET."""
    try:
        r = requests.head(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        return r.status_code < 500
    except requests.RequestException:
        pass
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout, stream=True)
        r.close()
        return r.status_code < 500
    except requests.RequestException:
        return False


def preflight_check_hosts(url_groups: dict[str, list[str]], timeout: int = 8) -> set[str]:
    """
    Probe one representative URL per host. Returns the set of reachable netlocs.
    url_groups: {label: [url, ...]} — only the first URL per unique host is probed.
    """
    seen_hosts: dict[str, str] = {}
    for urls in url_groups.values():
        for url in urls:
            host = urlparse(url).netloc.lower()
            if host and host not in seen_hosts:
                seen_hosts[host] = url

    reachable: set[str] = set()
    for host, probe_url in seen_hosts.items():
        ok = probe_host(probe_url, timeout)
        if ok:
            reachable.add(host)
            print(f"[PREFLIGHT] OK   {host}", flush=True)
        else:
            print(f"[PREFLIGHT] FAIL {host} — all URLs from this host will be skipped", flush=True)
    return reachable


def filter_urls_by_host(urls: list[str], reachable_hosts: set[str]) -> list[str]:
    if not reachable_hosts:
        return urls
    return [u for u in urls if urlparse(u).netloc.lower() in reachable_hosts]


def try_rediscover_domain_urls(
    host: str,
    original_urls: list[str],
    timeout: int = 8,
) -> list[str]:
    """
    When a host IS reachable but specific URL paths may have changed:
    visit the domain root and look for navigation links matching the same
    path patterns as the original URLs (/the-loai/, /danh-sach/, /tac-gia/).

    Returns a replacement list of discovered URLs (may be empty if none found).
    """
    if not original_urls:
        return []
    scheme = urlparse(original_urls[0]).scheme or "https"
    root_url = f"{scheme}://{host}/"

    # Collect path patterns we care about from the original URLs
    patterns: list[str] = []
    for url in original_urls:
        path = urlparse(url).path
        segments = [s for s in path.split("/") if s]
        if segments:
            patterns.append(f"/{segments[0]}/")  # e.g. /the-loai/ /danh-sach/

    patterns = list(dict.fromkeys(patterns))
    if not patterns:
        return []

    try:
        resp = requests.get(root_url, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        html = resp.text
    except requests.RequestException as exc:
        print(f"[REDISCOVER] domain root not reachable: {root_url} | {exc}", flush=True)
        return []

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    found: list[str] = []
    seen: set[str] = set()
    for anchor in soup.select("a[href]"):
        href = str(anchor.get("href") or "")
        full_url = urljoin(root_url, href).split("#", 1)[0]
        parsed = urlparse(full_url)
        if parsed.netloc.lower() != host:
            continue
        path = parsed.path
        if not any(path.startswith(pat) for pat in patterns):
            continue
        canonical = full_url.rstrip("/") + "/"
        if canonical in seen:
            continue
        seen.add(canonical)
        found.append(canonical)

    if found:
        print(f"[REDISCOVER] {host}: found {len(found)} candidate URL(s) matching {patterns}", flush=True)
        for url in found[:10]:
            print(f"  {url}", flush=True)
    else:
        print(f"[REDISCOVER] {host}: no matching URLs found in domain root", flush=True)
    return found


def preflight_and_rediscover(
    url_groups: dict[str, list[str]],
    timeout: int = 8,
) -> tuple[set[str], dict[str, list[str]]]:
    """
    Extended preflight that also tries URL rediscovery for reachable hosts
    whose original paths may have changed.

    Returns:
      - reachable_hosts: set of netloc strings that responded OK
      - rediscovered: {host: [new_urls]} for hosts where new paths were found
    """
    seen_hosts: dict[str, list[str]] = {}
    for label, urls in url_groups.items():
        for url in urls:
            host = urlparse(url).netloc.lower()
            if host:
                seen_hosts.setdefault(host, [])
                seen_hosts[host].append(url)

    reachable: set[str] = set()
    rediscovered: dict[str, list[str]] = {}

    for host, urls in seen_hosts.items():
        # Probe the first URL directly
        ok = probe_host(urls[0], timeout)
        if ok:
            reachable.add(host)
            print(f"[PREFLIGHT] OK   {host}", flush=True)
        else:
            # Domain root might still be up with changed paths
            print(f"[PREFLIGHT] FAIL {host} — checking for URL changes…", flush=True)
            new_urls = try_rediscover_domain_urls(host, urls, timeout)
            if new_urls:
                reachable.add(host)
                rediscovered[host] = new_urls
                print(f"[PREFLIGHT] RECOVERED {host} with {len(new_urls)} new URL(s)", flush=True)
            else:
                print(f"[PREFLIGHT] DOWN {host} — all URLs from this host will be skipped", flush=True)

    return reachable, rediscovered


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
                print(f"[WARN] discovery fetch retry {attempt}/{retries}: {url} | {exc}")
                time.sleep(retry_sleep)
    raise RuntimeError(f"Không fetch được discovery URL sau {retries} lần: {url} | {last_error}") from last_error


def is_not_found_error(exc: requests.RequestException) -> bool:
    return isinstance(exc, HTTPError) and exc.response is not None and exc.response.status_code == 404


def is_pagination_end_error(exc: Exception) -> bool:
    return isinstance(exc, HTTPError) and exc.response is not None and exc.response.status_code in {404, 500}


def is_source_unavailable_error(exc: Exception) -> bool:
    if isinstance(exc, RuntimeError) and exc.__cause__ is not None:
        return is_source_unavailable_error(exc.__cause__)
    if isinstance(exc, RequestsConnectionError):
        return True
    text = str(exc).lower()
    return any(
        marker in text
        for marker in [
            "failed to resolve",
            "name resolution",
            "no address associated with hostname",
            "connection reset by peer",
            "connection aborted",
            "temporary failure in name resolution",
        ]
    )


def add_query_page(url: str, page: int) -> str:
    if page <= 1:
        return url
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["page"] = str(page)
    return urlunparse(parsed._replace(query=urlencode(query)))


def add_skydemonorder_page(url: str, page: int) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault("pp", "48")
    if page > 1:
        query["pg"] = str(page)
    else:
        query.pop("pg", None)
    return urlunparse(parsed._replace(query=urlencode(query)))


def add_qidian_page(url: str, page: int) -> str:
    if page <= 1:
        return url
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") + f"/page{page}/"
    return urlunparse(parsed._replace(path=path))


def add_path_page(url: str, page: int) -> str:
    if page <= 1:
        return url
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") + f"/trang-{page}/"
    return urlunparse(parsed._replace(path=path))


def truyenfull_today_author_urls(author_values: list[str]) -> list[str]:
    urls: list[str] = []
    for value in author_values:
        value = clean_text(value)
        if not value:
            continue
        if value.startswith("http://") or value.startswith("https://"):
            urls.append(value)
            continue
        key = value.strip().lower()
        slug = key if key in TRUYENFULL_TODAY_AUTHOR_SLUGS else slugify_vietnamese(value)
        if slug:
            urls.append(f"https://truyenfull.today/tac-gia/{slug}/")
    return list(dict.fromkeys(urls))


def collect_truyenfull_today_story_urls_from_lists(
    list_urls: list[str],
    pages: int,
    timeout: int,
    retries: int,
    retry_sleep: float,
    limit: int,
) -> list[str]:
    story_urls: list[str] = []
    seen: set[str] = set()
    for list_url in list_urls:
        host = urlparse(list_url).netloc.lower()
        for page in range(1, pages + 1):
            page_url = add_path_page(list_url, page)
            try:
                soup = BeautifulSoup(fetch_html(page_url, timeout, retries, retry_sleep), "html.parser")
            except Exception as exc:
                print(f"[WARN] skip truyenfull_today author seed list {page_url}: {exc}")
                break
            for link in soup.select("h2 a[href], h3 a[href], .truyen-title a[href], .list-truyen a[href], .row a[href]"):
                href = link.get("href") or ""
                story_url = urljoin(page_url, href).split("#", 1)[0].rstrip("/") + "/"
                parsed_story = urlparse(story_url)
                if parsed_story.netloc and parsed_story.netloc != host:
                    continue
                if "/the-loai/" in parsed_story.path or "/danh-sach/" in parsed_story.path:
                    continue
                if re.search(r"/(?:quyen-\d+-)?chuong-\d+", parsed_story.path) or "/tac-gia/" in parsed_story.path or story_url in seen:
                    continue
                title = clean_text(link.get_text(" ", strip=True))
                if not title or len(title) < 3:
                    continue
                seen.add(story_url)
                story_urls.append(story_url)
                if limit > 0 and len(story_urls) >= limit:
                    return story_urls
    return story_urls


def discover_truyenfull_today_author_urls_from_stories(
    story_urls: list[str],
    timeout: int,
    retries: int,
    retry_sleep: float,
    limit: int,
) -> list[str]:
    author_urls: list[str] = []
    seen: set[str] = set()
    for story_url in story_urls:
        try:
            soup = BeautifulSoup(fetch_html(story_url, timeout, retries, retry_sleep), "html.parser")
        except Exception as exc:
            print(f"[WARN] skip truyenfull_today author seed story {story_url}: {exc}")
            continue
        author_link = soup.select_one(".info a[href*='/tac-gia/'], .col-info-desc a[href*='/tac-gia/'], a[href*='/tac-gia/']")
        if author_link is None:
            continue
        href = author_link.get("href") or ""
        author_url = urljoin(story_url, href).split("#", 1)[0].rstrip("/") + "/"
        if author_url in seen:
            continue
        seen.add(author_url)
        author_urls.append(author_url)
        print(f"[INFO] truyenfull_today auto author: {clean_text(author_link.get_text(' ', strip=True))} | {author_url}")
        if limit > 0 and len(author_urls) >= limit:
            break
    return author_urls


def cap_by_source(candidates: list[StoryCandidate], max_per_source: int) -> list[StoryCandidate]:
    if max_per_source <= 0:
        return candidates
    counts: dict[str, int] = {}
    capped: list[StoryCandidate] = []
    for candidate in candidates:
        count = counts.get(candidate.source_code, 0)
        if count >= max_per_source:
            continue
        counts[candidate.source_code] = count + 1
        capped.append(candidate)
    return capped


def discover_qidian(
    ranks: list[str],
    limit_per_page: int,
    pages: int,
    timeout: int,
    retries: int,
    retry_sleep: float,
) -> list[StoryCandidate]:
    candidates: list[StoryCandidate] = []
    unavailable_hosts: set[str] = set()
    for rank in ranks:
        base_rank_url = QIDIAN_RANK_URLS[rank]
        host = urlparse(base_rank_url).netloc.lower()
        if host in unavailable_hosts:
            print(f"[SKIP] qidian host unavailable in this run: {base_rank_url}")
            continue
        for page in range(1, pages + 1):
            rank_url = add_qidian_page(base_rank_url, page)
            try:
                books = parse_rank_page(rank, rank_url, limit_per_page, timeout, retries, retry_sleep)
            except Exception as exc:
                print(f"[WARN] skip qidian {rank_url}: {exc}")
                if is_source_unavailable_error(exc):
                    unavailable_hosts.add(host)
                    print(f"[WARN] stop qidian rank because host looks unavailable: {base_rank_url}")
                    break
                continue
            if not books:
                print(f"[WARN] qidian parsed 0 books: {rank_url}")
                continue
            for book in books:
                candidates.append(
                    StoryCandidate(
                        source_code="qidian",
                        source_url=book.book_url,
                        title=book.title,
                        author=book.author,
                        category=book.category,
                        status=book.status,
                        description=book.intro,
                        rank_name=rank,
                        rank_position=(page - 1) * max(1, limit_per_page) + book.position,
                        language="zh",
                        discovered_from=rank_url,
                        tags=[part for part in [book.category, book.status] if part],
                    )
                )
    return candidates


def discover_qidian_playwright(
    ranks: list[str],
    limit_per_page: int,
    pages: int,
    args: argparse.Namespace,
) -> list[StoryCandidate]:
    from scripts.story_pipeline.discover_qidian_playwright import discover_books

    browser_args = argparse.Namespace(
        ranks=ranks,
        rank_url="",
        pages=pages,
        limit_per_page=limit_per_page,
        profile_dir=args.qidian_profile_dir,
        channel=args.qidian_channel,
        executable_path=args.qidian_executable_path,
        headful=args.qidian_headful,
        manual_wait=args.qidian_manual_wait,
        wait_ms=args.qidian_wait_ms,
        timeout=args.timeout,
        slow_mo=args.qidian_slow_mo,
        debug_html_dir=args.qidian_debug_html_dir,
    )
    books = discover_books(browser_args)
    candidates: list[StoryCandidate] = []
    for index, book in enumerate(books, start=1):
        candidates.append(
            StoryCandidate(
                source_code="qidian",
                source_url=book.book_url,
                title=book.title,
                author=book.author,
                category=book.category,
                status=book.status,
                description=book.intro,
                rank_name=book.rank_name,
                rank_position=index,
                language="zh",
                discovered_from=book.rank_url,
                tags=[part for part in [book.category, book.status] if part],
            )
        )
    return candidates


def discover_wattpad_vn(
    list_urls: list[str],
    timeout: int,
    pages: int,
    retries: int,
    retry_sleep: float,
) -> list[StoryCandidate]:
    candidates: list[StoryCandidate] = []
    unavailable_hosts: set[str] = set()
    for list_url in list_urls:
        host = urlparse(list_url).netloc.lower()
        if host in unavailable_hosts:
            print(f"[SKIP] wattpad_vn host unavailable in this run: {list_url}")
            continue
        rank_position = 0
        for page in range(1, pages + 1):
            page_url = add_query_page(list_url, page)
            try:
                soup = BeautifulSoup(fetch_html(page_url, timeout, retries, retry_sleep), "html.parser")
            except Exception as exc:
                if page > 1 and is_not_found_error(exc):
                    print(f"[INFO] stop wattpad_vn pagination at page {page}: {page_url}")
                    break
                print(f"[WARN] skip wattpad_vn {page_url}: {exc}")
                if is_source_unavailable_error(exc):
                    unavailable_hosts.add(host)
                    print(f"[WARN] stop wattpad_vn list because host looks unavailable: {list_url}")
                    break
                continue
            page_count = 0
            for heading in soup.select("h3"):
                link = heading.select_one("a[href]")
                if link is None:
                    continue
                href = link.get("href") or ""
                story_url = urljoin(page_url, href)
                if "/the-loai/" in story_url or "/tac-gia/" in story_url:
                    continue

                block_text_parts: list[str] = []
                node = heading
                for _ in range(8):
                    node = node.find_next_sibling()
                    if node is None or getattr(node, "name", None) == "h3":
                        break
                    block_text_parts.append(node.get_text(" ", strip=True))
                block_text = clean_text(" ".join(block_text_parts))
                author_match = re.search(r"Tác giả\s*:\s*(.*?)(?:Thể loại|Số chương|Lượt xem|$)", block_text)
                category_match = re.search(r"Thể loại\s*:\s*(.*?)(?:Số chương|Lượt xem|$)", block_text)
                chapters_match = re.search(r"Số chương\s*:\s*([\d.]+)", block_text)
                views_match = re.search(r"Lượt xem\s*:\s*([\d.]+)", block_text)
                category = clean_text(category_match.group(1)) if category_match else ""
                tags = [clean_text(tag) for tag in re.split(r"[,，]", category) if clean_text(tag)]
                rank_position += 1
                page_count += 1
                candidates.append(
                    StoryCandidate(
                        source_code="wattpad_vn",
                        source_url=story_url,
                        title=clean_text(link.get_text(" ", strip=True)),
                        author=clean_text(author_match.group(1)) if author_match else "",
                        category=category,
                        total_chapters=parse_int(chapters_match.group(1)) if chapters_match else 0,
                        views=parse_int(views_match.group(1)) if views_match else 0,
                        rank_name=Path(urlparse(list_url).path).name or "home",
                        rank_position=rank_position,
                        language="vi",
                        discovered_from=page_url,
                        tags=tags,
                    )
                )
            if page_count == 0:
                print(f"[INFO] wattpad_vn parsed 0 stories, stop list: {page_url}")
                break
    return candidates


def discover_truyenfull_today(
    list_urls: list[str],
    timeout: int,
    pages: int,
    retries: int,
    retry_sleep: float,
) -> list[StoryCandidate]:
    candidates: list[StoryCandidate] = []
    unavailable_hosts: set[str] = set()
    for list_url in list_urls:
        host = urlparse(list_url).netloc.lower()
        if host in unavailable_hosts:
            print(f"[SKIP] truyenfull_today host unavailable in this run: {list_url}")
            continue
        rank_position = 0
        seen: set[str] = set()
        for page in range(1, pages + 1):
            page_url = add_path_page(list_url, page)
            print(f"[INFO] truyenfull_today fetch page {page}/{pages}: {page_url}", flush=True)
            try:
                soup = BeautifulSoup(fetch_html(page_url, timeout, retries, retry_sleep), "html.parser")
            except Exception as exc:
                if page > 1 and is_not_found_error(exc):
                    print(f"[INFO] stop truyenfull_today pagination at page {page}: {page_url}")
                    break
                print(f"[WARN] skip truyenfull_today {page_url}: {exc}")
                if is_source_unavailable_error(exc):
                    unavailable_hosts.add(host)
                    print(f"[WARN] stop truyenfull_today list because host looks unavailable: {list_url}")
                    break
                continue

            page_count = 0
            links = soup.select(
                "h2 a[href], h3 a[href], .truyen-title a[href], "
                ".list-truyen a[href], .row a[href]"
            )
            for link in links:
                href = link.get("href") or ""
                story_url = canonical_story_url(urljoin(page_url, href))
                parsed_story = urlparse(story_url)
                if parsed_story.netloc and parsed_story.netloc != host:
                    continue
                if "/the-loai/" in parsed_story.path or "/danh-sach/" in parsed_story.path:
                    continue
                if re.search(r"/(?:quyen-\d+-)?chuong-\d+", parsed_story.path) or story_url in seen:
                    continue
                title = clean_text(link.get_text(" ", strip=True))
                if not title or len(title) < 3:
                    continue
                seen.add(story_url)

                container = link.find_parent(["article", "li", "div", "section"])
                block_text = clean_text(container.get_text(" ", strip=True)) if container else title
                tags = [
                    clean_text(tag.get_text(" ", strip=True))
                    for tag in (container.select("a[href*='/the-loai/']") if container else [])
                    if clean_text(tag.get_text(" ", strip=True))
                ]
                author = ""
                author_node = container.select_one(".author, [class*='author']") if container else None
                if author_node:
                    author = clean_text(author_node.get_text(" ", strip=True))
                chapters_match = re.search(r"Chương\s*([\d.]+)", block_text, flags=re.IGNORECASE)
                status = "Hoàn thành" if re.search(r"\b(full|hoàn|trọn bộ)\b", block_text, flags=re.IGNORECASE) else ""
                cover_node = container.select_one("img") if container else None
                cover_image_url = ""
                if cover_node:
                    cover_image_url = cover_node.get("data-src") or cover_node.get("src") or ""
                    cover_image_url = urljoin(page_url, cover_image_url) if cover_image_url else ""

                rank_position += 1
                page_count += 1
                candidates.append(
                    StoryCandidate(
                        source_code="truyenfull_today",
                        source_url=story_url,
                        title=title,
                        author=author,
                        category=", ".join(dict.fromkeys(tags)),
                        status=status,
                        description=block_text[:500],
                        cover_image_url=cover_image_url,
                        rank_name=Path(urlparse(list_url).path).name or "truyenfull",
                        rank_position=rank_position,
                        total_chapters=parse_int(chapters_match.group(1)) if chapters_match else 0,
                        language="vi",
                        discovered_from=page_url,
                        tags=list(dict.fromkeys(tags)),
                    )
                )
            print(
                f"[INFO] truyenfull_today page done: parsed={page_count} "
                f"list_total={rank_position} url={page_url}",
                flush=True,
            )
            if page_count == 0:
                print(f"[INFO] truyenfull_today parsed 0 stories, stop list: {page_url}")
                break
    return candidates


def discover_truyenfull_today_story_urls(
    story_urls: list[str],
    timeout: int,
    retries: int,
    retry_sleep: float,
) -> list[StoryCandidate]:
    candidates: list[StoryCandidate] = []
    seen: set[str] = set()
    for story_url in story_urls:
        story_url = story_url.rstrip("/") + "/"
        if story_url in seen:
            continue
        seen.add(story_url)
        try:
            soup = BeautifulSoup(fetch_html(story_url, timeout, retries, retry_sleep), "html.parser")
        except Exception as exc:
            print(f"[WARN] skip truyenfull_today story seed {story_url}: {exc}")
            continue

        title_node = soup.select_one("h1, .truyen-title, .title")
        title = clean_text(title_node.get_text(" ", strip=True)) if title_node else Path(urlparse(story_url).path).name
        if not title:
            continue

        author = ""
        author_node = soup.select_one("a[href*='/tac-gia/']")
        if author_node:
            author = clean_text(author_node.get_text(" ", strip=True))

        info_node = soup.select_one(".info, .col-info-desc, .col-truyen-main") or soup
        tags = [
            clean_text(tag.get_text(" ", strip=True))
            for tag in info_node.select("a[href*='/the-loai/']")
            if clean_text(tag.get_text(" ", strip=True))
        ]
        page_text = clean_text(soup.get_text(" ", strip=True))
        latest_chapter_numbers = [
            parse_int(match.group(1))
            for match in re.finditer(r"/chuong-(\d+)/?", " ".join(a.get("href") or "" for a in soup.select("a[href*='/chuong-']")))
        ]
        total_chapters = max(latest_chapter_numbers) if latest_chapter_numbers else 0
        total_match = re.search(r"\b([\d.]+)\s+chương\b", page_text, flags=re.IGNORECASE)
        if total_match:
            total_chapters = max(total_chapters, parse_int(total_match.group(1)))
        status = "Hoàn thành" if re.search(r"\b(full|hoàn thành|trọn bộ)\b", page_text, flags=re.IGNORECASE) else ""

        description_node = soup.select_one(".desc-text, .description, .desc, .truyen-info")
        description = clean_text(description_node.get_text(" ", strip=True)) if description_node else page_text[:500]
        cover_node = soup.select_one("img")
        cover_image_url = ""
        if cover_node:
            cover_image_url = cover_node.get("data-src") or cover_node.get("src") or ""
            cover_image_url = urljoin(story_url, cover_image_url) if cover_image_url else ""

        candidates.append(
            StoryCandidate(
                source_code="truyenfull_today",
                source_url=story_url,
                title=title,
                author=author,
                category=", ".join(dict.fromkeys(tags)),
                status=status,
                description=description[:500],
                cover_image_url=cover_image_url,
                rank_name="classic_seed",
                rank_position=len(candidates) + 1,
                total_chapters=total_chapters,
                language="vi",
                discovered_from=story_url,
                tags=list(dict.fromkeys(tags + ["classic", "author_seed"])),
            )
        )
    return candidates


def discover_hako(
    list_urls: list[str],
    timeout: int,
    pages: int,
    retries: int,
    retry_sleep: float,
) -> list[StoryCandidate]:
    candidates: list[StoryCandidate] = []
    unavailable_hosts: set[str] = set()
    for list_url in list_urls:
        host = urlparse(list_url).netloc.lower()
        if host in unavailable_hosts:
            print(f"[SKIP] hako host unavailable in this run: {list_url}")
            continue
        rank_position = 0
        seen: set[str] = set()
        for page in range(1, pages + 1):
            page_url = add_query_page(list_url, page)
            try:
                soup = BeautifulSoup(fetch_html(page_url, timeout, retries, retry_sleep), "html.parser")
            except Exception as exc:
                if page > 1 and is_not_found_error(exc):
                    print(f"[INFO] stop hako pagination at page {page}: {page_url}")
                    break
                print(f"[WARN] skip hako {page_url}: {exc}")
                if is_source_unavailable_error(exc):
                    unavailable_hosts.add(host)
                    print(f"[WARN] stop hako list because host looks unavailable: {list_url}")
                    break
                continue
            page_count = 0
            for link in soup.select("a[href*='/truyen/']"):
                href = link.get("href") or ""
                story_url = urljoin(page_url, href).split("#", 1)[0]
                if re.search(r"/c\d+", story_url) or story_url in seen:
                    continue
                title = clean_text(link.get_text(" ", strip=True))
                if not title or len(title) < 3:
                    continue
                seen.add(story_url)
                container = link.find_parent(["article", "div", "li", "section"])
                block_text = clean_text(container.get_text(" ", strip=True)) if container else title
                tags = [
                    clean_text(tag.get_text(" ", strip=True))
                    for tag in (container.select("a[href*='/the-loai/'], .tag, .series-tag") if container else [])
                    if clean_text(tag.get_text(" ", strip=True))
                ]
                cover_node = container.select_one("img") if container else None
                cover_image_url = ""
                if cover_node:
                    cover_image_url = cover_node.get("data-src") or cover_node.get("src") or ""
                    cover_image_url = urljoin(page_url, cover_image_url) if cover_image_url else ""
                rank_position += 1
                page_count += 1
                candidates.append(
                    StoryCandidate(
                        source_code="hako",
                        source_url=story_url,
                        title=title,
                        category=", ".join(dict.fromkeys(tags)),
                        description=block_text[:500],
                        cover_image_url=cover_image_url,
                        rank_name=Path(urlparse(list_url).path).name or "danh-sach",
                        rank_position=rank_position,
                        language="vi",
                        discovered_from=page_url,
                        tags=list(dict.fromkeys(tags)),
                    )
                )
            if page_count == 0:
                print(f"[INFO] hako parsed 0 stories, stop list: {page_url}")
                break
    return candidates


def parse_naver_meta(block_text: str) -> tuple[str, int, str, str]:
    author = ""
    total_chapters = 0
    status = ""
    category = "Korean Web Novel"

    meta_match = re.search(r"평점\s*[\d.]+\s*\|\s*([^|]+)\|\s*총\s*([\d,]+)\s*화\s*/\s*([^|]+)", block_text)
    if meta_match:
        author = clean_text(meta_match.group(1))
        total_chapters = parse_int(meta_match.group(2))
        status = clean_text(meta_match.group(3))
    else:
        fallback_match = re.search(r"\|\s*([^|]+)\|\s*총\s*([\d,]+)\s*화\s*/\s*([^|]+)", block_text)
        if fallback_match:
            author = clean_text(fallback_match.group(1))
            total_chapters = parse_int(fallback_match.group(2))
            status = clean_text(fallback_match.group(3))

    return author, total_chapters, status, category


def discover_naver_series(
    list_urls: list[str],
    timeout: int,
    pages: int,
    retries: int,
    retry_sleep: float,
) -> list[StoryCandidate]:
    candidates: list[StoryCandidate] = []
    unavailable_hosts: set[str] = set()
    for list_url in list_urls:
        host = urlparse(list_url).netloc.lower()
        if host in unavailable_hosts:
            print(f"[SKIP] naver_series host unavailable in this run: {list_url}")
            continue
        rank_position = 0
        seen: set[str] = set()
        for page in range(1, pages + 1):
            page_url = add_query_page(list_url, page)
            try:
                soup = BeautifulSoup(fetch_html(page_url, timeout, retries, retry_sleep), "html.parser")
            except Exception as exc:
                if page > 1 and is_not_found_error(exc):
                    print(f"[INFO] stop naver_series pagination at page {page}: {page_url}")
                    break
                print(f"[WARN] skip naver_series {page_url}: {exc}")
                if is_source_unavailable_error(exc):
                    unavailable_hosts.add(host)
                    print(f"[WARN] stop naver_series list because host looks unavailable: {list_url}")
                    break
                continue

            page_count = 0
            links = soup.select("h3 a[href*='/novel/detail.series'], h3 a[href*='productNo=']")
            if not links:
                links = soup.select("a[href*='/novel/detail.series'], a[href*='productNo=']")
            for link in links:
                href = link.get("href") or ""
                story_url = urljoin(page_url, href).split("#", 1)[0]
                if story_url in seen:
                    continue
                title = clean_text(link.get_text(" ", strip=True))
                title = re.sub(r"^(새로운 에피소드|신규)\s*", "", title).strip()
                if not title or len(title) < 2:
                    continue
                seen.add(story_url)

                container = link.find_parent("li") or link.find_parent(["article", "div", "section"])
                block_text = clean_text(container.get_text(" ", strip=True)) if container else title
                author, total_chapters, status, category = parse_naver_meta(block_text)
                description = ""
                if status:
                    after_status = block_text.split(status, 1)[-1]
                    description = clean_text(after_status)[:500]
                rank_position += 1
                page_count += 1
                candidates.append(
                    StoryCandidate(
                        source_code="naver_series",
                        source_url=story_url,
                        title=title,
                        author=author,
                        category=category,
                        status=status,
                        description=description,
                        rank_name="top100",
                        rank_position=rank_position,
                        total_chapters=total_chapters,
                        language="ko",
                        discovered_from=page_url,
                        tags=["Korean", "Naver Series", "웹소설", category, status],
                    )
                )
            if page_count == 0:
                print(f"[INFO] naver_series parsed 0 stories, stop list: {page_url}")
                break
    return candidates


def discover_royalroad(
    list_urls: list[str],
    timeout: int,
    pages: int,
    retries: int,
    retry_sleep: float,
) -> list[StoryCandidate]:
    def story_container(link: BeautifulSoup) -> BeautifulSoup | None:
        container = link.find_parent(["article", "li"])
        if container is not None:
            return container
        node = link
        for _ in range(8):
            node = node.find_parent("div")
            if node is None:
                break
            text = clean_text(node.get_text(" ", strip=True))
            if re.search(r"\b(?:Pages|Chapters|Followers|Views)\b", text, flags=re.IGNORECASE):
                return node
        return link.find_parent("div")

    candidates: list[StoryCandidate] = []
    unavailable_hosts: set[str] = set()
    for list_url in list_urls:
        host = urlparse(list_url).netloc.lower()
        if host in unavailable_hosts:
            print(f"[SKIP] royalroad host unavailable in this run: {list_url}")
            continue
        rank_position = 0
        seen: set[str] = set()
        for page in range(1, pages + 1):
            page_url = add_query_page(list_url, page)
            try:
                soup = BeautifulSoup(fetch_html(page_url, timeout, retries, retry_sleep), "html.parser")
            except Exception as exc:
                if page > 1 and is_pagination_end_error(exc):
                    print(f"[INFO] stop royalroad pagination at page {page}: {page_url}")
                    break
                print(f"[WARN] skip royalroad {page_url}: {exc}")
                if is_source_unavailable_error(exc):
                    unavailable_hosts.add(host)
                    print(f"[WARN] stop royalroad list because host looks unavailable: {list_url}")
                    break
                continue

            page_count = 0
            for link in soup.select("a[href*='/fiction/']"):
                href = link.get("href") or ""
                story_url = urljoin(page_url, href).split("#", 1)[0]
                if not re.search(r"/fiction/\d+/", story_url) or "/chapter/" in story_url or story_url in seen:
                    continue
                title = clean_text(link.get_text(" ", strip=True))
                if not title or len(title) < 3:
                    continue
                seen.add(story_url)

                container = story_container(link)
                block_text = clean_text(container.get_text(" ", strip=True)) if container else title
                author = ""
                author_node = container.select_one("a[href*='/profile/']") if container else None
                if author_node:
                    author = clean_text(author_node.get_text(" ", strip=True))
                chapters_match = (
                    re.search(r"Chapters?\s*:\s*([\d,]+)", block_text, flags=re.IGNORECASE)
                    or re.search(r"([\d,]+)\s+Chapters?\b", block_text, flags=re.IGNORECASE)
                )
                tags = [
                    clean_text(tag.get_text(" ", strip=True))
                    for tag in (container.select(".tags a, a[href*='tagsAdd='], .label") if container else [])
                    if clean_text(tag.get_text(" ", strip=True))
                ]
                cover_node = container.select_one("img") if container else None
                cover_image_url = ""
                if cover_node:
                    cover_image_url = cover_node.get("data-src") or cover_node.get("src") or ""
                    cover_image_url = urljoin(page_url, cover_image_url) if cover_image_url else ""

                rank_position += 1
                page_count += 1
                candidates.append(
                    StoryCandidate(
                        source_code="royalroad",
                        source_url=story_url,
                        title=title,
                        author=author,
                        category=", ".join(dict.fromkeys(tags)),
                        description=block_text[:500],
                        cover_image_url=cover_image_url,
                        rank_name=Path(urlparse(list_url).path).name or "fictions",
                        rank_position=rank_position,
                        total_chapters=parse_int(chapters_match.group(1)) if chapters_match else 0,
                        language="en",
                        discovered_from=page_url,
                        tags=list(dict.fromkeys(["English", "Royal Road", *tags])),
                    )
                )
            if page_count == 0:
                print(f"[INFO] royalroad parsed 0 stories, stop list: {page_url}")
                break
    return candidates


def skydemonorder_rank_name(list_url: str) -> str:
    parsed = urlparse(list_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if query.get("sort"):
        return query["sort"]
    tail = Path(parsed.path.rstrip("/")).name
    return tail or "projects"


def is_skydemonorder_project_url(url: str) -> bool:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    return len(parts) == 2 and parts[0] == "projects" and bool(parts[1])


def skydemonorder_story_urls_from_page(page_url: str, html: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for link in soup.select("a[href*='/projects/']"):
        href = link.get("href") or ""
        story_url = canonical_story_url(urljoin(page_url, href).split("#", 1)[0])
        if story_url in seen or not is_skydemonorder_project_url(story_url):
            continue
        title = clean_text(link.get_text(" ", strip=True))
        seen.add(story_url)
        found.append((story_url, title))
    return found


def skydemonorder_inferred_tags(title: str, description: str) -> list[str]:
    text = f"{title} {description}".lower()
    rules = [
        (r"\b(cultivation|cultivator|martial|murim|demon|heavenly demon|qi|sect)\b", "cultivation"),
        (r"\b(sword|swordsman|sword saint|swordmaster)\b", "martial arts"),
        (r"\b(system|simulation|status window)\b", "system"),
        (r"\b(dungeon|tower|hunter)\b", "dungeon"),
        (r"\b(regression|regressor|returner|reincarnation|reincarnated)\b", "regression"),
        (r"\b(another world|otherworld|isekai|portal)\b", "isekai"),
        (r"\b(magic|mage|academy)\b", "magic academy"),
        (r"\b(apocalypse|post-apocalyptic|survival)\b", "apocalypse"),
    ]
    return [tag for pattern, tag in rules if re.search(pattern, text)]


def discover_skydemonorder(
    list_urls: list[str],
    timeout: int,
    pages: int,
    *,
    profile_dir: str,
    headful: bool,
    manual_wait: int,
    wait_ms: int,
) -> list[StoryCandidate]:
    from scripts.story_pipeline.crawl_skydemonorder_chapters import (
        extract_project_metadata,
        import_playwright,
        safe_slug,
    )

    sync_playwright, PlaywrightTimeoutError = import_playwright()
    candidates: list[StoryCandidate] = []
    seen: set[str] = set()
    profile_path = Path(profile_dir)
    profile_path.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=profile_path.as_posix(),
            headless=not headful,
            viewport={"width": 1366, "height": 900},
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            args=["--disable-blink-features=AutomationControlled", "--lang=en-US"],
        )
        list_page = context.pages[0] if context.pages else context.new_page()
        detail_page = context.new_page()
        first_page = True

        try:
            for list_url in list_urls:
                rank_name = skydemonorder_rank_name(list_url)
                rank_position = 0
                for page_number in range(1, pages + 1):
                    page_url = add_skydemonorder_page(list_url, page_number)
                    try:
                        list_page.goto(page_url, wait_until="domcontentloaded", timeout=timeout * 1000)
                        if first_page and manual_wait > 0:
                            print(f"[WAIT] skydemonorder manual wait {manual_wait}s: {list_page.url}", flush=True)
                            list_page.wait_for_timeout(manual_wait * 1000)
                        first_page = False
                        if wait_ms > 0:
                            list_page.wait_for_timeout(wait_ms)
                    except PlaywrightTimeoutError as exc:
                        print(f"[WARN] skip skydemonorder list timeout {page_url}: {exc}")
                        break

                    story_links = skydemonorder_story_urls_from_page(page_url, list_page.content())
                    if not story_links:
                        print(f"[INFO] skydemonorder parsed 0 stories, stop list: {page_url}")
                        break

                    for story_url, list_title in story_links:
                        if story_url in seen:
                            continue
                        seen.add(story_url)
                        rank_position += 1

                        metadata: dict[str, str] = {}
                        try:
                            detail_page.goto(story_url, wait_until="domcontentloaded", timeout=timeout * 1000)
                            if wait_ms > 0:
                                detail_page.wait_for_timeout(wait_ms)
                            metadata = extract_project_metadata(detail_page)
                        except Exception as exc:
                            print(f"[WARN] skydemonorder detail fallback {story_url}: {exc}")

                        title = clean_text(metadata.get("title") or list_title)
                        if not title:
                            title = safe_slug(urlparse(story_url).path.rstrip("/").rsplit("/", 1)[-1]).replace("-", " ").title()
                        description = clean_text(metadata.get("description") or "")
                        author = clean_text(metadata.get("author") or "")
                        cover_image_url = metadata.get("cover_image_url") or ""
                        tags = [
                            "English",
                            "Sky Demon Order",
                            rank_name,
                            *skydemonorder_inferred_tags(title, description),
                        ]

                        candidates.append(
                            StoryCandidate(
                                source_code="skydemonorder",
                                source_url=story_url,
                                title=title,
                                author=author,
                                category=", ".join(dict.fromkeys(tags)),
                                description=description,
                                cover_image_url=cover_image_url,
                                rank_name=rank_name,
                                rank_position=rank_position,
                                total_chapters=0,
                                language="en",
                                discovered_from=page_url,
                                tags=list(dict.fromkeys(tags)),
                            )
                        )
        finally:
            context.close()
    return candidates


def dedupe_candidates(candidates: Iterable[StoryCandidate]) -> list[StoryCandidate]:
    seen: set[tuple[str, str]] = set()
    deduped: list[StoryCandidate] = []
    for candidate in candidates:
        candidate.source_url = canonical_story_url(candidate.source_url)
        key = (candidate.source_code, candidate.source_url)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def upsert_candidates(candidates: Iterable[StoryCandidate]) -> tuple[int, int]:
    """Upsert candidates to DB. Returns (total_upserted, new_inserts)."""
    from story_db.story_pipeline_db import repository as repo

    for code, source in SOURCES.items():
        repo.upsert_source(code, source["name"], source["base_url"])

    candidate_list = list(candidates)
    count = 0
    new_inserts = 0
    total = len(candidate_list)
    for index, candidate in enumerate(candidate_list, start=1):
        metadata = {
            "discovered_from": candidate.discovered_from,
            "tags": candidate.tags,
            "views": candidate.views,
            "matched_include_keywords": candidate.matched_include_keywords,
            "discovery_updated_at": datetime.now(timezone.utc).isoformat(),
        }
        story = repo.upsert_story(
            candidate.source_code,
            {
                "source_story_id": stable_source_story_id(candidate.source_url),
                "title": candidate.title,
                "original_title": candidate.title,
                "author": candidate.author or None,
                "category": candidate.category or None,
                "status": candidate.status or None,
                "language": candidate.language,
                "source_url": candidate.source_url,
                "catalog_url": candidate.source_url,
                "description": candidate.description or None,
                "cover_image_url": candidate.cover_image_url or None,
                "rank_name": candidate.rank_name or None,
                "rank_position": candidate.rank_position or None,
                "total_chapters": candidate.total_chapters,
                "metadata": metadata,
                "touch_catalog_checked_at": False,
            },
        )
        count += 1
        if story.get("is_new_insert"):
            new_inserts += 1
        print(
            "[DB] upsert story "
            f"{index}/{total} id={story.get('id')} new={story.get('is_new_insert', False)} "
            f"source={candidate.source_code} rank={candidate.rank_name or '-'}#{candidate.rank_position or '-'} "
            f"title={candidate.title} url={candidate.source_url}",
            flush=True,
        )
    return count, new_inserts


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Discover truyện hot từ Qidian/Hako/Wattpad VN/TruyenFull Today/Naver Series/Royal Road/Sky Demon Order, lọc genre/content, "
            "rồi ghi candidate vào story_data/discovery và bảng stories."
        )
    )
    parser.add_argument(
        "--sources",
        nargs="+",
        choices=sorted(SOURCES),
        default=DEFAULT_PRODUCTION_SOURCES,
    )
    parser.add_argument(
        "--qidian-ranks",
        nargs="+",
        choices=sorted(QIDIAN_RANK_URLS),
        default=["free", "free_all", "hotsales", "readindex", "yuepiao"],
    )
    parser.add_argument(
        "--qidian-browser",
        action="store_true",
        help="Dùng Playwright/browser profile để discover Qidian thay vì requests.",
    )
    parser.add_argument("--qidian-profile-dir", default=".browser/qidian")
    parser.add_argument("--qidian-channel", default="", help="Ví dụ: chrome để mở Google Chrome thật.")
    parser.add_argument("--qidian-executable-path", default="")
    parser.add_argument("--qidian-headful", action="store_true", help="Mở browser thật để xử lý captcha/login.")
    parser.add_argument("--qidian-manual-wait", type=int, default=90)
    parser.add_argument("--qidian-wait-ms", type=int, default=2500)
    parser.add_argument("--qidian-slow-mo", type=int, default=0)
    parser.add_argument("--qidian-debug-html-dir", default="story_data/debug/qidian_playwright")
    parser.add_argument("--wattpad-urls", nargs="*", default=DEFAULT_WATTPAD_URLS)
    parser.add_argument("--truyenfull-today-urls", nargs="*", default=DEFAULT_TRUYENFULL_TODAY_URLS)
    parser.add_argument("--truyenfull-today-author-urls", nargs="*", default=DEFAULT_TRUYENFULL_TODAY_AUTHOR_URLS)
    parser.add_argument(
        "--truyenfull-today-list-only",
        action="store_true",
        help="Chỉ discover từ --truyenfull-today-urls; bỏ author/classic seed mặc định.",
    )
    parser.add_argument(
        "--truyenfull-today-authors",
        nargs="*",
        default=[],
        help=(
            "Tên hoặc slug tác giả nổi bật, ví dụ: 'Nhĩ Căn' nhi-can 'Vong Ngữ'. "
            "Script tự đổi thành /tac-gia/<slug>/."
        ),
    )
    parser.add_argument(
        "--no-truyenfull-today-auto-authors",
        action="store_true",
        help="Tắt auto-discover tác giả nổi bật từ các story hot/classic.",
    )
    parser.add_argument(
        "--truyenfull-today-auto-author-story-limit",
        type=int,
        default=30,
        help="Số story hot/classic tối đa dùng để dò author page.",
    )
    parser.add_argument(
        "--truyenfull-today-auto-author-limit",
        type=int,
        default=12,
        help="Số author page auto-discover tối đa.",
    )
    parser.add_argument(
        "--truyenfull-today-classic-story-urls",
        nargs="*",
        default=DEFAULT_TRUYENFULL_TODAY_CLASSIC_STORY_URLS,
    )
    parser.add_argument("--hako-urls", nargs="*", default=DEFAULT_HAKO_URLS)
    parser.add_argument("--naver-series-urls", nargs="*", default=DEFAULT_NAVER_SERIES_URLS)
    parser.add_argument("--royalroad-urls", nargs="*", default=DEFAULT_ROYALROAD_URLS)
    parser.add_argument("--skydemonorder-urls", nargs="*", default=DEFAULT_SKYDEMONORDER_URLS)
    parser.add_argument("--skydemonorder-profile-dir", default=".browser/skydemonorder")
    parser.add_argument("--skydemonorder-headful", action="store_true", help="Mở browser thật để xử lý Cloudflare/login.")
    parser.add_argument("--skydemonorder-manual-wait", type=int, default=0)
    parser.add_argument("--skydemonorder-wait-ms", type=int, default=1500)
    parser.add_argument(
        "--pages",
        type=int,
        default=1,
        help="Số page lấy cho mỗi list/rank URL. Tăng lên 3-10 để mở rộng candidate pool.",
    )
    parser.add_argument(
        "--limit-per-page",
        type=int,
        default=40,
        help="Giới hạn book mỗi Qidian ranking page. Wattpad/Hako parse hết item nhìn thấy trên page.",
    )
    parser.add_argument(
        "--limit-per-source",
        type=int,
        default=0,
        help="Giới hạn candidate accepted cuối cùng cho mỗi source. 0 = không giới hạn.",
    )
    parser.add_argument("--min-chapters", type=int, default=80)
    parser.add_argument("--include-keywords", default=",".join(DEFAULT_INCLUDE_KEYWORDS))
    parser.add_argument("--exclude-keywords", default=",".join(DEFAULT_EXCLUDE_KEYWORDS))
    parser.add_argument(
        "--no-require-include",
        action="store_true",
        help="Không bắt buộc candidate phải match include keyword.",
    )
    parser.add_argument(
        "--no-exclude-filter",
        action="store_true",
        help="Không loại candidate theo exclude keyword.",
    )
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-sleep", type=float, default=2.0)
    parser.add_argument("--preflight-timeout", type=int, default=8, help="Timeout (giây) cho preflight host probe.")
    parser.add_argument("--skip-preflight", action="store_true", help="Bỏ qua preflight check, chạy thẳng như cũ.")
    parser.add_argument("--output", default="")
    parser.add_argument("--no-db", action="store_true", help="Chỉ xuất JSON, không upsert DB.")
    parser.add_argument(
        "--no-url-skip",
        action="store_true",
        help="Bỏ qua URL skip cache, scan tất cả URLs (dùng cho manual run).",
    )
    parser.add_argument(
        "--url-skip-state",
        default="story_data/discovery/url_skip_state.json",
        help="Đường dẫn file JSON lưu URL skip state.",
    )
    args = parser.parse_args()

    include_keywords = split_terms(args.include_keywords, DEFAULT_INCLUDE_KEYWORDS)
    exclude_keywords = split_terms(args.exclude_keywords, DEFAULT_EXCLUDE_KEYWORDS)
    if args.no_exclude_filter:
        exclude_keywords = []

    url_skip_cache = UrlSkipCache(
        Path(args.url_skip_state),
        enabled=not args.no_url_skip and not args.no_db,
    )

    def _discover_with_skip(
        source_code: str,
        urls: list[str],
        discover_fn,
        *fn_args,
        known_urls: set[str] | None = None,
        **fn_kwargs,
    ) -> list[StoryCandidate]:
        """Run discover_fn per-URL, skip cached URLs, record results in url_skip_cache."""
        results: list[StoryCandidate] = []
        for url in urls:
            if url_skip_cache.should_skip(url):
                print(
                    f"[SKIP-CACHE] {source_code} {url} "
                    f"(backoff until {url_skip_cache.skip_until_str(url)})",
                    flush=True,
                )
                continue
            url_candidates = discover_fn([url], *fn_args, **fn_kwargs)
            if known_urls is not None:
                new_count = sum(
                    1 for c in url_candidates
                    if canonical_story_url(c.source_url) not in known_urls
                )
                # Update known_urls so subsequent URLs in same run don't double-count
                known_urls.update(canonical_story_url(c.source_url) for c in url_candidates)
            else:
                new_count = len(url_candidates)
            url_skip_cache.record_result(url, new_count)
            results.extend(url_candidates)
        return results

    # Preflight: probe mỗi host một lần, thử tìm URL mới nếu path thay đổi
    reachable_hosts: set[str] = set()
    _rediscovered_host_urls: dict[str, list[str]] = {}
    if not args.skip_preflight:
        all_url_groups: dict[str, list[str]] = {
            "qidian": [u for ranks in [QIDIAN_RANK_URLS] for u in ranks.values()] if "qidian" in args.sources else [],
            "wattpad_vn": args.wattpad_urls if "wattpad_vn" in args.sources else [],
            "truyenfull_today": [
                *args.truyenfull_today_urls,
                *args.truyenfull_today_author_urls,
                *args.truyenfull_today_classic_story_urls,
            ] if "truyenfull_today" in args.sources else [],
            "hako": args.hako_urls if "hako" in args.sources else [],
            "naver_series": args.naver_series_urls if "naver_series" in args.sources else [],
            "royalroad": args.royalroad_urls if "royalroad" in args.sources else [],
            "skydemonorder": args.skydemonorder_urls if "skydemonorder" in args.sources else [],
        }
        reachable_hosts, _rediscovered_host_urls = preflight_and_rediscover(all_url_groups, args.preflight_timeout)

    def live(urls: list[str]) -> list[str]:
        if args.skip_preflight:
            return urls
        result: list[str] = []
        for u in urls:
            host = urlparse(u).netloc.lower()
            if host not in reachable_hosts:
                continue
            # If this host has rediscovered URLs, prefer those over the original
            if host in _rediscovered_host_urls:
                result.extend(_rediscovered_host_urls.pop(host))
            else:
                result.append(u)
        skipped = len(urls) - len(result)
        if skipped:
            print(f"[PREFLIGHT] skipped {skipped}/{len(urls)} URLs (host unreachable)", flush=True)
        return list(dict.fromkeys(result))

    # Pre-load known story source URLs per source for accurate new-story detection in skip cache.
    # One bulk query per source at start of run — updated in-place by _discover_with_skip.
    _known_urls_by_source: dict[str, set[str]] = {}
    if not args.no_db and not args.no_url_skip:
        try:
            from story_db.story_pipeline_db import repository as _repo_preload
            for _sc in args.sources:
                if _sc == "qidian":  # qidian handled separately (no per-URL skip)
                    continue
                _known_urls_by_source[_sc] = _repo_preload.get_story_source_urls_by_source(_sc)
        except Exception as _exc:
            print(f"[SKIP-CACHE] could not load known URLs: {_exc}", flush=True)

    raw_candidates: list[StoryCandidate] = []
    if "qidian" in args.sources:
        if args.qidian_browser:
            raw_candidates.extend(
                discover_qidian_playwright(
                    args.qidian_ranks,
                    args.limit_per_page,
                    args.pages,
                    args,
                )
            )
        else:
            raw_candidates.extend(
                discover_qidian(
                    args.qidian_ranks,
                    args.limit_per_page,
                    args.pages,
                    args.timeout,
                    args.retries,
                    args.retry_sleep,
                )
            )
    if "wattpad_vn" in args.sources:
        raw_candidates.extend(
            _discover_with_skip(
                "wattpad_vn",
                live(args.wattpad_urls),
                discover_wattpad_vn,
                args.timeout,
                args.pages,
                args.retries,
                args.retry_sleep,
                known_urls=_known_urls_by_source.get("wattpad_vn"),
            )
        )
    if "truyenfull_today" in args.sources:
        tf_list_urls = live(args.truyenfull_today_urls)
        tf_classic_urls = live(args.truyenfull_today_classic_story_urls)
        author_urls = (
            []
            if args.truyenfull_today_list_only
            else [
                *live(args.truyenfull_today_author_urls),
                *truyenfull_today_author_urls(args.truyenfull_today_authors),
            ]
        )
        if not args.truyenfull_today_list_only and not args.no_truyenfull_today_auto_authors and tf_list_urls:
            seed_story_urls = [
                *tf_classic_urls,
                *collect_truyenfull_today_story_urls_from_lists(
                    tf_list_urls,
                    args.pages,
                    args.timeout,
                    args.retries,
                    args.retry_sleep,
                    args.truyenfull_today_auto_author_story_limit,
                ),
            ]
            author_urls.extend(
                discover_truyenfull_today_author_urls_from_stories(
                    list(dict.fromkeys(seed_story_urls)),
                    args.timeout,
                    args.retries,
                    args.retry_sleep,
                    args.truyenfull_today_auto_author_limit,
                )
            )
            author_urls = list(dict.fromkeys(author_urls))
        if not args.truyenfull_today_list_only and tf_classic_urls:
            raw_candidates.extend(
                discover_truyenfull_today_story_urls(
                    tf_classic_urls,
                    args.timeout,
                    args.retries,
                    args.retry_sleep,
                )
            )
        all_tf_urls = [*tf_list_urls, *author_urls]
        if all_tf_urls:
            raw_candidates.extend(
                _discover_with_skip(
                    "truyenfull_today",
                    all_tf_urls,
                    discover_truyenfull_today,
                    args.timeout,
                    args.pages,
                    args.retries,
                    args.retry_sleep,
                    known_urls=_known_urls_by_source.get("truyenfull_today"),
                )
            )
    if "hako" in args.sources:
        raw_candidates.extend(
            _discover_with_skip(
                "hako",
                live(args.hako_urls),
                discover_hako,
                args.timeout,
                args.pages,
                args.retries,
                args.retry_sleep,
                known_urls=_known_urls_by_source.get("hako"),
            )
        )
    if "naver_series" in args.sources:
        raw_candidates.extend(
            _discover_with_skip(
                "naver_series",
                live(args.naver_series_urls),
                discover_naver_series,
                args.timeout,
                args.pages,
                args.retries,
                args.retry_sleep,
                known_urls=_known_urls_by_source.get("naver_series"),
            )
        )
    if "royalroad" in args.sources:
        raw_candidates.extend(
            _discover_with_skip(
                "royalroad",
                live(args.royalroad_urls),
                discover_royalroad,
                args.timeout,
                args.pages,
                args.retries,
                args.retry_sleep,
                known_urls=_known_urls_by_source.get("royalroad"),
            )
        )
    if "skydemonorder" in args.sources:
        raw_candidates.extend(
            discover_skydemonorder(
                live(args.skydemonorder_urls),
                args.timeout,
                args.pages,
                profile_dir=args.skydemonorder_profile_dir,
                headful=args.skydemonorder_headful,
                manual_wait=args.skydemonorder_manual_wait,
                wait_ms=args.skydemonorder_wait_ms,
            )
        )

    deduped = dedupe_candidates(raw_candidates)
    accepted = apply_filters(
        deduped,
        include_keywords,
        exclude_keywords,
        require_include=not args.no_require_include,
        min_chapters=args.min_chapters,
    )
    accepted.sort(
        key=lambda item: story_priority_sort_key(
            source_code=item.source_code,
            rank_position=item.rank_position,
        )
    )
    accepted = cap_by_source(accepted, args.limit_per_source)

    raw_by_source: dict[str, int] = {}
    accepted_by_source: dict[str, int] = {}
    for candidate in raw_candidates:
        raw_by_source[candidate.source_code] = raw_by_source.get(candidate.source_code, 0) + 1
    for candidate in accepted:
        accepted_by_source[candidate.source_code] = accepted_by_source.get(candidate.source_code, 0) + 1

    output_path = (
        Path(args.output)
        if args.output
        else Path("story_data/discovery")
        / f"hot_stories_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "sources": args.sources,
                "total_raw": len(raw_candidates),
                "total_deduped": len(deduped),
                "total_accepted": len(accepted),
                "pages": args.pages,
                "limit_per_page": args.limit_per_page,
                "limit_per_source": args.limit_per_source,
                "include_keywords": include_keywords,
                "exclude_keywords": exclude_keywords,
                "stories": [asdict(candidate) for candidate in accepted],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    db_count = 0
    db_new = 0
    if not args.no_db:
        db_count, db_new = upsert_candidates(accepted)

    print(
        f"Discovery xong: raw={len(raw_candidates)} dedupe={len(deduped)} "
        f"accepted={len(accepted)} db_upsert={db_count} db_new={db_new} output={output_path}"
    )
    for source_code in args.sources:
        print(
            "[SUMMARY] "
            f"{source_code} raw={raw_by_source.get(source_code, 0)} "
            f"accepted={accepted_by_source.get(source_code, 0)}"
        )


if __name__ == "__main__":
    main()
