#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import requests
from concurrent.futures import TimeoutError as FutureTimeout
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
for path in (ROOT, SCRIPT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from story_db.story_pipeline_db import repository as repo
from scripts.story_pipeline.crawl_story_alternate_sources import (
    SOURCE_LANGUAGES,
    apply_next_missing_start,
    crawl_alternate_source,
    fetch_html,
    parse_catalog_for_source,
)

SEARCH_PROVIDERS = ("lightnovelpub", "novelbin", "freewebnovel", "novelhub", "royalroad")

PROVIDER_PROBE_URLS: dict[str, str] = {
    "lightnovelpub": "https://lightnovelpub.org/",
    "novelbin": "https://novelbin.com/",
    "freewebnovel": "https://freewebnovel.com/",
    "novelhub": "https://novelhub.net/",
    "royalroad": "https://www.royalroad.com/",
}

# Language each provider searches in — determines which queries to use
PROVIDER_LANGUAGE: dict[str, str] = {
    "lightnovelpub": "en",
    "novelbin": "en",
    "freewebnovel": "en",
    "novelhub": "en",
    "royalroad": "en",
}

# De-accented Vietnamese word/phrase patterns that won't match English novel sites.
# Words chosen to be distinctive to romanised Vietnamese (low false-positive risk).
_VI_ROMANIZED_RE = re.compile(
    r"\b(?:"
    # Vietnamese-only names / terms (very safe)
    r"nguyen|truyen|chuong|hiep|kiem|thien|tien|bach|hac|"
    r"thuc|toan|hoang|phong|vong|ngu|xuan|danh|minh|thanh|"
    # Common romanised words safe to add (rare in English novel titles)
    r"che|hoi|gioi|nhan|dieu|phap|vo|vo thuat|"
    r"tu tien|tu luyen|dat|dai|"
    # More connectors / suffixes
    r"duc|hung|cuong|binh|tan|van|son|tra|kiet|nhat|vu|bao|"
    r"lam|tung|quoc|phu|thu|huu|trung|bac|tay|bau|lang"
    r")\b",
    re.IGNORECASE,
)
_EN_TRANSLATION_SIGNAL_WORDS = {
    "a",
    "an",
    "and",
    "become",
    "boss",
    "can",
    "cultivation",
    "cultivate",
    "dao",
    "demon",
    "divine",
    "doctor",
    "dragon",
    "earth",
    "emperor",
    "empire",
    "end",
    "female",
    "follow",
    "god",
    "great",
    "he",
    "heavenly",
    "human",
    "i",
    "immortal",
    "in",
    "king",
    "lead",
    "martial",
    "novel",
    "of",
    "origin",
    "rebirth",
    "return",
    "she",
    "story",
    "supporting",
    "sword",
    "system",
    "the",
    "transmigration",
    "venerable",
    "villain",
    "world",
    "you",
}


def is_romanized_vietnamese(query: str) -> bool:
    """Return True if an ASCII string is romanized (de-accented) Vietnamese, not real English."""
    if not query.isascii() or not query.strip():
        return False
    words = query.split()
    if len(words) < 2:
        return False
    lower_words = {word.lower().strip("'") for word in words}
    if lower_words & _EN_TRANSLATION_SIGNAL_WORDS:
        return False
    hits = sum(1 for w in words if _VI_ROMANIZED_RE.search(w))
    return hits / len(words) >= 0.25

VI_PHRASE_ALIASES = [
    ("đế chế đại việt", "Empire of Dai Viet"),
    ("đế chế", "Empire"),
    ("đại việt", "Dai Viet"),
    ("đế bá", "Emperor's Domination"),
    ("quang âm chi ngoại", "Outside of Time"),
    ("tuyệt thế võ thần", "Peerless Martial God"),
    ("đấu phá thương khung", "Battle Through the Heavens"),
    ("phàm nhân tu tiên", "A Record of a Mortal's Journey to Immortality"),
    ("nhất niệm vĩnh hằng", "A Will Eternal"),
    ("ngã dục phong thiên", "I Shall Seal the Heavens"),
    ("tiên nghịch", "Renegade Immortal"),
    ("cầu ma", "Beseech the Devil"),
    ("thế giới hoàn mỹ", "Perfect World"),
    ("vũ luyện điên phong", "Martial Peak"),
    ("võ luyện đỉnh phong", "Martial Peak"),
    ("toàn chức pháp sư", "Versatile Mage"),
    ("đại chúa tể", "The Great Ruler"),
    ("mục thần ký", "Tales of Herding Gods"),
    ("thánh khư", "The Sacred Ruins"),
    ("linh vực", "Spirit Realm"),
    ("vĩnh sinh", "Immortality"),
    ("trọng sinh", "Rebirth"),
    ("xuyên không", "Transmigration"),
    ("xuyên nhanh", "Quick Transmigration"),
    ("mạt thế", "Apocalypse"),
    ("hệ thống", "System"),
    ("tu tiên", "Cultivation"),
    ("tu luyện", "Cultivation"),
    ("tiên đế", "Immortal Emperor"),
    ("tiên tôn", "Immortal Venerable"),
    ("ma đế", "Demon Emperor"),
    ("võ thần", "Martial God"),
    ("võ đế", "Martial Emperor"),
    ("kiếm thần", "Sword God"),
    ("kiếm đạo", "Sword Dao"),
    ("thần y", "Divine Doctor"),
    ("nữ phụ", "Female Supporting Character"),
    ("nam chính", "Male Lead"),
    ("phản diện", "Villain"),
    ("thiên mệnh", "Heavenly Fate"),
    ("dị giới", "Another World"),
    ("đô thị", "Urban"),
    ("thôn phệ", "Devouring"),
    ("bắt đầu", "Starting With"),
    ("vô địch", "Invincible"),
    ("trùm", "Boss"),
    # power/ability compounds — must come before single-word "năng" which de-accents to "nang"
    ("toàn năng", "Omnipotent"),
    ("đại năng", "Great Power"),
    ("đại sư", "Grand Master"),
    ("cao thủ", "Master"),
    ("thiên tài", "Genius"),
    ("vô song", "Peerless"),
    ("bá đạo", "Domination"),
    ("lao tù", "Prison"),
    ("ác ma", "Devil"),
    ("thành chủ", "City Lord"),
    ("kiêu hùng", "Heroic"),
    ("vạn năm", "Ten Thousand Years"),
    ("một vạn năm", "Ten Thousand Years"),
    ("tu sĩ", "Cultivator"),
    ("tiên cảnh", "Immortal Realm"),
    ("dị năng", "Superpower"),
    ("siêu năng", "Superpower"),
]

VI_WORD_ALIASES = {
    "ta": "I",
    "tôi": "I",
    "ngươi": "You",
    # "nàng" intentionally omitted: normalizes to "nang" which collides with "năng" (power/ability)
    # "hắn" intentionally omitted: normalizes to "han" which can collide with proper nouns
    "của": "Of",
    "ở": "In",
    "tại": "In",
    "trong": "In",
    "là": "Is",
    "làm": "Become",
    "thành": "Become",
    "bắt": "Start",
    "đầu": "Beginning",
    "thần": "God",
    "tiên": "Immortal",
    "ma": "Demon",
    "võ": "Martial",
    "kiếm": "Sword",
    "đế": "Emperor",
    "tôn": "Venerable",
    "vương": "King",
    "long": "Dragon",
    "xà": "Snake",
    "đạo": "Dao",
    "truyện": "Story",
    "tiểu": "Small",
    "thuyết": "Novel",
    "thế": "World",
    "giới": "World",
    "mạt": "End",
    "hệ": "System",
    "thống": "System",
    "phụ": "Supporting",
    "chính": "Lead",
    "nữ": "Female",
    "nam": "Male",
    "de": "Emperor",
    "che": "Empire",
    "dai": "Great",
    "viet": "Viet",
    "nguyen": "Origin",
    "lai": "Return",
    "theo": "Follow",
    "hoi": "Can",
    "tu": "Cultivation",
    "luyen": "Cultivation",
    "than": "God",
    "thien": "Heavenly",
    "dia": "Earth",
    "nhan": "Human",
    "vo": "Martial",
    "kiem": "Sword",
    "vuong": "King",
    "dao": "Dao",
    "truyen": "Story",
    "tieu": "Small",
    "thuyet": "Novel",
    "the": "World",
    "gioi": "World",
    "he": "System",
    "thong": "System",
    "phu": "Supporting",
    "chinh": "Lead",
    "nu": "Female",
}


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,vi;q=0.8",
}


