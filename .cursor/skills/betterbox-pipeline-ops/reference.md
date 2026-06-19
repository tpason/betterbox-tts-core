# Pipeline Reference (condensed)

## Crawl scripts

| Script | Source |
|---|---|
| `crawl_stories_from_db.py` | All active stories |
| `crawl_truyenfull_today_chapters.py` | TruyenFull Today |
| `crawl_royalroad_chapters.py` | Royal Road |
| `crawl_skydemonorder_chapters.py` | SkyDemonOrder |
| `download_qidian_public_chapters.py` | Qidian free chapters |

## Translate

```bash
viterbox/venv/bin/python scripts/story_pipeline/translate_chapters_from_db.py \
  --post-translate queue
```

## Char map

```bash
viterbox/venv/bin/python scripts/story_pipeline/build_char_map_from_story.py --story-id 21180
viterbox/venv/bin/python scripts/story_pipeline/extract_char_map.py --story-id 21180
```

## Voice survey

```bash
viterbox/venv/bin/python scripts/story_pipeline/survey_vieneu_voices.py --top 12
bash scripts/story_pipeline/preview_voices_xianxia.sh
```

## Unified wetriedtls pipeline

```bash
viterbox/venv/bin/python scripts/story_pipeline/wetriedtls_pipeline.py \
  --story-title "A Regressor's Tale" --device cuda
```

## Key story reference

- Vĩnh Thoái Hiệp Sĩ: `story_id=21180`, genre `western_fantasy`, slug `21180-vinh-thoai-hiep-si`

## Genre system

10 genres in `scripts/story_pipeline/genre_prompts.py`. Auto-inferred — do not pin `--genre` in production.
