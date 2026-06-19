#!/usr/bin/env python3
"""Scan translated/polished chapters for two classes of quality gap:

1. **Untranslated EN fragments** — English words/phrases still present in the
   Vietnamese output (word-for-word or missed segments).
2. **Inconsistent term translations** — the same source term is rendered with
   different Vietnamese strings across chapters.

Usage:
    python scan_translation_gaps.py --slug a-regressors-tale-of-cultivation
    python scan_translation_gaps.py --slug a-regressors-tale-of-cultivation --mode polished
    python scan_translation_gaps.py --slug a-regressors-tale-of-cultivation --jsonl gaps.jsonl
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from story_memory import load_seed_glossary, load_story_memory  # noqa: E402

# ---------------------------------------------------------------------------
# EN fragment detection
# ---------------------------------------------------------------------------

# Allowlist: short tokens that appear in Vietnamese (proper nouns, loanwords,
# game-like status-window text inside brackets, etc.)
_EN_ALLOWLIST: frozenset[str] = frozenset(
    [
        # Common loanwords kept in VI text
        "ok", "okay", "boss", "game", "level", "status", "hp", "mp", "sp",
        "exp", "rank", "grade", "skill", "buff", "debuff", "cooldown",
        "online", "offline", "chat", "guild", "party", "raid", "dungeon",
        "item", "drop", "quest", "npc", "ui", "dps", "tank", "healer",
        # Common single EN tokens that appear in system box text
        "lv", "max", "min", "stat",
    ]
)

# Matches [...] system/status box spans so we can mask them before scanning.
_BRACKET_SPAN_RE = re.compile(r"\[[^\]]{1,500}\]")

# Matches a run of 2+ consecutive ASCII-letter words (after bracket masking).
_EN_PHRASE_RE = re.compile(
    r"\b([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,}){1,9})\b"  # Title Case phrase ≥2 words
)

# Also match ALL-CAPS abbreviations ≥ 3 chars (e.g. "HP MANA" run together)
_EN_ALLCAPS_RE = re.compile(r"\b([A-Z]{3,}(?:\s+[A-Z]{3,})+)\b")

# Korean romanized name syllables that are NOT English words.
# A 2-word Title Case phrase where BOTH words are Korean syllables is a name,
# not an untranslated EN phrase.
_KOREAN_NAME_SYLLABLES: frozenset[str] = frozenset(
    [
        # Surnames
        "Kim", "Lee", "Park", "Choi", "Jung", "Kang", "Cho", "Yoon", "Jang",
        "Lim", "Han", "Oh", "Seo", "Shin", "Kwon", "Hwang", "Ahn", "Song",
        "Yoo", "Hong", "Jeon", "Moon", "Yang", "Ko", "Bae", "Baek", "Heo",
        "Jeok", "Jin", "Min", "Ryu", "Nam", "Ha",
        # Common given-name syllables
        "Young", "Hyun", "Joon", "Min", "Soo", "Ji", "Hoon", "Seok", "Woo",
        "Eun", "Myeong", "Yeon", "Semin", "Rae", "Ho", "Tae", "In", "Sang",
        "Gyu", "Jun", "Hyun", "Jae", "Won", "Sung", "Kyung",
        # Title suffixes sometimes embedded in name rendering
        "Nim", "Hyung",
        # Additional KR/CN romanised surnames and syllables
        "Gwak", "Byuk", "Byeok", "Chang", "Chun", "Dae", "Gon", "Gun",
        "Hae", "Hang", "Hee", "Hyuk", "Ik", "Il", "Jo", "Jong", "Joo",
        "Keun", "Kun", "Kyu", "Mun", "Pyo", "Ryun", "Sam", "Sim", "So",
        "Su", "Uk", "Wook", "Yeong", "Yi", "Yo", "Yong", "Yu", "Yuk",
        # Chinese romanised names that appear in xianxia/wuxia titles
        "Wei", "Chen", "Wang", "Liu", "Zhao", "Li", "Zhang", "Wu", "Lin",
        "Xu", "Ye", "Qian", "Feng", "Xiao", "Yan", "Bai", "Su", "Luo",
        "Tang", "Gu", "Chu", "Mo", "Mu", "Xue", "Song", "Lei", "Fang",
        "Zhu", "Qi", "Lan", "Jiang", "Shang",
    ]
)


def _looks_like_korean_name(phrase: str) -> bool:
    """True if every word in the phrase is a known Korean name syllable."""
    words = phrase.split()
    return len(words) >= 1 and all(w in _KOREAN_NAME_SYLLABLES for w in words)


def _mask_brackets(text: str) -> str:
    """Replace [...] spans with spaces so EN patterns don't match inside them."""
    return _BRACKET_SPAN_RE.sub(lambda m: " " * len(m.group()), text)


