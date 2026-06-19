#!/bin/sh
set -eu

python docker/scripts/wait_for_db.py

set -- python scripts/story_pipeline/audio_segment_worker_vieneu.py \
  --device "${AUDIO_SEGMENT_VIENEU_DEVICE:-auto}" \
  --backend "${AUDIO_SEGMENT_VIENEU_BACKEND:-auto}" \
  --min-free-ram-gb "${AUDIO_SEGMENT_MIN_FREE_RAM_GB:-1.5}" \
  --max-cpu-percent "${AUDIO_SEGMENT_MAX_CPU_PERCENT:-90}" \
  --resource-wait-seconds "${AUDIO_SEGMENT_RESOURCE_WAIT_SECONDS:-10}" \
  --output-root "${AUDIO_SEGMENT_OUTPUT_ROOT:-story_audio_segments}" \
  --voice-key "${AUDIO_SEGMENT_VIENEU_VOICE_KEY:-preset_binh_an}" \
  --voice "${AUDIO_SEGMENT_VIENEU_VOICE:-Bình An}" \
  --voice-profile "${AUDIO_SEGMENT_VIENEU_VOICE_PROFILE:-preset_binh_an}" \
  --gpu-lock-path "${AUDIO_SEGMENT_GPU_LOCK_PATH:-/tmp/betterbox_tts_gpu.lock}"

if [ -n "${AUDIO_SEGMENT_VIENEU_REFERENCE_AUDIO:-}" ]; then
  set -- "$@" --reference-audio "${AUDIO_SEGMENT_VIENEU_REFERENCE_AUDIO}"
fi

if [ -n "${AUDIO_SEGMENT_VIENEU_REFERENCE_TEXT:-}" ]; then
  set -- "$@" --reference-text "${AUDIO_SEGMENT_VIENEU_REFERENCE_TEXT}"
fi

exec "$@"
