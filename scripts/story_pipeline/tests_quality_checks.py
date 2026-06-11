#!/usr/bin/env python3
"""Regression tests cho deterministic quality checks (chạy không cần pytest).

    viterbox/venv/bin/python scripts/story_pipeline/tests_quality_checks.py
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from check_translation_quality import (  # noqa: E402
    _has_repeated_content,
    check_completeness,
    issue_to_repair_hint,
    split_blocking_warnings,
)

PASS = 0
FAIL = 0


def check(name: str, condition: bool) -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok: {name}")
    else:
        FAIL += 1
        print(f"FAIL: {name}")


VI_PARA = (
    "Anh ta bước đi trên con đường dài dẫn về phía ngôi làng nhỏ nằm ở chân núi, "
    "trong lòng vẫn còn nghĩ về trận chiến vừa rồi và những người đồng đội đã ngã xuống."
)
VI_PARA_NEAR = (
    "Anh ta bước đi trên con đường dài dẫn về phía ngôi làng nhỏ nằm ở chân núi, "
    "trong lòng vẫn còn nghĩ về trận chiến vừa qua và những người đồng đội đã ngã xuống!"
)
VI_OTHER = (
    "Buổi sáng hôm sau, cả đội tập trung ở quảng trường trung tâm để nghe chỉ huy "
    "phổ biến nhiệm vụ mới trước khi lên đường hành quân về phía biên giới phía bắc."
)


def test_repeated_content() -> None:
    print("\n[_has_repeated_content]")
    check("no repeat → False", not _has_repeated_content(f"{VI_PARA}\n\n{VI_OTHER}"))
    check("exact repeat → True", _has_repeated_content(f"{VI_PARA}\n\n{VI_OTHER}\n\n{VI_PARA}"))
    check(
        "exact repeat khác hoa/thường + dấu câu → True",
        _has_repeated_content(f"{VI_PARA}\n\n{VI_OTHER}\n\n{VI_PARA.upper()}"),
    )
    check(
        "near-duplicate (vài từ khác) → True",
        _has_repeated_content(f"{VI_PARA}\n\n{VI_PARA_NEAR}"),
    )
    check(
        "đoạn ngắn (< min_block) không tính",
        not _has_repeated_content("Ngắn.\n\nNgắn."),
    )
    check(
        "hai đoạn khác hẳn nhau → False",
        not _has_repeated_content(f"{VI_PARA}\n\n{VI_OTHER}\n\n{VI_PARA[:80]}{VI_OTHER[80:]}"),
    )


def test_completeness() -> None:
    print("\n[check_completeness]")
    src_en = " ".join(["The knight walked down the long road toward the village."] * 40)
    out_full = " ".join(["Hiệp sĩ bước đi trên con đường dài dẫn về ngôi làng."] * 40)
    out_short = " ".join(["Hiệp sĩ bước đi trên con đường dài dẫn về ngôi làng."] * 10)

    check("đủ độ dài → []", check_completeness(out_full, src_en, "en") == [])
    issues = check_completeness(out_short, src_en, "en")
    check("output ~25% source EN → length_ratio_low", any(i.startswith("length_ratio_low") for i in issues))
    check("source < 200 chars → skip", check_completeness("x", "ngắn", "en") == [])

    # Structure drift: 12 đoạn source → 3 đoạn output, total length giữ nguyên
    src_paras = "\n\n".join([f"Paragraph {i}: " + "word " * 30 for i in range(12)])
    out_3paras = "\n\n".join(["Đoạn dịch: " + "chữ " * 120 for _ in range(3)])
    issues = check_completeness(out_3paras, src_paras, "en")
    check(
        "12 đoạn → 3 đoạn → structure_drift:paragraphs",
        any(i.startswith("structure_drift:paragraphs") for i in issues),
    )

    # Dialogue drift: 12 dòng thoại source → 2 output
    src_dlg = "\n".join(['"Line of dialogue here," he said loudly.'] * 12) + "\n" + "word " * 100
    out_dlg = "\n".join(['"Câu thoại còn lại," anh ta nói.'] * 2) + "\n" + "chữ " * 100
    issues = check_completeness(out_dlg, src_dlg, "en")
    check(
        "12 dòng thoại → 2 → structure_drift:dialogue_lines",
        any(i.startswith("structure_drift:dialogue_lines") for i in issues),
    )


def test_blocking_split_and_hints() -> None:
    print("\n[split_blocking_warnings + repair hints]")
    blocking, warnings = split_blocking_warnings(
        ["not_vietnamese", "length_ratio_low:0.45<0.75", "structure_drift:paragraphs:3/12", "wrong_pronoun:5"]
    )
    check("not_vietnamese + wrong_pronoun blocking", blocking == ["not_vietnamese", "wrong_pronoun:5"])
    check(
        "length_ratio_low + structure_drift là warnings",
        warnings == ["length_ratio_low:0.45<0.75", "structure_drift:paragraphs:3/12"],
    )
    check(
        "hint length_ratio_low nhắc dịch đầy đủ",
        "không tóm tắt" in issue_to_repair_hint("length_ratio_low:0.45<0.75"),
    )
    check(
        "hint structure_drift nhắc giữ cấu trúc",
        "cấu trúc" in issue_to_repair_hint("structure_drift:paragraphs:3/12"),
    )


def main() -> int:
    test_repeated_content()
    test_completeness()
    test_blocking_split_and_hints()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