_JOB_TITLE_PREFIXES: frozenset[str] = frozenset(
    [
        "Manager", "Director", "Deputy", "Chief", "President", "Chairman",
        "Manager", "Scout", "Scouts", "Boy", "Old", "Section", "General",
        "Vice", "Senior", "Junior", "Team", "Group", "Head",
    ]
)


def _looks_like_name_or_title(phrase: str) -> bool:
    """Skip romanized KR names and 'Manager Kim' style fragments."""
    words = phrase.split()
    if not words:
        return False
    if _looks_like_korean_name(phrase):
        return True
    if len(words) == 2 and words[0] in _JOB_TITLE_PREFIXES:
        return True
    if len(words) >= 2 and words[0] in _JOB_TITLE_PREFIXES and all(
        w[0:1].isupper() for w in words[1:]
    ):
        return True
    if len(words) == 2 and words[1] in _KOREAN_NAME_SYLLABLES:
        return True
    return False


def _extract_en_fragments(text: str) -> list[str]:
    masked = _mask_brackets(text)
    fragments: list[str] = []
    for m in _EN_PHRASE_RE.finditer(masked):
        phrase = m.group(1)
        words = phrase.split()
        if all(w.lower() in _EN_ALLOWLIST for w in words):
            continue
        if _looks_like_name_or_title(phrase):
            continue
        fragments.append(phrase)
    for m in _EN_ALLCAPS_RE.finditer(masked):
        phrase = m.group(1)
        if all(w.lower() in _EN_ALLOWLIST for w in phrase.split()):
            continue
        if _looks_like_name_or_title(phrase):
            continue
        fragments.append(phrase)
    return list(dict.fromkeys(fragments))  # deduplicate, preserve order


def load_chapters_from_db(
    story_id: str,
    mode: str = "polished",
    *,
    from_chapter: int = 0,
    to_chapter: int = 0,
    limit: int | None = None,
) -> dict[str, str]:
    """Load chapter text from DB for gap scanning."""
    from story_db.story_pipeline_db.db import connect

    col = "polished_text_content" if mode == "polished" else "translated_text_content"
    query = f"""
        SELECT chapter_number, {col} AS content
        FROM chapters
        WHERE story_id = %s AND {col} IS NOT NULL AND length(trim({col})) > 0
    """
    params: list[Any] = [story_id]
    if from_chapter:
        query += " AND chapter_number >= %s"
        params.append(from_chapter)
    if to_chapter:
        query += " AND chapter_number <= %s"
        params.append(to_chapter)
    query += " ORDER BY chapter_number"
    if limit:
        query += " LIMIT %s"
        params.append(limit)

    chapters: dict[str, str] = {}
    with connect() as conn:
        for row in conn.execute(query, params).fetchall():
            num = int(row["chapter_number"])
            text = str(row["content"] or "").strip()
            if text:
                chapters[f"chapter{num:04d}"] = text
    return chapters


def scan_story_gaps_from_db(
    *,
    story_id: str,
    slug: str = "",
    mode: str = "polished",
    from_chapter: int = 0,
    to_chapter: int = 0,
    story_memory_dir: str = "",
    char_map_file: str = "",
    genre: str = "",
    limit: int | None = None,
    check_en: bool = True,
    check_terms: bool = True,
) -> dict[str, Any]:
    """Scan DB chapter content for EN fragments and glossary violations."""
    chapters = load_chapters_from_db(
        story_id,
        mode=mode,
        from_chapter=from_chapter,
        to_chapter=to_chapter,
        limit=limit,
    )
    en_findings: list[dict[str, Any]] = []
    if check_en:
        for chapter_name, text in sorted(chapters.items()):
            frags = _extract_en_fragments(text)
            if frags:
                en_findings.append({"type": "en_fragment", "chapter": chapter_name, "fragments": frags})

    inconsistencies: list[dict[str, Any]] = []
    mem_dir = story_memory_dir
    if not mem_dir and slug:
        mem_root = ROOT / "story_data" / "story_memory"
        if mem_root.is_dir():
            for d in mem_root.iterdir():
                if d.is_dir() and slug in d.name:
                    mem_dir = str(d)
                    break

    if check_terms and mem_dir:
        inconsistencies = _find_inconsistencies(
            chapters,
            mem_dir,
            story_id=story_id,
            slug=slug,
            char_map_file=char_map_file,
            genre=genre,
        )

    priority_issues = sum(1 for f in inconsistencies if f.get("priority"))
    return {
        "chapters_scanned": len(chapters),
        "en_chapter_count": len(en_findings),
        "term_issue_count": len(inconsistencies),
        "priority_term_issues": priority_issues,
        "should_repolish": bool(en_findings or priority_issues),
        "en_findings": en_findings,
        "inconsistencies": inconsistencies,
    }


