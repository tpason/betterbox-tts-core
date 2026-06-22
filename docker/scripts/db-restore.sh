#!/usr/bin/env bash
# Roll back the story DB from a backup created by db-backup.sh.
#
# DEFAULT = physical restore (byte-exact): stop Postgres, wipe the pgdata volume,
# untar the cold backup, start Postgres. Fast and exact.
#
# --logical = restore the pg_dump into the running DB with pg_restore --clean.
#
# This is DESTRUCTIVE: it overwrites the current database. Requires --confirm.
#
# Usage:
#   docker/scripts/db-restore.sh <backup_dir> --confirm            # physical rollback
#   docker/scripts/db-restore.sh <backup_dir> --logical --confirm  # logical rollback
set -euo pipefail

CONTAINER="betterbox-story-postgres"
VOLUME="story_db_betterbox_story_pgdata"
COMPOSE_FILE="story_db/docker-compose.yml"
DB_NAME="betterbox_story"
DB_USER="betterbox"

BACKUP_DIR=""
MODE="physical"
CONFIRM=0
while [ $# -gt 0 ]; do
  case "$1" in
    --logical) MODE="logical" ;;
    --physical) MODE="physical" ;;
    --confirm) CONFIRM=1 ;;
    -*) echo "unknown arg: $1" >&2; exit 2 ;;
    *) BACKUP_DIR="$1" ;;
  esac
  shift
done

if [ -z "$BACKUP_DIR" ] || [ ! -d "$BACKUP_DIR" ]; then
  echo "ERROR: pass a valid backup dir (see db_backups/<timestamp>/)." >&2
  exit 2
fi
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT"

if [ "$CONFIRM" -ne 1 ]; then
  echo "REFUSING: this OVERWRITES the current '$DB_NAME' database (mode=$MODE)."
  echo "Re-run with --confirm if you are sure. Backup: $BACKUP_DIR"
  exit 2
fi

# verify checksums if present
if [ -f "$BACKUP_DIR/SHA256SUMS" ]; then
  echo "[restore] verifying checksums..."
  ( cd "$BACKUP_DIR" && sha256sum -c SHA256SUMS )
fi

if [ "$MODE" = "physical" ]; then
  TAR="$BACKUP_DIR/pgdata.tar.gz"
  [ -f "$TAR" ] || { echo "ERROR: $TAR not found (use --logical if only a dump exists)." >&2; exit 1; }
  echo "[restore] PHYSICAL rollback from $TAR"
  echo "[restore] stopping Postgres..."
  docker compose -f "$COMPOSE_FILE" stop postgres
  echo "[restore] wiping + restoring volume '$VOLUME'..."
  docker run --rm -v "${VOLUME}:/data" -v "$(cd "$BACKUP_DIR" && pwd):/backup:ro" alpine \
    sh -c "rm -rf /data/* /data/..?* /data/.[!.]* 2>/dev/null; tar xzf /backup/pgdata.tar.gz -C /data"
  echo "[restore] starting Postgres..."
  docker compose -f "$COMPOSE_FILE" start postgres
else
  DUMP="$BACKUP_DIR/${DB_NAME}.dump"
  [ -f "$DUMP" ] || { echo "ERROR: $DUMP not found." >&2; exit 1; }
  echo "[restore] LOGICAL rollback from $DUMP (pg_restore --clean)"
  if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    docker compose -f "$COMPOSE_FILE" start postgres
  fi
  docker exec -i "$CONTAINER" pg_restore --clean --if-exists --no-owner \
    -U "$DB_USER" -d "$DB_NAME" < "$DUMP"
fi

echo "[restore] waiting for Postgres to be ready..."
for i in $(seq 1 30); do
  if docker exec "$CONTAINER" pg_isready -U "$DB_USER" -d "$DB_NAME" >/dev/null 2>&1; then
    echo "[restore] DONE. DB is back up."
    docker exec "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -c \
      "SELECT pg_size_pretty(pg_database_size('$DB_NAME')) AS db_size, (SELECT count(*) FROM chapters) AS chapters;" 2>/dev/null || true
    exit 0
  fi
  sleep 2
done
echo "[restore] WARNING: Postgres did not report ready within timeout; check 'docker logs $CONTAINER'." >&2
exit 1
