from pathlib import Path

from scripts.story_pipeline.discover_hot_stories import write_discovery_snapshot


def test_write_discovery_snapshot_falls_back_to_tmp(tmp_path, monkeypatch):
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o555)

    fallback_dir = tmp_path / "fallback"
    monkeypatch.setenv("DISCOVERY_OUTPUT_DIR", str(fallback_dir))

    target = blocked / "hot_stories_test.json"
    written = write_discovery_snapshot({"ok": True}, target)

    assert written is not None
    assert written.parent == fallback_dir
    assert written.read_text(encoding="utf-8").startswith("{\n")

    blocked.chmod(0o755)
