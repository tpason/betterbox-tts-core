#!/bin/bash
set -euo pipefail

INTERVAL="${CHAR_MAP_INTERVAL_SECONDS:-43200}"  # 12 hours default

echo "[char-map-updater] Starting. Interval=${INTERVAL}s"

while true; do
  echo "[char-map-updater] Running at $(date -u +%Y-%m-%dT%H:%M:%SZ)"

  python /app/scripts/story_pipeline/update_char_maps_cron.py \
    --ollama-url    "${OLLAMA_URL:-http://host.docker.internal:11434}" \
    --model         "${CHAR_MAP_MODEL:-qwen3:14b}" \
    --limit         "${CHAR_MAP_LIMIT:-10}" \
    --min-polished  "${CHAR_MAP_MIN_POLISHED:-10}" \
    --new-chapter-threshold "${CHAR_MAP_NEW_CHAPTER_THRESHOLD:-50}" \
    --sample-chapters       "${CHAR_MAP_SAMPLE_CHAPTERS:-30}" \
    --timeout               "${CHAR_MAP_TIMEOUT:-600}" \
    --delay                 "${CHAR_MAP_DELAY:-3}" \
    --min-free-vram-gb      "${CHAR_MAP_MIN_FREE_VRAM_GB:-0}" \
    --min-free-ram-gb       "${CHAR_MAP_MIN_FREE_RAM_GB:-1.5}" \
    --max-cpu-pct           "${CHAR_MAP_MAX_CPU_PCT:-85}" \
    --resource-wait         "${CHAR_MAP_RESOURCE_WAIT:-1800}" \
    --resource-poll         "${CHAR_MAP_RESOURCE_POLL:-30}" \
    ${CHAR_MAP_APPEND_ONLY:+--append-only} \
    || echo "[char-map-updater] Run failed (non-zero), will retry next interval."

  echo "[char-map-updater] Sleeping ${INTERVAL}s..."
  sleep "$INTERVAL"
done
