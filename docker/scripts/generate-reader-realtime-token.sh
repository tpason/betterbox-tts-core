#!/usr/bin/env bash
# Print a cryptographically strong READER_REALTIME_TOKEN for docker/.env
set -euo pipefail
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
