#!/usr/bin/env python3
"""
Translation/polish quality scanner.

Hai cách dùng:
  1. Library: check_polished_quality(text, genre, char_map_path) → list[str]
     Gọi từ polish_worker.py sau mỗi chapter để log cảnh báo.

  2. CLI: scan và optionally trigger repolish cho chapters có vấn đề.
     python check_translation_quality.py --story-id <id> [--repolish-bad]

Quality rules (blocking):
  - not_vietnamese: output không phải tiếng Việt
  - cjk_not_translated: còn ký tự CJK chưa dịch
  - repeated_content: đoạn văn lặp (exact hoặc near-duplicate — model looping)
  - forbidden_term: term bị cấm trong char map (## !! TRÁNH:)
  - wrong_pronoun: dùng hắn/nàng/lão/y trong văn kể (western_fantasy only)
  - large_en_block: đoạn tiếng Anh > 80 chars chưa dịch

Quality rules (warning — chưa block, đang calibrate):
  - length_ratio_low: output ngắn bất thường so với source (có thể bị tóm tắt/bỏ đoạn)
  - structure_drift: số đoạn văn / dòng thoại lệch mạnh so với source
  - source_unavailable: không load được source text → không check completeness được
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
for p in (str(ROOT), str(SCRIPT_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

def connect():
    """Lazy DB import — library use (check_polished_quality / run_full_quality_check)
    không cần story_db; chỉ CLI scan/retranslate mới cần."""
    from story_db.story_pipeline_db.db import connect as _connect
    return _connect()

# ── Vietnamese detection (standalone, no circular import) ────────────────────
_VI_DIACRITIC_RE = re.compile(
    r"[àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợ"
    r"ùúủũụưừứửữựỳýỷỹỵđ"
    r"ÀÁẢÃẠĂẰẮẲẴẶÂẦẤẨẪẬÈÉẺẼẸÊỀẾỂỄỆÌÍỈĨỊÒÓỎÕỌÔỒỐỔỖỘƠỜỚỞỠỢ"
    r"ÙÚỦŨỤƯỪỨỬỮỰỲÝỶỸỴĐ]"
)
_VI_WORDS = {"của", "và", "là", "có", "không", "được", "người", "một", "trong", "với",
             "tôi", "anh", "cô", "ông", "bà", "họ", "đã", "để", "khi", "như",
             "cho", "từ", "về", "lên", "xuống", "ra", "vào", "rằng", "nhưng",
             "mà", "vì", "nếu", "thì", "hay", "hoặc", "đây", "đó", "này", "kia"}
_CJK_RE = re.compile(r"[㐀-鿿぀-ヿ가-힯]")


def is_probably_vietnamese(text: str) -> bool:
    sample = re.sub(r"\s+", " ", text or "").strip()
    if len(sample) < 80:
        return False
    if len(_CJK_RE.findall(sample)) >= 8:
        return False
    diacritics = len(_VI_DIACRITIC_RE.findall(sample))
    words = re.findall(r"[\wÀ-ỹ]+", sample.lower(), flags=re.UNICODE)
    vi_hits = len({w for w in words if w in _VI_WORDS})
    return diacritics >= 12 or vi_hits >= 4


# ── Issue constants ─────────────────────────────────────────────────────────

# Issues in this set trigger automatic re-polish/re-translate retry.
BLOCKING_QUALITY_ISSUES: frozenset[str] = frozenset({
    "not_vietnamese",
    "cjk_not_translated",
    "repeated_content",
    "large_en_block",
    "wrong_pronoun",   # sai đại từ hắn/nàng trong western_fantasy/do_thi/lang_man
    "forbidden_term",  # dùng từ bị cấm trong char map
})

# Length-ratio floors theo ngôn ngữ nguồn: len(polished, no-ws) / len(source, no-ws).
# Warning-only cho tới khi đo empirical từ known-good chapters và promote
# `truncated_output` vào BLOCKING_QUALITY_ISSUES. Override qua env:
#   QUALITY_LENGTH_FLOOR_EN=0.8 QUALITY_LENGTH_FLOOR_ZH=1.3 ...
_LENGTH_RATIO_FLOORS_DEFAULT: dict[str, float] = {
    "en": 0.75,  # VI thường ~0.9–1.2x EN chars
    "zh": 1.2,   # VI ~1.5–2.2x ZH chars
    "cn": 1.2,
    "ko": 0.8,
    "kr": 0.8,
    "vi": 0.7,   # polish VI→VI — khớp min_output_ratio fallback hiện có
}
_LENGTH_RATIO_FLOOR_FALLBACK = 0.7


def _length_ratio_floor(source_language: str) -> float:
    lang = (source_language or "").strip().lower()
    env = os.environ.get(f"QUALITY_LENGTH_FLOOR_{lang.upper()}")
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    return _LENGTH_RATIO_FLOORS_DEFAULT.get(lang, _LENGTH_RATIO_FLOOR_FALLBACK)


def issue_to_repair_hint(issue: str) -> str:
    """Convert a quality issue code to a Vietnamese repair instruction for the model."""
    base = issue.split(":")[0]
    if base == "not_vietnamese":
        return "Toàn bộ output phải bằng tiếng Việt — không để lại nội dung bằng ngôn ngữ khác."
    if base == "cjk_not_translated":
        return "Còn ký tự tiếng Trung/Hàn chưa dịch — dịch toàn bộ sang tiếng Việt tự nhiên."
    if base == "repeated_content":
        return "Có đoạn văn bị lặp lại — xóa nội dung lặp, giữ mỗi đoạn xuất hiện một lần."
    if base == "large_en_block":
        return "Còn đoạn tiếng Anh chưa dịch — dịch toàn bộ sang tiếng Việt tự nhiên."
    if base == "wrong_pronoun":
        return "Dùng sai đại từ hắn/nàng/lão/y cho thể loại này — đổi thành anh ta/cô ấy theo char map."
    if base == "forbidden_term":
        term = issue[len("forbidden_term:"):].strip() if ":" in issue else ""
        return f"Dùng từ bị cấm {term} — đổi theo char map." if term else "Còn từ bị cấm trong char map — kiểm tra và đổi."
    if base in {"length_ratio_low", "truncated_output"}:
        return ("Output ngắn bất thường so với bản gốc — dịch ĐẦY ĐỦ mọi câu, mọi đoạn; "
                "tuyệt đối không tóm tắt, không bỏ đoạn.")
    if base == "structure_drift":
        return ("Số đoạn văn/dòng thoại lệch mạnh so với bản gốc — giữ nguyên cấu trúc đoạn "
                "và đầy đủ các câu thoại của bản gốc.")
    if base == "judge":
        sub = issue.split(":", 1)[1].strip() if ":" in issue else ""
        judge_hints = {
            "word_for_word": ("Bản dịch bám từng chữ nguồn — viết lại thành câu tiếng Việt "
                              "tự nhiên, đúng nghĩa, không giữ cú pháp ngôn ngữ nguồn."),
            "omission": "Có câu/ý trong nguyên bản bị bỏ sót — dịch đầy đủ mọi câu, không tóm tắt.",
            "mistranslation": "Có chỗ dịch sai nghĩa so với nguyên bản — dịch lại đúng nghĩa trong ngữ cảnh.",
            "wrong_pronoun": "Xưng hô/đại từ sai hoặc bất nhất — thống nhất theo char map.",
            "unnatural": "Câu văn lủng củng, không tự nhiên — viết lại mượt mà để đọc audio.",
        }
        return judge_hints.get(sub, f"Lỗi chất lượng (LLM judge): {sub or issue}")
    return f"Lỗi chất lượng: {issue}"


# ── Patterns ────────────────────────────────────────────────────────────────

# Hán Việt pronouns that shouldn't appear in western_fantasy/do_thi narrative
_WRONG_PRONOUN_GENRES = {"western_fantasy", "do_thi", "lang_man", "korean_cultivation"}
# Match "hắn/nàng/lão/y" as standalone words in narrative (outside quoted dialogue).
# Capitalized Hắn/Nàng cũng là pronoun (đầu câu — rất phổ biến); Lão/Y hoa KHÔNG
# tính vì có thể là title trước tên riêng (Lão Trần) hoặc tên viết tắt.
_WRONG_PRONOUN_RE = re.compile(r"\b(hắn|Hắn|nàng|Nàng|lão|y)\b")
# Compound nouns that legitimately contain lão/y — not pronoun usage
# e.g. trưởng lão (elder), ông lão (old man), y tá (nurse), y học (medicine)
_COMPOUND_NOUN_RE = re.compile(
    r"\b(trưởng|ông|bà|cụ|già)\s+lão\b"
    r"|\blão\s+(thành|làng|luyện|thực|thọ|giả|nhân|quái|tổ|tiền|tinh|hóa|già|phu|sư|gia|đại)\b"
    r"|\by\s+(tá|học|phục|lệnh|khoa|sĩ|viện|thuật)\b"
    r"|\b(nội|đông|đồng|trung)\s+y\b",
    re.IGNORECASE | re.UNICODE,
)
# Detect large untranslated English blocks (80+ non-Vietnamese chars)
_EN_BLOCK_RE = re.compile(r"[A-Za-z][A-Za-z ,\.'\-]{79,}")


def _has_cjk_contamination(text: str, threshold: int = 5) -> bool:
    """True if text has >= threshold CJK characters (untranslated source still present)."""
    return len(_CJK_RE.findall(text)) >= threshold


_NORMALIZE_PARA_RE = re.compile(r"[\s\.,;:!\?\"'“”‘’\-—…]+", re.UNICODE)


def _normalize_paragraph(p: str) -> str:
    return _NORMALIZE_PARA_RE.sub(" ", p.lower()).strip()


def _has_repeated_content(
    text: str, min_block: int = 120, near_dup_ratio: float = 0.92, window: int = 8
) -> bool:
    """True if any paragraph of >= min_block chars repeats (model looping).

    Bắt cả exact-duplicate (sau normalize whitespace/punctuation/case) lẫn
    near-duplicate: SequenceMatcher ratio >= near_dup_ratio so với các paragraph
    trong sliding window `window` đoạn gần nhất — giữ O(n·window).
    """
    paragraphs = [
        _normalize_paragraph(p)
        for p in re.split(r"\n\s*\n", text)
        if len(p.strip()) >= min_block
    ]
    seen: set[str] = set()
    for i, p in enumerate(paragraphs):
        if p in seen:
            return True
        seen.add(p)
        for j in range(max(0, i - window), i):
            other = paragraphs[j]
            # Quick length pre-filter: very different lengths can't be near-dups.
            if min(len(p), len(other)) / max(len(p), len(other), 1) < near_dup_ratio:
                continue
            if SequenceMatcher(None, p, other).ratio() >= near_dup_ratio:
                return True
    return False


# ── Completeness / structure checks (warning-only until calibrated) ─────────

def _strip_len(text: str) -> int:
    return len(re.sub(r"\s+", "", text or ""))


def _count_dialogue_lines(text: str) -> int:
    count = 0
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(('"', "'", "“", "‘", "—", "[", "【")):
            count += 1
    return count


def check_completeness(
    polished_text: str, source_text: str, source_language: str = ""
) -> list[str]:
    """So polished với source — bắt tóm tắt/bỏ đoạn. Trả warnings (chưa blocking)."""
    issues: list[str] = []
    src_len = _strip_len(source_text)
    out_len = _strip_len(polished_text)
    if src_len < 200:
        return issues  # source quá ngắn, ratio không có ý nghĩa

    ratio = out_len / src_len
    floor = _length_ratio_floor(source_language)
    if ratio < floor:
        issues.append(f"length_ratio_low:{ratio:.2f}<{floor:.2f}")

    # Structural signals: bắt missing-middle-paragraphs khi total length vẫn bình thường.
    src_paras = len([p for p in re.split(r"\n\s*\n", source_text) if p.strip()])
    out_paras = len([p for p in re.split(r"\n\s*\n", polished_text) if p.strip()])
    if src_paras >= 8 and out_paras < src_paras * 0.5:
        issues.append(f"structure_drift:paragraphs:{out_paras}/{src_paras}")

    src_dlg = _count_dialogue_lines(source_text)
    out_dlg = _count_dialogue_lines(polished_text)
    if src_dlg >= 10 and out_dlg < src_dlg * 0.5:
        issues.append(f"structure_drift:dialogue_lines:{out_dlg}/{src_dlg}")

    return issues


def _extract_forbidden_terms(char_map_path: str) -> list[str]:
    """Parse '!! TRÁNH:' lines in char map header for banned terms."""
    terms: list[str] = []
    try:
        text = Path(char_map_path).read_text(encoding="utf-8")
    except OSError:
        return terms
    for line in text.splitlines():
        # Match lines like: ## !! TRÁNH: "Tinh Khí Tinh Tế" (sai), "Xây Dựng Khí" ...
        if "TRÁNH" not in line and "tranh" not in line.lower():
            continue
        # Extract quoted terms
        for m in re.finditer(r'"([^"]+)"', line):
            terms.append(m.group(1))
    return terms


def _count_wrong_pronouns(text: str) -> int:
    """Count wrong pronouns in narrative (exclude quoted dialogue lines)."""
    count = 0
    for line in text.splitlines():
        stripped = line.strip()
        # Skip lines that are mostly dialogue (start with quote/bracket character)
        if stripped.startswith(('"', "'", "\u201c", "\u2018", "\u2014", "[")):
            continue
        # Remove compound nouns (trưởng lão, ông lão, y tá...) before counting
        cleaned = _COMPOUND_NOUN_RE.sub(" ", stripped)
        count += len(_WRONG_PRONOUN_RE.findall(cleaned))
    return count


def check_polished_quality(
    text: str,
    genre: str = "",
    char_map_path: str = "",
    source_text: str = "",
    source_language: str = "",
) -> list[str]:
    """
    Trả về list các quality issue (empty = OK).
    Issues trong BLOCKING_QUALITY_ISSUES sẽ trigger retry; còn lại là warnings.
    Gọi sau khi polish xong, trước khi save vào DB.

    source_text/source_language (optional): bật completeness check
    (length_ratio_low / structure_drift — warning-only cho tới khi calibrate xong).
    """
    issues: list[str] = []
    if not text or len(text.strip()) < 100:
        issues.append("output_too_short")
        return issues

    # Check 1: must be Vietnamese
    if not is_probably_vietnamese(text):
        issues.append("not_vietnamese")

    # Check 2: CJK contamination (untranslated source still present).
    # Threshold 8 to avoid false positives from embedded Korean/Chinese terms in Korean LN.
    cjk_count = len(_CJK_RE.findall(text))
    if cjk_count >= 8:
        issues.append(f"cjk_not_translated:{cjk_count}")

    # Check 3: model looping (duplicate paragraphs)
    if _has_repeated_content(text):
        issues.append("repeated_content")

    # Check 4: forbidden terms from char map
    if char_map_path:
        bad_terms = _extract_forbidden_terms(char_map_path)
        for term in bad_terms:
            if term in text:
                issues.append(f"forbidden_term:{term!r}")

    # Check 5: wrong pronouns for genre
    if genre in _WRONG_PRONOUN_GENRES:
        pronoun_count = _count_wrong_pronouns(text)
        if pronoun_count >= 3:
            issues.append(f"wrong_pronoun:{pronoun_count}")

    # Check 6: untranslated English blocks
    en_blocks = _EN_BLOCK_RE.findall(text)
    if en_blocks:
        issues.append(f"large_en_block:{len(en_blocks)}")

    # Check 7: completeness vs source (warning-only)
    if source_text:
        issues.extend(check_completeness(text, source_text, source_language))

    return issues


def split_blocking_warnings(issues: list[str]) -> tuple[list[str], list[str]]:
    """Phân issues thành (blocking, warnings) theo BLOCKING_QUALITY_ISSUES."""
    blocking = [i for i in issues if any(i.startswith(b) for b in BLOCKING_QUALITY_ISSUES)]
    warnings = [i for i in issues if i not in blocking]
    return blocking, warnings


def run_full_quality_check(
    text: str,
    *,
    genre: str = "",
    char_map: str = "",
    story_id: str = "",
    slug: str = "",
    story_memory_dir: str = "",
    source_text: str = "",
    source_language: str = "",
    log: Callable[[str], None] | None = None,
) -> tuple[list[str], list[str]]:
    """Full quality check — char map heuristics + story-memory QA. Returns (blocking, warnings).

    Đây là logic chung cho cả worker gate (polish_worker._quality_check) lẫn CLI
    scanner, để offline scan và gate không bao giờ drift nhau.

    Blocking = BLOCKING_QUALITY_ISSUES + story-memory term/name drift (glossary
    forbidden terms). Register/format drift từ story memory chỉ là warning.
    """
    issues = check_polished_quality(
        text,
        genre=genre,
        char_map_path=char_map,
        source_text=source_text,
        source_language=source_language,
    )
    blocking, warnings = split_blocking_warnings(issues)

    try:
        from story_memory import find_story_memory_quality_issues, load_story_memory
        memory = load_story_memory(
            story_memory_dir=story_memory_dir,
            story_id=story_id,
            slug=slug,
            char_map_file=char_map,
        )
        if memory.loaded:
            for issue in find_story_memory_quality_issues(text, memory, genre=genre):
                if issue.startswith("term/name drift"):
                    blocking.append(issue)
                else:
                    warnings.append(issue)
    except Exception as exc:  # noqa: BLE001 — memory QA không được làm chết caller
        if log:
            log(f"[QUALITY] story memory QA error: {exc}")

    return blocking, warnings


# ── DB scan ─────────────────────────────────────────────────────────────────

def fetch_polished_chapters(story_id: str, from_ch: int, to_ch: int) -> list[dict]:
    query = """
        SELECT
            c.id AS chapter_id, c.chapter_number, c.title AS chapter_title,
            c.polished_text_content, c.polished_text_path,
            c.raw_text_content, c.raw_text_path, c.translated_text_path,
            c.is_polished, c.is_translated,
            s.id AS story_id,
            s.title AS story_title, s.metadata AS story_metadata,
            src.code AS source_code,
            COALESCE(NULLIF(c.raw_language, ''), s.language, '') AS raw_language
        FROM chapters c
        JOIN stories s ON s.id = c.story_id
        JOIN sources src ON src.id = s.source_id
        WHERE s.id = %(story_id)s
          AND c.is_polished = TRUE
          AND (c.polished_text_content IS NOT NULL OR c.polished_text_path IS NOT NULL)
    """
    params: dict[str, Any] = {"story_id": story_id}
    if from_ch:
        query += " AND c.chapter_number >= %(from_ch)s"
        params["from_ch"] = from_ch
    if to_ch:
        query += " AND c.chapter_number <= %(to_ch)s"
        params["to_ch"] = to_ch
    query += " ORDER BY c.chapter_number"
    with connect() as conn:
        return [dict(r) for r in conn.execute(query, params).fetchall()]


def _read_polished_text(row: dict) -> str:
    content = row.get("polished_text_content") or ""
    if not content and row.get("polished_text_path"):
        try:
            p = Path(row["polished_text_path"])
            if not p.is_absolute():
                p = ROOT / p
            content = p.read_text(encoding="utf-8")
        except OSError:
            pass
    return content


def _read_source_text(row: dict) -> str:
    """Load raw source text: ưu tiên raw_text_content (DB-only crawls) rồi raw_text_path."""
    content = row.get("raw_text_content") or ""
    if not content and row.get("raw_text_path"):
        try:
            p = Path(row["raw_text_path"])
            if not p.is_absolute():
                p = ROOT / p
            content = p.read_text(encoding="utf-8")
        except OSError:
            pass
    return content


def scan_story(
    story_id: str,
    from_ch: int = 0,
    to_ch: int = 0,
    char_map_path: str = "",
    genre: str = "",
    story_memory_dir: str = "",
    judge_fn: Callable[[str, str, str], Any] | None = None,
) -> list[dict]:
    """Scan polished chapters, return list of {chapter_number, issues, blocking, warnings}.

    Dùng run_full_quality_check — cùng logic với worker gate (char map heuristics
    + story-memory QA + completeness), offline scan không drift so với gate.
    """
    rows = fetch_polished_chapters(story_id, from_ch, to_ch)
    results = []
    for row in rows:
        text = _read_polished_text(row)
        if not text:
            results.append({
                "chapter_number": row["chapter_number"],
                "issues": ["no_polished_text"], "blocking": ["no_polished_text"], "warnings": [],
            })
            continue
        source_text = _read_source_text(row)
        raw_language = (row.get("raw_language") or "").strip().lower()
        slug = ""
        if row.get("raw_text_path"):
            slug = Path(row["raw_text_path"]).parent.name
        blocking, warnings = run_full_quality_check(
            text,
            genre=genre,
            char_map=char_map_path,
            story_id=str(row.get("story_id") or story_id),
            slug=slug,
            story_memory_dir=story_memory_dir,
            log=print,
        )
        if source_text:
            # Completeness chạy riêng để gắn warning đúng nhóm (đã nằm trong
            # run_full_quality_check khi truyền source — ở đây truyền tách để
            # rows thiếu source vẫn được báo source_unavailable).
            warnings.extend(check_completeness(text, source_text, raw_language))
        else:
            warnings.append("source_unavailable")
        # LLM judge (optional): sampled semantic QA — kết quả là warnings trong
        # scanner (act qua --issue-filter judge: nếu muốn retranslate).
        if judge_fn and source_text:
            result = judge_fn(source_text, text, str(row.get("chapter_id") or ""))
            warnings.extend(result.issues)
            warnings.extend(result.warnings)
        if blocking or warnings:
            results.append({
                "chapter_number": row["chapter_number"],
                "chapter_id": row["chapter_id"],
                "issues": blocking + warnings,
                "blocking": blocking,
                "warnings": warnings,
            })
    return results


def reset_polished_for_repolish(
    chapter_ids: list[str], dry_run: bool = True, force_running: bool = False
) -> int:
    """
    Mark chapters as needing repolish: set is_polished=False AND reset polish_chapter jobs
    to pending (excluding running jobs by default to avoid races).
    """
    if not chapter_ids:
        return 0
    if dry_run:
        print(f"[DRY] Would reset is_polished=False + re-queue jobs for {len(chapter_ids)} chapters")
        return len(chapter_ids)
    status_exclude = [] if force_running else ["running"]
    with connect() as conn:
        conn.execute(
            "UPDATE chapters SET is_polished = FALSE WHERE id = ANY(%(ids)s::uuid[])",
            {"ids": chapter_ids},
        )
        conn.execute(
            f"""
            UPDATE story_jobs
            SET status = 'pending', attempts = 0, run_after = now(),
                locked_by = NULL, locked_at = NULL, last_error = NULL
            WHERE job_type = 'polish_chapter'
              AND chapter_id = ANY(%(ids)s::uuid[])
              {"AND status NOT IN %(exclude)s" if status_exclude else ""}
            """,
            {"ids": chapter_ids, "exclude": tuple(status_exclude)} if status_exclude else {"ids": chapter_ids},
        )
    return len(chapter_ids)


def retranslate_bad_chapters(bad_rows: list[dict], dry_run: bool = True, force_running: bool = False) -> int:
    """
    Reset bad chapters for full re-translation + re-polish via the job queue.
    Steps:
    1. Delete polished output files (so worker doesn't skip due to file exists)
    2. Reset is_translated=FALSE, is_polished=FALSE in DB
    3. Reset existing story_job to pending (or insert new job)
    Worker picks up the job and runs translate→polish.
    """
    if not bad_rows:
        return 0

    chapter_ids = [r["chapter_id"] for r in bad_rows if r.get("chapter_id")]
    if not chapter_ids:
        return 0

    if dry_run:
        print(f"[DRY] Would retranslate {len(chapter_ids)} chapters (force_running={force_running}):")
        for r in bad_rows:
            print(f"  ch{r['chapter_number']:04d} → delete polished file + reset DB flags + re-enqueue job")
        return len(chapter_ids)

    # Step 1: Delete polished output files
    deleted_files = 0
    for row in bad_rows:
        p_path = row.get("polished_text_path") or ""
        if p_path:
            p = Path(p_path) if Path(p_path).is_absolute() else ROOT / p_path
            if p.exists():
                p.unlink()
                deleted_files += 1

    # Step 2: Reset DB flags
    with connect() as conn:
        conn.execute(
            """
            UPDATE chapters
            SET is_translated = FALSE, is_polished = FALSE,
                translated_text_content = NULL, polished_text_content = NULL
            WHERE id = ANY(%(ids)s::uuid[])
            """,
            {"ids": chapter_ids},
        )
        # Step 3: Reset non-running jobs to pending (skip running by default to avoid races).
        status_filter = "" if force_running else "AND status NOT IN ('running')"
        updated = conn.execute(
            f"""
            UPDATE story_jobs
            SET status = 'pending', attempts = 0, run_after = now(),
                locked_by = NULL, locked_at = NULL, last_error = NULL
            WHERE job_type = 'polish_chapter'
              AND chapter_id = ANY(%(ids)s::uuid[])
              {status_filter}
            RETURNING chapter_id
            """,
            {"ids": chapter_ids},
        ).fetchall()
        updated_ids = {str(r["chapter_id"]) for r in updated}

        # Warn about running jobs that were intentionally skipped.
        if not force_running:
            running = conn.execute(
                """
                SELECT chapter_id FROM story_jobs
                WHERE job_type = 'polish_chapter'
                  AND chapter_id = ANY(%(ids)s::uuid[])
                  AND status = 'running'
                """,
                {"ids": chapter_ids},
            ).fetchall()
            if running:
                skipped_ids = [str(r["chapter_id"]) for r in running]
                print(f"[WARN] {len(skipped_ids)} chapter(s) are currently running — skipped to avoid races. "
                      f"Re-run after workers finish, or use --force-running to override.")
                chapter_ids = [cid for cid in chapter_ids if cid not in skipped_ids]

    # Step 4: For chapters with no existing job, insert new ones
    need_new_job = [r for r in bad_rows if r.get("chapter_id") and str(r["chapter_id"]) not in updated_ids]
    if need_new_job:
        from story_db.story_pipeline_db import repository as repo
        from genre_prompts import resolve_genre_from_context, find_char_map_file
        for row in need_new_job:
            raw_text_path = row.get("raw_text_path") or ""
            slug = Path(raw_text_path).parent.name if raw_text_path else ""
            chapter_num = int(row.get("chapter_number") or 0)
            chapter_stem = Path(raw_text_path).stem if raw_text_path else f"chapter{chapter_num:04d}"
            polished_path = ROOT / "story_data" / "polished" / slug / f"{chapter_stem}.txt"
            raw_language = row.get("raw_language") or "en"
            story_id = str(row.get("story_id") or "")
            source_code = row.get("source_code") or ""
            char_map_file = find_char_map_file(story_id=story_id, slug=slug)
            genre = resolve_genre_from_context(
                "", raw_language=raw_language, source_code=source_code, char_map_file=char_map_file
            )
            translated_path = str(ROOT / "story_data" / "translated" / slug / f"{chapter_stem}.txt")
            repo.enqueue_chapter_job(
                "polish_chapter",
                row["chapter_id"],
                story_id=story_id,
                source_code=source_code,
                model="qwen3:14b",
                input_path=raw_text_path,
                output_path=polished_path.as_posix(),
                payload={
                    "raw_language": raw_language,
                    "story_slug": slug,
                    "chapter_number": chapter_num,
                    "chapter_title": row.get("chapter_title") or chapter_stem,
                    "source_chapter_title": row.get("chapter_title") or chapter_stem,
                    "source_story_title": row.get("story_title") or "",
                    "translate_story_metadata": True,
                    "post_translate": "polish",
                    "genre": genre,
                    "char_map_file": char_map_file,
                    "translated_text_path": translated_path,
                },
            )
            # Force-reset inserted job to pending
            with connect() as conn:
                conn.execute(
                    """
                    UPDATE story_jobs
                    SET status = 'pending', attempts = 0, run_after = now()
                    WHERE job_type = 'polish_chapter' AND chapter_id = %(id)s::uuid
                    """,
                    {"id": row["chapter_id"]},
                )

    print(f"[RETRANSLATE] Reset {len(chapter_ids)} chapters: deleted {deleted_files} polished files, "
          f"reset {len(updated_ids)} existing jobs + inserted {len(need_new_job)} new jobs → pending")
    print("  Polish worker sẽ tự pick up. Không cần restart worker.")
    return len(chapter_ids)


# ── Pronoun post-processing ──────────────────────────────────────────────────

_DIALOGUE_STARTS = ('"', "'", "“", "‘", "—", "[")

_PRONOUN_FIXES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bhắn\b"), "anh ta"),
    (re.compile(r"\bHắn\b"), "Anh ta"),
    (re.compile(r"\bnàng\b"), "cô ấy"),
    # Nàng hoa đầu câu — guard: không thay khi đứng trước tên riêng (Nàng Bạch Tuyết)
    (re.compile(r"\bNàng\b(?!\s+[A-ZÀ-Ỹ])"), "Cô ấy"),
]
_SAFE_PRONOUN_REPLACEMENTS: list[tuple[re.Pattern, str]] = [
    # standalone 'y' pronoun (he/him archaic) — skip compound nouns (y tá, y học...)
    (re.compile(r"\by\b"), "anh ta"),
    # standalone 'lão' pronoun (he/the old one) — skip compound nouns (trưởng lão, lão nhân...)
    (re.compile(r"\blão\b"), "ông ta"),
    # Lão hoa đầu câu — guard: không thay khi là title trước tên riêng (Lão Trần)
    (re.compile(r"\bLão\b(?!\s+[A-ZÀ-Ỹ])"), "Ông ta"),
]


def _replace_safe(line: str, pat: re.Pattern, replacement: str) -> tuple[str, int]:
    """Replace pronoun pattern, skipping spans covered by _COMPOUND_NOUN_RE."""
    compound_spans = [(m.start(), m.end()) for m in _COMPOUND_NOUN_RE.finditer(line)]
    count = [0]
    def replacer(m: re.Match) -> str:
        if any(s <= m.start() < e for s, e in compound_spans):
            return m.group(0)
        count[0] += 1
        return replacement
    result = pat.sub(replacer, line)
    return result, count[0]


def _fix_pronouns_in_text(text: str) -> tuple[str, int]:
    """Replace hắn→anh ta, nàng→cô ấy, y→anh ta, lão→ông ta in narrative lines only.
    Skips dialogue lines and compound nouns (y tá, y học, ông lão, trưởng lão...).
    Returns (new_text, n_replaced)."""
    lines = text.splitlines(keepends=True)
    total_replaced = 0
    result = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(_DIALOGUE_STARTS):
            result.append(line)
            continue
        new_line = line
        for pat, replacement in _PRONOUN_FIXES:
            new_line, n = pat.subn(replacement, new_line)
            total_replaced += n
        for pat, replacement in _SAFE_PRONOUN_REPLACEMENTS:
            new_line, n = _replace_safe(new_line, pat, replacement)
            total_replaced += n
        result.append(new_line)
    return "".join(result), total_replaced


def fix_pronouns_in_db(
    bad_rows: list[dict], dry_run: bool = True
) -> int:
    """
    Post-process: replace hắn→anh ta, nàng→cô ấy in polished_text_content.
    Safe for first-person stories where hắn/nàng always refer to secondary characters.
    Also updates polished file on disk if it exists.
    """
    rows_with_pronoun = [
        r for r in bad_rows
        if any("wrong_pronoun" in issue for issue in r.get("issues", []))
    ]
    if not rows_with_pronoun:
        print("[FIX-PRONOUNS] No chapters with wrong_pronoun issues.")
        return 0

    if dry_run:
        print(f"[DRY] Would fix pronouns in {len(rows_with_pronoun)} chapters")
        return len(rows_with_pronoun)

    fixed = 0
    with connect() as conn:
        for row in rows_with_pronoun:
            chapter_id = str(row.get("chapter_id") or "")
            if not chapter_id:
                continue
            db_row = conn.execute(
                "SELECT polished_text_content, polished_text_path FROM chapters WHERE id = %(id)s::uuid",
                {"id": chapter_id},
            ).fetchone()
            if not db_row:
                continue
            text = db_row["polished_text_content"] or ""
            if not text and db_row["polished_text_path"]:
                p = Path(db_row["polished_text_path"])
                if not p.is_absolute():
                    p = ROOT / p
                try:
                    text = p.read_text(encoding="utf-8")
                except OSError:
                    pass
            if not text:
                print(f"  ch{row['chapter_number']:04d}: no content, skipping")
                continue

            new_text, n = _fix_pronouns_in_text(text)
            if n == 0:
                continue

            conn.execute(
                "UPDATE chapters SET polished_text_content = %(content)s WHERE id = %(id)s::uuid",
                {"content": new_text, "id": chapter_id},
            )
            # Also fix on disk if file exists
            p_path = db_row["polished_text_path"] or ""
            if p_path:
                p = Path(p_path) if Path(p_path).is_absolute() else ROOT / p_path
                if p.exists():
                    p.write_text(new_text, encoding="utf-8")
            print(f"  ch{row['chapter_number']:04d}: replaced {n} pronoun(s)")
            fixed += 1

    return fixed


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Scan polished chapter quality")
    parser.add_argument("--story-id", required=True)
    parser.add_argument("--from-chapter", type=int, default=0)
    parser.add_argument("--to-chapter", type=int, default=0)
    parser.add_argument("--char-map", default="", help="Path to char map file")
    parser.add_argument("--story-memory-dir", default="",
                        help="Override story memory dir (mặc định: convention story_data/story_memory/{story_id}-{slug})")
    parser.add_argument("--genre", default="", help="Override genre for checks")
    parser.add_argument("--repolish-bad", action="store_true",
                        help="Mark chapters with issues as is_polished=False so workers reprocess")
    parser.add_argument("--retranslate-bad", action="store_true",
                        help="Full re-translate: delete polished files, reset DB flags, re-enqueue jobs")
    parser.add_argument("--fix-pronouns", action="store_true",
                        help="Post-process: replace hắn→anh ta, nàng→cô ấy in narrative lines (DB update)")
    parser.add_argument("--issue-filter", default="",
                        help="Comma-separated issue types to filter on (e.g. not_vietnamese,forbidden_term)")
    parser.add_argument("--dry-run", action="store_true",
                        help="With --repolish-bad/--retranslate-bad/--fix-pronouns: show what would happen")
    parser.add_argument("--force-running", action="store_true",
                        help="Also reset currently-running jobs (risk of race; use only when workers are stopped)")
    parser.add_argument("--min-issues", type=int, default=1,
                        help="Min number of issues to flag a chapter (default: 1)")
    parser.add_argument("--llm-judge", action="store_true",
                        help="Chạy LLM judge (sampled semantic QA) trên mỗi chapter — chậm, "
                             "+1 Ollama call/chapter. Kết quả là warnings (judge:*) — kết hợp "
                             "--issue-filter judge: nếu muốn act.")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--judge-model", default="qwen3:14b")
    args = parser.parse_args()

    # Auto-find char map if not specified
    char_map = args.char_map
    if not char_map:
        with connect() as conn:
            rows = conn.execute(
                "SELECT metadata->>'char_map_path' AS cmp FROM stories WHERE id = %(id)s",
                {"id": args.story_id},
            ).fetchall()
            if rows and rows[0]["cmp"]:
                cmp = rows[0]["cmp"]
                p = Path(cmp) if Path(cmp).is_absolute() else ROOT / cmp
                if p.exists():
                    char_map = str(p)

    # Auto-detect genre
    genre = args.genre
    if not genre and char_map:
        from genre_prompts import infer_genre_from_char_map, load_char_map
        # infer_genre_from_char_map expects char map TEXT, not the file path —
        # truyền path làm genre rơi về DB fallback (sai genre cho map có header riêng).
        genre = infer_genre_from_char_map(load_char_map(char_map))
    if not genre:
        with connect() as conn:
            rows = conn.execute(
                "SELECT src.code AS source_code, s.language FROM stories s JOIN sources src ON src.id = s.source_id WHERE s.id = %(id)s",
                {"id": args.story_id},
            ).fetchall()
            if rows:
                from genre_prompts import detect_genre
                genre = detect_genre("", raw_language=rows[0]["language"] or "", source_code=rows[0]["source_code"] or "")

    print(f"[SCAN] story={args.story_id} genre={genre!r} char_map={'yes' if char_map else 'no'}")

    judge_fn = None
    if args.llm_judge:
        from llm_quality_judge import judge_chapter_quality

        def judge_fn(src: str, out: str, chapter_id: str):
            return judge_chapter_quality(
                src, out, genre=genre, ollama_url=args.ollama_url,
                model=args.judge_model, seed=chapter_id,
            )

    bad = scan_story(
        args.story_id,
        from_ch=args.from_chapter,
        to_ch=args.to_chapter,
        char_map_path=char_map,
        genre=genre,
        story_memory_dir=args.story_memory_dir,
        judge_fn=judge_fn,
    )

    bad = [r for r in bad if len(r["issues"]) >= args.min_issues]

    # Apply --issue-filter if specified
    issue_filter = [s.strip() for s in args.issue_filter.split(",") if s.strip()]
    if issue_filter:
        bad = [
            r for r in bad
            if any(any(f in issue for f in issue_filter) for issue in r["issues"])
        ]

    if not bad:
        print("[OK] Không tìm thấy chapter nào có vấn đề.")
        return

    n_blocking = sum(1 for r in bad if r.get("blocking"))
    print(f"\n[ISSUES] {len(bad)} chapter(s) có vấn đề ({n_blocking} blocking):\n")
    for r in bad:
        parts = []
        if r.get("blocking"):
            parts.append("BLOCK: " + ", ".join(r["blocking"]))
        if r.get("warnings"):
            parts.append("warn: " + ", ".join(r["warnings"]))
        print(f"  ch{r['chapter_number']:04d}: {' | '.join(parts) or ', '.join(r['issues'])}")

    # Actions chỉ áp dụng cho chapters có blocking issue — warnings (length_ratio_low,
    # source_unavailable...) không tự trigger retranslate. Nếu user truyền --issue-filter
    # thì coi như chủ động chọn, dùng nguyên list đã filter.
    if not issue_filter:
        actionable = [r for r in bad if r.get("blocking")]
        if len(actionable) != len(bad) and (args.retranslate_bad or args.repolish_bad):
            print(f"\n[NOTE] {len(bad) - len(actionable)} chapter(s) chỉ có warnings — bỏ qua khi "
                  f"retranslate/repolish (dùng --issue-filter để chọn warnings cụ thể).")
        bad = actionable if (args.retranslate_bad or args.repolish_bad) else bad
        if not bad and (args.retranslate_bad or args.repolish_bad):
            print("[OK] Không có chapter nào với blocking issue.")
            return

    if args.fix_pronouns:
        n = fix_pronouns_in_db(bad, dry_run=args.dry_run)
        action = "Would fix" if args.dry_run else "Fixed"
        print(f"\n[FIX-PRONOUNS] {action} pronouns in {n} chapters.")
    elif args.retranslate_bad:
        n = retranslate_bad_chapters(bad, dry_run=args.dry_run, force_running=args.force_running)
        if not args.dry_run:
            print(f"\n[DONE] {n} chapters queued for re-translation via job queue.")
    elif args.repolish_bad:
        ids = [r["chapter_id"] for r in bad if r.get("chapter_id")]
        n = reset_polished_for_repolish(ids, dry_run=args.dry_run, force_running=args.force_running)
        action = "Would reset" if args.dry_run else "Reset"
        print(f"\n[REPOLISH] {action} is_polished=False + re-queued jobs cho {n} chapters → workers sẽ repolish")


if __name__ == "__main__":
    main()
