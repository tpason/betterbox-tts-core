---
name: betterbox-implementation
description: Implements non-trivial BetterBox-TTS changes with .agent/ planning, implementation notes, and Codex review handoff. Use when adding features, refactoring pipeline code, multi-file fixes, or when the user asks for plan-then-implement workflow aligned with Claude Code.
---

# BetterBox Implementation Workflow

Use for changes touching multiple files, DB schema, workers, or Docker services.

## Checklist

```
- [ ] Read .agent/PROJECT_CONTEXT.md and .agent/STATUS.md
- [ ] Write/update .agent/PLAN.md (scope, files, risks, verification)
- [ ] **Plan review:** `bash scripts/request_codex_review.sh --plan` (skip for trivial single-file fixes)
- [ ] Implement minimal correct diff
- [ ] Run verification from plan
- [ ] Write .agent/CLAUDE_IMPLEMENTATION.md (status: ready-for-review)
- [ ] **Implement review:** `bash scripts/request_codex_review.sh` — agent MUST run this command
- [ ] Fix critical/major findings; update .agent/STATUS.md
```

## Step 1 — Plan (`.agent/PLAN.md`)

Include: goal, non-goals, files to change, dependency order, verification commands, open questions.

For small single-file fixes, skip PLAN.md — note scope directly in CLAUDE_IMPLEMENTATION.md.

## Step 2 — Implement

- Match existing patterns in touched files
- Minimize scope — no drive-by refactors
- DB-only for text content; no new legacy txt paths
- `story_reader/` changes: `git -C story_reader ...`

## Step 3a — Plan review (before coding)

```bash
bash scripts/request_codex_review.sh --plan
```

Read `.agent/CODEX_REVIEW.md`. If `fix-required`, revise PLAN.md before Step 2.

Skip for trivial single-file fixes.

## Step 3 — Implementation notes (`.agent/CLAUDE_IMPLEMENTATION.md`)

```markdown
# Implementation — <short title>
Updated: YYYY-MM-DD
Status: ready-for-review | plan-only

## Summary
What changed and why (2-4 sentences).

## Files changed
- path/to/file.py — what changed

## Verification
Commands run and results.

## Open questions
Items for reviewer if any.
```

## Step 4 — Codex review (required)

The implementing agent **must run** (not just suggest):

```bash
bash scripts/request_codex_review.sh
```

Polls until `.agent/CODEX_REVIEW.md` updates (default 300s). Options:

```bash
bash scripts/request_codex_review.sh 600          # longer timeout
bash scripts/request_codex_review.sh --send-only  # no poll
```

Uses [ai-devkit](https://github.com/codeaholicguy/ai-devkit) `agent send` to the Codex session in this repo.

## Step 5 — Fix loop

1. Read `.agent/CODEX_REVIEW.md`
2. Fix all `critical` and `major` findings
3. Re-run verification
4. Update CLAUDE_IMPLEMENTATION.md with fix round
5. Re-request review if changes are substantial

## Templates

See [templates.md](templates.md) for PLAN.md and STATUS.md snippets.
