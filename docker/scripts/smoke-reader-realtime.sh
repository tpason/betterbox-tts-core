#!/usr/bin/env bash
# Smoke-test story-reader realtime: health + broadcast.
# Usage:
#   READER_REALTIME_URL=http://localhost:3000 READER_REALTIME_TOKEN=... bash docker/scripts/smoke-reader-realtime.sh
set -euo pipefail

BASE="${READER_REALTIME_URL:-http://localhost:3000}"
BASE="${BASE%/}"

echo "== GET $BASE/api/health"
HEALTH="$(curl -fsS "$BASE/api/health")"
echo "$HEALTH"
echo "$HEALTH" | grep -q '"websocket":true' || {
  echo "FAIL: websocket is not true — is story-reader running start:ws / dev:ws?" >&2
  exit 1
}

echo "== POST $BASE/api/realtime/broadcast"
AUTH=()
if [[ -n "${READER_REALTIME_TOKEN:-}" ]]; then
  AUTH=(-H "Authorization: Bearer $READER_REALTIME_TOKEN")
fi
curl -fsS "${AUTH[@]}" -H "Content-Type: application/json" \
  -d '{"type":"notification_update"}' \
  "$BASE/api/realtime/broadcast"
echo
echo "OK"
