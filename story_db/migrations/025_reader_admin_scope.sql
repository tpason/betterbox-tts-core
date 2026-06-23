-- Admin RBAC scopes for story_admin (reader_users.role = admin)
ALTER TABLE reader_users
ADD COLUMN IF NOT EXISTS admin_scope TEXT;

UPDATE reader_users
SET admin_scope = 'full'
WHERE role = 'admin' AND (admin_scope IS NULL OR btrim(admin_scope) = '');

ALTER TABLE reader_users
DROP CONSTRAINT IF EXISTS reader_users_admin_scope_check;

ALTER TABLE reader_users
ADD CONSTRAINT reader_users_admin_scope_check CHECK (
  admin_scope IS NULL
  OR admin_scope IN ('full', 'ops', 'moderator')
);

CREATE INDEX IF NOT EXISTS idx_reader_users_admin_scope ON reader_users(admin_scope)
WHERE role = 'admin';