def log(message: str) -> None:
    print(message, flush=True)


def probe_host(url: str, timeout: int = 8) -> bool:
    """Quick reachability check — tries HEAD then GET."""
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


def preflight_check_providers(providers: set[str], timeout: int = 8) -> set[str]:
    """Probe each search provider once. Returns the set of reachable providers."""
    reachable: set[str] = set()
    probe_items: list[tuple[str, str | None]] = []
    for provider in sorted(providers):
        probe_url = PROVIDER_PROBE_URLS.get(provider)
        if not probe_url:
            reachable.add(provider)
            continue
        probe_items.append((provider, probe_url))

    def _probe(item: tuple[str, str]) -> tuple[str, str, bool]:
        provider, probe_url = item
        return provider, probe_url, probe_host(probe_url, timeout)

    executor = ThreadPoolExecutor(max_workers=max(1, len(probe_items)))
    futures = {executor.submit(_probe, item): item for item in probe_items}
    completed: set[Any] = set()
    try:
        for future in as_completed(futures, timeout=max(1, timeout + 2)):
            completed.add(future)
            provider, probe_url, ok = future.result()
            if ok:
                reachable.add(provider)
                log(f"[PREFLIGHT] OK   {provider} ({probe_url})")
            else:
                log(f"[PREFLIGHT] FAIL {provider} ({probe_url}) — will be skipped this run")
    except FutureTimeout:
        pass
    finally:
        for future, (provider, probe_url) in futures.items():
            if future in completed:
                continue
            future.cancel()
            log(f"[PREFLIGHT] FAIL {provider} ({probe_url}) — probe timed out, will be skipped this run")
        executor.shutdown(wait=False, cancel_futures=True)
    return reachable


def probe_ollama(base_url: str, timeout: float = 3.0) -> bool:
    try:
        response = requests.get(f"{base_url.rstrip('/')}/api/tags", timeout=timeout)
        return response.status_code < 500
    except requests.RequestException:
        return False


def normalize_text(value: str | None) -> str:
    value = (value or "").replace("Đ", "D").replace("đ", "d")
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.casefold()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


VI_WORD_ALIASES.update({normalize_text(k): v for k, v in list(VI_WORD_ALIASES.items()) if normalize_text(k)})


