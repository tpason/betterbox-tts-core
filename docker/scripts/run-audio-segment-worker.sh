#!/bin/sh
set -eu

python docker/scripts/wait_for_db.py

exec python scripts/story_pipeline/audio_segment_worker_viterbox.py \
  --device "${AUDIO_SEGMENT_DEVICE:-cuda}" \
  --min-free-vram-gb "${AUDIO_SEGMENT_MIN_FREE_VRAM_GB:-7}" \
  --min-runtime-free-vram-gb "${AUDIO_SEGMENT_MIN_RUNTIME_FREE_VRAM_GB:-1}" \
  --gpu-wait-seconds "${AUDIO_SEGMENT_GPU_WAIT_SECONDS:-20}" \
  --min-free-ram-gb "${AUDIO_SEGMENT_MIN_FREE_RAM_GB:-1.5}" \
  --max-cpu-percent "${AUDIO_SEGMENT_MAX_CPU_PERCENT:-90}" \
  --resource-wait-seconds "${AUDIO_SEGMENT_RESOURCE_WAIT_SECONDS:-10}" \
  --output-root "${AUDIO_SEGMENT_OUTPUT_ROOT:-story_audio_segments}" \
  --voice-key "${AUDIO_SEGMENT_VOICE_KEY:-viterbox_default}" \
  --reference-audio "${AUDIO_SEGMENT_REFERENCE_AUDIO:-wavs/vieneu_alloy1512_1005.wav}" \
  --gpu-lock-path "${AUDIO_SEGMENT_GPU_LOCK_PATH:-/tmp/betterbox_tts_gpu.lock}" \
  --no-cpu-fallback
