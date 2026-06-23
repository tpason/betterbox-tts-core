from story_db.story_pipeline_db.repository import (
    RANK_TIER_SIZE,
    STORY_PRIORITY_ORDER_SQL,
    _CLAIM_ORDER_SQL,
    rank_tier,
    story_priority_sort_key,
)


def test_claim_order_sql_newest_story_prefers_recent_stories():
    sql = _CLAIM_ORDER_SQL["newest_story"]
    assert "s.created_at DESC" in sql
    assert "j.created_at ASC" in sql


def test_claim_order_sql_fifo_is_legacy_oldest_first():
    sql = _CLAIM_ORDER_SQL["fifo"]
    assert sql == "j.priority, j.created_at ASC"


def test_claim_order_sql_non_vi_first_prioritizes_foreign_sources():
    sql = _CLAIM_ORDER_SQL["non_vi_first"]
    assert "royalroad" in sql
    assert "wattpad_vn" not in sql  # VI sources defer via ELSE branch
    assert "s.created_at DESC" in sql
    assert "'en'" in sql or "'zh'" in sql


def test_claim_order_sql_non_vi_rank_tier_buckets_by_rank():
    sql = _CLAIM_ORDER_SQL["non_vi_rank_tier"]
    assert "royalroad" in sql
    assert f"/ {RANK_TIER_SIZE}" in sql
    assert "s.rank_position ASC" in sql
    assert "j.source_code ASC" in sql
    assert "chapter_number" in sql


def test_story_priority_order_sql_prefers_non_vi_and_rank_tiers():
    assert "src.code" in STORY_PRIORITY_ORDER_SQL
    assert f"/ {RANK_TIER_SIZE}" in STORY_PRIORITY_ORDER_SQL
    assert "s.rank_position ASC" in STORY_PRIORITY_ORDER_SQL


def test_rank_tier_groups_top_three():
    assert rank_tier(1) == 0
    assert rank_tier(3) == 0
    assert rank_tier(4) == 1
    assert rank_tier(6) == 1
    assert rank_tier(None) > rank_tier(100)


def test_story_priority_sort_key_orders_non_vi_before_vi_at_same_rank():
    non_vi = story_priority_sort_key(source_code="royalroad", rank_position=2)
    vi = story_priority_sort_key(source_code="truyenfull_today", rank_position=2)
    assert non_vi < vi


def test_claim_active_stories_sql_interpolates_priority_order():
    """Regression: ORDER BY must not pass literal {STORY_PRIORITY_ORDER_SQL} to Postgres."""
    from story_db.story_pipeline_db import repository as repo

    rows = repo.claim_active_stories(
        worker_id="pytest-claim-active-stories",
        limit=1,
        claim_ttl_minutes=1,
        finished_cooldown_minutes=0,
    )
    assert isinstance(rows, list)
    for row in rows:
        repo.release_story_claim(row["id"], worker_id="pytest-claim-active-stories", status="test")
