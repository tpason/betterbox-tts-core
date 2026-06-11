#!/usr/bin/env python3
"""
Auto-extract terminology glossary cho story memory — generic cho mọi nguồn/thể loại.

Mục đích: thuật ngữ (cảnh giới, công pháp, tổ chức, danh hiệu, vật phẩm...) được
dịch NHẤT QUÁN và đúng register thể loại (Hán Việt cho tu tiên, giữ nguyên tên Tây
cho western fantasy) thay vì mỗi chunk dịch một kiểu word-by-word.

Flow:
  1. Mine candidate terms từ raw/translated text (cụm Capitalized lặp lại, term trong 《》/quotes).
  2. Gửi 1 batch Ollama call: map candidates → canonical_vi theo policy thể loại.
  3. Merge với glossary.json hiện có (entry cũ thắng — giữ chỉnh sửa tay).
  4. Ghi story_data/story_memory/{story_id}-{slug}/glossary.json — format chuẩn
     story_memory.py đọc được ngay, tự inject vào translate + polish.

Usage:
  # Seed từ 10 chương đầu
  python scripts/story_pipeline/extract_term_glossary.py --story-id <id> --to-chapter 10

  # Incremental: chỉ thêm term mới trong khoảng chương
  python scripts/story_pipeline/extract_term_glossary.py --story-id <id> --from-chapter 21 --to-chapter 40

  # Xem candidates không gọi LLM / không ghi file
  python scripts/story_pipeline/extract_term_glossary.py --story-id <id> --to-chapter 10 --dry-run

polish_worker.py gọi update_term_glossary() tự động (seed khi thiếu, incremental theo cooldown).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from extract_char_map import fetch_chapters, get_chapter_text, story_slug_from_row, unload_ollama_model
from story_memory import DEFAULT_MEMORY_ROOT

try:
    from story_db.story_pipeline_db import repository as repo
    _REPO_AVAILABLE = True
except Exception:
    _REPO_AVAILABLE = False


# ── Genre → term translation policy ────────────────────────────────────────────

_HAN_VIET_GENRES = {"tien_hiep", "korean_cultivation", "kiem_hiep", "huyen_huyen"}

_POLICY_HAN_VIET = """\
Thể loại tu tiên/võ hiệp ({genre}): cảnh giới, công pháp, bí kíp, tông môn, pháp bảo,
danh hiệu PHẢI dịch sang âm Hán Việt chuẩn của truyện tu tiên — KHÔNG dịch nghĩa từng chữ.
Ví dụ: "Three Flowers Gathered at the Peak" → "Tam Hoa Tụ Đỉnh" (KHÔNG phải "ba hoa hội tụ đỉnh cao");
"Qi Refining" → "Luyện Khí"; "Transcendent Cultivation Record" → "Siêu Việt Tu Chân Lục";
"cultivator" → "tu sĩ". TÊN NGƯỜI giữ phiên âm gốc (Hàn: Seo Eun-Hyun; Trung: âm Hán Việt)."""

_POLICY_KEEP_WESTERN = """\
Thể loại {genre}: tên người, địa danh, tổ chức, danh hiệu phương Tây GIỮ NGUYÊN theo
nguyên bản hoặc dịch tự nhiên hiện đại — TUYỆT ĐỐI KHÔNG Hán Việt hóa. Danh hiệu có
tính biểu tượng dịch theo nghĩa văn học (Demon Slayer → Sát Quỷ Nhân chỉ khi truyện
có sắc thái đó, mặc định giữ nghĩa tự nhiên)."""


def term_policy_for_genre(genre: str) -> str:
    if genre in _HAN_VIET_GENRES:
        return _POLICY_HAN_VIET.format(genre=genre or "tu tiên")
    return _POLICY_KEEP_WESTERN.format(genre=genre or "fantasy/hiện đại")


# ── Candidate term mining ───────────────────────────────────────────────────────

# Common English words that start sentences/phrases — không phải proper noun signal.
_EN_PHRASE_STOP_FIRST = {
    "The", "A", "An", "This", "That", "These", "Those", "It", "He", "She", "They",
    "We", "You", "I", "My", "His", "Her", "Their", "Our", "Its", "There", "Here",
    "When", "While", "After", "Before", "Then", "Now", "But", "And", "Or", "If",
    "As", "At", "In", "On", "Of", "To", "For", "From", "With", "Without", "By",
    "However", "Although", "Though", "Because", "Since", "Even", "Just", "Only",
    "What", "Why", "How", "Where", "Who", "Whose", "Which", "Yes", "No", "Not",
    "Chapter", "Translator", "Editor", "Author", "Note",
}

# Cụm >=2 từ Capitalized (cho phép of/the/at/in/and ở giữa): "Core Formation",
# "Three Flowers Gathered at the Peak", "Heavenly Demon Sect".
_EN_PHRASE_RE = re.compile(
    r"\b([A-Z][a-zA-Z'\-]+(?:\s+(?:of|the|at|in|and|[A-Z][a-zA-Z'\-]+)){1,6})\b"
)
# Term trong 《》, "..." hoặc '...' có chữ hoa — thường là tên bí kíp/kỹ năng.
_BRACKET_TERM_RE = re.compile(r"[《\[]([^《》\[\]\n]{3,60})[》\]]")

# Cụm Capitalized tiếng Việt (>=2 từ có dấu): "Tam Hoa Tụ Đỉnh", "Thiên Môn".
_VI_PHRASE_RE = re.compile(
    r"\b([A-ZÀ-Ỹ][a-zà-ỹơưăâêôđ'\-]+(?:\s+[A-ZÀ-Ỹ][a-zà-ỹơưăâêôđ'\-]+){1,5})\b"
)


def _clean_phrase(phrase: str) -> str:
    phrase = phrase.strip().strip("'\"-–—")
    # Bỏ leading article
    for art in ("The ", "A ", "An "):
        if phrase.startswith(art):
            phrase = phrase[len(art):]
    return phrase.strip()


def _first_sentence_with(text: str, phrase: str) -> str:
    idx = text.find(phrase)
    if idx < 0:
        return ""
    start = max(text.rfind(".", 0, idx), text.rfind("\n", 0, idx)) + 1
    end = text.find(".", idx)
    end = end + 1 if end > 0 else min(len(text), idx + 160)
    return re.sub(r"\s+", " ", text[start:end]).strip()[:200]


def mine_candidate_terms(
    texts: list[str],
    *,
    lang: str = "en",
    min_count: int = 3,
    max_terms: int = 50,
) -> list[dict[str, str]]:
    """Tìm recurring candidate terms. Returns [{term, context}] sorted by frequency."""
    counts: Counter[str] = Counter()
    contexts: dict[str, str] = {}
    phrase_re = _EN_PHRASE_RE if lang == "en" else _VI_PHRASE_RE

    for text in texts:
        if not text:
            continue
        for m in phrase_re.finditer(text):
            phrase = _clean_phrase(m.group(1))
            words = phrase.split()
            if len(words) < 2 or len(phrase) > 60:
                continue
            if words[0] in _EN_PHRASE_STOP_FIRST:
                continue
            # Phải còn >=2 từ Capitalized sau khi clean
            caps = [w for w in words if w[:1].isupper()]
            if len(caps) < 2:
                continue
            counts[phrase] += 1
            if phrase not in contexts:
                contexts[phrase] = _first_sentence_with(text, m.group(1))
        for m in _BRACKET_TERM_RE.finditer(text):
            phrase = _clean_phrase(m.group(1))
            if len(phrase) < 3 or not any(c.isupper() for c in phrase):
                continue
            counts[phrase] += 1
            if phrase not in contexts:
                contexts[phrase] = _first_sentence_with(text, m.group(1))

    # Gộp phrase con vào phrase dài hơn nếu phrase dài phổ biến tương đương
    # (ví dụ "Three Flowers" vs "Three Flowers Gathered at the Peak").
    results = [
        {"term": term, "context": contexts.get(term, "")}
        for term, cnt in counts.most_common()
        if cnt >= min_count
    ]
    return results[:max_terms]


# ── Ollama: candidates → glossary items ─────────────────────────────────────────

GLOSSARY_SYSTEM = """Bạn là chuyên gia biên dịch truyện (web novel/light novel) sang tiếng Việt, chuyên xây dựng glossary thuật ngữ nhất quán.

