#!/usr/bin/env bash
# Bootstrap ai-devkit + third-party skills for BetterBox-TTS Cursor workflow.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

echo "[1/4] ai-devkit install from .ai-devkit.json..."
npx ai-devkit@latest install --overwrite

echo "[2/4] taste-skill..."
npx skills add https://github.com/Leonxlnx/taste-skill

echo "[3/4] addyosmani/agent-skills + ponytail..."
npx skills add https://github.com/addyosmani/agent-skills
npx skills add https://github.com/DietrichGebert/ponytail

echo "[4/4] link BetterBox project skills..."
mkdir -p .agents/skills
for skill in betterbox-implementation betterbox-pipeline-ops betterbox-code-review multi-agent-handoff; do
  src="$REPO_ROOT/.cursor/skills/$skill"
  dst="$REPO_ROOT/.agents/skills/$skill"
  if [[ -d "$src" && ! -e "$dst" ]]; then
    ln -s "$src" "$dst"
    echo "  linked $skill"
  fi
done

chmod +x "$REPO_ROOT/scripts/request_codex_review.sh"

echo ""
echo "Done."
echo "  ai-devkit: .ai-devkit.json + docs/ai/"
echo "  Codex review: bash scripts/request_codex_review.sh [--plan]"
echo "  Verify Codex: ai-devkit agent list --json"
