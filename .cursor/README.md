# Cursor Agent Setup — BetterBox-TTS

Cursor dùng chung workflow với Claude Code và Codex qua `.agent/` (local, gitignored).

## Cấu trúc

```
.cursor/
  README.md              ← file này
  rules/                 ← context tự động (alwaysApply hoặc glob)
  skills/                ← BetterBox project skills (commit được)
.agents/skills/          ← third-party skills (gitignored, cài bằng script)
.agent/                  ← handoff Codex/Claude (gitignored)
```

## ai-devkit ([codeaholicguy/ai-devkit](https://github.com/codeaholicguy/ai-devkit))

Control plane cho multi-agent. Config: `.ai-devkit.json`

```bash
bash scripts/install-cursor-skills.sh   # full bootstrap
npx ai-devkit@latest install --overwrite  # chỉ ai-devkit skills
ai-devkit agent list --json               # Codex phải running trong repo này
```

**Skills ai-devkit (symlink `.cursor/skills/`):** `agent-communication`, `agent-orchestration`, `dev-lifecycle`, `dev-review`, `verify`, `memory`, `tdd`, `structured-debug`, `security-review`, `simplify-implementation`

**Codex review gate (bắt buộc sau plan/implement):**

```bash
bash scripts/request_codex_review.sh --plan      # sau PLAN.md
bash scripts/request_codex_review.sh             # sau CLAUDE_IMPLEMENTATION.md
```

Rule `codex-review-gate.mdc` — Cursor **phải chạy** script trên, không chỉ gợi ý user.

## Tự động hay manual?

| Layer | Cơ chế | Manual? |
|---|---|---|
| **Rules** `.cursor/rules/*.mdc` | Inject vào mọi prompt (`alwaysApply`) hoặc khi mở file khớp `globs` | Không |
| **Skills** `.agents/skills/*/SKILL.md` | Cursor index `description` → agent **tự chọn** skill khớp task → đọc full `SKILL.md` | Không (trừ skill có `disable-model-invocation: true`) |
| **Rule `skill-auto-routing.mdc`** | Bảng routing: task nào → skill nào | Không |

**Quan trọng:** Cursor **không** inject cả 47 skill vào prompt (tràn context). Chỉ inject metadata + rules; agent **tự đọc** skill body khi match.

Skills third-party **không** có `disable-model-invocation` → eligible auto-invoke. Rule `skill-auto-routing.mdc` nhắc agent không chờ user gọi tên skill.

## Cài / cập nhật skills bên thứ ba

```bash
bash scripts/install-cursor-skills.sh
```

Hoặc từng repo:

```bash
npx skills add https://github.com/Leonxlnx/taste-skill
npx skills add https://github.com/addyosmani/agent-skills
npx skills add https://github.com/DietrichGebert/ponytail
```

---

## Rules (luôn load hoặc theo glob)

