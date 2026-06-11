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


def test_llm_judge_offline() -> None:
    print("\n[llm_quality_judge — offline]")
    from llm_quality_judge import (
        JudgeResult,
        _parse_judge_json,
        _rotating_offsets,
        build_judge_prompt,
    )

    # JSON parsing robustness
    plain = '{"verdicts": [{"issue": "word_for_word", "severity": "major", "evidence": "x", "window": 1}]}'
    check("plain JSON parse", _parse_judge_json(plain)[0]["issue"] == "word_for_word")
    fenced = f"```json\n{plain}\n```"
    check("markdown fence parse", _parse_judge_json(fenced)[0]["issue"] == "word_for_word")
    noisy = f"Đây là kết quả:\n{plain}\nHết."
    check("text thừa quanh JSON parse", _parse_judge_json(noisy)[0]["issue"] == "word_for_word")
    try:
        _parse_judge_json("không có json")
        check("no JSON → raise", False)
    except ValueError:
        check("no JSON → raise", True)

    # JudgeResult mapping: chỉ major thành issues; minor + error thành warnings
    r = JudgeResult(verdicts=[
        {"issue": "word_for_word", "severity": "major"},
        {"issue": "unnatural", "severity": "minor"},
        {"issue": "không_hợp_lệ", "severity": "major"},
    ])
    check("major → judge:word_for_word", r.issues == ["judge:word_for_word"])
    check("minor → judge_minor:unnatural", r.warnings == ["judge_minor:unnatural"])
    check("issue lạ bị bỏ qua", all("không_hợp_lệ" not in i for i in r.issues + r.warnings))
    check("error → judge_error warning", "judge_error:Timeout" in JudgeResult(error="Timeout").warnings)

    # Rotating windows: deterministic theo seed+attempt, đổi khi attempt đổi
    o1 = _rotating_offsets(3, "ch1", 0)
    o2 = _rotating_offsets(3, "ch1", 0)
    o3 = _rotating_offsets(3, "ch1", 1)
    check("offsets deterministic", o1 == o2)
    check("offsets rotate theo attempt", o1 != o3)
    check("offsets trong [0,1)", all(0 <= o < 1 for o in o1))

    src = "The knight walked. " * 200
    out = "Hiệp sĩ bước đi. " * 200
    prompt = build_judge_prompt(src, out, seed="ch1", n_windows=3, window_chars=300)
    check("prompt có 3 cặp window", prompt.count("=== CẶP") == 3)
    check("prompt có /no_think", prompt.startswith("/no_think"))
    check("prompt yêu cầu JSON verdicts", '"verdicts"' in prompt)


