#!/bin/sh
set -eu

python docker/scripts/wait_for_db.py

set -- python scripts/story_pipeline/audio_worker_vieneu.py \
  --device "${AUDIO_VIENEU_DEVICE:-auto}" \
  --backend "${AUDIO_VIENEU_BACKEND:-auto}" \
  --voice "${AUDIO_VIENEU_VOICE:-Xuân Vĩnh}" \
  --voice-profile "${AUDIO_VIENEU_VOICE_PROFILE:-xianxia_story_male}"

if [ -n "${AUDIO_VIENEU_REFERENCE_AUDIO:-}" ]; then
  set -- "$@" --reference-audio "${AUDIO_VIENEU_REFERENCE_AUDIO}"
fi

if [ -n "${AUDIO_VIENEU_REFERENCE_TEXT:-}" ]; then
  set -- "$@" --reference-text "${AUDIO_VIENEU_REFERENCE_TEXT}"
fi

exec "$@"
