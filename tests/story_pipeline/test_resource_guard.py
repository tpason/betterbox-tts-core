from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = ROOT / "scripts" / "story_pipeline"
for path in (ROOT, SCRIPT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from resource_guard import (  # noqa: E402
    ResourceSnapshot,
    ResourceThresholds,
    check_safe,
)


class ResourceGuardTest(unittest.TestCase):
    def test_safe_when_above_thresholds(self) -> None:
        snap = ResourceSnapshot(cpu_percent=40.0, ram_free_mb=8000, vram_free_mb=12000)
        ok, reasons = check_safe(snap, ResourceThresholds.polish())
        self.assertTrue(ok)
        self.assertEqual(reasons, [])

    def test_block_low_vram(self) -> None:
        snap = ResourceSnapshot(cpu_percent=40.0, ram_free_mb=8000, vram_free_mb=4000)
        ok, reasons = check_safe(snap, ResourceThresholds.polish())
        self.assertFalse(ok)
        self.assertTrue(any("VRAM" in r for r in reasons))

    def test_block_high_cpu(self) -> None:
        snap = ResourceSnapshot(cpu_percent=95.0, ram_free_mb=8000, vram_free_mb=12000)
        ok, reasons = check_safe(snap, ResourceThresholds.polish())
        self.assertFalse(ok)
        self.assertTrue(any("CPU" in r for r in reasons))

    def test_qa_deterministic_no_gpu_required(self) -> None:
        snap = ResourceSnapshot(cpu_percent=50.0, ram_free_mb=3000, vram_free_mb=-1)
        ok, _ = check_safe(snap, ResourceThresholds.qa_deterministic(), require_gpu=False)
        self.assertTrue(ok)

    @patch("resource_guard.time.sleep", return_value=None)
    @patch("resource_guard.snapshot")
    def test_wait_until_safe_returns_when_ok(self, mock_snap, _sleep) -> None:
        from resource_guard import wait_until_safe

        mock_snap.return_value = ResourceSnapshot(cpu_percent=10.0, ram_free_mb=16000, vram_free_mb=14000)
        snap = wait_until_safe(
            ResourceThresholds.qa_deterministic(),
            poll_seconds=1,
            max_wait_seconds=5,
            wait_for_workers=False,
            require_gpu=False,
            log=lambda _m: None,
        )
        self.assertEqual(snap.ram_free_mb, 16000)


if __name__ == "__main__":
    unittest.main()
