#!/usr/bin/env bash
# Smoke-test story-reader realtime: health + broadcast.
# Usage:
#   READER_REALTIME_URL=http://localhost:3000 bash docker/scripts/smoke-reader-realtime.sh
#   READER_REALTIME_URL=http://127.0.0.1:3003 bash docker/scripts/smoke-reader-realtime.sh  # local dev
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/docker/scripts/lib/load-env-key.sh"

if [[ -f "$ROOT/.env" ]]; then
  load_env_key READER_REALTIME_TOKEN "$ROOT/.env"
  load_env_key READER_REALTIME_URL "$ROOT/.env"
  load_env_key READER_REALTIME_DEV_URL "$ROOT/.env"
fi

BASE="${READER_REALTIME_URL:-http://localhost:3000}"
BASE="${BASE%/}"

echo "== GET $BASE/api/health"
HEALTH="$(curl -fsS --max-time 8 "$BASE/api/health")"
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
HTTP_CODE="$(curl -sS --max-time 8 -o /tmp/smoke-broadcast.json -w '%{http_code}' "${AUTH[@]}" \
  -H "Content-Type: application/json" \
  -d '{"type":"notification_update"}' \
  "$BASE/api/realtime/broadcast")"
cat /tmp/smoke-broadcast.json
echo

if [[ "$HTTP_CODE" == "200" ]]; then
  echo "OK broadcast 200"
elif [[ "$HTTP_CODE" == "401" ]] && echo "$HEALTH" | grep -q '"nodeEnv":"development"'; then
  echo "FAIL: dev server expects open broadcast or matching READER_REALTIME_TOKEN" >&2
  exit 1
elif [[ "$HTTP_CODE" == "401" ]]; then
  echo "FAIL: set READER_REALTIME_TOKEN in .env for production broadcast" >&2
  exit 1
else
  echo "FAIL: unexpected HTTP $HTTP_CODE" >&2
  exit 1
fi

echo "OK"
