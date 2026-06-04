#!/bin/sh
set -eu

python docker/scripts/wait_for_db.py

exec python scripts/story_pipeline/audio_worker_viterbox.py \
  --device "${AUDIO_DEVICE:-cuda}" \
  --min-free-vram-gb "${AUDIO_MIN_FREE_VRAM_GB:-7}" \
  --reference-audio "${AUDIO_REFERENCE_AUDIO:-wavs/vieneu_alloy1512_1005.wav}"
