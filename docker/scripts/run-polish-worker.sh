#!/bin/sh
set -eu

interval="${POLISH_INTERVAL_SECONDS:-30}"

python docker/scripts/wait_for_db.py

while true; do
  echo "[polish] resource check $(date -Iseconds)"

  if ! WORKERS=$(python docker/scripts/check_resources.py \
      --max-workers "${POLISH_WORKERS:-1}" \
      --min-free-ram-gb "${POLISH_MIN_FREE_RAM_GB:-2.0}" \
      --max-cpu-percent "${POLISH_MAX_CPU_PERCENT:-80}" \
      --workers-only 2>/dev/null); then
    echo "[polish] resources too low - skip batch, sleep ${interval}s"
    sleep "${interval}"
    continue
  fi

  set -- python scripts/story_pipeline/polish_worker.py \
    --workers "${WORKERS}" \
    --batch-size "${WORKERS}" \
    --once \
    --ollama-url "${OLLAMA_URL:-http://host.docker.internal:11434}" \
    --vi-model "${POLISH_VI_MODEL:-qwen3:14b}" \
    --translate-model "${POLISH_TRANSLATE_MODEL:-qwen3:14b}" \
    --post-translate "${POLISH_POST_TRANSLATE:-polish}" \
    --char-map-text-source "${POLISH_CHAR_MAP_TEXT_SOURCE:-auto}" \
    --char-map-min-frequency "${POLISH_CHAR_MAP_MIN_FREQUENCY:-1}" \
    --char-map-create-cooldown "${POLISH_CHAR_MAP_CREATE_COOLDOWN:-30}"

  if [ -n "${POLISH_STORY_MEMORY_DIR:-}" ]; then
    set -- "$@" --story-memory-dir "${POLISH_STORY_MEMORY_DIR}"
  fi

  if [ "${POLISH_FAIL_ON_STORY_MEMORY_ISSUES:-0}" = "1" ]; then
    set -- "$@" --fail-on-story-memory-issues
  fi

  if [ -n "${POLISH_SOURCE_CODES:-}" ]; then
    for source_code in ${POLISH_SOURCE_CODES}; do
      set -- "$@" --source-code "$source_code"
    done
  fi

  if [ "${POLISH_NO_SAVE_FILES:-1}" = "1" ]; then
    set -- "$@" --no-save-files
  fi

  "$@" || echo "[polish] batch failed; will retry after ${interval}s"

  echo "[polish] sleep ${interval}s"
  sleep "${interval}"
done