def slugify_ascii(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", normalize_text(value)).strip("-")


def strip_title_noise(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", value or "").strip()
    noise_patterns = [
        r"\s*[-–—]\s*(?:truyện\s+chữ|zhihu)\s*$",
        r"\s*\((?:dịch|convert|full|trọn\s*bộ|hoàn\s*thành|bản\s+chuẩn.*)\)\s*$",
        r"\s*\[(?:dịch|convert|full|trọn\s*bộ|hoàn\s*thành)\]\s*$",
    ]
    previous = None
    while previous != cleaned:
        previous = cleaned
        for pattern in noise_patterns:
            cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE).strip()
    return cleaned


def title_case_ascii(value: str) -> str:
    small_words = {"a", "an", "and", "as", "at", "for", "from", "in", "of", "on", "the", "to", "with"}
    words = re.sub(r"[^A-Za-z0-9']+", " ", value or "").strip().split()
    result: list[str] = []
    for index, word in enumerate(words):
        lower = word.lower()
        if index > 0 and lower in small_words:
            result.append(lower)
        elif word.isupper() and len(word) <= 4:
            result.append(word)
        else:
            result.append(lower[:1].upper() + lower[1:])
    return " ".join(result)


def heuristic_title_aliases(title: str) -> list[str]:
    cleaned = strip_title_noise(title)
    normalized = normalize_text(cleaned)
    aliases: list[str] = []
    if not normalized:
        return aliases

    deaccented = title_case_ascii(normalized)
    if deaccented:
        aliases.append(deaccented)

    translated = normalized
    for phrase, alias in sorted(VI_PHRASE_ALIASES, key=lambda item: len(item[0]), reverse=True):
        normalized_phrase = normalize_text(phrase)
        translated = re.sub(rf"\b{re.escape(normalized_phrase)}\b", f" {alias} ", translated)
    words: list[str] = []
    for word in translated.split():
        words.append(VI_WORD_ALIASES.get(word, word))
    literal = title_case_ascii(" ".join(words))
    # Discard mixed garbage: if any word still looks like romanised Vietnamese, the
    # phrase/word map didn't cover the title well enough — don't pollute search queries.
    if literal and any(_VI_ROMANIZED_RE.search(w) for w in literal.split()):
        literal = ""
    # Also discard if too many original tokens survived unchanged (≥40% untranslated).
    if literal and normalized:
        original_tokens = set(normalized.split())
        translated_tokens = set(literal.lower().split())
        unchanged_count = sum(1 for t in original_tokens if t in translated_tokens)
        if original_tokens and unchanged_count / len(original_tokens) >= 0.40:
            literal = ""
    if literal and literal != deaccented:
        aliases.append(literal)

    compact = re.sub(r"\b(?:convert|full|tron bo|truyen chu|zhihu)\b", " ", normalized)
    compact = title_case_ascii(compact)
    if compact and compact not in aliases:
        aliases.append(compact)
    return list(dict.fromkeys(alias for alias in aliases if alias and len(alias) >= 3))


def title_score(query: str, candidate_title: str, candidate_url: str = "") -> float:
    query_norm = normalize_text(query)
    candidate_norm = normalize_text(candidate_title)
    if not query_norm or not candidate_norm:
        return 0.0
    query_tokens = set(query_norm.split())
    candidate_tokens = set(candidate_norm.split())
    overlap = len(query_tokens & candidate_tokens) / max(1, len(query_tokens | candidate_tokens))
    ratio = SequenceMatcher(None, query_norm, candidate_norm).ratio()
    slug_bonus = 0.1 if slugify_ascii(query) and slugify_ascii(query) in candidate_url else 0.0
    return min(1.0, max(overlap, ratio) + slug_bonus)


def load_aliases(path_value: str) -> dict[str, list[str]]:
    if not path_value:
        return {}
    path = Path(path_value)
    if not path.is_absolute():
        path = ROOT / path
    data = json.loads(path.read_text(encoding="utf-8"))
    result: dict[str, list[str]] = {}
    if not isinstance(data, dict):
        return result
    for key, value in data.items():
        if isinstance(value, str):
            result[str(key)] = [value]
        elif isinstance(value, list):
            result[str(key)] = [str(item) for item in value if str(item).strip()]
    return result


def metadata_aliases(story: dict[str, Any]) -> list[str]:
    metadata = story.get("metadata") or {}
    values: list[str] = []
    for key in ("english_title", "en_title", "original_english_title", "korean_title", "ko_title", "aliases", "alternate_titles"):
        item = metadata.get(key)
        if isinstance(item, str):
            values.append(item)
        elif isinstance(item, list):
            values.extend(str(value) for value in item if str(value).strip())
    for alt in metadata.get("alternate_sources") or []:
        if isinstance(alt, dict):
            title = alt.get("title") or alt.get("source_title")
            if title:
                values.append(str(title))
    return values


def story_queries(story: dict[str, Any], alias_map: dict[str, list[str]], *, include_heuristic: bool = True) -> list[str]:
    values = [
        story.get("display_title"),
        story.get("title"),
        story.get("original_title"),
        *(alias_map.get(str(story.get("id")), [])),
        *(alias_map.get(str(story.get("title")), [])),
        *metadata_aliases(story),
    ]
    cleaned: list[str] = []
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if not text or text.lower() == "none":
            continue
        cleaned.append(text)
        if include_heuristic:
            cleaned.extend(heuristic_title_aliases(text))
    return list(dict.fromkeys(cleaned))


def strip_json_response(value: str) -> str:
    cleaned = (value or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        return cleaned[start:end + 1]
    return cleaned


def infer_aliases_with_ollama(story: dict[str, Any], args: argparse.Namespace) -> list[str]:
    if args.alias_inference not in {"ollama", "both"}:
        return []
    title = str(story.get("display_title") or story.get("title") or story.get("original_title") or "").strip()
    if not title:
        return []
    metadata = story.get("metadata") or {}
    prompt = (
        "You help match translated Asian webnovel titles to English novel sites.\n"
        "Return only valid JSON with key aliases, an array of 3-8 likely English search titles.\n"
        "Include known official English titles if you recognize the work. Otherwise include concise literal translations.\n"
        "Do not include Vietnamese without accents. Do not explain.\n\n"
        f"Vietnamese title: {title}\n"
        f"Original title: {story.get('original_title') or ''}\n"
        f"Author: {story.get('author') or ''}\n"
        f"Existing metadata: {json.dumps({k: metadata.get(k) for k in ['aliases', 'english_title', 'alternate_titles'] if k in metadata}, ensure_ascii=False)}\n"
    )
    try:
        response = requests.post(
            f"{args.ollama_url.rstrip('/')}/api/generate",
            json={
                "model": args.alias_model,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.15, "num_ctx": 2048},
            },
            timeout=args.alias_timeout,
        )
        response.raise_for_status()
        parsed = json.loads(strip_json_response(str(response.json().get("response") or "")))
        aliases = parsed.get("aliases") if isinstance(parsed, dict) else []
        if not isinstance(aliases, list):
            return []
        result = [str(item).strip() for item in aliases if str(item).strip()]
        if result:
            log(f"[ALIAS] ollama {title!r} -> {result[:5]}")
        return list(dict.fromkeys(result))
    except Exception as exc:
        log(f"[WARN] alias inference failed title={title!r}: {type(exc).__name__}: {exc}")
        return []


def translate_story_title_ollama(
    story: dict[str, Any],
    to_lang: str,
    args: argparse.Namespace,
) -> list[str]:
    """
    Use Ollama to produce search-ready titles in `to_lang` for a story written in a different language.
    Returns a list of candidate search strings (empty if translation not needed or fails).
    """
    title = str(story.get("display_title") or story.get("title") or story.get("original_title") or "").strip()
    if not title:
        return []
    from_lang = (story.get("language") or "vi").lower()
    if from_lang == to_lang:
        return []

    lang_names = {"en": "English", "vi": "Vietnamese", "zh": "Chinese", "ko": "Korean"}
    from_name = lang_names.get(from_lang, from_lang)
    to_name = lang_names.get(to_lang, to_lang)
    metadata = story.get("metadata") or {}

    prompt = (
        f"You help find {from_name} web novels on {to_name}-language reading sites.\n"
        f"Return ONLY valid JSON with key 'queries': array of 2-5 {to_name} search strings "
        f"that would match this novel on {to_name} sites.\n"
        f"Prefer the official {to_name} title if you recognise the work. "
        f"Otherwise give accurate literal translations or well-known transliterations.\n"
        f"Do NOT include {from_name} or romanised {from_name}. Do NOT explain.\n\n"
        f"Title: {title}\n"
        f"Author: {story.get('author') or ''}\n"
        f"Known metadata: {json.dumps({k: metadata.get(k) for k in ['aliases', 'english_title', 'alternate_titles'] if k in metadata}, ensure_ascii=False)}\n"
    )
    try:
        response = requests.post(
            f"{args.ollama_url.rstrip('/')}/api/generate",
            json={
                "model": args.alias_model,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.10, "num_ctx": 1024},
            },
            timeout=args.alias_timeout,
        )
        response.raise_for_status()
        parsed = json.loads(strip_json_response(str(response.json().get("response") or "")))
        queries = parsed.get("queries") if isinstance(parsed, dict) else []
        if not isinstance(queries, list):
            return []
        result = [str(q).strip() for q in queries if str(q).strip()]
        if result:
            log(f"[TRANSLATE] {title!r} ({from_lang}→{to_lang}): {result[:4]}")
        return list(dict.fromkeys(result))
    except Exception as exc:
        log(f"[WARN] translate_story_title failed title={title!r} to={to_lang}: {type(exc).__name__}: {exc}")
        return []


