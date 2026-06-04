#!/usr/bin/env python3
"""Shared utilities for all crawl scripts.

Provides Session-based HTTP fetch (connection reuse), parallel batch fetch,
common text helpers, and unified block detection.
"""
from __future__ import annotations

import re
import random
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable

import requests
from requests import Session
from requests.adapters import HTTPAdapter


DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "vi,en-US;q=0.9,en;q=0.8",
}

COMMON_LOCK_PATTERNS: tuple[str, ...] = (
    # Vietnamese
    "cần đăng nhập",
    "vui lòng đăng nhập",
    "chương này bị khóa",
    "chương đã bị khóa",
    "bạn không có quyền",
    # English — paywall / points gates
    "locked",
    "please log in",
    "login required",
    "log in to purchase",
    "log in to subscribe",
    "members only",
    "subscribe to read",
    "premium chapter",
    "unlock this episode",
    "unlock for",
    "you have 0 points",
    "not enough points",
    "purchase points",
    "buy points",
    "earn points",
    "chapter locked",
    "this chapter is locked",
    "to read this chapter",
    # Chinese
    "需要登录",
    "章节锁定",
)

TRANSIENT_STATUS_CODES: set[int] = {408, 425, 429, 500, 502, 503, 504}


@dataclass
class CrawlResult:
    """Unified result from any crawl operation."""
    status: str  # "success" | "locked" | "captcha" | "empty" | "error"
    content: str | None
    reason: str | None = None
    source: str = "requests"  # "requests" | "playwright" | "browser_use"


class BlockedError(Exception):
    """Raised when content requires login, CAPTCHA, or ad interaction."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------

def make_session(extra_headers: dict[str, str] | None = None) -> Session:
    """Create a requests.Session with connection pooling enabled.

    Reusing the session across requests to the same host avoids repeated
    TCP handshakes and TLS negotiations, typically saving 100–300 ms per
    request on HTTPS sites.
    """
    session = Session()
    headers = {**DEFAULT_HEADERS}
    if extra_headers:
        headers.update(extra_headers)
    session.headers.update(headers)
    # pool_connections: number of distinct hosts to pool
    # pool_maxsize: max idle connections per host
    adapter = HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# ---------------------------------------------------------------------------
# HTTP fetch helpers
# ---------------------------------------------------------------------------

def fetch_html(
    url: str,
    timeout: int = 30,
    retries: int = 3,
    retry_sleep: float = 2.0,
    *,
    session: Session | None = None,
    label: str = "crawl",
) -> str:
    """Fetch URL with retry. Accepts an optional shared Session for connection reuse.

    If no session is provided a temporary one is created and closed after use.
    """
    own_session = session is None
    sess = session if session is not None else make_session()
    last_error: Exception | None = None
    try:
        for attempt in range(1, retries + 1):
            try:
                response = sess.get(url, timeout=timeout)
                if response.status_code in TRANSIENT_STATUS_CODES:
                    response.raise_for_status()
                response.raise_for_status()
                return response.text
            except requests.RequestException as exc:
                last_error = exc
                if attempt < retries:
                    retry_after = None
                    response = getattr(exc, "response", None)
                    if response is not None:
                        retry_after_header = response.headers.get("Retry-After")
                        if retry_after_header:
                            try:
                                retry_after = float(retry_after_header)
                            except ValueError:
                                retry_after = None
                    sleep_for = retry_after if retry_after is not None else retry_sleep * attempt
                    sleep_for += random.uniform(0, min(0.75, retry_sleep))
                    print(
                        f"[WARN] {label} retry {attempt}/{retries} in {sleep_for:.1f}s: "
                        f"{url} | {exc}",
                        flush=True,
                    )
                    time.sleep(sleep_for)
    finally:
        if own_session:
            sess.close()
    raise RuntimeError(
        f"Cannot fetch URL after {retries} attempts: {url} | {last_error}"
    ) from last_error


def fetch_html_batch(
    urls: list[str],
    *,
    fetch_fn: Callable[[str], str] | None = None,
    max_workers: int = 3,
    inter_request_delay: float = 0.5,
    timeout: int = 30,
    retries: int = 3,
    retry_sleep: float = 2.0,
    label: str = "crawl",
) -> dict[str, str | Exception]:
    """Fetch multiple URLs in parallel. Returns ``{url: html_or_exception}``.

    Each worker thread creates its own Session for thread safety.
    ``inter_request_delay`` introduces a per-worker sleep before each request
    to avoid hammering a single host.
    """
    results: dict[str, str | Exception] = {}

    def _fetch_one(url: str) -> tuple[str, str | Exception]:
        if inter_request_delay > 0:
            time.sleep(inter_request_delay)
        try:
            if fetch_fn is not None:
                return url, fetch_fn(url)
            # Each thread owns its session — requests.Session is not thread-safe
            # when shared across threads that mutate state (e.g. cookies).
            sess = make_session()
            try:
                return url, fetch_html(
                    url,
                    timeout=timeout,
                    retries=retries,
                    retry_sleep=retry_sleep,
                    session=sess,
                    label=label,
                )
            finally:
                sess.close()
        except Exception as exc:
            return url, exc

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_fetch_one, url): url for url in urls}
        for future in as_completed(futures):
            url, result = future.result()
            results[url] = result

    return results


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def safe_slug(value: str, fallback: str = "story") -> str:
    """Normalize a string to a URL-safe ASCII slug via NFKD decomposition."""
    normalized = unicodedata.normalize("NFKD", (value or "").strip().lower())
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")
    return slug or fallback


def compact_text(value: str | None) -> str:
    """Collapse all whitespace to a single space and strip edges."""
    return re.sub(r"\s+", " ", value or "").strip()


def clean_text(value: str | None) -> str:
    """Preserve paragraph structure: collapse inline whitespace, join with blank lines."""
    value = (value or "").replace("\xa0", " ")
    lines: list[str] = []
    for line in value.splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line:
            lines.append(line)
    return "\n\n".join(lines).strip()


def parse_chapter_number(title: str, url: str, fallback: int) -> int:
    """Extract chapter number from title or URL path."""
    for text in (title, url):
        match = re.search(
            r"(?:chương|chuong|chapter|chap|ch)[^\d]{0,12}0*(\d{1,5})",
            text,
            flags=re.IGNORECASE,
        )
        if match:
            return int(match.group(1))
    return fallback


# ---------------------------------------------------------------------------
# Block detection
# ---------------------------------------------------------------------------

def looks_blocked(
    text: str,
    extra_patterns: tuple[str, ...] | list[str] = (),
) -> bool:
    """Return True if content text suggests a paywall, login gate, or CAPTCHA."""
    lowered = text.casefold()
    all_patterns = COMMON_LOCK_PATTERNS + tuple(extra_patterns)
    return any(p in lowered for p in all_patterns)