{policy}

Với mỗi term, trả về JSON object:
- "source_terms": [term gốc và biến thể viết hoa/viết thường thường gặp]
- "canonical_vi": bản dịch tiếng Việt chuẩn, nhất quán
- "kind": một trong "person" | "place" | "organization" | "realm" | "technique" | "item" | "title" | "other"
- "wrong_translations": [các bản dịch sai/word-by-word dễ mắc] (có thể rỗng)
- "priority": true nếu là thuật ngữ cốt lõi xuất hiện thường xuyên (cảnh giới, tổ chức chính, danh hiệu nhân vật chính)

Quy tắc:
- Term là TÊN NGƯỜI: canonical_vi giữ nguyên phiên âm gốc, kind="person", không cần wrong_translations.
- Chỉ trả về JSON array, không markdown, không giải thích.
- Bỏ qua term không phải danh từ riêng/thuật ngữ (cụm câu thông thường) bằng cách không đưa vào kết quả."""

GLOSSARY_USER = """Truyện: {story_title}
Thể loại: {genre}

Danh sách term lặp lại nhiều lần trong truyện (kèm câu ngữ cảnh):

{term_block}

Trả về JSON array glossary cho các term trên (bỏ qua cụm không phải thuật ngữ/danh từ riêng)."""


def call_ollama_glossary(
    base_url: str,
    model: str,
    candidates: list[dict[str, str]],
    *,
    story_title: str,
    genre: str,
    temperature: float = 0.1,
    num_ctx: int = 8192,
    timeout: int = 300,
) -> list[dict[str, Any]]:
    term_block = "\n".join(
        f'- "{c["term"]}"' + (f' — ngữ cảnh: {c["context"]}' if c.get("context") else "")
        for c in candidates
    )
    system = GLOSSARY_SYSTEM.format(policy=term_policy_for_genre(genre))
    user = GLOSSARY_USER.format(story_title=story_title, genre=genre or "(không rõ)", term_block=term_block)
    if "qwen3" in model.lower():
        user = "/no_think\n\n" + user
    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "options": {"temperature": temperature, "num_ctx": num_ctx},
        "keep_alive": "10m",
    }
    resp = requests.post(base_url.rstrip("/") + "/api/chat", json=payload, timeout=timeout)
    resp.raise_for_status()
    content = resp.json().get("message", {}).get("content", "")
    content = re.sub(r"<think>.*?</think>\s*", "", content, flags=re.DOTALL | re.IGNORECASE)
    content = re.sub(r"^```(?:json)?\s*", "", content.strip(), flags=re.IGNORECASE)
    content = re.sub(r"\s*```$", "", content)
    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"\[[\s\S]+\]", content)
        if not m:
            return []
        try:
            result = json.loads(m.group())
        except Exception:
            return []
    if not isinstance(result, list):
        return []
    items: list[dict[str, Any]] = []
    for item in result:
        if not isinstance(item, dict):
            continue
        canonical = str(item.get("canonical_vi") or "").strip()
        sources = item.get("source_terms") or []
        if isinstance(sources, str):
            sources = [sources]
        sources = [str(s).strip() for s in sources if str(s).strip()]
        if not canonical or not sources:
            continue
        cleaned: dict[str, Any] = {
            "id": re.sub(r"[^a-z0-9]+", "_", sources[0].lower()).strip("_") or "term",
            "source_terms": sources,
            "canonical_vi": canonical,
        }
        kind = str(item.get("kind") or "").strip()
        if kind:
            cleaned["kind"] = kind
        wrong = item.get("wrong_translations") or []
        if isinstance(wrong, str):
            wrong = [wrong]
        wrong = [str(w).strip() for w in wrong if str(w).strip() and str(w).strip() != canonical]
        if wrong:
            cleaned["wrong_translations"] = wrong
            # wrong_translations của term ưu tiên cao → forbidden để QA gate enforce
            if item.get("priority"):
                cleaned["forbidden"] = wrong
        if item.get("priority"):
            cleaned["priority"] = True
        items.append(cleaned)
    return items


# ── Glossary file merge ─────────────────────────────────────────────────────────

def _norm_term(term: str) -> str:
    return re.sub(r"\s+", " ", term.strip().lower())


def glossary_path_for(story_id: str, slug: str) -> Path:
    return DEFAULT_MEMORY_ROOT / f"{story_id}-{slug}" / "glossary.json"


def load_existing_glossary(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "[]")
        return data if isinstance(data, list) else []
    except Exception:
        return []


def merge_glossary(
    existing: list[dict[str, Any]],
    new_items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Merge new items vào existing. Entry cũ thắng (giữ chỉnh tay). Returns (merged, n_added)."""
    covered: set[str] = set()
    for item in existing:
        sources = item.get("source_terms") or item.get("source") or []
        if isinstance(sources, str):
            sources = [sources]
        for s in sources:
            covered.add(_norm_term(str(s)))
        canonical = item.get("canonical_vi") or item.get("vi") or ""
        if canonical:
            covered.add(_norm_term(str(canonical)))

    added = 0
    merged = list(existing)
    for item in new_items:
        sources = [_norm_term(s) for s in item.get("source_terms", [])]
        if any(s in covered for s in sources):
            continue
        merged.append(item)
        covered.update(sources)
        added += 1
    return merged, added


