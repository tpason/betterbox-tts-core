#!/bin/sh
set -eu

interval="${ALTERNATE_INTERVAL_SECONDS:-21600}"

python docker/scripts/wait_for_db.py

while true; do
  echo "[alternate] start $(date -Iseconds)"

  set -- python scripts/story_pipeline/auto_crawl_alternate_sources.py \
    --limit-stories "${ALTERNATE_LIMIT_STORIES:-20}" \
    --min-score "${ALTERNATE_MIN_SCORE:-0.72}" \
    --max-candidates "${ALTERNATE_MAX_CANDIDATES:-5}" \
    --resume-from "${ALTERNATE_RESUME_FROM:-polished}" \
    --timeout "${ALTERNATE_TIMEOUT:-30}" \
    --retries "${ALTERNATE_RETRIES:-3}" \
    --retry-sleep "${ALTERNATE_RETRY_SLEEP:-2.0}" \
    --search-delay "${ALTERNATE_SEARCH_DELAY:-0.5}" \
    --provider-failure-limit "${ALTERNATE_PROVIDER_FAILURE_LIMIT:-1}" \
    --chapter-delay "${ALTERNATE_CHAPTER_DELAY:-1.5}" \
    --post-translate "${ALTERNATE_POST_TRANSLATE:-copy}" \
    --alias-inference "${ALTERNATE_ALIAS_INFERENCE:-heuristic}" \
    --alias-model "${ALTERNATE_ALIAS_MODEL:-qwen3:8b}" \
    --alias-timeout "${ALTERNATE_ALIAS_TIMEOUT:-120}" \
    --translate-check-timeout "${ALTERNATE_TRANSLATE_CHECK_TIMEOUT:-3}" \
    --char-map-text-source "${ALTERNATE_CHAR_MAP_TEXT_SOURCE:-auto}" \
    --char-map-min-frequency "${ALTERNATE_CHAR_MAP_MIN_FREQUENCY:-1}" \
    --ollama-url "${OLLAMA_URL:-http://host.docker.internal:11434}"

  if [ -n "${ALTERNATE_TARGET_SOURCES:-}" ]; then
    set -- "$@" --source ${ALTERNATE_TARGET_SOURCES}
  fi

  if [ -n "${ALTERNATE_PROVIDERS:-}" ]; then
    set -- "$@" --providers ${ALTERNATE_PROVIDERS}
  fi

  if [ -n "${ALTERNATE_ALIAS_JSON:-}" ]; then
    set -- "$@" --alias-json "${ALTERNATE_ALIAS_JSON}"
  fi

  if [ "${ALTERNATE_INCLUDE_COMPLETED:-0}" = "1" ]; then
    set -- "$@" --include-completed
  fi

  if [ "${ALTERNATE_APPLY:-0}" = "1" ]; then
    set -- "$@" --apply
  fi

  if [ "${ALTERNATE_POLISH_INLINE:-0}" = "1" ]; then
    set -- "$@" --polish-inline
  fi

  if [ -n "${ALTERNATE_STORY_MEMORY_DIR:-}" ]; then
    set -- "$@" --story-memory-dir "${ALTERNATE_STORY_MEMORY_DIR}"
  fi

  if [ "${ALTERNATE_FAIL_ON_STORY_MEMORY_ISSUES:-0}" = "1" ]; then
    set -- "$@" --fail-on-story-memory-issues
  fi

  if [ "${ALTERNATE_REQUEUE_DONE:-0}" = "1" ]; then
    set -- "$@" --requeue-done
  fi

  if [ "${ALTERNATE_LOG_SKIPPED_QUERIES:-0}" = "1" ]; then
    set -- "$@" --log-skipped-queries
  fi

  if [ "${ALTERNATE_ONLY_NEEDS_ALTERNATE:-0}" = "1" ]; then
    set -- "$@" --only-needs-alternate
  fi

  if [ "${ALTERNATE_TRANSLATE_FOR_SEARCH:-1}" = "0" ]; then
    set -- "$@" --no-translate-for-search
  fi

  if [ "${ALTERNATE_DIRECT_SLUG_CANDIDATES:-0}" = "1" ]; then
    set -- "$@" --direct-slug-candidates
  fi

  if [ "${ALTERNATE_MAX_CHAPTERS:-0}" != "0" ]; then
    set -- "$@" --max-chapters "${ALTERNATE_MAX_CHAPTERS}"
  fi

  if [ "${ALTERNATE_NO_STORY_SKIP:-0}" = "1" ]; then
    set -- "$@" --no-story-skip
  fi

  "$@" || echo "[alternate] run failed; will retry after ${interval}s"

  echo "[alternate] sleep ${interval}s"
  sleep "${interval}"
done
