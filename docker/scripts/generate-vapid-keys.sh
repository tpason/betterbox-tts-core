#!/usr/bin/env bash
# Generate VAPID keys for Web Push. Append output to root .env / docker/.env
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT/story_reader"

if [[ ! -d node_modules/web-push ]]; then
  echo "Run: cd story_reader && npm ci" >&2
  exit 1
fi

node -e "
const webpush = require('web-push');
const keys = webpush.generateVAPIDKeys();
console.log('# Web Push — append to .env');
console.log('VAPID_PUBLIC_KEY=' + keys.publicKey);
console.log('VAPID_PRIVATE_KEY=' + keys.privateKey);
console.log('VAPID_SUBJECT=mailto:admin@example.com');
console.log('# Optional: protect POST /api/push/send from external callers');
console.log('PUSH_SEND_SECRET=' + require('crypto').randomBytes(24).toString('hex'));
"