# ---------------------------------------------------------------------------
# Inconsistency detection
# ---------------------------------------------------------------------------

def _build_surface_pattern(term: str) -> re.Pattern[str]:
    escaped = re.escape(term)
    return re.compile(r"(?<!\w)" + escaped + r"(?!\w)", re.IGNORECASE)


def _find_inconsistencies(
    chapters: dict[str, str],
    story_memory_dir: str,
    story_id: str = "",
    slug: str = "",
    char_map_file: str = "",
    genre: str = "",
) -> list[dict[str, Any]]:
    """For each glossary entry (story + seed), check if wrong_translations
    appear in the output.  Story entries take precedence; seed entries fill in
    the gaps.  Returns a list of findings."""
    try:
        mem = load_story_memory(
            story_memory_dir=story_memory_dir,
            story_id=story_id,
            slug=slug,
            char_map_file=char_map_file,
        )
    except Exception as exc:
        print(f"[WARN] Could not load story memory: {exc}", file=sys.stderr)
        return []

    # Build combined glossary: story entries first (higher precedence), then
    # seed entries whose canonical_vi doesn't duplicate a story entry.
    story_canonicals: set[str] = set()
    combined_items: list[dict[str, Any]] = []
    for item in mem.glossary:
        cv = str(item.get("canonical_vi") or item.get("vi") or "").strip().lower()
        if cv:
            story_canonicals.add(cv)
        combined_items.append(item)

    if genre:
        try:
            seed_items = load_seed_glossary(genre)
            for item in seed_items:
                cv = str(item.get("canonical_vi") or item.get("vi") or "").strip().lower()
                if cv and cv not in story_canonicals:
                    combined_items.append(item)
        except Exception as exc:
            print(f"[WARN] Could not load seed glossary for genre {genre!r}: {exc}", file=sys.stderr)

    findings: list[dict[str, Any]] = []

    for item in combined_items:
        canonical = str(item.get("canonical_vi") or item.get("vi") or "").strip()
        if not canonical:
            continue

        wrong_list = item.get("wrong_translations") or []
        if not wrong_list:
            continue

        for wrong in wrong_list:
            wrong = str(wrong).strip()
            if not wrong:
                continue
            pat = _build_surface_pattern(wrong)
            hits: list[str] = []
            for chapter_name, text in chapters.items():
                if pat.search(text):
                    hits.append(chapter_name)
            if hits:
                findings.append(
                    {
                        "type": "wrong_term",
                        "wrong": wrong,
                        "correct": canonical,
                        "chapters": hits,
                        "chapter_count": len(hits),
                        "priority": bool(item.get("priority")),
                    }
                )

    return findings


# ---------------------------------------------------------------------------
# Chapter loading
# ---------------------------------------------------------------------------

CHAPTER_RE = re.compile(r"chapter(\d+)\.txt$", re.IGNORECASE)


def _chapter_num(path: Path) -> int:
    m = CHAPTER_RE.match(path.name)
    return int(m.group(1)) if m else 0


