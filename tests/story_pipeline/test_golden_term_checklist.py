from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = ROOT / "scripts" / "story_pipeline"
for path in (ROOT, SCRIPT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from golden_term_checklist import (  # noqa: E402
    gate_passed,
    run_golden_checklist,
    summarize_golden_findings,
)


class GoldenTermChecklistTest(unittest.TestCase):
    def test_forbidden_hoi_phuc(self) -> None:
        chapters = {"chapter0001": "Anh ta hồi phục về quá khứ sau khi chết."}
        findings = run_golden_checklist(chapters, check_encouraged=False)
        self.assertFalse(gate_passed(findings))
        self.assertTrue(any("hồi phục" in f.matched for f in findings))

    def test_ok_hoi_quy(self) -> None:
        chapters = {
            "chapter0001": (
                "Người Hồi Quy mở mắt. Linh lực trong kinh mạch vẫn còn yếu ở cảnh giới Luyện Khí. "
                "Tu sĩ xung quanh không hay biết bí mật của hắn."
            )
        }
        findings = run_golden_checklist(chapters)
        self.assertTrue(gate_passed(findings))
        self.assertEqual(summarize_golden_findings(findings)["blocking"], 0)

    def test_forbidden_en_regressor(self) -> None:
        chapters = {"chapter0001": "The Regressor opened his eyes in the past."}
        findings = run_golden_checklist(chapters, check_encouraged=False)
        self.assertFalse(gate_passed(findings))
        self.assertTrue(any("Regressor" in f.matched for f in findings))

    def test_forbidden_trong_sinh_regressor(self) -> None:
        chapters = {"chapter0001": "Bây giờ khi tôi đã Trọng Sinh, tôi phải sống khác."}
        findings = run_golden_checklist(chapters, check_encouraged=False)
        self.assertFalse(gate_passed(findings))
        self.assertTrue(any("Trọng Sinh" in f.matched for f in findings))

    def test_missing_encouraged_is_warning_only(self) -> None:
        chapters = {"chapter0001": "Anh ta thức dậy trong một căn phòng lạ."}
        findings = run_golden_checklist(chapters)
        self.assertTrue(gate_passed(findings))
        self.assertTrue(any(f.kind == "missing_encouraged" for f in findings))

    def test_western_fantasy_forbidden_han(self) -> None:
        chapters = {"chapter0001": "Hắn nhìn ra cửa sổ và suy nghĩ về chiến trường."}
        findings = run_golden_checklist(chapters, profile="western_fantasy", check_encouraged=False)
        self.assertFalse(gate_passed(findings))
        self.assertTrue(any("hắn" in f.matched.lower() for f in findings))

    def test_western_fantasy_forbidden_encrid(self) -> None:
        chapters = {"chapter0001": "Encrid rút kiếm ra khỏi vỏ."}
        findings = run_golden_checklist(chapters, profile="western_fantasy", check_encouraged=False)
        self.assertFalse(gate_passed(findings))

    def test_western_fantasy_ok_enkrid(self) -> None:
        chapters = {"chapter0001": "Enkrid rút kiếm ra. Anh ta nhìn Shinar — cô ta gật đầu."}
        findings = run_golden_checklist(chapters, profile="western_fantasy", check_encouraged=False)
        self.assertTrue(gate_passed(findings))


if __name__ == "__main__":
    unittest.main()
