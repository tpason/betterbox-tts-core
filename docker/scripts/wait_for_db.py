from __future__ import annotations

import os
import sys
import time

import psycopg


database_url = os.environ.get(
    "STORY_DATABASE_URL",
    "postgresql://betterbox:betterbox@host.docker.internal:54329/betterbox_story",
)
timeout_seconds = int(os.environ.get("DB_WAIT_TIMEOUT_SECONDS", "120"))
deadline = time.monotonic() + timeout_seconds
last_error: Exception | None = None

while time.monotonic() < deadline:
    try:
        with psycopg.connect(database_url, connect_timeout=5) as conn:
            conn.execute("SELECT 1")
        print("[wait-db] ready", flush=True)
        sys.exit(0)
    except Exception as exc:  # pragma: no cover - runtime guard
        last_error = exc
        print(f"[wait-db] not ready: {exc}", flush=True)
        time.sleep(2)

raise SystemExit(f"Database did not become ready in {timeout_seconds}s: {last_error}")
