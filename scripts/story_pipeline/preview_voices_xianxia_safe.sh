#!/usr/bin/env bash
# Safe voice A/B preview — waits for CPU/RAM/VRAM before each profile.
#
# Usage:
#   bash scripts/story_pipeline/preview_voices_xianxia_safe.sh
#   bash scripts/story_pipeline/preview_voices_xianxia_safe.sh --device cpu
#
# Output: /tmp/vieneu_voice_samples/*.wav
# Do NOT use --force unless you accept OOM / system freeze risk.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PYTHON="${ROOT}/viterbox/venv/bin/python"
OUT="${VOICE_PREVIEW_DIR:-/tmp/vieneu_voice_samples}"
# cpu = không tranh GPU với Ollama/crawler; cuda nhanh hơn khi pipeline nghỉ
DEVICE="${VOICE_PREVIEW_DEVICE:-cpu}"

exec "$PYTHON" "$ROOT/scripts/story_pipeline/preview_vieneu_presets.py" \
  --device "$DEVICE" \
  --skip-existing \
  --output-dir "$OUT" \
  "$@"
