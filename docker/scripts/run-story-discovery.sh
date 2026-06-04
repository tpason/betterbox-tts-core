#!/bin/sh
set -eu

interval="${DISCOVERY_INTERVAL_SECONDS:-86400}"

python docker/scripts/wait_for_db.py

while true; do
  echo "[discovery] start $(date -Iseconds)"

  set -- python scripts/story_pipeline/discover_hot_stories.py \
    --pages "${DISCOVERY_PAGES:-2}" \
    --min-chapters "${DISCOVERY_MIN_CHAPTERS:-30}"

  if [ -n "${DISCOVERY_SOURCES:-}" ]; then
    set -- "$@" --sources ${DISCOVERY_SOURCES}
  fi

  "$@" || echo "[discovery] run failed; will retry after ${interval}s"

  echo "[discovery] sleep ${interval}s"
  sleep "${interval}"
done
