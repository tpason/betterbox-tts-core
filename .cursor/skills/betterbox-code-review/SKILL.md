---
name: betterbox-code-review
description: Reviews BetterBox-TTS code changes for correctness, DB safety, queue idempotency, and regressions. Writes findings to .agent/CODEX_REVIEW.md. Use when user asks for review, before merge, when substituting for Codex, or after receiving "Review request".
---

# BetterBox Code Review

Aligned with `AGENTS.md` and Codex workflow.

## Review process

1. Read `.agent/CLAUDE_IMPLEMENTATION.md` (or infer from user request)
2. Read `.agent/PROJECT_CONTEXT.md` if scope is unclear
3. Run `git status` and `git diff` (use `git -C story_reader` for reader changes)
4. Prioritize: bugs, data safety, queue/DB edge cases, regressions
5. Write `.agent/CODEX_REVIEW.md`
6. Reply: `Review done. See .agent/CODEX_REVIEW.md`

## Output format

```markdown
# Codex Review
Owner: Cursor
Status: pending-fixes | approved
Updated: YYYY-MM-DD

## Scope
<1-2 sentences>

## Findings

### 1. <Title>
Severity: critical | major | minor
File: path:line
Issue: ...
Fix: ...

## Verdict
approved | fix-required
```

Use `approved` when no critical/major findings.

## Review priorities (this project)

### Correctness
- Job queue idempotency (`story_jobs`, segment enqueue ON CONFLICT)
- Text hash guards on audio re-stitch
- Translate/polish fallback ratio (0.70 min output)

### Data safety
- No accidental overwrite of polished content without `--overwrite`
- Char map updates via `update_story_metadata()`, not legacy files
- Migrations backward-compatible

### Architecture
- DB-only text path for production
- VieNeu v3 as primary audio; Viterbox only as explicit legacy
- Repository layer for DB access, not ad-hoc SQL in scripts

### Performance / ops
- GPU workers: local vs Docker assumptions
- Audio not auto-enqueued (disk protection)
- Crawler rate limits (Hako especially)

## Severity guide

| Level | When |
|---|---|
| critical | Data loss, wrong content shipped, security, broken production path |
| major | Bug in common path, missing idempotency, test gap on risky logic |
| minor | Style, naming, optional hardening |

## Cursor-native alternatives

For diff-only review without `.agent/` handoff, user may invoke built-in **Bugbot** or **Security Review** skills — still write to CODEX_REVIEW.md if coordinating with Claude Code.
