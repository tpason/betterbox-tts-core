# Claude Code / Codex Coordination

This workspace uses local shared files so Claude Code and Codex can work as separate agents without losing context.

Shared coordination directory:

- Root repo: `.agent/`
- Story Reader repo: `story_reader/.agent/`

Role split:

- Claude Code owns planning and implementation by default.
- Codex owns review by default.
- Claude Code should read Codex feedback, fix actionable issues, and update the implementation notes.

Claude Code workflow:

1. Read this file and the relevant project docs.
2. Write or update `.agent/PLAN.md` before implementation.
3. Implement the requested change in the correct repo.
4. Record changed files, verification, and open questions in `.agent/CLAUDE_IMPLEMENTATION.md`.
5. Trigger Codex review: `bash ~/scripts/request_codex_review.sh` — sends message to Codex and polls `.agent/CODEX_REVIEW.md` until updated (timeout 5 min). If Codex is not running, skip and note in STATUS.md.
6. Read `.agent/CODEX_REVIEW.md`, fix all `critical` and `major` findings, and update `.agent/STATUS.md`.

Repository boundaries:

- Root BetterBox-TTS is one Git repo.
- `story_reader/` is a separate Git repo. Use `git -C story_reader ...` for Story Reader work.
- Do not commit local coordination state, internal docs, secrets, generated models, audio, or frontend assets unless the user explicitly asks.

# BetterBox-TTS — Project Reference

Pipeline audiobook bán tự động: **crawl truyện → translate (zh/en/ko→vi) → polish → TTS audio → story reader web**.

---

## Tài liệu chi tiết

| File | Nội dung |
|---|---|
| `STORY_PIPELINE_RUNBOOK.md` | Lệnh đầy đủ cho từng bước pipeline |
| `STORY_PIPELINE_TRANSLATE_POLISH_CONTEXT.md` | Context chất lượng translate/polish, quyết định thiết kế |
| `story_reader/CLAUDE.md` | Story reader Next.js — bắt buộc đọc khi làm UI |
| `story_db/README.md` | Schema DB, import commands |
| `docker/env.example` | Tất cả env vars cho Docker |

---

## Infrastructure

### Python venv
```bash
# Luôn dùng path này để chạy scripts
viterbox/venv/bin/python scripts/story_pipeline/<script>.py
# Hoặc activate:
source viterbox/venv/bin/activate
```

### PostgreSQL
```
postgresql://betterbox:betterbox@127.0.0.1:54329/betterbox_story
```
```bash
cd story_db && docker compose up -d   # start DB
python story_db/apply_migrations.py   # migrate
python -m story_db.story_pipeline_db.cli <command>   # CLI
```

### Ollama
```
http://127.0.0.1:11434  (host)
http://host.docker.internal:11434  (Docker container)
```
```bash
OLLAMA_KEEP_ALIVE=24h OLLAMA_NUM_PARALLEL=2 OLLAMA_CONTEXT_LENGTH=4096 ollama serve
```

---

## Models

| Model | Dùng cho |
|---|---|
| `qwen3:14b` | Dịch zh/en/ko→vi, polish, char map extraction — model duy nhất |

**Workflow khuyến nghị:** dịch + polish bằng `qwen3:14b` (một model cho toàn bộ pipeline).

---

## Docker Services

Chạy toàn bộ pipeline:
```bash
docker compose up -d                    # base services (DB migrate + reader + crawlers)
docker compose --profile ai up -d       # thêm Ollama + polish-worker
docker compose --profile gpu up -d      # thêm audio-worker + audio-segment-worker
docker compose --profile audio up -d    # thêm audio-enqueuer
```

| Service | Profile | Mô tả |
|---|---|---|
| `story-db-migrate` | (base) | Chạy migrations khi start, tự stop |
| `story-reader` | (base) | Next.js app tại port 3000 |
| `story-discovery-scheduler` | (base) | Discovery truyện hot theo interval (mặc định 24h) |
| `story-crawler-scheduler` | (base) | Crawl catalog + chapter từ DB, scalable replicas |
| `story-alternate-scheduler` | (base) | Tìm nguồn phụ cho story thiếu chapter (mặc định dry-run) |
| `audio-enqueuer` | `audio` | Enqueue audio jobs định kỳ |
| `ollama` | `ai` | Ollama server (GPU) |
| `polish-worker` | `ai` | Worker translate/polish từ job queue |
| `audio-worker` | `gpu` | Worker TTS audio từ job queue (1 chapter = 1 file) |
| `audio-segment-worker` | `gpu` | Worker TTS audio theo segments, stitch thành MP3 |

