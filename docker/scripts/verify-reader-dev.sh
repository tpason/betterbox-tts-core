#!/usr/bin/env bash
# Lightweight reader dev check — curl only, no Playwright (safe after reboot).
# Usage: bash docker/scripts/verify-reader-dev.sh [base_url]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/docker/scripts/lib/load-env-key.sh"

if [[ -f "$ROOT/.env" ]]; then
  load_env_key READER_REALTIME_TOKEN "$ROOT/.env"
fi

BASE="${1:-${READER_REALTIME_URL:-http://127.0.0.1:3003}}"
BASE="${BASE%/}"

echo "== GET $BASE/api/health"
HEALTH="$(curl -fsS --max-time 5 "$BASE/api/health")"
echo "$HEALTH"

echo "$HEALTH" | grep -q '"ok":true' || { echo "FAIL: ok not true" >&2; exit 1; }

if echo "$HEALTH" | grep -q '"websocket":true'; then
  echo "OK websocket=true"
else
  echo "FAIL: websocket=false — use dev:ws / start:ws, not plain next dev/start" >&2
  exit 1
fi

echo "== POST $BASE/api/realtime/broadcast"
AUTH=()
if [[ -n "${READER_REALTIME_TOKEN:-}" ]]; then
  AUTH=(-H "Authorization: Bearer $READER_REALTIME_TOKEN")
fi
HTTP_CODE="$(curl -sS --max-time 5 -o /tmp/verify-broadcast.json -w '%{http_code}' "${AUTH[@]}" \
  -H "Content-Type: application/json" \
  -d '{"type":"notification_update"}' \
  "$BASE/api/realtime/broadcast")"
cat /tmp/verify-broadcast.json
echo

if [[ "$HTTP_CODE" == "200" ]]; then
  echo "verify-reader-dev OK (broadcast 200)"
elif [[ "$HTTP_CODE" == "401" ]] && echo "$HEALTH" | grep -q '"nodeEnv":"development"'; then
  echo "WARN broadcast 401 in dev — unset READER_REALTIME_TOKEN or pass matching token" >&2
  echo "verify-reader-dev OK (health only)"
elif [[ "$HTTP_CODE" == "401" ]]; then
  echo "WARN broadcast 401 — set READER_REALTIME_TOKEN for production" >&2
  echo "verify-reader-dev OK (health only)"
else
  echo "FAIL: broadcast HTTP $HTTP_CODE" >&2
  exit 1
fi
