#!/bin/sh
set -eu

interval="${AUDIO_ENQUEUE_INTERVAL_SECONDS:-1800}"

python docker/scripts/wait_for_db.py

while true; do
  echo "[audio-enqueuer] start $(date -Iseconds)"

  set -- python scripts/story_pipeline/enqueue_audio_jobs_from_db.py \
    --limit "${AUDIO_ENQUEUE_LIMIT:-200}" \
    --audio-output-root "${AUDIO_OUTPUT_ROOT:-story_audio}"

  if [ -n "${AUDIO_ENQUEUE_SOURCES:-}" ]; then
    set -- "$@" --source ${AUDIO_ENQUEUE_SOURCES}
  fi

  "$@" || echo "[audio-enqueuer] run failed; will retry after ${interval}s"

  echo "[audio-enqueuer] sleep ${interval}s"
  sleep "${interval}"
done
