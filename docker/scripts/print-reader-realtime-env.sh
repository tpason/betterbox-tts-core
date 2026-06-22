#!/usr/bin/env bash
# Generate READER_REALTIME_TOKEN and print .env lines for root docker/.env
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TOKEN="$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")"

cat <<EOF
# Paste into $ROOT/.env (same value for story-reader + pipeline workers):
READER_REALTIME_TOKEN=$TOKEN
READER_REALTIME_URL=http://story-reader:3000
EOF
