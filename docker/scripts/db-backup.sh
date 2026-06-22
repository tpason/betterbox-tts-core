#!/usr/bin/env bash
# Backup the story DB before risky maintenance (e.g. VACUUM FULL reclaim).
#
# Produces TWO backups in a timestamped dir so you have a reliable rollback point:
#   1. logical  : pg_dump -Fc (hot, while Postgres runs)  -> portable, version-tolerant
#   2. physical : cold tar of the pgdata volume (Postgres stopped) -> byte-exact, fast restore
#
# Rollback with: docker/scripts/db-restore.sh <backup_dir>
#
# Usage:
#   docker/scripts/db-backup.sh [--no-physical] [--out DIR]
#
#   --no-physical   skip the cold volume tar (keep Postgres running; logical dump only)
#   --out DIR       backup root (default: ./db_backups)
set -euo pipefail

CONTAINER="betterbox-story-postgres"
VOLUME="story_db_betterbox_story_pgdata"
COMPOSE_FILE="story_db/docker-compose.yml"
DB_NAME="betterbox_story"
DB_USER="betterbox"

OUT_ROOT="./db_backups"
DO_PHYSICAL=1
while [ $# -gt 0 ]; do
  case "$1" in
    --no-physical) DO_PHYSICAL=0 ;;
    --out) OUT_ROOT="$2"; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT"

TS="$(date -u +%Y%m%d-%H%M%SZ)"
BACKUP_DIR="$OUT_ROOT/$TS"
mkdir -p "$BACKUP_DIR"
echo "[backup] target dir: $BACKUP_DIR"

if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  echo "[backup] ERROR: container '$CONTAINER' is not running; cannot pg_dump." >&2
  exit 1
fi

# --- 1. logical dump (hot) via the container's own pg_dump (matches server version) ---
DUMP="$BACKUP_DIR/${DB_NAME}.dump"
echo "[backup] pg_dump -Fc -> $DUMP"
docker exec "$CONTAINER" pg_dump -Fc -U "$DB_USER" "$DB_NAME" > "$DUMP"
echo "[backup] verifying dump (pg_restore --list)..."
docker exec -i "$CONTAINER" pg_restore --list < "$DUMP" > "$BACKUP_DIR/dump.toc" 2>/dev/null
DUMP_BYTES=$(stat -c %s "$DUMP")
echo "[backup] logical dump OK ($((DUMP_BYTES/1000000)) MB, $(wc -l < "$BACKUP_DIR/dump.toc") objects)"

# --- 2. physical cold backup (Postgres stopped) ---
if [ "$DO_PHYSICAL" -eq 1 ]; then
  echo "[backup] stopping Postgres for a consistent cold volume copy..."
  docker compose -f "$COMPOSE_FILE" stop postgres
  # If the tar (or anything below) fails while Postgres is stopped, make sure we bring it back.
  restart_pg() { echo "[backup] (trap) ensuring Postgres is started again..."; docker compose -f "$COMPOSE_FILE" start postgres || true; }
  trap restart_pg EXIT
  TAR="$BACKUP_DIR/pgdata.tar.gz"
  echo "[backup] tarring volume '$VOLUME' -> $TAR"
  docker run --rm -v "${VOLUME}:/data:ro" -v "$(cd "$BACKUP_DIR" && pwd):/backup" alpine \
    tar czf "/backup/pgdata.tar.gz" -C /data .
  echo "[backup] starting Postgres back up..."
  docker compose -f "$COMPOSE_FILE" start postgres
  trap - EXIT
  # wait for healthy
  for i in $(seq 1 30); do
    if docker exec "$CONTAINER" pg_isready -U "$DB_USER" -d "$DB_NAME" >/dev/null 2>&1; then break; fi
    sleep 2
  done
  TAR_BYTES=$(stat -c %s "$TAR")
  echo "[backup] physical backup OK ($((TAR_BYTES/1000000)) MB)"
else
  echo "[backup] --no-physical: skipped cold volume tar."
fi

# --- manifest + checksums ---
( cd "$BACKUP_DIR" && sha256sum ./*.dump ./*.tar.gz 2>/dev/null > SHA256SUMS || true )
cat > "$BACKUP_DIR/MANIFEST.txt" <<EOF
story DB backup
created_utc : $TS
db          : $DB_NAME (user=$DB_USER)
container   : $CONTAINER
volume      : $VOLUME
logical     : ${DB_NAME}.dump (pg_dump -Fc) + dump.toc
physical    : $([ "$DO_PHYSICAL" -eq 1 ] && echo "pgdata.tar.gz (cold volume copy)" || echo "(skipped)")

ROLLBACK:
  docker/scripts/db-restore.sh "$BACKUP_DIR"            # physical (preferred, byte-exact)
  docker/scripts/db-restore.sh "$BACKUP_DIR" --logical  # logical (pg_restore --clean)
EOF

echo ""
echo "[backup] DONE. Rollback point: $BACKUP_DIR"
echo "[backup] To roll back:  docker/scripts/db-restore.sh \"$BACKUP_DIR\""
