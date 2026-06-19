#!/usr/bin/env bash
# Request Codex review via ai-devkit agent send + poll .agent/CODEX_REVIEW.md
#
# Usage:
#   bash scripts/request_codex_review.sh              # after implement (default)
#   bash scripts/request_codex_review.sh --plan       # after PLAN.md only
#   bash scripts/request_codex_review.sh 600          # custom timeout (seconds)
#   bash scripts/request_codex_review.sh --plan 120
#   bash scripts/request_codex_review.sh --send-only  # send message, do not poll
#
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
PLAN_FILE="$REPO_ROOT/.agent/PLAN.md"
IMPL_FILE="$REPO_ROOT/.agent/CLAUDE_IMPLEMENTATION.md"
REVIEW_FILE="$REPO_ROOT/.agent/CODEX_REVIEW.md"
TIMEOUT=300
POLL_INTERVAL=8
MODE="implement"
SEND_ONLY=0

AI_DEVKIT="${AI_DEVKIT_CMD:-ai-devkit}"
if ! command -v "$AI_DEVKIT" >/dev/null 2>&1; then
  AI_DEVKIT="npx ai-devkit@latest"
fi

usage() {
  sed -n '2,10p' "$0" | tail -n +2
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --plan) MODE="plan"; shift ;;
    --implement) MODE="implement"; shift ;;
    --send-only) SEND_ONLY=1; shift ;;
    -h|--help) usage 0 ;;
    *)
      if [[ "$1" =~ ^[0-9]+$ ]]; then
        TIMEOUT="$1"
      else
        echo "[ERROR] Unknown argument: $1" >&2
        usage 1
      fi
      shift
      ;;
  esac
done

find_codex_agent() {
  $AI_DEVKIT agent list --json 2>/dev/null | python3 -c "
import json, sys, os
agents = json.load(sys.stdin)
repo = os.path.realpath('${REPO_ROOT}')
for a in agents:
    atype = (a.get('type') or '').lower()
    proj_raw = a.get('projectPath') or a.get('cwd') or ''
    proj = os.path.realpath(proj_raw) if proj_raw else ''
    if atype == 'codex' and proj and (proj == repo or proj.startswith(repo + os.sep) or 'BetterBox-TTS' in proj):
        print(a['name'])
        break
" 2>/dev/null || true
}

CODEX_ID="$(find_codex_agent)"

if [[ -z "$CODEX_ID" ]]; then
  echo "[ERROR] No Codex agent found for BetterBox-TTS." >&2
  echo "        Start Codex in this repo, then retry." >&2
  echo "        Example: cd $REPO_ROOT && codex" >&2
  echo "        Check: $AI_DEVKIT agent list --json" >&2
  exit 1
fi

if [[ "$MODE" == "plan" ]]; then
  SOURCE_FILE="$PLAN_FILE"
  if [[ ! -f "$SOURCE_FILE" ]]; then
    echo "[ERROR] $SOURCE_FILE not found. Write the plan first." >&2
    exit 1
  fi
  MESSAGE="Review request (plan): Read .agent/PLAN.md and .agent/PROJECT_CONTEXT.md. Review scope, risks, and approach before implementation. Write findings to .agent/CODEX_REVIEW.md (Verdict: approved or fix-required). Reply Review done."
else
  SOURCE_FILE="$IMPL_FILE"
  if [[ ! -f "$SOURCE_FILE" ]]; then
    echo "[ERROR] $SOURCE_FILE not found. Write implementation notes first." >&2
    exit 1
  fi
  MESSAGE="Review request (implementation): Read .agent/CLAUDE_IMPLEMENTATION.md, run git status and git diff (and git -C story_reader diff if reader changed). Prioritize bugs, DB/queue safety, regressions. Write findings to .agent/CODEX_REVIEW.md. Reply Review done."
fi

echo "[INFO] Mode: $MODE"
echo "[INFO] Codex agent: $CODEX_ID"
echo "[INFO] Source: ${SOURCE_FILE#$REPO_ROOT/}"

BEFORE_MTIME="$(stat -c %Y "$REVIEW_FILE" 2>/dev/null || echo "0")"

$AI_DEVKIT agent send --id "$CODEX_ID" "$MESSAGE"

if [[ "$SEND_ONLY" -eq 1 ]]; then
  echo "[INFO] Message sent (--send-only). Poll $REVIEW_FILE manually."
  exit 0
fi

echo "[INFO] Polling for review (timeout: ${TIMEOUT}s)..."

ELAPSED=0
while [[ "$ELAPSED" -lt "$TIMEOUT" ]]; do
  sleep "$POLL_INTERVAL"
  ELAPSED=$((ELAPSED + POLL_INTERVAL))

  AFTER_MTIME="$(stat -c %Y "$REVIEW_FILE" 2>/dev/null || echo "0")"
  if [[ "$AFTER_MTIME" != "$BEFORE_MTIME" && "$AFTER_MTIME" != "0" ]]; then
    echo ""
    echo "[INFO] Codex review ready (${ELAPSED}s elapsed)"
    echo "================================================"
    cat "$REVIEW_FILE"
    echo "================================================"
    exit 0
  fi

  echo "[WAIT] ${ELAPSED}s / ${TIMEOUT}s — waiting for Codex to update .agent/CODEX_REVIEW.md..."
done

echo ""
echo "[TIMEOUT] Codex did not update review within ${TIMEOUT}s." >&2
echo "          Check .agent/CODEX_REVIEW.md or the Codex terminal." >&2
exit 1