### Scale crawler
```bash
CRAWLER_REPLICAS=3 docker compose up -d   # 3 replicas crawl song song
```

### Env vars quan trọng
```env
STORY_DATABASE_URL=postgresql://betterbox:betterbox@host.docker.internal:54329/betterbox_story
ALTERNATE_APPLY=0          # 1 để thật sự import từ nguồn phụ
CRAWLER_ONLY_INCOMPLETE=1  # chỉ crawl story chưa complete
AUDIO_VIENEU_VOICE_PROFILE=preset_binh_an
AUDIO_SEGMENT_VIENEU_VOICE_KEY=preset_binh_an
AUDIO_SEGMENT_VIENEU_VOICE_PROFILE=preset_binh_an
```

---

## Pipeline Scripts — `scripts/story_pipeline/`

### Crawl
| Script | Nguồn | Ghi chú |
|---|---|---|
| `crawl_stories_from_db.py` | Tất cả | Crawl tất cả story active trong DB |
| `crawl_hako_chapters.py` | Hako | Rate-limit, thêm `--delay 6` |
| `crawl_truyenfull_today_chapters.py` | TruyenFull Today | |
| `crawl_wattpad_chapters.py` | Wattpad VN | |
| `crawl_royalroad_chapters.py` | Royal Road | |
| `crawl_lightnovelpub_chapters.py` | LightNovelPub | |
| `crawl_qidian_catalog.py` | Qidian | Cần browser profile nếu WAF |
| `crawl_docln_chapters.py` | DocLN | |
| `crawl_skydemonorder_chapters.py` | SkyDemonOrder | |
| `crawl_story_alternate_sources.py` | Nguồn phụ | Merge vào story_id chính |
| `auto_crawl_alternate_sources.py` | Multi | Tự tìm nguồn phụ cho story thiếu chapter |
| `discover_hot_stories.py` | Multi | Discovery candidate, ghi DB |

### Download raw text
| Script | Mô tả |
|---|---|
| `download_hako_chapter_texts.py` | Download raw từ Hako manifest |
| `download_chapter_texts.py` | Wattpad/TruyenFull (file-based) |
| `download_qidian_public_chapters.py` | Chapter free Qidian |

**Pattern production:** `--emit-polish-job` để enqueue vào DB queue thay vì block.

### Translate / Polish
| Script | Mô tả |
|---|---|
| `translate_chapter_texts_ollama.py` | Dịch file-based |
| `translate_chapters_from_db.py` | Dịch non-VI raw từ DB; `--post-translate queue` enqueue polish job |
| `polish_chapter_texts_ollama.py` | Polish file-based |
| `polish_worker.py` | Worker từ DB queue; VI polish trực tiếp, non-VI translate rồi polish |
| `repolish_story_from_db.py` | Re-polish trực tiếp từ DB, không qua queue |
| `reformat_polished_chapter_content.py` | Repair noise mà không gọi LLM |
| `polish_story_titles_ollama.py` | Polish tên truyện |

**Prompt profiles:** `--prompt-profile full` (default, chất lượng cao) | `--prompt-profile fast` | `--polish-mode clean` (không LLM).

**Default hiện tại:** DB-only, không ghi txt files cho translate/polish bình thường. Không cần truyền `--no-save-files`; cũng không pin `--genre` trừ khi debug override vì genre tự suy ra từ DB metadata/source/language/char map.

### Char Map
| Script | Khi nào dùng |
|---|---|
| `build_char_map_from_story.py` | Build mới hoặc rebuild toàn bộ (two-pass: regex scan + LLM batch) |
| `extract_char_map.py` | Sampling 30 chapters, append định kỳ |

**Char map sống trong DB** (từ 2026-06-17): `stories.metadata.char_map_content` (JSONB field).
`extract_char_map.py` đọc/ghi `char_map_content` qua `repo.update_story_metadata()` — không còn ghi file.

`polish_worker` và `repolish_story_from_db` tự trigger `extract_char_map.py` khi map stale (mỗi 150 chapters).

