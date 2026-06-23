"""Smoke tests for Docker pipeline DB paths and writable runtime files."""

from pathlib import Path

import pytest

from scripts.story_pipeline.discover_hot_stories import UrlSkipCache, write_discovery_snapshot
from story_db.story_pipeline_db import repository as repo


WORKER = "pytest-docker-runtime"


@pytest.mark.parametrize(
    "claim_order",
    ["fifo", "newest-story", "non-vi-first", "non-vi-rank-tier"],
)
def test_claim_story_jobs_all_orders(claim_order: str):
    rows = repo.claim_story_jobs(
        "polish_chapter",
        worker_id=f"{WORKER}-jobs-{claim_order}",
        limit=1,
        claim_order=claim_order,
    )
    assert isinstance(rows, list)
    for row in rows:
        assert "{" not in str(row.get("id", ""))


def test_claim_active_stories_sql():
    rows = repo.claim_active_stories(
        worker_id=f"{WORKER}-crawl",
        limit=1,
        claim_ttl_minutes=1,
        finished_cooldown_minutes=0,
    )
    assert isinstance(rows, list)
    for row in rows:
        repo.release_story_claim(row["id"], worker_id=f"{WORKER}-crawl", status="test")


def test_list_stories_sql_helpers():
    repo.list_active_stories(limit=1)
    repo.list_stories_needing_alternate_source(limit=1)
    repo.list_bilingual_ready_stories(limit=1)
    repo.find_stories(limit=1)


def test_story_priority_sql_has_no_literal_braces():
    assert "{" not in repo.STORY_PRIORITY_ORDER_SQL
    for sql in repo._CLAIM_ORDER_SQL.values():
        assert "{" not in sql


def test_url_skip_cache_save_fallback(tmp_path, monkeypatch):
    blocked = tmp_path / "blocked" / "url_skip_state.json"
    blocked.parent.mkdir()
    blocked.parent.chmod(0o555)
    monkeypatch.setenv("DISCOVERY_URL_SKIP_STATE", str(blocked))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmpdir"))

    cache = UrlSkipCache(blocked)
    cache.record_result("https://example.com/list", 0)

    fallback = tmp_path / "tmpdir" / "betterbox-discovery" / "url_skip_state.json"
    assert fallback.exists()
    blocked.parent.chmod(0o755)


def test_write_discovery_snapshot_blocked_dir(tmp_path, monkeypatch):
    blocked = tmp_path / "blocked" / "out.json"
    blocked.parent.mkdir()
    blocked.parent.chmod(0o555)
    monkeypatch.setenv("DISCOVERY_OUTPUT_DIR", str(tmp_path / "writable"))

    written = write_discovery_snapshot({"ok": True}, blocked)
    assert written is not None
    assert written.parent.name == "writable"
    blocked.parent.chmod(0o755)