def _load_chapters(directory: Path, limit: int | None) -> dict[str, str]:
    files = sorted(
        [p for p in directory.glob("chapter*.txt") if CHAPTER_RE.match(p.name)],
        key=_chapter_num,
    )
    if limit:
        files = files[:limit]
    result: dict[str, str] = {}
    for f in files:
        try:
            result[f.name] = f.read_text(encoding="utf-8")
        except Exception as exc:
            print(f"[WARN] Cannot read {f}: {exc}", file=sys.stderr)
    return result


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _print_report(
    slug: str,
    mode: str,
    en_findings: list[dict[str, Any]],
    inconsistencies: list[dict[str, Any]],
) -> None:
    print(f"\n{'='*60}")
    print(f"TRANSLATION GAP REPORT — {slug} ({mode})")
    print(f"{'='*60}")

    if inconsistencies:
        priority = [f for f in inconsistencies if f.get("priority")]
        non_priority = [f for f in inconsistencies if not f.get("priority")]
        print(f"\n[WRONG TERMS] {len(inconsistencies)} pattern(s) found in output")
        print(f"  Priority violations: {len(priority)}")
        print(f"  Non-priority: {len(non_priority)}")

        all_sorted = sorted(inconsistencies, key=lambda x: (-x["chapter_count"], -int(x.get("priority", False))))
        for f in all_sorted[:30]:
            flag = " [PRIORITY]" if f["priority"] else ""
            chaps = ", ".join(f["chapters"][:5])
            if len(f["chapters"]) > 5:
                chaps += f", …+{len(f['chapters'])-5}"
            print(f"  '{f['wrong']}' → '{f['correct']}'{flag}")
            print(f"    found in: {chaps}")
        if len(all_sorted) > 30:
            print(f"  … and {len(all_sorted)-30} more")
    else:
        print("\n[WRONG TERMS] None found — glossary coverage looks good!")

    if en_findings:
        print(f"\n[UNTRANSLATED EN] {len(en_findings)} chapter(s) have English fragments")
        for f in en_findings[:20]:
            phrases = "; ".join(f["fragments"][:5])
            if len(f["fragments"]) > 5:
                phrases += f" …+{len(f['fragments'])-5}"
            print(f"  {f['chapter']}: {phrases}")
        if len(en_findings) > 20:
            print(f"  … and {len(en_findings)-20} more chapters")
    else:
        print("\n[UNTRANSLATED EN] None detected")

    print()


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"[REPORT] Wrote {len(rows)} rows → {path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--slug", required=True, help="Story slug, e.g. a-regressors-tale-of-cultivation")
    ap.add_argument(
        "--mode",
        choices=["translated", "polished"],
        default="translated",
        help="Which output stage to scan (default: translated)",
    )
    ap.add_argument("--limit", type=int, default=None, help="Only scan first N chapters")
    ap.add_argument("--story-id", default="", help="UUID story ID for memory lookup")
    ap.add_argument("--char-map-file", default="", help="Path to char_map .txt file")
    ap.add_argument("--genre", default="", help="Genre override for seed glossary")
    ap.add_argument("--story-memory-dir", default="", help="Override story_memory dir path")
    ap.add_argument(
        "--jsonl", default="", help="Write structured findings to this JSONL file"
    )
    ap.add_argument("--no-en-check", action="store_true", help="Skip English fragment scan")
    ap.add_argument("--no-term-check", action="store_true", help="Skip wrong-term scan")
    args = ap.parse_args()

    # Resolve input directory
    input_dir = ROOT / "story_data" / args.mode / args.slug
    if not input_dir.is_dir():
        raise SystemExit(f"Directory not found: {input_dir}")

    chapters = _load_chapters(input_dir, args.limit)
    if not chapters:
        raise SystemExit(f"No chapter*.txt files found in {input_dir}")

    print(f"Scanning {len(chapters)} chapter(s) from {input_dir}…")

    # Resolve story memory dir
    story_memory_dir = args.story_memory_dir
    if not story_memory_dir:
        mem_root = ROOT / "story_data" / "story_memory"
        if mem_root.is_dir():
            candidates = [
                d for d in mem_root.iterdir()
                if d.is_dir() and args.slug in d.name
            ]
            if candidates:
                story_memory_dir = str(candidates[0])

    # Resolve char_map file
    char_map_file = args.char_map_file
    if not char_map_file:
        for ext in (".txt",):
            candidate = ROOT / "story_data" / "char_maps" / f"{args.slug}{ext}"
            if not candidate.exists():
                # Try with UUID prefix
                cm_dir = ROOT / "story_data" / "char_maps"
                if cm_dir.is_dir():
                    for f in cm_dir.glob(f"*{args.slug}*"):
                        candidate = f
                        break
            if candidate.exists():
                char_map_file = str(candidate)
                break

    # --- EN fragment scan ---
    en_findings: list[dict[str, Any]] = []
    if not args.no_en_check:
        for chapter_name, text in sorted(chapters.items()):
            frags = _extract_en_fragments(text)
            if frags:
                en_findings.append({"type": "en_fragment", "chapter": chapter_name, "fragments": frags})

    # --- Wrong term scan ---
    inconsistencies: list[dict[str, Any]] = []
    if not args.no_term_check and story_memory_dir:
        inconsistencies = _find_inconsistencies(
            chapters,
            story_memory_dir=story_memory_dir,
            story_id=args.story_id,
            slug=args.slug,
            char_map_file=char_map_file,
            genre=args.genre,
        )
    elif not args.no_term_check and not story_memory_dir:
        print("[WARN] No story_memory dir found — skipping wrong-term scan", file=sys.stderr)

    # --- Output ---
    _print_report(args.slug, args.mode, en_findings, inconsistencies)

    if args.jsonl:
        all_rows = en_findings + inconsistencies
        _write_jsonl(ROOT / args.jsonl if not Path(args.jsonl).is_absolute() else Path(args.jsonl), all_rows)

    # Exit code: 1 if priority violations found
    priority_hits = [f for f in inconsistencies if f.get("priority")]
    if priority_hits:
        print(f"[EXIT 1] {len(priority_hits)} priority term violation(s) detected", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