def test_recaps() -> None:
    print("\n[story_memory recaps]")
    import json
    import tempfile
    from concurrent.futures import ThreadPoolExecutor

    from story_memory import (
        StoryMemory,
        build_recap_context,
        build_story_memory_prompt,
        save_chapter_recap,
    )

    with tempfile.TemporaryDirectory() as tmp:
        check("save recap ch1", save_chapter_recap(tmp, 1, "Enkrid gặp Shinar, xưng hô anh/cô."))
        check("save recap ch2", save_chapter_recap(tmp, 2, "Cả đội hành quân về biên giới."))
        check("recap rỗng → False", not save_chapter_recap(tmp, 3, "   "))
        check("chapter <= 0 → False", not save_chapter_recap(tmp, 0, "x"))

        data = json.loads((__import__("pathlib").Path(tmp) / "recaps.json").read_text())
        check("recaps.json có 2 entries", set(data.keys()) == {"1", "2"})
        check("entry có updated_at", bool(data["1"].get("updated_at")))

        # Concurrent writes không mất entry (per-story lock + atomic replace)
        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(lambda n: save_chapter_recap(tmp, n, f"Recap chương {n}."), range(3, 23)))
        data = json.loads((__import__("pathlib").Path(tmp) / "recaps.json").read_text())
        check("concurrent: đủ 22 entries", len(data) == 22)

        # Prune: max_entries giữ chương mới nhất
        save_chapter_recap(tmp, 99, "Chương cuối.", max_entries=5)
        data = json.loads((__import__("pathlib").Path(tmp) / "recaps.json").read_text())
        check("prune giữ 5 chương mới nhất", set(data.keys()) == {"99", "22", "21", "20", "19"})

        # build_recap_context: chỉ chương < current, theo thứ tự, giới hạn 3
        mem = StoryMemory(recaps={
            "5": {"recap": "Sự kiện năm."}, "6": {"recap": "Sự kiện sáu."},
            "7": {"recap": "Sự kiện bảy."}, "9": {"recap": "Sự kiện chín (tương lai)."},
        })
        ctx = build_recap_context(mem, 8)
        check("recap context có ch5-7", all(f"Chương {n}" in ctx for n in (5, 6, 7)))
        check("không có chương >= current", "chín" not in ctx)
        check("thứ tự thời gian (5 trước 7)", ctx.index("Chương 5") < ctx.index("Chương 7"))
        check("current_chapter=0 → empty", build_recap_context(mem, 0) == "")

        prompt = build_story_memory_prompt(mem, "văn bản", current_chapter=8)
        check("prompt có block TÓM TẮT", "TÓM TẮT CÁC CHƯƠNG TRƯỚC" in prompt)
        prompt0 = build_story_memory_prompt(mem, "văn bản")
        check("không truyền current_chapter → không có recap", "TÓM TẮT" not in prompt0)


def test_validate_char_map() -> None:
    print("\n[validate_char_map]")
    from genre_prompts import validate_char_map

    good_map = """## Thể loại: Tu tiên Hàn Quốc (korean cultivation)

[ALIASES]
Eun-hyun = Seo Eun-Hyun

### Seo Eun-Hyun
- Tên khác: Deputy Manager Seo
- Ngôi thứ ba: anh ta
- Tự xưng: tôi
"""
    check("map sạch → []", validate_char_map(good_map) == [])
    check("empty map → []", validate_char_map("") == [])

    bad_map = """## Thể loại: Tu tiên Hàn Quốc (korean cultivation)

[ALIASES]
Eun-hyun = Seo Eun-Hyun
Ai Đó = Người Không Tồn Tại
Kim Yeon = Kim Young-hoon

### Seo Eun-Hyun
- Ngôi thứ ba: hắn
- Tự xưng: ta

### Kim Yeon
- Ngôi thứ ba: cô ta

### Nhân Vật Thiếu Pronoun
- Tự xưng: tôi

### Kim Young-hoon
- Ngôi thứ ba: anh ta
"""
    issues = validate_char_map(bad_map)
    check("alias target lạ → alias_target_unknown",
          any(i.startswith("alias_target_unknown:ai đó") for i in issues))
    check("alias LHS trùng entry → alias_shadows_entry",
          any(i.startswith("alias_shadows_entry:kim yeon") for i in issues))
    check("entry thiếu pronoun → entry_missing_pronoun",
          any(i == "entry_missing_pronoun:Nhân Vật Thiếu Pronoun" for i in issues))
    check("genre korean_cultivation + 'hắn' → conflict",
          any(i.startswith("entry_pronoun_genre_conflict:Seo Eun-Hyun:hắn") for i in issues))
    check("'cô ta' không bị flag", all("Kim Yeon:" not in i for i in issues if "conflict" in i))

    # Genre tiên hiệp: hắn hợp lệ
    tienhiep_map = """## Thể loại: tiên hiệp

### Lý Mộ Trần
- Ngôi thứ ba: hắn
"""
    check("tiên hiệp + 'hắn' → OK", validate_char_map(tienhiep_map) == [])


def main() -> int:
    test_repeated_content()
    test_completeness()
    test_blocking_split_and_hints()
    test_llm_judge_offline()
    test_recaps()
    test_validate_char_map()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
