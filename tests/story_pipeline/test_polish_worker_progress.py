import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = ROOT / "scripts" / "story_pipeline"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from polish_worker import job_progress_label  # noqa: E402


def test_job_progress_label_uses_story_title_and_chapter():
    job = {
        "story_id": "abc",
        "source_code": "royalroad",
        "payload": {
            "chapter_number": 12,
            "source_story_title": "Pokemon: CommonBorn(SI)",
            "story_slug": "pokemon-commonborn",
            "raw_language": "en",
        },
    }
    assert job_progress_label(job) == "Pokemon: CommonBorn(SI) | ch0012 | royalroad"


def test_char_map_lookahead_window_caps_backfill(monkeypatch):
    """covered=0 at ch3418 must not request ch1..ch3428."""
    import polish_worker as pw

    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["from"] = int(cmd[cmd.index("--from-chapter") + 1])
        captured["to"] = int(cmd[cmd.index("--to-chapter") + 1])
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(pw.subprocess, "run", fake_run)
    monkeypatch.setattr(pw.repo, "get_story_by_id", lambda _id: {"metadata": {}})
    monkeypatch.setattr(pw.repo, "update_story_metadata", lambda *a, **k: None)

    job = {
        "story_id": "story-1",
        "source_code": "wattpad_vn",
        "payload": {"chapter_number": 3418, "source_story_title": "Test Story"},
    }
    args = type("A", (), {"no_auto_char_map": False, "no_incremental_char_map": False, "char_map_lookahead": 10, "char_map_model": "", "vi_model": "qwen3:14b", "char_map_timeout": 90, "ollama_url": "http://127.0.0.1:11434"})()

    pw._run_lookahead_char_map(
        job,
        args,
        current_chapter=3418,
        char_map_path="/tmp/map.txt",
        slug="test",
        genre="",
        text_source="raw",
    )

    assert captured["from"] == 3419
    assert captured["to"] == 3428