### Audio
| Script | Mô tả |
|---|---|
| `generate_chapter_audio_vieneu.py` | Generate VieNeu-TTS v3 audio chapter (file-based/debug) |
| `audio_worker_vieneu.py` | VieNeu worker audio từ DB, job type `audio_chapter` |
| `audio_segment_worker_vieneu.py` | VieNeu worker audio theo segments, stitch MP3, job type `audio_chapter_segments` |
| `vieneu_voice_profiles.py` | Curated reference voices cho audiobook |
| `vieneu_audiobook_stitch.py` | Text splitting, generation loop, silence/crossfade/stitch |
| `preview_text_viterbox.py` | Legacy Viterbox preview fallback |
| `generate_chapter_audio_viterbox.py` | Legacy Viterbox chapter generation |
| `audio_worker_viterbox.py` | Legacy Viterbox DB worker |
| `audio_segment_worker_viterbox.py` | Legacy Viterbox segment worker |
| `merge_chapter_audio.py` | Merge nhiều chapter WAV thành 1 file |
| `enqueue_audio_jobs_from_db.py` | Enqueue audio jobs thủ công theo story/chapter range |

**Default voice:** `preset_binh_an` -> `Bình An` (VieNeu v3 built-in preset).
`torchcodec==0.10.0` is pinned for current `torch==2.10.0+cu128` compatibility.

**Lưu ý:** Audio KHÔNG tự enqueue sau polish để tránh đầy disk. Enqueue thủ công khi cần.

### Backfill / Utility
| Script | Mô tả |
|---|---|
| `backfill_chapter_text_content.py` | Backfill `*_text_content` DB columns từ file paths |
| `backfill_story_covers.py` | Backfill cover images |
| `backfill_story_descriptions_ollama.py` | Generate description bằng LLM |
| `update_story_authors.py` | Cập nhật author còn thiếu |
| `update_chapter_outputs_from_files.py` | Sync file disk → DB columns |
| `reader_content_format.py` | Format polished text sang reader format (paragraph segments) |

---

## Story Data Paths

**DB-only mode (từ 2026-06-17):** Scripts không ghi txt files nữa. Mọi content sống trong DB.

```
story_data/
  text/<slug>/chapter0001.txt          # raw Vietnamese (LEGACY — không ghi mới, cần sudo rm nếu còn)
  raw_zh/<slug>/chapter1.txt           # raw Chinese — LEGACY
  raw_en/<slug>/chapter1.txt           # raw English — LEGACY
  translated/<slug>/chapter1.txt       # translated → vi — LEGACY
  polished/<slug>/chapter0001.txt      # polished — LEGACY
  char_maps/{story_id}-{slug}.txt      # char map — LEGACY (nay trong stories.metadata.char_map_content)
  hako/<slug>/chapters.json            # Hako catalog (JSON, giữ nguyên)
  qidian/catalogs/<book_id>/           # Qidian catalog (JSON, giữ nguyên)
  discovery/hot_stories_*.json         # discovery output (JSON, giữ nguyên)

story_audio/<slug>/chapter0001.wav
story_audio_segments/<slug>/           # segment files trước khi stitch
story_audio_merged/<slug>_all.wav
```

**Xóa LEGACY dirs (cần sudo vì Docker tạo với owner root):**
```bash
sudo rm -rf story_data/raw_en story_data/translated story_data/polished story_data/char_maps story_data/polish_preview
```

---

## Database Schema (key columns)

### `stories`
`id`, `title`, `slug`, `source_code`, `story_url`, `cover_image_url`, `total_chapters`, `is_completed`, `rank_position`, `primary_category_id`, `metadata` (JSONB — **char_map_content** (DB-only từ 2026-06-17), char_map_updated_at, char_map_updated_to_chapter, auto_alternate_*)

### `chapters`
`story_id`, `chapter_number`, `is_locked`, `is_downloaded`, `is_translated`, `is_polished`, `is_audio_generated`, `raw_text_content`, `translated_text_content`, `polished_text_content`, `reader_formatted_text_content`, `reader_formatted_content_version`, `audio_path`

Cột **`raw_text_content`/`translated_text_content`/`polished_text_content`** = source of truth.
Cột `raw_text_path`/`translated_text_path`/`polished_text_path` = LEGACY, không còn được set bởi scripts.

### `story_jobs`
Job queue: `job_type` (polish_chapter | translate_chapter | audio_chapter | audio_chapter_segments), `status` (pending | running | done | failed), `chapter_id`, `story_id`.

### CLI quick checks
```bash
python -m story_db.story_pipeline_db.cli pending-locked --limit 20
python -m story_db.story_pipeline_db.cli audio-pending --limit 20
python -m story_db.story_pipeline_db.cli jobs --status pending --limit 20
python -m story_db.story_pipeline_db.cli stories --only-incomplete --limit 20
python -m story_db.story_pipeline_db.cli categories --limit 50
python -m story_db.story_pipeline_db.cli find-stories --title "Vĩnh Thoái Hiệp Sĩ"
```