# ── Main update API (dùng bởi CLI và polish_worker) ─────────────────────────────

def update_term_glossary(
    *,
    story_id: str = "",
    story_title: str = "",
    from_chapter: int = 0,
    to_chapter: int = 0,
    text_source: str = "raw",
    ollama_url: str = "http://127.0.0.1:11434",
    model: str = "qwen3:14b",
    genre: str = "",
    min_count: int = 3,
    max_terms: int = 50,
    dry_run: bool = False,
    unload_after: bool = False,
) -> dict[str, Any]:
    """Mine + translate + merge glossary. Returns summary dict."""
    rows = fetch_chapters(
        story_title=story_title,
        story_id=story_id,
        from_chapter=from_chapter,
        to_chapter=to_chapter,
        text_source=text_source,
    )
    if not rows:
        return {"status": "no_chapters", "added": 0}

    story_id = story_id or str(rows[0].get("story_id") or "")
    slug = story_slug_from_row(rows[0])
    title = str(rows[0].get("story_title") or story_title or slug)
    texts = [get_chapter_text(row, text_source) for row in rows]

    # Đoán language để chọn mining regex: raw EN vs VI translated/polished
    lang = "en" if text_source == "raw" else "vi"
    sample = "\n".join(texts[:2])[:2000]
    if text_source == "raw" and re.search(r"[À-ỹ]", sample):
        lang = "vi"  # raw đã là tiếng Việt (nguồn VN)

    candidates = mine_candidate_terms(texts, lang=lang, min_count=min_count, max_terms=max_terms)
    g_path = glossary_path_for(story_id, slug)
    existing = load_existing_glossary(g_path)

    # Lọc candidates đã có trong glossary trước khi gọi LLM (tiết kiệm token)
    covered: set[str] = set()
    for item in existing:
        sources = item.get("source_terms") or item.get("source") or []
        if isinstance(sources, str):
            sources = [sources]
        covered.update(_norm_term(str(s)) for s in sources)
    candidates = [c for c in candidates if _norm_term(c["term"]) not in covered]

    print(f"[GLOSSARY] story={title!r} chapters={len(rows)} lang={lang} candidates={len(candidates)} existing={len(existing)}")
    if dry_run or not candidates:
        for c in candidates:
            print(f"  - {c['term']}")
        return {"status": "dry_run" if dry_run else "no_new_terms", "candidates": len(candidates), "added": 0, "path": str(g_path)}

    new_items = call_ollama_glossary(
        ollama_url, model, candidates, story_title=title, genre=genre,
    )
    if unload_after:
        unload_ollama_model(ollama_url, model)
    merged, added = merge_glossary(existing, new_items)
    if added:
        g_path.parent.mkdir(parents=True, exist_ok=True)
        g_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[GLOSSARY] +{added} terms → {g_path}")
    else:
        print("[GLOSSARY] Không có term mới sau merge.")

    # Track tiến độ trong stories.metadata để incremental mode biết điểm bắt đầu
    if _REPO_AVAILABLE and story_id and to_chapter:
        try:
            repo.update_story_metadata(story_id, {"glossary_updated_to_chapter": int(to_chapter)})
        except Exception as exc:
            print(f"[GLOSSARY] metadata update failed: {exc}")

    return {"status": "ok", "added": added, "total": len(merged), "path": str(g_path)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Auto-extract terminology glossary cho story memory")
    parser.add_argument("--story-id", default="")
    parser.add_argument("--story-title", default="")
    parser.add_argument("--from-chapter", type=int, default=0)
    parser.add_argument("--to-chapter", type=int, default=10)
    parser.add_argument("--text-source", choices=("raw", "translated", "polished"), default="raw",
                        help="Nguồn text để mine terms. raw (EN/nguồn gốc) cho kết quả tốt nhất.")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default="qwen3:14b")
    parser.add_argument("--genre", default="", help="Override genre; mặc định tự resolve từ DB/char map.")
    parser.add_argument("--min-count", type=int, default=3, help="Term phải lặp >= N lần. Default: 3.")
    parser.add_argument("--max-terms", type=int, default=50)
    parser.add_argument("--dry-run", action="store_true", help="Chỉ in candidates, không gọi LLM/ghi file.")
    args = parser.parse_args()

    if not args.story_id and not args.story_title:
        parser.error("Cần --story-id hoặc --story-title")

    genre = args.genre
    if not genre and args.story_id and _REPO_AVAILABLE:
        try:
            from genre_prompts import find_char_map_file, resolve_genre_from_context
            story = repo.get_story_by_id(args.story_id)
            genre = resolve_genre_from_context(
                str(story.get("category") or ""),
                raw_language=str(story.get("language") or ""),
                char_map_file=find_char_map_file(story_id=args.story_id, slug=""),
            )
        except Exception:
            genre = ""

    result = update_term_glossary(
        story_id=args.story_id,
        story_title=args.story_title,
        from_chapter=args.from_chapter,
        to_chapter=args.to_chapter,
        text_source=args.text_source,
        ollama_url=args.ollama_url,
        model=args.model,
        genre=genre,
        min_count=args.min_count,
        max_terms=args.max_terms,
        dry_run=args.dry_run,
        unload_after=True,
    )
    print(f"[GLOSSARY] result: {result}")


if __name__ == "__main__":
    main()