def search_lightnovelpub(query: str, args: argparse.Namespace) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    direct_slug = slugify_ascii(query)
    if args.direct_slug_candidates and direct_slug and query.isascii():
        results.append(
            {
                "source_code": "lightnovelpub",
                "url": f"https://lightnovelpub.org/novel/{direct_slug}/",
                "title": query,
                "score": 0.55,
                "search_query": query,
                "provider": "direct_slug",
            }
        )

    search_url = f"https://lightnovelpub.org/search/?keyword={quote(query)}"
    try:
        html = fetch_html(search_url, args.timeout, args.retries, args.retry_sleep)
    except Exception as exc:
        log(f"[WARN] lightnovelpub search failed query={query!r}: {type(exc).__name__}: {exc}")
        failures = getattr(args, "_search_provider_failures", None)
        if failures is None:
            failures = {}
            setattr(args, "_search_provider_failures", failures)
        failures["lightnovelpub"] = int(failures.get("lightnovelpub", 0)) + 1
        if failures["lightnovelpub"] >= args.provider_failure_limit:
            disabled = getattr(args, "_disabled_search_providers", None)
            if disabled is None:
                disabled = set()
                setattr(args, "_disabled_search_providers", disabled)
            if "lightnovelpub" not in disabled:
                disabled.add("lightnovelpub")
                log(f"[WARN] disable lightnovelpub search for this run after {failures['lightnovelpub']} failed query")
        return results
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = {item["url"].rstrip("/") for item in results}
    for anchor in soup.select("a[href*='/novel/']"):
        href = anchor.get("href") or ""
        url = urljoin(search_url, href).split("#", 1)[0]
        parsed = urlparse(url)
        if parsed.netloc and "lightnovelpub.org" not in parsed.netloc:
            continue
        if "/chapter/" in parsed.path:
            continue
        url = url.rstrip("/") + "/"
        if url.rstrip("/") in seen:
            continue
        seen.add(url.rstrip("/"))
        title = anchor.get_text(" ", strip=True)
        if title.lower() in {"quick read", "start reading", "read now"}:
            title = parsed.path.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").title()
        results.append(
            {
                "source_code": "lightnovelpub",
                "url": url,
                "title": title,
                "score": title_score(query, title, url),
                "search_query": query,
                "provider": "search",
            }
        )
    return results


