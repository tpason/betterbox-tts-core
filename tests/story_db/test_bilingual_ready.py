from story_db.story_pipeline_db.repository import (
    ENGLISH_LEARNING_SOURCE_CODES,
    list_bilingual_ready_stories,
)


def test_english_learning_source_codes_include_reader_sources():
    for code in (
        "royalroad",
        "skydemonorder",
        "wetriedtls",
        "fanmtl",
        "lightnovelpub",
    ):
        assert code in ENGLISH_LEARNING_SOURCE_CODES


def test_list_bilingual_ready_stories_returns_shape():
    rows = list_bilingual_ready_stories(min_polished=1, min_bilingual_chapters=1, limit=5)
    assert isinstance(rows, list)
    if not rows:
        return
    row = rows[0]
    assert "id" in row
    assert "title" in row
    assert "source_code" in row
    assert int(row["bilingual_ready_count"]) >= 1
    assert int(row["polished_count"]) >= 1


def test_list_bilingual_ready_stories_filters_source():
    rows = list_bilingual_ready_stories(
        source_codes=["skydemonorder"],
        min_polished=10,
        min_bilingual_chapters=10,
        limit=3,
    )
    for row in rows:
        assert row["source_code"] == "skydemonorder"
        assert int(row["bilingual_ready_count"]) >= 10
