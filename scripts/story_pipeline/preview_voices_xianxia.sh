#!/usr/bin/env bash
# preview_voices_xianxia.sh — Preview candidate voices với đoạn text xianxia chuẩn.
#
# Usage:
#   bash scripts/story_pipeline/preview_voices_xianxia.sh
#   bash scripts/story_pipeline/preview_voices_xianxia.sh --output-dir /tmp/voice_preview
#   bash scripts/story_pipeline/preview_voices_xianxia.sh --device cpu
#
# Output: /tmp/voice_preview_xianxia/{voice_key}.wav
# Nghe output để chọn voice phù hợp nhất trước khi đặt DEFAULT_VIENEU_VOICE_PROFILE.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
VENV_PYTHON="$ROOT/viterbox/venv/bin/python"
PREVIEW_SCRIPT="$SCRIPT_DIR/preview_text_viterbox.py"
OUTPUT_DIR="${OUTPUT_DIR:-/tmp/voice_preview_xianxia}"
DEVICE="${DEVICE:-cuda}"

# Parse args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
    --device)     DEVICE="$2"; shift 2 ;;
    *)            echo "Unknown arg: $1"; exit 1 ;;
  esac
done

mkdir -p "$OUTPUT_DIR"

# Đoạn text test — western_fantasy / xianxia vibe, ~200 chars
TEXT="Enkrid nhìn lên bầu trời, linh lực trong kinh mạch chầm chậm lưu chuyển. Trước mắt là con đường tu luyện dài vô tận, nhưng anh không do dự. Mỗi bước tiến đều phải trả bằng mồ hôi và ý chí sắt đá."

echo "=== Voice Preview: Xianxia Narrator Candidates ==="
echo "Output dir: $OUTPUT_DIR"
echo "Device: $DEVICE"
echo ""

# Danh sách candidate — tên voice và đường dẫn WAV tương đối từ ROOT
declare -A VOICE_WAVS
# VieNeu-TTS-140h voice bank (unregistered)
VOICE_WAVS["vieneu_capybara1812_0027"]="voice_bank/vieneu/capybara1812_0027/vieneu_capybara1812_0027_0000.wav"
VOICE_WAVS["vieneu_capybara1812_1003"]="voice_bank/vieneu/capybara1812_1003/vieneu_capybara1812_1003_0000.wav"
VOICE_WAVS["vieneu_capybara1812_1017"]="voice_bank/vieneu/capybara1812_1017/vieneu_capybara1812_1017_0000.wav"
VOICE_WAVS["vieneu_jellyfish1010_0028"]="voice_bank/vieneu/jellyfish1010_0028/vieneu_jellyfish1010_0028_0000.wav"
# Existing voice bank (already registered, for comparison)
VOICE_WAVS["xianxia_spirit_male"]="wavs/vieneu_jellyfish1010_0006.wav"
VOICE_WAVS["xianxia_story_male_current"]="wavs/vieneu_capybara1812_0048.wav"
# Dolly candidates (new)
VOICE_WAVS["dolly_steady_man"]="wavs/dolly_steady_man.wav"
VOICE_WAVS["dolly_reliable_man"]="wavs/dolly_reliable_man.wav"
VOICE_WAVS["dolly_humorous_elder"]="wavs/dolly_humorous_elder.wav"
VOICE_WAVS["dolly_calm_leader"]="wavs/dolly_calm_leader.wav"
VOICE_WAVS["dolly_confident_man"]="wavs/dolly_confident_man.wav"
VOICE_WAVS["dolly_male_narrator"]="wavs/dolly_male_narrator.wav"
VOICE_WAVS["dolly_narrator"]="wavs/dolly_narrator.wav"
VOICE_WAVS["dolly_distinguished_gentleman"]="wavs/dolly_distinguished_gentleman.wav"
VOICE_WAVS["dolly_thoughtful_man"]="wavs/dolly_thoughtful_man.wav"
# Dolly — already registered (comparison)
VOICE_WAVS["dolly_serene_elder"]="wavs/dolly_serene_elder.wav"
VOICE_WAVS["dolly_steadfast_narrator"]="wavs/dolly_steadfast_narrator.wav"
VOICE_WAVS["dolly_wise_scholar"]="wavs/dolly_wise_scholar.wav"

TOTAL=${#VOICE_WAVS[@]}
DONE=0
FAILED=()

for voice_key in "${!VOICE_WAVS[@]}"; do
  wav_path="$ROOT/${VOICE_WAVS[$voice_key]}"
  out_file="$OUTPUT_DIR/${voice_key}.wav"

  if [[ ! -f "$wav_path" ]]; then
    echo "[SKIP] $voice_key — WAV not found: ${VOICE_WAVS[$voice_key]}"
    FAILED+=("$voice_key (WAV missing)")
    continue
  fi

  DONE=$((DONE + 1))
  echo "[$DONE/$TOTAL] $voice_key"

  if "$VENV_PYTHON" "$PREVIEW_SCRIPT" \
    --reference-audio "$wav_path" \
    --text "$TEXT" \
    --output "$out_file" \
    --device "$DEVICE" \
    --max-chars 300 \
    2>/dev/null; then
    echo "  → $out_file"
  else
    echo "  [FAIL] $voice_key"
    FAILED+=("$voice_key (synthesis error)")
  fi
  echo ""
done

echo "=== Done: $DONE/$TOTAL voices previewed ==="
if [[ ${#FAILED[@]} -gt 0 ]]; then
  echo "Failed/skipped:"
  for f in "${FAILED[@]}"; do echo "  - $f"; done
fi
echo ""
echo "Output files:"
ls -1 "$OUTPUT_DIR/"*.wav 2>/dev/null | sed "s|$OUTPUT_DIR/||" | sort
echo ""
echo "Nghe xong → update DEFAULT_VIENEU_VOICE_PROFILE trong:"
echo "  scripts/story_pipeline/vieneu_voice_profiles.py"
