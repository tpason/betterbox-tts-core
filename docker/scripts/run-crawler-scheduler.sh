#!/bin/sh
set -eu

interval="${CRAWLER_INTERVAL_SECONDS:-300}"

python docker/scripts/wait_for_db.py

while true; do
  echo "[crawler] resource check $(date -Iseconds)"

  # Measure CPU/RAM, get recommended workers (exits 1 if resources too low).
  if ! WORKERS=$(python docker/scripts/check_resources.py \
      --max-workers "${CRAWLER_WORKERS:-4}" \
      --min-free-ram-gb "${CRAWLER_MIN_FREE_RAM_GB:-1.0}" \
      --max-cpu-percent "${CRAWLER_MAX_CPU_PERCENT:-85}" \
      --workers-only 2>/dev/null); then
    echo "[crawler] resources too low — skip pass, sleep ${interval}s"
    sleep "${interval}"
    continue
  fi

  echo "[crawler] start workers=${WORKERS} $(date -Iseconds)"

  set -- python scripts/story_pipeline/crawl_stories_from_db.py \
    --workers "${WORKERS}" \
    --min-catalog-check-hours "${CRAWLER_MIN_CATALOG_CHECK_HOURS:-1}" \
    --claim-finished-cooldown-minutes "${CRAWLER_CLAIM_COOLDOWN_MINUTES:-60}" \
    --timeout "${CRAWLER_TIMEOUT:-30}" \
    --retries "${CRAWLER_RETRIES:-5}" \
    --retry-sleep "${CRAWLER_RETRY_SLEEP:-2.0}" \
    --chapter-delay "${CRAWLER_CHAPTER_DELAY:-1.5}" \
    --chapter-workers "${CRAWLER_CHAPTER_WORKERS:-2}" \
    --max-consecutive-content-misses "${CRAWLER_MAX_CONSECUTIVE_CONTENT_MISSES:-1}" \
    --post-translate "${CRAWLER_POST_TRANSLATE:-polish}" \
    --no-persist-files

  if [ "${CRAWLER_ONLY_INCOMPLETE:-1}" = "1" ]; then
    set -- "$@" --only-incomplete
  fi

  if [ -n "${CRAWLER_SOURCES:-}" ]; then
    set -- "$@" --sources ${CRAWLER_SOURCES}
  fi

  if [ "${CRAWLER_LIMIT_STORIES:-0}" != "0" ]; then
    set -- "$@" --limit-stories "${CRAWLER_LIMIT_STORIES}"
  fi

  if [ "${CRAWLER_MAX_CHAPTERS:-0}" != "0" ]; then
    set -- "$@" --max-chapters "${CRAWLER_MAX_CHAPTERS}"
  fi

  "$@" || echo "[crawler] run failed; will retry after ${interval}s"

  echo "[crawler] sleep ${interval}s"
  sleep "${interval}"
done