---

## Genre System (10 genres)

Defined in `scripts/story_pipeline/genre_prompts.py`. Auto-detected từ char map bằng `infer_genre_from_char_map()`.

| Genre | Đặc điểm |
|---|---|
| `tien_hiep` | Hán Việt trang nghiêm |
| `huyen_huyen` | Hán Việt vừa phải |
| `he_thong` | LitRPG, bảng UI game |
| `kiem_hiep` | Wuxia |
| `do_thi` | Đô thị/hiện đại, không Hán Việt cổ |
| `xuyen_khong` | Isekai/xuyên không |
| `mat_the` | Apocalypse, câu ngắn căng thẳng |
| `vong_du` | VRMMO |
| `lang_man` | Romance |
| `western_fantasy` | Korean LN / fantasy phương Tây — **không dùng hắn/nàng/lão/y** |

---

## Quality System (translate/polish)

- **Fallback ratio:** `min_output_ratio = 0.70` — nếu output < 70% input → fallback clean-only chunk
- **Context window:** 600 chars preceding context qua chunk boundaries
- **Char map injection:** 2 blocks rõ ràng (story voice rules + per-character rules)
- **TTS cleanup:** `clean_for_audiobook_tts()` — xóa separator, chuẩn hóa quote/ellipsis, tách âm báo

---

## Key Story: Vĩnh Thoái Hiệp Sĩ

- `story_id: 21180`, slug: `21180-vinh-thoai-hiep-si`
- Genre: `western_fantasy` (Korean LN)
- Nguồn chính: Hako (`https://ln.hako.vn/truyen/21180-vinh-thoai-hiep-si`)
- Char map: trong DB — `stories.metadata.char_map_content` (story_id=21180)
- Nhân vật chính: Enkrid (nam, "anh ta"), Shinar (nữ tiên tộc, "cô ta"), Luagarne, Rem, Krais
- Alias: Encrid → Enkrid, Enkrido → Enkrid
- Quy tắc: không Hán Việt cổ phong, không "hắn/nàng/lão/y"

Repolish từ Docker:
```bash
docker compose exec story-crawler-scheduler \
  python /app/scripts/story_pipeline/repolish_story_from_db.py \
  --story-title "Vĩnh Thoái Hiệp Sĩ" \
  --ollama-url http://host.docker.internal:11434 \
  --overwrite --from-chapter 543
```

---

## Story Reader (Next.js)

**Đọc `story_reader/CLAUDE.md` trước mọi thay đổi UI.**

- Framework: Next.js 15 + TypeScript + React 19
- Styling: single `app/globals.css` (~13k+ lines), append-only cascade
- WebGL: Three.js (dynamic import, gated by battery/perf)
- DB: PostgreSQL qua `lib/stories.ts`
- Port: 3000 (Docker: `story-reader` service)

### API routes
```
GET /api/stories               # list stories (mặc định minChapters=1)
GET /api/stories/[storyId]     # story detail
GET /api/stories/[storyId]/chapters
GET /api/chapters/[chapterId]/audio
GET /api/reader/sessions
GET /api/categories
```

### Xianxia vocabulary
| Plain | Xianxia |
|---|---|
| database/server | Thiên Thư |
| sync/save | khắc / lưu vào Thiên Thư |
| signup | Nhập môn |
| profile/account | Động phủ |
| library | Linh Quyển Đại Thư |
| reading history | Tàng thư |

---

## Crawl Sources

| Source | Ngôn ngữ | Notes |
|---|---|---|
| `truyenfull_today` | vi | Production default |
| `truyenyy` | vi | |
| `docln` | vi | |
| `sttruyen`, `truyenchuhay`, `truyenhoangdung` | vi | |
| `wattpad_vn` | vi | |
| `royalroad` | en | Production default |
| `lightnovelpub` | en | Alternate source |
| `novelbin`, `freewebnovel`, `novelhub` | en | Flaky search, dùng khi có URL |
| `hako` | vi/ja | Rate-limit; không để trong Docker default |
| `qidian` | zh | WAF/captcha — dùng browser profile |
| `naver_series` | ko | Chỉ metadata catalog, không tải chapter viewer |
| `skydemonorder` | en | |

**Nguồn bị disable trong Docker mặc định:** `hako`, `manhwatv`, `qidian`, `naver_series`.