def candidate(
    *,
    source_code: str,
    url: str,
    title: str,
    query: str,
    provider: str,
    score: float | None = None,
) -> dict[str, Any]:
    return {
        "source_code": source_code,
        "url": url,
        "title": title,
        "score": title_score(query, title, url) if score is None else score,
        "search_query": query,
        "provider": provider,
    }


def direct_slug_candidate(source_code: str, query: str, base_url: str, *, enabled: bool) -> dict[str, Any] | None:
    if not enabled:
        return None
    slug = slugify_ascii(query)
    if not slug or not query.isascii():
        return None
    return candidate(
        source_code=source_code,
        url=base_url.format(slug=slug),
        title=query,
        query=query,
        provider="direct_slug",
        score=0.52,
    )


def search_anchor_candidates(
    *,
    source_code: str,
    query: str,
    search_url: str,
    host_marker: str,
    href_marker: str,
    title_fallback_from_url: bool = True,
    args: argparse.Namespace,
) -> list[dict[str, Any]]:
    try:
        html = fetch_html(search_url, args.timeout, args.retries, args.retry_sleep)
    except Exception as exc:
        log(f"[WARN] {source_code} search failed query={query!r}: {type(exc).__name__}: {exc}")
        failures = getattr(args, "_search_provider_failures", None)
        if failures is None:
            failures = {}
            setattr(args, "_search_provider_failures", failures)
        failures[source_code] = int(failures.get(source_code, 0)) + 1
        if failures[source_code] >= args.provider_failure_limit:
            disabled = getattr(args, "_disabled_search_providers", None)
            if disabled is None:
                disabled = set()
                setattr(args, "_disabled_search_providers", disabled)
            if source_code not in disabled:
                disabled.add(source_code)
                log(f"[WARN] disable {source_code} search for this run after {failures[source_code]} failed query")
        return []

    soup = BeautifulSoup(html, "html.parser")
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for anchor in soup.select("a[href]"):
        href = anchor.get("href") or ""
        if href_marker not in href:
            continue
        url = urljoin(search_url, href).split("#", 1)[0].rstrip("/")
        parsed = urlparse(url)
        if parsed.netloc and host_marker not in parsed.netloc:
            continue
        if "/chapter" in parsed.path.lower():
            continue
        if url in seen:
            continue
        seen.add(url)
        title = anchor.get_text(" ", strip=True)
        if title_fallback_from_url and (not title or title.lower() in {"read now", "novel", "details"}):
            title = parsed.path.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").title()
        if not title:
            continue
        results.append(candidate(source_code=source_code, url=url + "/", title=title, query=query, provider="search"))
    return results


def search_novelbin(query: str, args: argparse.Namespace) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    direct = direct_slug_candidate("novelbin", query, "https://novelbin.com/b/{slug}/", enabled=args.direct_slug_candidates)
    if direct:
        results.append(direct)
    results.extend(
        search_anchor_candidates(
            source_code="novelbin",
            query=query,
            search_url=f"https://novelbin.com/search?keyword={quote(query)}",
            host_marker="novelbin.com",
            href_marker="/b/",
            args=args,
        )
    )
    results.extend(
        search_anchor_candidates(
            source_code="novelbin",
            query=query,
            search_url=f"https://novelbin.com/search?keyword={quote(query)}",
            host_marker="novelbin.com",
            href_marker="/novel",
            args=args,
        )
    )
    return results


def search_freewebnovel(query: str, args: argparse.Namespace) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    direct = direct_slug_candidate("freewebnovel", query, "https://freewebnovel.com/novel/{slug}/", enabled=args.direct_slug_candidates)
    if direct:
        results.append(direct)
    results.extend(
        search_anchor_candidates(
            source_code="freewebnovel",
            query=query,
            search_url=f"https://freewebnovel.com/search?keyword={quote(query)}",
            host_marker="freewebnovel.com",
            href_marker="/novel/",
            args=args,
        )
    )
    return results


def search_novelhub(query: str, args: argparse.Namespace) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    direct = direct_slug_candidate("novelhub", query, "https://novelhub.net/novel/{slug}/", enabled=args.direct_slug_candidates)
    if direct:
        results.append(direct)
    results.extend(
        search_anchor_candidates(
            source_code="novelhub",
            query=query,
            search_url=f"https://novelhub.net/search?keyword={quote(query)}",
            host_marker="novelhub.net",
            href_marker="/novel/",
            args=args,
        )
    )
    return results


def search_royalroad(query: str, args: argparse.Namespace) -> list[dict[str, Any]]:
    return search_anchor_candidates(
        source_code="royalroad",
        query=query,
        search_url=f"https://www.royalroad.com/fictions/search?title={quote(query)}",
        host_marker="royalroad.com",
        href_marker="/fiction/",
        args=args,
    )


