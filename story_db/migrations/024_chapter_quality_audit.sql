-- Per-chapter translate/polish quality audit state (QA pipeline).

ALTER TABLE chapters
    ADD COLUMN IF NOT EXISTS quality_status TEXT,
    ADD COLUMN IF NOT EXISTS quality_audit_version INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS quality_checked_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS quality_issues JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS quality_repair_attempts INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS quality_last_action TEXT;

CREATE INDEX IF NOT EXISTS idx_chapters_quality_audit
    ON chapters (story_id, quality_status)
    WHERE is_polished = TRUE AND polished_text_content IS NOT NULL;