| Rule | Scope | Nguồn |
|---|---|---|
| `agent-coordination.mdc` | always | BetterBox |
| `betterbox-orientation.mdc` | always | BetterBox |
| `skill-auto-routing.mdc` | always | Auto-pick skills |
| `codex-review-gate.mdc` | always | **Bắt buộc** gửi Codex review qua ai-devkit |
| `ponytail.mdc` | always | [ponytail](https://github.com/DietrichGebert/ponytail) |
| `story-pipeline-python.mdc` | `scripts/story_pipeline/**` | BetterBox |
| `story-reader-nextjs.mdc` | `story_reader/**` | BetterBox |
| `docker-services.mdc` | `docker-compose.yml` | BetterBox |

---

## Skills inventory (47 total trong `.agents/skills/`)

### BetterBox (project — `.cursor/skills/`, commit được)

| Skill | Khi nào |
|---|---|
| `betterbox-implementation` | Feature/fix nhiều file + handoff Codex |
| `betterbox-pipeline-ops` | Crawl, polish, TTS, enqueue audio |
| `betterbox-code-review` | Review thay Codex |
| `multi-agent-handoff` | Giao việc qua ai-devkit |

### [ponytail](https://github.com/DietrichGebert/ponytail) — YAGNI, ít code hơn

| Skill | Khi nào |
|---|---|
| `ponytail` | Always-on (rule) — thang ưu tiên stdlib → native → deps → 1 dòng |
| `ponytail-review` | Review diff, tìm over-engineering |
| `ponytail-audit` | Audit toàn repo |
| `ponytail-debt` | Ghi nợ kỹ thuật từ shortcut `ponytail:` |
| `ponytail-gain` | Benchmark impact |
| `ponytail-help` | Quick reference |

### [addyosmani/agent-skills](https://github.com/addyosmani/agent-skills) — engineering lifecycle

**Define:** `interview-me`, `idea-refine`, `spec-driven-development`

**Plan:** `planning-and-task-breakdown`

**Build:** `incremental-implementation`, `test-driven-development`, `context-engineering`, `source-driven-development`, `doubt-driven-development`, `frontend-ui-engineering`, `api-and-interface-design`

**Verify:** `browser-testing-with-devtools`, `debugging-and-error-recovery`

**Review:** `code-review-and-quality`, `code-simplification`, `security-and-hardening`, `performance-optimization`

**Ship:** `git-workflow-and-versioning`, `ci-cd-and-automation`, `deprecation-and-migration`, `documentation-and-adrs`, `observability-and-instrumentation`, `shipping-and-launch`

**Meta:** `using-agent-skills`

### [taste-skill](https://github.com/Leonxlnx/taste-skill) — anti-slop UI

| Skill | Khi nào |
|---|---|
| `design-taste-frontend` | Default UI (v2 experimental) |
| `design-taste-frontend-v1` | Pin v1 nếu cần |
| `gpt-taste` | Variant strict cho GPT/Codex |
| `redesign-existing-projects` | Audit + fix UI hiện có |
| `image-to-code` | Image → analyze → code |
| `high-end-visual-design` | Soft/premium UI |
| `minimalist-ui` | Editorial (Notion/Linear) |
| `industrial-brutalist-ui` | Brutalist/mechanical |
| `stitch-design-taste` | Google Stitch + DESIGN.md |
| `full-output-enforcement` | Agent cắt output giữa chừng |
| `imagegen-frontend-web` | Generate web comps (images only) |
| `imagegen-frontend-mobile` | Generate mobile comps |
| `brandkit` | Brand kit boards |

---

## Gợi ý kết hợp cho BetterBox

| Task | Skills / rules |
|---|---|
| Pipeline Python | `betterbox-pipeline-ops` + `story-pipeline-python` rule |
| Story reader UI | `design-taste-frontend` + `story-reader-nextjs` rule + `DESIGN.md` |
| Refactor gọn code | `ponytail` rule + `code-simplification` |
| Feature lớn | `betterbox-implementation` + `incremental-implementation` + `test-driven-development` |
| Review trước merge | `betterbox-code-review` hoặc `ponytail-review` |

**Lưu ý:** Không cần gọi tên skill trong prompt — agent tự route qua `skill-auto-routing.mdc`. Chỉ gọi tên khi muốn **ép** một skill cụ thể (override routing).

---

## Role split (3 agents)

| Agent | Default |
|---|---|
| Claude Code | Plan + implement |
| Codex | Review |
| Cursor | Implement hoặc review; sync qua `.agent/` |

## Session start

1. `.agent/PROJECT_CONTEXT.md` + `.agent/STATUS.md`
2. Thay đổi lớn → `.agent/PLAN.md` → `bash scripts/request_codex_review.sh --plan`
3. Implement → `.agent/CLAUDE_IMPLEMENTATION.md` → `bash scripts/request_codex_review.sh`

## Commit policy

- Commit: `.cursor/rules/`, `.cursor/skills/`, `scripts/install-cursor-skills.sh`
- Không commit: `.agents/`, `.agent/`, secrets, audio
