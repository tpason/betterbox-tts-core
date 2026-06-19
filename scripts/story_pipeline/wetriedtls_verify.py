#!/usr/bin/env python3
"""Phase 4 — verify polished quality + optional TTS sample before audio enqueue.

Runs:
  1. scan_translation_gaps (EN fragments + glossary wrong_translations)
  2. golden_term_checklist (Regressor→Hồi Quy, no literal EN cultivation terms)
  3. Optional VieNeu TTS sample for chapters 1–3 (--tts-sample)

Exit 0 = gate passed; exit 1 = blocking issues (use before enqueue audio).

Usage:
  viterbox/venv/bin/python scripts/story_pipeline/wetriedtls_verify.py \\
      --story-title "A Regressor's Tale of Cultivation"

  viterbox/venv/bin/python scripts/story_pipeline/wetriedtls_verify.py \\
      --story-title "A Regressor's Tale of Cultivation" \\
      --from-chapter 1 --to-chapter 3 --tts-sample --device cuda
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from story_db.story_pipeline_db import repository as repo
from genre_prompts import find_char_map_file, infer_genre_from_story_signals, resolve_genre_from_context
from golden_term_checklist import gate_passed, run_golden_checklist, summarize_golden_findings
from scan_translation_gaps import load_chapters_from_db, scan_story_gaps_from_db
from story_quality_common import resolve_golden_profile as resolve_golden_profile_common


def _log(msg: str) -> None:
    print(msg, flush=True)


def find_story(title: str, source_code: str, story_id: str = "") -> dict[str, Any]:
    if story_id:
        story = repo.get_story_by_id(story_id)
        if not story:
            raise SystemExit(f"[ERROR] story_id={story_id} not found")
        return story
    stories = repo.find_stories(title_contains=title, source_codes=[source_code])
    if not stories:
        raise SystemExit(f"[ERROR] No story matching {title!r} in source {source_code!r}")
    if len(stories) > 1:
        lines = [f"  - {s['title']} (id={s['id']})" for s in stories]
        raise SystemExit(f"[ERROR] Multiple matches:\n" + "\n".join(lines))
    return stories[0]


def story_slug(story: dict[str, Any]) -> str:
    meta = story.get("metadata") or {}
    return str(meta.get("slug") or story.get("source_story_id") or "")


def resolve_profile(story: dict[str, Any], source_code: str, profile: str) -> str:
    if profile and profile != "auto":
        return profile
    return resolve_golden_profile_common(story)


def resolve_genre(story: dict[str, Any], source_code: str) -> str:
    meta = story.get("metadata") or {}
    slug = story_slug(story)
    char_map_file = find_char_map_file(story_id=str(story["id"]), slug=slug)
    return resolve_genre_from_context(
        str(story.get("category") or meta.get("genre") or ""),
        raw_language=str(story.get("language") or "en"),
        source_code=source_code,
        char_map_file=char_map_file,
        title=str(story.get("original_title") or story.get("title") or ""),
        description=str(meta.get("source_description") or story.get("description") or ""),
    )


def verify_story(
    story: dict[str, Any],
    *,
    source_code: str,
    mode: str = "polished",
    from_chapter: int = 0,
    to_chapter: int = 0,
    profile: str = "auto",
    max_en_chapters: int = 0,
    json_out: str = "",
) -> dict[str, Any]:
    story_id = str(story["id"])
    slug = story_slug(story)
    genre = resolve_genre(story, source_code)
    effective_profile = resolve_profile(story, source_code, profile)
    char_map_file = find_char_map_file(story_id=story_id, slug=slug)

    gap_report = scan_story_gaps_from_db(
        story_id=story_id,
        slug=slug,
        mode=mode,
        from_chapter=from_chapter,
        to_chapter=to_chapter,
        char_map_file=char_map_file,
        genre=genre,
    )

    chapters = load_chapters_from_db(
        story_id,
        mode=mode,
        from_chapter=from_chapter,
        to_chapter=to_chapter,
    )
    golden_findings = run_golden_checklist(chapters, profile=effective_profile)
    golden_summary = summarize_golden_findings(golden_findings)

    en_count = gap_report["en_chapter_count"]
    if max_en_chapters and en_count > max_en_chapters:
        gap_blocking = True
    else:
        gap_blocking = bool(gap_report["en_findings"]) or gap_report["priority_term_issues"] > 0

    golden_blocking = not gate_passed(golden_findings)
    passed = not gap_blocking and not golden_blocking

    result: dict[str, Any] = {
        "story_id": story_id,
        "story_title": story.get("title"),
        "slug": slug,
        "genre": genre,
        "profile": effective_profile,
        "mode": mode,
        "from_chapter": from_chapter,
        "to_chapter": to_chapter,
        "passed": passed,
        "gap_report": gap_report,
        "golden_findings": [f.to_dict() for f in golden_findings],
        "golden_summary": golden_summary,
        "blocking": {
            "gap_scan": gap_blocking,
            "golden_checklist": golden_blocking,
        },
    }

    if json_out:
        Path(json_out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    return result


def run_qa_gate(
    story: dict[str, Any],
    *,
    source_code: str,
    from_chapter: int = 1,
    to_chapter: int = 0,
    profile: str = "auto",
    llm_judge: bool = True,
    ollama_url: str = "http://127.0.0.1:11434",
    judge_model: str = "qwen3:14b",
    story_memory_dir: str = "",
) -> dict[str, Any]:
    """Full QA gate: gap scan + golden checklist + deterministic scan + optional LLM judge."""
    from check_translation_quality import scan_story
    from llm_quality_judge import judge_chapter_quality

    story_id = str(story["id"])
    slug = story_slug(story)
    base = verify_story(
        story,
        source_code=source_code,
        from_chapter=from_chapter,
        to_chapter=to_chapter,
        profile=profile,
    )
    genre = base["genre"]
    char_map_file = find_char_map_file(story_id=story_id, slug=slug)

    judge_fn = None
    if llm_judge:
        def judge_fn(src: str, out: str, chapter_id: str):  # noqa: E301
            return judge_chapter_quality(
                src,
                out,
                genre=genre,
                ollama_url=ollama_url,
                model=judge_model,
                seed=chapter_id,
            )

    scan_bad = scan_story(
        story_id,
        from_ch=from_chapter,
        to_ch=to_chapter,
        char_map_path=char_map_file,
        genre=genre,
        story_memory_dir=story_memory_dir,
        judge_fn=judge_fn,
    )

    qa_chapters: list[dict[str, Any]] = []
    for row in scan_bad:
        judge_major = [w for w in (row.get("warnings") or []) if w.startswith("judge:")]
        blocking = list(row.get("blocking") or [])
        if blocking or judge_major:
            qa_chapters.append({
                "chapter_number": row["chapter_number"],
                "chapter_id": row.get("chapter_id"),
                "blocking": blocking,
                "judge_major": judge_major,
                "warnings": row.get("warnings") or [],
            })

    passed = base["passed"] and not qa_chapters
    return {
        **base,
        "passed": passed,
        "qa_chapters": qa_chapters,
        "llm_judge": llm_judge,
        "blocking": {
            **base["blocking"],
            "deterministic_or_judge": bool(qa_chapters),
        },
    }


def print_qa_report(result: dict[str, Any]) -> None:
    print_report(result)
    if result.get("llm_judge"):
        bad = result.get("qa_chapters") or []
        _log(f"[LLM QA] chapters_with_issues={len(bad)}")
        for row in bad[:8]:
            parts = []
            if row.get("blocking"):
                parts.append("BLOCK: " + ", ".join(row["blocking"][:3]))
            if row.get("judge_major"):
                parts.append("JUDGE: " + ", ".join(row["judge_major"][:3]))
            _log(f"  ch{row['chapter_number']:04d}: {' | '.join(parts)}")
    if result["passed"]:
        _log("[QA] ✓ FULL QA PASSED (gap + golden + deterministic + LLM judge)")
    else:
        _log("[QA] ✗ FULL QA FAILED — schedule repolish")


def print_report(result: dict[str, Any]) -> None:
    gap = result["gap_report"]
    _log(
        f"[VERIFY] story={result['story_title']} profile={result['profile']} "
        f"genre={result['genre']}"
    )
    _log(
        f"[GAP SCAN] chapters={gap['chapters_scanned']} "
        f"en_chapters={gap['en_chapter_count']} "
        f"term_issues={gap['term_issue_count']} "
        f"priority={gap['priority_term_issues']}"
    )
    for item in gap["en_findings"][:5]:
        frags = "; ".join(item["fragments"][:4])
        _log(f"  EN {item['chapter']}: {frags}")
    for item in gap["inconsistencies"][:5]:
        if item.get("priority"):
            _log(f"  TERM {item.get('chapter', '?')}: {item.get('wrong', item)}")

    gs = result["golden_summary"]
    _log(f"[GOLDEN] blocking={gs['blocking']} warnings={gs['warnings']}")
    for item in result["golden_findings"]:
        if item["severity"] == "blocking":
            _log(f"  ✗ {item['chapter']}: {item['matched']!r} — {item['detail']}")
        elif item["severity"] == "warning":
            _log(f"  ⚠ {item['chapter']}: {item['detail']}")

    if result["passed"]:
        _log("[VERIFY] ✓ GATE PASSED — safe to enqueue audio")
    else:
        _log("[VERIFY] ✗ GATE FAILED — fix polish/repolish before audio enqueue")
        if result["blocking"]["gap_scan"]:
            _log("  → gap scan: EN fragments hoặc priority glossary violations")
        if result["blocking"]["golden_checklist"]:
            _log("  → golden checklist: forbidden terms (vd. hồi phục, Regressor)")


def generate_tts_samples(
    story: dict[str, Any],
    *,
    from_chapter: int,
    to_chapter: int,
    output_dir: str,
    device: str,
    voice_profile: str,
    max_chars: int,
) -> None:
    """Write polished DB chapters to temp dir and synthesize VieNeu preview WAVs."""
    story_id = str(story["id"])
    slug = story_slug(story) or str(story_id)
    chapters = load_chapters_from_db(
        story_id,
        mode="polished",
        from_chapter=from_chapter,
        to_chapter=to_chapter,
    )
    if not chapters:
        _log("[TTS] No polished chapters in range — skip sample")
        return

    out_root = Path(output_dir) / slug
    out_root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="wetriedtls_verify_") as tmp:
        tmp_dir = Path(tmp)
        for chapter_name, text in sorted(chapters.items()):
            num = int("".join(c for c in chapter_name if c.isdigit()) or "0")
            if num <= 0:
                continue
            sample_text = (text or "").strip()
            if max_chars > 0 and len(sample_text) > max_chars:
                sample_text = sample_text[:max_chars].rsplit(" ", 1)[0] + "…"
            (tmp_dir / f"chapter{num:04d}.txt").write_text(sample_text + "\n", encoding="utf-8")

        from scripts.story_pipeline.generate_chapter_audio_vieneu import load_vieneu, synthesize_chapter
        from scripts.story_pipeline.vieneu_audiobook_stitch import DEFAULT_MAX_NEW_FRAMES

        ns = argparse.Namespace(
            mode="v3turbo",
            device=device,
            backend="auto",
            onnx_dir=None,
            hf_token=None,
            voice="",
            voice_profile=voice_profile,
            reference_audio=None,
            reference_text=None,
            emotion="natural",
            temperature=0.8,
            top_k=25,
            top_p=0.95,
            max_new_frames=DEFAULT_MAX_NEW_FRAMES,
            repetition_penalty=1.2,
            max_chars=256,
            no_watermark=False,
            max_chars_per_unit=None,
            min_chars_per_unit=None,
            sentence_pause_ms=500,
            crossfade_ms=50,
            trim_threshold=0.006,
            trim_margin_ms=80,
            edge_fade_in_ms=5,
            edge_fade_out_ms=22,
        )
        ns.voice_profile = voice_profile or None

        _log(f"[TTS] Loading VieNeu device={device} profile={voice_profile}")
        tts = load_vieneu(ns)
        try:
            for chapter_path in sorted(tmp_dir.glob("chapter*.txt")):
                out_wav = out_root / chapter_path.name.replace(".txt", ".wav")
                _log(f"[TTS] Synthesizing {chapter_path.name} → {out_wav}")
                synthesize_chapter(tts, chapter_path, out_wav, ns)
        finally:
            del tts

    _log(f"[TTS] Samples written to {out_root}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify polish quality gate before TTS/audio enqueue.")
    parser.add_argument("--story-title", default="")
    parser.add_argument("--story-id", default="")
    parser.add_argument("--source-code", default="wetriedtls")
    parser.add_argument("--from-chapter", type=int, default=1, help="Verify from chapter (default 1).")
    parser.add_argument("--to-chapter", type=int, default=3, help="Verify to chapter (default 3).")
    parser.add_argument("--mode", choices=("polished", "translated"), default="polished")
    parser.add_argument(
        "--profile",
        default="auto",
        help="Golden checklist profile (auto | korean_cultivation_regressor | korean_cultivation).",
    )
    parser.add_argument(
        "--max-en-chapters",
        type=int,
        default=0,
        help="Fail if more than N chapters have EN fragments (0 = any EN chapter fails).",
    )
    parser.add_argument("--json-out", default="", help="Write full report JSON to path.")
    parser.add_argument("--tts-sample", action="store_true", help="Generate VieNeu WAV samples for verify range.")
    parser.add_argument("--tts-output-dir", default="/tmp/wetriedtls_tts_verify")
    parser.add_argument("--tts-max-chars", type=int, default=1500, help="Max chars per TTS sample chapter.")
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--voice-profile", default="preset_binh_an")
    parser.add_argument(
        "--llm-judge",
        action="store_true",
        help="Thêm 1 lần gọi Ollama/chapter (LLM semantic QA) sau gap + golden.",
    )
    parser.add_argument("--judge-model", default="qwen3:14b")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--story-memory-dir", default="")
    parser.add_argument(
        "--warn-only",
        action="store_true",
        help="Exit 0 even when gate fails (print report only).",
    )
    args = parser.parse_args()

    if not args.story_id and not args.story_title:
        parser.error("Provide --story-id or --story-title")

    story = find_story(args.story_title, args.source_code, args.story_id)
    if args.llm_judge:
        result = run_qa_gate(
            story,
            source_code=args.source_code,
            from_chapter=args.from_chapter,
            to_chapter=args.to_chapter,
            profile=args.profile,
            llm_judge=True,
            ollama_url=args.ollama_url,
            judge_model=args.judge_model,
            story_memory_dir=args.story_memory_dir,
        )
        print_qa_report(result)
    else:
        result = verify_story(
            story,
            source_code=args.source_code,
            mode=args.mode,
            from_chapter=args.from_chapter,
            to_chapter=args.to_chapter,
            profile=args.profile,
            max_en_chapters=args.max_en_chapters,
            json_out=args.json_out,
        )
        print_report(result)

    if args.tts_sample:
        generate_tts_samples(
            story,
            from_chapter=args.from_chapter,
            to_chapter=args.to_chapter,
            output_dir=args.tts_output_dir,
            device=args.device,
            voice_profile=args.voice_profile,
            max_chars=args.tts_max_chars,
        )

    if result["passed"] or args.warn_only:
        raise SystemExit(0)
    raise SystemExit(1)


if __name__ == "__main__":
    main()
