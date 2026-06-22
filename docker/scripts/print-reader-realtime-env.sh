#!/usr/bin/env bash
# Generate READER_REALTIME_TOKEN and print .env lines for root docker/.env
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TOKEN="$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")"

cat <<EOF
# Paste into $ROOT/.env (same value for story-reader + pipeline workers):
READER_REALTIME_TOKEN=$TOKEN
READER_REALTIME_URL=http://story-reader:3000

# While developing UI locally (pipeline workers use DEV URL when set):
# READER_REALTIME_DEV_URL=http://host.docker.internal:3003
# Run: bash docker/scripts/dev-story-reader.sh
EOF
