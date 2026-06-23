#!/usr/bin/env bash
# Safe voice A/B preview — one VieNeu process per profile, resource checks each run.
#
# Usage:
#   bash scripts/story_pipeline/preview_voices_xianxia_safe.sh
#   VOICE_PREVIEW_DEVICE=cuda bash scripts/story_pipeline/preview_voices_xianxia_safe.sh
#
# Output: story_data/voice_samples/*.wav (survives reboot; gitignored)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PYTHON="${ROOT}/viterbox/venv/bin/python"
PREVIEW="${ROOT}/scripts/story_pipeline/preview_vieneu_presets.py"
OUT="${VOICE_PREVIEW_DIR:-${ROOT}/story_data/voice_samples}"
DEVICE="${VOICE_PREVIEW_DEVICE:-cpu}"
COOLDOWN="${TTS_PROFILE_COOLDOWN_SECONDS:-15}"

# Top xianxia narrator candidates — one subprocess each to limit RAM/CPU spikes
PROFILES=(
  phoaudiobook_lu_thu
  preset_trong_huu
  preset_binh_an
  xianxia_spirit_male
  xianxia_story_male
  dolly_steady_man
  dolly_reliable_man
)

mkdir -p "$OUT"
LOG="${VOICE_PREVIEW_LOG:-${OUT}/generation.log}"
mkdir -p "$(dirname "$LOG")"
echo "=== Voice preview (safe) device=${DEVICE} output=${OUT} ===" | tee -a "$LOG"

for profile in "${PROFILES[@]}"; do
  out_wav="${OUT}/${profile}.wav"
  if [[ -f "$out_wav" ]] && [[ "$(stat -c%s "$out_wav" 2>/dev/null || echo 0)" -gt 1000 ]]; then
    echo "[SKIP] existing ${out_wav}"
    continue
  fi
  echo ""
  echo ">>> profile=${profile}"
  "$PYTHON" "$PREVIEW" \
    --device "$DEVICE" \
    --profiles "$profile" \
    --output-dir "$OUT" \
    "$@" || echo "[WARN] failed ${profile} — continuing"
  if [[ "$COOLDOWN" != "0" ]]; then
    echo "[COOLDOWN] ${COOLDOWN}s"
    sleep "$COOLDOWN"
  fi
done

echo ""
echo "=== Done. Listen: ${OUT}/ ==="
ls -1 "$OUT"/*.wav 2>/dev/null || true
