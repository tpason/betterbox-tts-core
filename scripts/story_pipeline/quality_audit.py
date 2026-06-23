#!/usr/bin/env python3
"""Unified translate/polish quality audit — Tier 0/1/2 compose + DB persistence."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
for p in (str(ROOT), str(SCRIPT_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from story_db.story_pipeline_db import repository as repo
from story_db.story_pipeline_db.db import connect

from check_translation_quality import (
    _read_polished_text,
    _read_source_text,
    run_full_quality_check,
    split_blocking_warnings,
)
from genre_prompts import find_char_map_file, resolve_genre_from_context
from golden_term_checklist import gate_passed, run_golden_checklist
from llm_quality_judge import judge_chapter_quality
from quality_remediation import request_chapter_repair
from story_quality_common import resolve_golden_profile
from term_alignment_check import check_term_alignment, term_alignment_to_issues_dict

AUDIT_VERSION = int(os.environ.get("QUALITY_AUDIT_VERSION", "1"))

# Priority stories for backfill (EN translate/polish active).
DEFAULT_PRIORITY_STORY_IDS = [
    "1a1af87a-e85e-476f-87b7-1aeac2dadb1d",  # A Regressor's Tale
    "7a857982-7270-4183-9a1b-a19fa946b836",  # Pokemon: CommonBorn
    "28a91d4c-af97-40b5-b453-ced0dbfdb449",  # Black Knight
    "55a98abd-8584-474b-8385-53e2823f9539",  # Heavenly Demon
    "13cc6d36-4fe7-4dc1-980f-c001ecd9e535",  # Vĩnh Thoái Hiệp Sĩ
]


@dataclass
class ChapterAuditResult:
    chapter_id: str
    chapter_number: int
    passed: bool
    blocking: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    issues_detail: list[dict[str, Any]] = field(default_factory=list)
    tiers_run: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chapter_id": self.chapter_id,
            "chapter_number": self.chapter_number,
            "passed": self.passed,
            "blocking": self.blocking,
            "warnings": self.warnings,
            "issues_detail": self.issues_detail,
            "tiers_run": self.tiers_run,
        }


def _should_run_judge(chapter_number: int, judge_sample: int, seed: str = "") -> bool:
    if judge_sample <= 0:
        return False
    if judge_sample == 1:
        return True
    h = int(hashlib.sha256(f"{seed}:{chapter_number}".encode()).hexdigest()[:8], 16)
    return (h % judge_sample) == 0


def audit_chapter_row(
    row: dict[str, Any],
    *,
    genre: str = "",
    char_map_path: str = "",
    golden_profile: str = "",
    tiers: tuple[int, ...] = (0, 1, 2),
    judge_sample: int = 0,
    ollama_url: str = "http://127.0.0.1:11434",
    judge_model: str = "qwen3:14b",
    log: Callable[[str], None] | None = None,
) -> ChapterAuditResult:
    chapter_id = str(row["chapter_id"])
    chapter_number = int(row["chapter_number"])
    polished = _read_polished_text(row)
    source = _read_source_text(row)
    source_language = (row.get("raw_language") or "").strip().lower()
    slug = ""
    if row.get("raw_text_path"):
        slug = Path(str(row["raw_text_path"])).parent.name

    blocking: list[str] = []
    warnings: list[str] = []
    detail: list[dict[str, Any]] = []
    tiers_run: list[int] = []

    if not polished or len(polished.strip()) < 100:
        return ChapterAuditResult(
            chapter_id=chapter_id,
            chapter_number=chapter_number,
            passed=False,
            blocking=["no_polished_text"],
            issues_detail=[{"code": "no_polished_text", "severity": "blocking", "tier": 0}],
            tiers_run=[0],
        )

    if 0 in tiers:
        tiers_run.append(0)
        b0, w0 = run_full_quality_check(
            polished,
            genre=genre,
            char_map=char_map_path,
            story_id=str(row.get("story_id") or ""),
            slug=slug,
            source_text=source,
            source_language=source_language,
            log=log,
        )
        blocking.extend(b0)
        warnings.extend(w0)
        for code in b0:
            detail.append({"code": code, "severity": "blocking", "tier": 0})
        for code in w0:
            detail.append({"code": code, "severity": "warning", "tier": 0})

        profile = golden_profile or resolve_golden_profile(
            {"id": row.get("story_id"), "language": source_language, "source_code": row.get("source_code")},
            genre=genre,
        )
        golden = run_golden_checklist(
            {f"chapter{chapter_number:04d}": polished},
            profile=profile,
            check_encouraged=False,
        )
        for f in golden:
            code = f"golden:{f.kind}:{f.matched!r}"
            if f.severity == "blocking":
                blocking.append(code)
                detail.append({"code": code, "severity": "blocking", "tier": 0, "evidence": f.matched})
            else:
                warnings.append(code)
                detail.append({"code": code, "severity": "warning", "tier": 0, "evidence": f.matched})

    if 1 in tiers and source:
        tiers_run.append(1)
        t1 = check_term_alignment(source, polished, genre=genre)
        for issue in t1:
            if issue not in blocking:
                blocking.append(issue)
                detail.extend(term_alignment_to_issues_dict([issue]))

    judge_blocking: list[str] = []
    if 2 in tiers and source and not blocking:
        if _should_run_judge(chapter_number, judge_sample, seed=str(row.get("story_id") or "")):
            tiers_run.append(2)
            judge = judge_chapter_quality(
                source,
                polished,
                genre=genre,
                ollama_url=ollama_url,
                model=judge_model,
                seed=chapter_id,
            )
            judge_blocking = judge.issues
            for code in judge_blocking:
                blocking.append(code)
                detail.append({"code": code, "severity": "blocking", "tier": 2})
            for code in judge.warnings:
                warnings.append(code)
                detail.append({"code": code, "severity": "warning", "tier": 2})
            if judge.error and log:
                log(f"[AUDIT] judge error ch{chapter_number}: {judge.error}")

    passed = not blocking
    return ChapterAuditResult(
        chapter_id=chapter_id,
        chapter_number=chapter_number,
        passed=passed,
        blocking=blocking,
        warnings=warnings,
        issues_detail=detail,
        tiers_run=tiers_run,
    )


def persist_audit_result(result: ChapterAuditResult) -> None:
    status = "passed" if result.passed else "failed"
    repo.update_chapter_quality_audit(
        result.chapter_id,
        status=status,
        audit_version=AUDIT_VERSION,
        issues=result.issues_detail,
        blocking_count=len(result.blocking),
    )


def fetch_audit_chapter_rows(
    story_id: str,
    *,
    from_chapter: int = 0,
    to_chapter: int = 0,
    chapter_numbers: list[int] | None = None,
    only_needing_audit: bool = False,
    limit: int = 0,
) -> list[dict[str, Any]]:
    query = """
        SELECT
            c.id AS chapter_id, c.chapter_number, c.title AS chapter_title,
            c.polished_text_content, c.polished_text_path,
            c.raw_text_content, c.raw_text_path, c.translated_text_path,
            c.is_polished, c.is_translated,
            c.quality_status, c.quality_audit_version, c.quality_repair_attempts,
            s.id AS story_id, s.title AS story_title, s.metadata AS story_metadata,
            src.code AS source_code,
            COALESCE(NULLIF(c.raw_language, ''), s.language, '') AS raw_language
        FROM chapters c
        JOIN stories s ON s.id = c.story_id
        JOIN sources src ON src.id = s.source_id
        WHERE c.story_id = %(story_id)s
          AND c.is_polished = TRUE
          AND c.polished_text_content IS NOT NULL
          AND length(trim(c.polished_text_content)) > 100
    """
    params: dict[str, Any] = {"story_id": story_id, "audit_version": AUDIT_VERSION}
    if chapter_numbers:
        query += " AND c.chapter_number = ANY(%(chapter_numbers)s::int[])"
        params["chapter_numbers"] = chapter_numbers
    else:
        if from_chapter:
            query += " AND c.chapter_number >= %(from_ch)s"
            params["from_ch"] = from_chapter
        if to_chapter:
            query += " AND c.chapter_number <= %(to_ch)s"
            params["to_ch"] = to_chapter
    if only_needing_audit:
        query += """
          AND (
            c.quality_status IS NULL
            OR c.quality_status IN ('pending_audit', 'failed')
            OR COALESCE(c.quality_audit_version, 0) < %(audit_version)s
          )
          AND COALESCE(c.quality_repair_attempts, 0) < %(max_attempts)s
        """
        params["max_attempts"] = int(os.environ.get("QUALITY_MAX_REPAIR_ATTEMPTS", "3"))
    query += " ORDER BY c.chapter_number"
    if limit > 0:
        query += " LIMIT %(limit)s"
        params["limit"] = limit
    with connect() as conn:
        return [dict(r) for r in conn.execute(query, params).fetchall()]


def audit_story_range(
    story_id: str,
    *,
    from_chapter: int = 0,
    to_chapter: int = 0,
    only_needing_audit: bool = False,
    limit: int = 0,
    tiers: tuple[int, ...] = (0, 1, 2),
    judge_sample: int = 5,
    repair: bool = False,
    dry_run: bool = False,
    ollama_url: str = "http://127.0.0.1:11434",
    judge_model: str = "qwen3:14b",
) -> dict[str, Any]:
    story = repo.get_story_by_id(story_id)
    slug = str((story.get("metadata") or {}).get("slug") or story.get("source_story_id") or "")
    char_map = find_char_map_file(story_id=story_id, slug=slug) or ""
    genre = resolve_genre_from_context(
        str(story.get("category") or ""),
        raw_language=str(story.get("language") or ""),
        source_code=str(story.get("source_code") or ""),
        char_map_file=char_map,
        title=str(story.get("original_title") or story.get("title") or ""),
    )
    profile = resolve_golden_profile(story, genre=genre)

    rows = fetch_audit_chapter_rows(
        story_id,
        from_chapter=from_chapter,
        to_chapter=to_chapter,
        only_needing_audit=only_needing_audit,
        limit=limit,
    )

    results: list[ChapterAuditResult] = []
    repaired: list[dict[str, Any]] = []

    for row in rows:
        result = audit_chapter_row(
            row,
            genre=genre,
            char_map_path=char_map,
            golden_profile=profile,
            tiers=tiers,
            judge_sample=judge_sample,
            ollama_url=ollama_url,
            judge_model=judge_model,
            log=print,
        )
        persist_audit_result(result)
        results.append(result)

        if not result.passed and repair:
            rep = request_chapter_repair(
                result.chapter_id,
                result.blocking,
                dry_run=dry_run,
            )
            repaired.append({"chapter_number": result.chapter_number, **rep})

    failed = [r for r in results if not r.passed]
    summary = {
        "story_id": story_id,
        "story_title": story.get("title"),
        "audited": len(results),
        "passed": sum(1 for r in results if r.passed),
        "failed": len(failed),
        "repaired": len(repaired),
        "audit_version": AUDIT_VERSION,
        "failed_chapters": [r.to_dict() for r in failed[:50]],
        "repair_actions": repaired,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    return summary


def backfill_priority_stories(
    *,
    judge_sample: int = 5,
    repair: bool = True,
    dry_run: bool = False,
    limit_per_story: int = 0,
    story_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    ids = story_ids or DEFAULT_PRIORITY_STORY_IDS
    out: list[dict[str, Any]] = []
    for sid in ids:
        print(f"\n[BACKFILL] story={sid}")
        summary = audit_story_range(
            sid,
            only_needing_audit=True,
            limit=limit_per_story,
            tiers=(0, 1, 2),
            judge_sample=judge_sample,
            repair=repair,
            dry_run=dry_run,
        )
        out.append(summary)
        print(
            f"  audited={summary['audited']} passed={summary['passed']} "
            f"failed={summary['failed']} repaired={summary['repaired']}"
        )
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit translate/polish quality (Tier 0/1/2)")
    parser.add_argument("--story-id", action="append", default=[])
    parser.add_argument("--from-chapter", type=int, default=0)
    parser.add_argument("--to-chapter", type=int, default=0)
    parser.add_argument("--chapter", type=int, default=0, help="Single chapter")
    parser.add_argument("--only-needing-audit", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--tiers", default="0,1,2", help="Comma-separated tier ids")
    parser.add_argument("--judge-sample", type=int, default=5, help="Run LLM judge 1/N chapters; 0=off")
    parser.add_argument("--repair", action="store_true", help="Enqueue repolish/retranslate for failures")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--backfill-priority", action="store_true", help="Audit+repair priority EN stories")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--judge-model", default="qwen3:14b")
    args = parser.parse_args()

    tier_ids = tuple(int(x) for x in args.tiers.split(",") if x.strip().isdigit())

    if args.backfill_priority:
        summaries = backfill_priority_stories(
            judge_sample=args.judge_sample,
            repair=args.repair,
            dry_run=args.dry_run,
            limit_per_story=args.limit,
            story_ids=args.story_id or None,
        )
        print(json.dumps(summaries, ensure_ascii=False, indent=2))
        return

    story_ids = args.story_id or DEFAULT_PRIORITY_STORY_IDS[:1]
    if not story_ids:
        raise SystemExit("--story-id required")

    for sid in story_ids:
        summary = audit_story_range(
            sid,
            from_chapter=args.chapter or args.from_chapter,
            to_chapter=args.chapter or args.to_chapter,
            only_needing_audit=args.only_needing_audit,
            limit=args.limit,
            tiers=tier_ids,
            judge_sample=args.judge_sample,
            repair=args.repair,
            dry_run=args.dry_run,
            ollama_url=args.ollama_url,
            judge_model=args.judge_model,
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