def _queries_for_provider_lang(
    queries: list[str],
    provider_lang: str,
    story_lang: str,
    story: dict[str, Any],
    args: argparse.Namespace,
    translation_cache: dict[str, list[str]],
) -> list[str]:
    """
    Return the ordered list of search strings appropriate for a provider of `provider_lang`.

    Strategy:
    - Non-ASCII queries are dropped (current providers are all ASCII-search only).
    - For English providers + Vietnamese story: skip romanised Vietnamese de-accented
      strings (e.g. "De Che Dai Viet") since they never match English sites.
    - When story language differs from provider language, prepend Ollama-translated
      queries so they appear first and most likely to match.
    """
    filtered: list[str] = []
    for query in queries:
        if not query.isascii():
            if getattr(args, "log_skipped_queries", False):
                log(f"[SKIP] non-ascii query for {provider_lang} provider: {query!r}")
            continue
        if provider_lang == "en" and story_lang == "vi" and is_romanized_vietnamese(query):
            if getattr(args, "log_skipped_queries", False):
                log(f"[SKIP] romanised-vi query on en provider: {query!r}")
            continue
        filtered.append(query)

    # Prepend translated queries when story language ≠ provider language
    if provider_lang != story_lang and story_lang in {"vi", "zh", "ko"}:
        cache_key = f"{story.get('id')}:{story_lang}:{provider_lang}"
        if cache_key not in translation_cache:
            if getattr(args, "translate_for_search", True) and getattr(args, "_translate_search_available", True):
                translation_cache[cache_key] = translate_story_title_ollama(story, provider_lang, args)
            else:
                translation_cache[cache_key] = []
        translated = translation_cache[cache_key]
        translated = [query for query in translated if query and query.isascii()]
        # Translated titles go first — more likely to match than heuristic aliases
        filtered = list(dict.fromkeys([*translated, *filtered]))

    return filtered


