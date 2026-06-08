#!/usr/bin/env python3
"""
Translation/polish quality scanner.

Hai cách dùng:
  1. Library: check_polished_quality(text, genre, char_map_path) → list[str]
     Gọi từ polish_worker.py sau mỗi chapter để log cảnh báo.

  2. CLI: scan và optionally trigger repolish cho chapters có vấn đề.
     python check_translation_quality.py --story-id <id> [--repolish-bad]

Quality rules:
  - not_vietnamese: output không phải tiếng Việt
  - forbidden_term: term bị cấm trong char map (## !! TRÁNH:)
  - wrong_pronoun: dùng hắn/nàng/lão/y trong văn kể (western_fantasy only)
  - large_en_block: đoạn tiếng Anh > 80 chars chưa dịch
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
for p in (str(ROOT), str(SCRIPT_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from story_db.story_pipeline_db.db import connect

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


# ── Patterns ────────────────────────────────────────────────────────────────

# Hán Việt pronouns that shouldn't appear in western_fantasy/do_thi narrative
_WRONG_PRONOUN_GENRES = {"western_fantasy", "do_thi", "lang_man"}
# Match "hắn/nàng/lão/y" as standalone words in narrative (outside quoted dialogue)
_WRONG_PRONOUN_RE = re.compile(r"\b(hắn|nàng|lão|y)\b")
# Detect large untranslated English blocks (80+ non-Vietnamese chars)
_EN_BLOCK_RE = re.compile(r"[A-Za-z][A-Za-z ,\.'\-]{79,}")


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
        # Skip lines that are mostly dialogue (start with quote character)
        if stripped.startswith(('"', "'", "“", "‘", "—")):
            continue
        count += len(_WRONG_PRONOUN_RE.findall(stripped))
    return count


def check_polished_quality(
    text: str,
    genre: str = "",
    char_map_path: str = "",
) -> list[str]:
    """
    Trả về list các quality issue (empty = OK).
    Gọi sau khi polish xong, trước khi save vào DB.
    """
    issues: list[str] = []
    if not text or len(text.strip()) < 100:
        issues.append("output_too_short")
        return issues

    # Check 1: must be Vietnamese
    if not is_probably_vietnamese(text):
        issues.append("not_vietnamese")

    # Check 2: forbidden terms from char map
    if char_map_path:
        bad_terms = _extract_forbidden_terms(char_map_path)
        for term in bad_terms:
            if term in text:
                issues.append(f"forbidden_term:{term!r}")

    # Check 3: wrong pronouns for genre
    if genre in _WRONG_PRONOUN_GENRES:
        pronoun_count = _count_wrong_pronouns(text)
        if pronoun_count >= 3:
            issues.append(f"wrong_pronoun:{pronoun_count}")

    # Check 4: untranslated English blocks
    en_blocks = _EN_BLOCK_RE.findall(text)
    if en_blocks:
        issues.append(f"large_en_block:{len(en_blocks)}")

    return issues


# ── DB scan ─────────────────────────────────────────────────────────────────

def fetch_polished_chapters(story_id: str, from_ch: int, to_ch: int) -> list[dict]:
    query = """
        SELECT
            c.id AS chapter_id, c.chapter_number, c.title AS chapter_title,
            c.polished_text_content, c.polished_text_path,
            c.raw_text_path, c.translated_text_path,
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


def scan_story(
    story_id: str,
    from_ch: int = 0,
    to_ch: int = 0,
    char_map_path: str = "",
    genre: str = "",
) -> list[dict]:
    """Scan polished chapters, return list of {chapter_number, issues}."""
    rows = fetch_polished_chapters(story_id, from_ch, to_ch)
    results = []
    for row in rows:
        text = _read_polished_text(row)
        if not text:
            results.append({"chapter_number": row["chapter_number"], "issues": ["no_polished_text"]})
            continue
        issues = check_polished_quality(text, genre=genre, char_map_path=char_map_path)
        if issues:
            results.append({"chapter_number": row["chapter_number"], "chapter_id": row["chapter_id"], "issues": issues})
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


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Scan polished chapter quality")
    parser.add_argument("--story-id", required=True)
    parser.add_argument("--from-chapter", type=int, default=0)
    parser.add_argument("--to-chapter", type=int, default=0)
    parser.add_argument("--char-map", default="", help="Path to char map file")
    parser.add_argument("--genre", default="", help="Override genre for checks")
    parser.add_argument("--repolish-bad", action="store_true",
                        help="Mark chapters with issues as is_polished=False so workers reprocess")
    parser.add_argument("--retranslate-bad", action="store_true",
                        help="Full re-translate: delete polished files, reset DB flags, re-enqueue jobs")
    parser.add_argument("--dry-run", action="store_true",
                        help="With --repolish-bad/--retranslate-bad: show what would happen without changing DB")
    parser.add_argument("--force-running", action="store_true",
                        help="Also reset currently-running jobs (risk of race; use only when workers are stopped)")
    parser.add_argument("--min-issues", type=int, default=1,
                        help="Min number of issues to flag a chapter (default: 1)")
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
        from genre_prompts import infer_genre_from_char_map
        genre = infer_genre_from_char_map(char_map)
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

    bad = scan_story(
        args.story_id,
        from_ch=args.from_chapter,
        to_ch=args.to_chapter,
        char_map_path=char_map,
        genre=genre,
    )

    bad = [r for r in bad if len(r["issues"]) >= args.min_issues]

    if not bad:
        print("[OK] Không tìm thấy chapter nào có vấn đề.")
        return

    print(f"\n[ISSUES] {len(bad)} chapter(s) có vấn đề:\n")
    for r in bad:
        print(f"  ch{r['chapter_number']:04d}: {', '.join(r['issues'])}")

    if args.retranslate_bad:
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
