#!/usr/bin/env bash
# Local story-reader dev with WebSocket + hot reload (no Docker rebuild).
#
# Usage (from repo root):
#   bash docker/scripts/dev-story-reader.sh
#   bash docker/scripts/dev-story-reader.sh --port 3000   # use :3000 (stop Docker story-reader first)
#
# Pipeline workers in Docker → this dev server:
#   READER_REALTIME_URL=http://host.docker.internal:3003
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
READER_DIR="$ROOT/story_reader"
PORT="${STORY_READER_DEV_PORT:-3003}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port)
      PORT="$2"
      shift 2
      ;;
    -h | --help)
      sed -n '2,8p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

if [[ ! -d "$READER_DIR/node_modules/next" ]]; then
  echo "Installing story_reader dependencies (first run)…" >&2
  (cd "$READER_DIR" && npm ci)
fi

# Load only whitelisted keys — root .env may contain unquoted values unsafe for `source`.
# shellcheck disable=SC1091
source "$ROOT/docker/scripts/lib/load-env-key.sh"

if [[ -f "$ROOT/.env" ]]; then
  load_env_key STORY_DATABASE_URL "$ROOT/.env"
  load_env_key READER_REALTIME_TOKEN "$ROOT/.env"
  load_env_key READER_REALTIME_URL "$ROOT/.env"
  load_env_key READER_REALTIME_DEV_URL "$ROOT/.env"
  load_env_key STORY_READER_DEV_PORT "$ROOT/.env"
fi

PORT="${STORY_READER_DEV_PORT:-$PORT}"

export PORT
export READER_BIND_HOST="0.0.0.0"
export NODE_ENV=development
export STORY_DATABASE_URL="${STORY_DATABASE_URL:-postgresql://betterbox:betterbox@127.0.0.1:54329/betterbox_story}"
if [[ "$STORY_DATABASE_URL" == *"@host.docker.internal:"* ]]; then
  export STORY_DATABASE_URL="${STORY_DATABASE_URL/@host.docker.internal/@127.0.0.1}"
fi

if command -v ss >/dev/null 2>&1; then
  if ss -tln | grep -q ":${PORT} "; then
    echo "WARN: port ${PORT} already in use — stop the old dev server or pass --port <n>" >&2
  fi
fi

cat <<EOF
== Story reader dev (npm run dev:ws — hot reload, no Docker rebuild)
   App:  http://127.0.0.1:${PORT}
   WS:   ws://127.0.0.1:${PORT}/reader-ws
   DB:   ${STORY_DATABASE_URL}

Docker story-reader on :3000 can stay up; use :${PORT} for UI dev.

Pipeline → this dev server — add to root .env (workers pick up on restart):
   READER_REALTIME_DEV_URL=http://host.docker.internal:${PORT}
   # production URL stays READER_REALTIME_URL=http://story-reader:3000

Smoke: READER_REALTIME_URL=http://127.0.0.1:${PORT} bash docker/scripts/smoke-reader-realtime.sh
Safe E2E (API only, no heavy page compile): PLAYWRIGHT_BASE_URL=http://127.0.0.1:${PORT} npm run test:e2e:realtime:api --prefix story_reader
EOF

# Limit Node heap during dev — reader page compile is heavy (~3000 modules).
export NODE_OPTIONS="${NODE_OPTIONS:---max-old-space-size=4096}"

cd "$READER_DIR"
exec npm run dev:ws
