---
name: betterbox-pipeline-ops
description: Runs BetterBox story pipeline operations — crawl, translate, polish, char map, VieNeu TTS audio, enqueue jobs, Docker profiles. Use when the user asks to run pipeline steps, debug workers, enqueue audio, repolish stories, or operate crawl/translate/TTS workflows.
---

# BetterBox Pipeline Operations

Full runbook: `STORY_PIPELINE_RUNBOOK.md`. Context: `.agent/PROJECT_CONTEXT.md`.

## Environment

```bash
viterbox/venv/bin/python scripts/story_pipeline/<script>.py
# Ollama: http://127.0.0.1:11434
# DB: postgresql://betterbox:betterbox@127.0.0.1:54329/betterbox_story
```

## Common flows

### Polish worker (DB queue)

```bash
viterbox/venv/bin/python scripts/story_pipeline/polish_worker.py \
  --once --workers 1 --batch-size 1 \
  --vi-model qwen3:14b --post-translate polish
```

Do not pass `--genre` or `--no-save-files` for normal runs.

### Re-polish one story

```bash
viterbox/venv/bin/python scripts/story_pipeline/repolish_story_from_db.py \
  --story-title "Vĩnh Thoái Hiệp Sĩ" \
  --ollama-url http://127.0.0.1:11434 \
  --overwrite --from-chapter 543
```

### Enqueue audio (segment mode)

```bash
viterbox/venv/bin/python scripts/story_pipeline/enqueue_audio_jobs_from_db.py \
  --segment --story-title "Vĩnh Thoái Hiệp Sĩ"
```

### Audio segment worker (local GPU)

```bash
viterbox/venv/bin/python scripts/story_pipeline/audio_segment_worker_vieneu.py \
  --device cuda --output-root story_audio_segments \
  --voice-profile preset_binh_an
```

### VieNeu smoke test

```bash
viterbox/venv/bin/python scripts/story_pipeline/generate_chapter_audio_vieneu.py \
  --input-dir /tmp/betterbox-vieneu-smoke-input \
  --output-root /tmp/betterbox-vieneu-smoke-output \
  --chapter 1 --overwrite --voice-profile preset_binh_an
```

## DB CLI quick checks

```bash
python -m story_db.story_pipeline_db.cli pending-locked --limit 20
python -m story_db.story_pipeline_db.cli audio-pending --limit 20
python -m story_db.story_pipeline_db.cli jobs --status pending --limit 20
python -m story_db.story_pipeline_db.cli find-stories --title "Vĩnh Thoái Hiệp Sĩ"
```

## Docker (when not using local GPU)

```bash
docker compose up -d
docker compose --profile ai up -d
docker compose --profile gpu up -d
```

Polish/translate + CUDA audio: prefer **local terminal**, not Docker (see `.agent/STATUS.md`).

## Defaults to remember

| Setting | Value |
|---|---|
| Text storage | DB columns only |
| Char map | `stories.metadata.char_map_content` |
| TTS engine | VieNeu v3 |
| Default voice | `preset_binh_an` |
| Auto audio enqueue | Off |

More commands: [reference.md](reference.md)
