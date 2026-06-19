#!/usr/bin/env bash
# Unattended fleet quality pipeline — auto resume, resource-safe.
#
# Usage:
#   bash scripts/story_pipeline/run_fleet_quality_daemon.sh          # foreground
#   bash scripts/story_pipeline/run_fleet_quality_daemon.sh --tmux  # detached tmux
#
# Env overrides (optional):
#   QUALITY_MIN_VRAM_MB=10240  QUALITY_MIN_RAM_MB=4096  QUALITY_MAX_CPU_PCT=85
#   FLEET_BATCH_SIZE=30  FLEET_DAEMON_IDLE=300
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PY="${ROOT}/viterbox/venv/bin/python"
SESSION="${FLEET_TMUX_SESSION:-fleet-quality-auto}"
LOG="${FLEET_LOG:-/tmp/fleet_quality_auto.log}"

CMD=(
  "$PY" "${ROOT}/scripts/story_pipeline/story_quality_pipeline.py" auto
  --batch-size "${FLEET_BATCH_SIZE:-30}"
  --qa-sample "${FLEET_QA_SAMPLE:-20}"
  --skip-llm-judge
  --resource-poll "${FLEET_RESOURCE_POLL:-30}"
  --daemon-idle "${FLEET_DAEMON_IDLE:-300}"
  --max-retries "${FLEET_MAX_RETRIES:-3}"
)

if [[ "${1:-}" == "--tmux" ]]; then
  if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "[fleet] tmux session '$SESSION' already running — attach: tmux attach -t $SESSION"
    exit 0
  fi
  tmux new-session -d -s "$SESSION" \
    "cd '$ROOT' && ${CMD[*]} 2>&1 | tee -a '$LOG'; echo EXIT:\$? >> '$LOG'"
  echo "[fleet] started tmux session '$SESSION' — log: $LOG"
  echo "[fleet] attach: tmux attach -t $SESSION"
  exit 0
fi

cd "$ROOT"
exec "${CMD[@]}" 2>&1 | tee -a "$LOG"
