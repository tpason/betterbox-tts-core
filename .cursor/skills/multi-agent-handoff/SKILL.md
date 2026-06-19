---
name: multi-agent-handoff
description: Exchanges work between Cursor, Codex, and Claude Code via .agent/ files and ai-devkit agent messaging. Use when handing off implementation to review, sending context to another agent, or when Codex/Claude Code session needs sync.
---

# Multi-Agent Handoff

Shared coordination: `.agent/` (root) and `story_reader/.agent/` (reader repo).

## File roles

| File | Writer | Reader |
|---|---|---|
| `PLAN.md` | Implementer | All |
| `CLAUDE_IMPLEMENTATION.md` | Implementer | Codex, Cursor |
| `CODEX_REVIEW.md` | Reviewer | Implementer |
| `STATUS.md` | Implementer | All |
| `PROJECT_CONTEXT.md` | Any | All (session start) |

## Request Codex review (preferred)

```bash
bash scripts/request_codex_review.sh [timeout_seconds]
```

Requires: Codex running in tmux, `.agent/CLAUDE_IMPLEMENTATION.md` present.

## ai-devkit messaging

If `ai-devkit` not on PATH: `npx ai-devkit@latest agent ...`

```bash
ai-devkit agent list --json
ai-devkit agent detail --id <name> --json --tail 20
ai-devkit agent send --id codex "Review request: see .agent/CLAUDE_IMPLEMENTATION.md"
ai-devkit agent send --id <name> --wait --timeout 120000 --json "<message>"
```

Use `name` from `list --json` as `--id`. Filter by `type` (`codex`, `claude`) and `projectPath` containing `BetterBox-TTS`.

## Handoff messages

**To Codex (review):**
```
Review request: Implementation ready. Read .agent/CLAUDE_IMPLEMENTATION.md,
run git diff, write findings to .agent/CODEX_REVIEW.md, reply 'Review done'.
```

**To Claude Code (implement fix):**
```
Fix request: Codex review in .agent/CODEX_REVIEW.md. Address critical and major
findings, update CLAUDE_IMPLEMENTATION.md and STATUS.md.
```

## When Codex is unavailable

1. Use skill `betterbox-code-review` in Cursor
2. Note in `.agent/STATUS.md`: "Codex skipped — Cursor self-review"
3. User can retry `request_codex_review.sh` later

## Do not commit

`.agent/`, `.claude/`, `.codex/`, local secrets, generated audio.
