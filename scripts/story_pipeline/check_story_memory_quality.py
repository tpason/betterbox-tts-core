#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from story_memory import (  # noqa: E402
    apply_story_memory_replacements,
    find_story_memory_quality_issues,
    load_story_memory,
    story_memory_status,
)


CHAPTER_PATTERN = re.compile(r"chapter(\d+)\.txt$", re.IGNORECASE)


def chapter_number(path: Path) -> int:
    match = CHAPTER_PATTERN.match(path.name)
    return int(match.group(1)) if match else 0


def list_input_files(args: argparse.Namespace) -> list[Path]:
    if args.files:
        return [Path(value) for value in args.files]
    input_dir = Path(args.input_dir)
    if not input_dir.is_absolute():
        input_dir = ROOT / input_dir
    if not input_dir.is_dir():
        raise SystemExit(f"Không tìm thấy input-dir: {input_dir}")
    if args.chapter:
        return [input_dir / f"chapter{args.chapter:04d}.txt"]
    return sorted(
        [path for path in input_dir.glob("chapter*.txt") if CHAPTER_PATTERN.match(path.name)],
        key=chapter_number,
    )


def write_jsonl(path: str, rows: list[dict[str, Any]]) -> None:
    if not path:
        return
    output = Path(path)
    if not output.is_absolute():
        output = ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"[REPORT] wrote {output}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Quét output dịch/polish theo story memory, không gọi Ollama.",
    )
    parser.add_argument("files", nargs="*", help="File cụ thể cần quét. Nếu truyền, bỏ qua --input-dir.")
    parser.add_argument("--input-dir", default="", help="Folder chứa chapter*.txt để quét.")
    parser.add_argument("--chapter", type=int, default=0, help="Chỉ quét một chapter trong --input-dir.")
    parser.add_argument("--story-id", default="")
    parser.add_argument("--slug", default="")
    parser.add_argument("--char-map-file", default="")
    parser.add_argument("--story-memory-dir", default="")
    parser.add_argument("--genre", default="")
    parser.add_argument("--jsonl-output", default="", help="Ghi report JSONL.")
    parser.add_argument(
        "--show-normalized-preview",
        action="store_true",
        help="In preview nếu story memory sẽ normalize tên/term.",
    )
    parser.add_argument(
        "--fail-on-issues",
        action="store_true",
        help="Exit code 1 nếu có issue.",
    )
    args = parser.parse_args()

    if not args.files and not args.input_dir:
        parser.error("Cần truyền file hoặc --input-dir")

    memory = load_story_memory(
        story_memory_dir=args.story_memory_dir,
        story_id=args.story_id,
        slug=args.slug,
        char_map_file=args.char_map_file,
    )
    print(f"[MEMORY] {story_memory_status(memory)}")

    rows: list[dict[str, Any]] = []
    total_issues = 0
    for path in list_input_files(args):
        if not path.is_absolute():
            path = ROOT / path
        if not path.exists():
            rows.append({"path": path.as_posix(), "status": "missing", "issues": ["file missing"]})
            total_issues += 1
            print(f"[MISSING] {path}")
            continue
        text = path.read_text(encoding="utf-8")
        normalized = apply_story_memory_replacements(text, memory)
        issues = find_story_memory_quality_issues(normalized, memory, genre=args.genre)
        changed = normalized != text
        row = {
            "path": path.as_posix(),
            "chapter_number": chapter_number(path),
            "changed_by_normalization": changed,
            "issues": issues,
        }
        rows.append(row)
        if issues:
            total_issues += len(issues)
            print(f"[WARN] {path.name}: {len(issues)} issue(s)")
            for issue in issues[:10]:
                print(f"  - {issue}")
        else:
            print(f"[OK] {path.name}" + (" (normalization would change text)" if changed else ""))
        if changed and args.show_normalized_preview:
            print("[NORMALIZED PREVIEW]")
            print(normalized[:700].strip())

    write_jsonl(args.jsonl_output, rows)
    print(f"[DONE] files={len(rows)} total_issues={total_issues}")
    if args.fail_on_issues and total_issues:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
