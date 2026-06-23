"""Migration ordering — base schema must run before additive alters."""
from __future__ import annotations

from pathlib import Path

from story_db.apply_migrations import collect_migration_files


def test_schema_before_quality_audit():
    files = collect_migration_files()
    names = [p.name for p in files]
    assert "001_schema.sql" in names
    assert "024_chapter_quality_audit.sql" in names
    assert names.index("001_schema.sql") < names.index("024_chapter_quality_audit.sql")