def discover_candidates(
    story: dict[str, Any],
    queries: list[str],
    args: argparse.Namespace,
    *,
    translation_cache: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    if translation_cache is None:
        translation_cache = {}

    story_lang = (story.get("language") or "vi").lower()
    providers = set(args.providers)

    # Group active providers by their search language
    providers_by_lang: dict[str, list[str]] = {}
    for provider in providers:
        lang = PROVIDER_LANGUAGE.get(provider, "en")
        providers_by_lang.setdefault(lang, []).append(provider)

    candidates: list[dict[str, Any]] = []

    for provider_lang, lang_providers in providers_by_lang.items():
        provider_queries = _queries_for_provider_lang(
            queries, provider_lang, story_lang, story, args, translation_cache
        )
        if not provider_queries:
            log(f"[SKIP] no usable queries for {provider_lang} providers={sorted(lang_providers)}")
            continue

        log(f"[SEARCH] {provider_lang} providers={sorted(lang_providers)} queries={provider_queries[:3]}")

        for query in provider_queries:
            disabled = getattr(args, "_disabled_search_providers", set())
            active = set(lang_providers) - disabled
            if not active:
                break
            if "lightnovelpub" in active:
                candidates.extend(search_lightnovelpub(query, args))
            if "novelbin" in active:
                candidates.extend(search_novelbin(query, args))
            if "freewebnovel" in active:
                candidates.extend(search_freewebnovel(query, args))
            if "novelhub" in active:
                candidates.extend(search_novelhub(query, args))
            if "royalroad" in active:
                candidates.extend(search_royalroad(query, args))
            time.sleep(args.search_delay)

    deduped: dict[str, dict[str, Any]] = {}
    for cand in candidates:
        key = cand["url"].rstrip("/")
        current = deduped.get(key)
        if current is None or float(cand.get("score") or 0) > float(current.get("score") or 0):
            deduped[key] = cand
    return sorted(deduped.values(), key=lambda item: float(item.get("score") or 0), reverse=True)


def build_alternate_args(args: argparse.Namespace, story: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        target_slug=args.target_slug or ((story.get("metadata") or {}).get("slug") or ""),
        raw_language=args.raw_language,
        source_start=0,
        target_start=0,
        chapter_offset=0,
        from_chapter=0,
        from_next_missing=True,
        resume_from=args.resume_from,
        to_chapter=0,
        max_chapters=args.max_chapters,
        latest_chapter=0,
        max_catalog_pages=args.max_catalog_pages,
        timeout=args.timeout,
        retries=args.retries,
        retry_sleep=args.retry_sleep,
        chapter_delay=args.chapter_delay,
        min_text_chars=args.min_text_chars,
        overwrite=False,
        requeue_done=args.requeue_done,
        stop_on_error=args.stop_on_error,
        catalog_output_root=args.catalog_output_root,
        text_output_root=args.text_output_root,
        raw_zh_output_root=args.raw_zh_output_root,
        raw_en_output_root=args.raw_en_output_root,
        raw_ko_output_root=args.raw_ko_output_root,
        polished_output_root=args.polished_output_root,
        translated_output_root=args.translated_output_root,
        vi_model=args.vi_model,
        translate_model=args.translate_model,
        polish_max_attempts=args.polish_max_attempts,
        polish_inline=args.polish_inline,
        overwrite_polish=args.overwrite_polish,
        ollama_url=args.ollama_url,
        temperature=args.temperature,
        num_ctx=args.num_ctx,
        ollama_timeout=args.ollama_timeout,
        ollama_retries=args.ollama_retries,
        keep_alive=args.keep_alive,
        prompt_profile=args.prompt_profile,
        polish_mode=args.polish_mode,
        post_translate=args.post_translate,
        min_output_ratio=args.min_output_ratio,
        polish_max_chars_per_chunk=args.polish_max_chars_per_chunk,
        translate_max_chars_per_chunk=args.translate_max_chars_per_chunk,
    )


def inspect_candidate(candidate: dict[str, Any], story: dict[str, Any], alt_args: SimpleNamespace) -> dict[str, Any] | None:
    try:
        catalog = parse_catalog_for_source(candidate["source_code"], candidate["url"], alt_args)
    except Exception as exc:
        log(f"[WARN] catalog failed {candidate['url']}: {type(exc).__name__}: {exc}")
        return None
    progress = repo.get_story_chapter_progress(story["id"])
    total = int(catalog.get("total_chapters") or len(catalog.get("chapters") or []) or 0)
    catalog_title = str(catalog.get("title") or candidate.get("title") or "")
    score = max(float(candidate.get("score") or 0), title_score(candidate.get("search_query") or "", catalog_title, candidate["url"]))
    return {
        **candidate,
        "catalog_title": catalog_title,
        "catalog_total_chapters": total,
        "target_max_chapter": progress["max_chapter"],
        "target_max_polished": progress["max_polished_chapter"],
        "target_tail_unpolished": progress["first_tail_unpolished_chapter"],
        "score": score,
        "has_new_chapters": total > progress["max_chapter"] or bool(progress["first_tail_unpolished_chapter"]),
    }


def select_target_stories(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.title:
        return repo.find_stories(
            title_contains=args.title or None,
            source_codes=args.source or None,
            limit=args.limit_stories,
        )

    stories = repo.list_stories_needing_alternate_source(
        source_codes=args.source or None,
        only_incomplete=not args.include_completed,
        limit=args.limit_stories,
    )
    if stories:
        log(f"[TARGET] using needs_alternate_source queue: {len(stories)} stories")
        return stories

    if args.only_needs_alternate:
        return []

    return repo.list_active_stories(
        source_codes=args.source or None,
        only_incomplete=not args.include_completed,
        limit=args.limit_stories,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Auto discover alternate sources for incomplete stories, then reuse crawl_story_alternate_sources merge logic."
    )
    parser.add_argument("--source", nargs="*", default=[], help="Filter target story source code.")
    parser.add_argument("--title", default="", help="Filter target story title.")
    parser.add_argument("--limit-stories", type=int, default=20)
    parser.add_argument("--only-incomplete", action="store_true", default=True)
    parser.add_argument("--include-completed", action="store_true")
    parser.add_argument(
        "--only-needs-alternate",
        action="store_true",
        help="Chỉ xử lý stories đã được crawler chính flag needs_alternate_source/source_host_unavailable.",
    )
    parser.add_argument("--providers", nargs="*", default=["lightnovelpub"], choices=SEARCH_PROVIDERS)
    parser.add_argument("--alias-json", default="", help="JSON map story_id/title -> alias list, e.g. English/Korean titles.")
    parser.add_argument("--alias-inference", choices=("heuristic", "ollama", "both", "off"), default="heuristic")
    parser.add_argument("--alias-model", default="translategemma:12b")
    parser.add_argument("--alias-timeout", type=int, default=120)
    parser.add_argument("--translate-check-timeout", type=float, default=3.0)
    parser.add_argument("--log-skipped-queries", action="store_true")
    parser.add_argument("--target-slug", default="")
    parser.add_argument("--min-score", type=float, default=0.72)
    parser.add_argument("--max-candidates", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--apply", action="store_true", help="Actually crawl selected alternate sources.")
    parser.add_argument("--polish-inline", action="store_true")
    parser.add_argument("--post-translate", choices=("polish", "copy"), default="copy")
    parser.add_argument("--raw-language", default="", help="Override raw language; empty uses source default.")
    parser.add_argument("--resume-from", choices=["polished", "downloaded", "row", "unpolished"], default="polished")
    parser.add_argument("--max-chapters", type=int, default=0)
    parser.add_argument("--max-catalog-pages", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-sleep", type=float, default=2.0)
    parser.add_argument("--search-delay", type=float, default=0.5)
    parser.add_argument("--provider-failure-limit", type=int, default=1)
    parser.add_argument("--direct-slug-candidates", action="store_true")
    parser.add_argument("--preflight-timeout", type=int, default=8, help="Timeout (s) cho preflight provider probe.")
    parser.add_argument("--skip-preflight", action="store_true", help="Bỏ qua preflight check, thử tất cả providers.")
    parser.add_argument(
        "--translate-for-search",
        action="store_true",
        default=True,
        help="Dùng Ollama dịch title sang ngôn ngữ của provider (vi→en, zh→en, ...) trước khi search.",
    )
    parser.add_argument("--no-translate-for-search", dest="translate_for_search", action="store_false")
    parser.add_argument("--chapter-delay", type=float, default=1.5)
    parser.add_argument("--min-text-chars", type=int, default=80)
    parser.add_argument("--requeue-done", action="store_true")
    parser.add_argument("--overwrite-polish", action="store_true")
    parser.add_argument("--stop-on-error", action="store_true")
    parser.add_argument("--catalog-output-root", default="story_data/catalogs")
    parser.add_argument("--text-output-root", default="story_data/text")
    parser.add_argument("--raw-zh-output-root", default="story_data/raw_zh")
    parser.add_argument("--raw-en-output-root", default="story_data/raw_en")
    parser.add_argument("--raw-ko-output-root", default="story_data/raw_ko")
    parser.add_argument("--polished-output-root", default="story_data/polished")
    parser.add_argument("--translated-output-root", default="story_data/translated")
    parser.add_argument("--vi-model", default="qwen3:14b")
    parser.add_argument("--translate-model", default="translategemma:12b")
    parser.add_argument("--polish-max-attempts", type=int, default=3)
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--temperature", type=float, default=0.25)
    parser.add_argument("--num-ctx", type=int, default=4096)
    parser.add_argument("--ollama-timeout", type=int, default=300)
    parser.add_argument("--ollama-retries", type=int, default=3)
    parser.add_argument("--keep-alive", default="24h")
    parser.add_argument("--prompt-profile", choices=("fast", "full"), default="full")
    parser.add_argument("--polish-mode", choices=("llm", "clean"), default="llm")
    parser.add_argument("--min-output-ratio", type=float, default=0.70)
    parser.add_argument("--polish-max-chars-per-chunk", type=int, default=5000)
    parser.add_argument("--translate-max-chars-per-chunk", type=int, default=2500)
    args = parser.parse_args()
    args.dry_run = not args.apply

    if args.translate_for_search:
        log(f"[TRANSLATE] checking Ollama for search aliases: {args.ollama_url}")
        args._translate_search_available = probe_ollama(args.ollama_url, args.translate_check_timeout)
        if args._translate_search_available:
            log("[TRANSLATE] Ollama reachable; cross-language search aliases enabled")
        else:
            log(f"[TRANSLATE] Ollama not reachable at {args.ollama_url}; using heuristic search aliases only")
    else:
        args._translate_search_available = False

    alias_map = load_aliases(args.alias_json)

    # Preflight: probe each provider once before wasting retries on every query
    if not args.skip_preflight:
        reachable = preflight_check_providers(set(args.providers), args.preflight_timeout)
        unreachable = set(args.providers) - reachable
        if unreachable:
            args._disabled_search_providers = unreachable
        if not reachable:
            log("[PREFLIGHT] no providers reachable — nothing to search, exiting")
            return
    else:
        setattr(args, "_disabled_search_providers", set())

    stories = select_target_stories(args)
    log(
        f"[START] stories={len(stories)} dry_run={args.dry_run} providers={','.join(args.providers)} "
        f"translate_for_search={args.translate_for_search}"
    )

    attempted = 0
    crawled = 0
    translation_cache: dict[str, list[str]] = {}  # shared across stories to avoid duplicate Ollama calls
    for story in stories:
        if story.get("is_completed") and not args.include_completed:
            continue
        queries = story_queries(story, alias_map, include_heuristic=args.alias_inference in {"heuristic", "both"})
        if args.alias_inference in {"ollama", "both"}:
            queries.extend(infer_aliases_with_ollama(story, args))
            queries = list(dict.fromkeys(query for query in queries if query))
        if not queries:
            continue
        progress = repo.get_story_chapter_progress(story["id"])
        log(
            f"\n[STORY] {story['title']} | {story['source_code']} | "
            f"max={progress['max_chapter']} polished={progress['max_polished_chapter']} queries={queries[:4]}"
        )
        candidates = discover_candidates(story, queries, args, translation_cache=translation_cache)
        alt_args = build_alternate_args(args, story)
        inspected: list[dict[str, Any]] = []
        for candidate in candidates[: args.max_candidates]:
            details = inspect_candidate(candidate, story, alt_args)
            if details:
                inspected.append(details)
                log(
                    f"[CANDIDATE] score={details['score']:.2f} total={details['catalog_total_chapters']} "
                    f"new={details['has_new_chapters']} title={details['catalog_title']} url={details['url']}"
                )
        inspected.sort(
            key=lambda item: (
                float(item.get("score") or 0),
                int(item.get("catalog_total_chapters") or 0),
            ),
            reverse=True,
        )
        selected = next(
            (
                item
                for item in inspected
                if float(item["score"]) >= args.min_score and item["has_new_chapters"]
            ),
            None,
        )
        if not selected:
            log("[SKIP] no confident source with new chapters")
            continue
        attempted += 1
        if args.dry_run:
            log(f"[DRY-RUN] would crawl {selected['url']} for {story['title']}")
            continue
        apply_next_missing_start(story, alt_args)
        result = crawl_alternate_source(story, selected["url"], alt_args)
        metadata = story.get("metadata") or {}
        previous = metadata.get("alternate_sources") or []
        auto_aliases = [
            alias
            for alias in [
                *(metadata.get("auto_alternate_aliases") or []),
                selected.get("catalog_title") or "",
                selected.get("title") or "",
                selected.get("search_query") or "",
            ]
            if str(alias).strip()
        ]
        repo.update_story_metadata(
            story["id"],
            {
                "alternate_sources": [*previous, result],
                "alternate_sources_updated_at": result["crawled_at"],
                "auto_alternate_last_url": selected["url"],
                "auto_alternate_last_title": selected.get("catalog_title") or selected.get("title") or "",
                "auto_alternate_last_query": selected.get("search_query") or "",
                "auto_alternate_aliases": list(dict.fromkeys(auto_aliases)),
                "needs_alternate_source": False,
                "alternate_source_active": True,
                "alternate_source_active_at": result["crawled_at"],
            },
        )
        crawled += 1

    log(f"\n[DONE] selected={attempted} crawled={crawled} dry_run={args.dry_run}")


if __name__ == "__main__":
    main()
