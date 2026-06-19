# BetterBox Story DB

Subproject này quản lý dữ liệu truyện/chapter cho pipeline crawl -> translate/polish -> TTS.

PostgreSQL lưu metadata, trạng thái, raw/translated/polished text và reader-formatted content.
File text trên disk chỉ còn là legacy/debug. Audio vẫn lưu trên disk vì dung lượng lớn.

## Chạy Postgres

```bash
cd /home/yuki/Desktop/python/BetterBox-TTS/story_db
docker compose up -d
```

Database mặc định:

```text
postgresql://betterbox:betterbox@127.0.0.1:54329/betterbox_story
```

Nếu muốn dùng URL khác:

```bash
export STORY_DATABASE_URL="postgresql://user:pass@host:port/db"
```

## Cài Python dependency

Từ root project:

```bash
source viterbox/venv/bin/activate
pip install -r general/requirements.txt
```

## Schema Chính

`sources`: nguồn truyện.

```text
qidian
wattpad_vn
hako
naver_series
royalroad
```

`stories`: metadata truyện, ranking, status, tổng chapter.

`chapters`: trạng thái từng chapter:

```text
is_locked
is_downloaded
is_translated
is_polished
is_audio_generated
raw_text_content
translated_text_content
polished_text_content
reader_formatted_text_content
audio_path
```

Các cột `*_text_content` là nguồn chính cho pipeline crawl/translate/polish và Story Reader.
Các cột `*_text_path` cũ vẫn có thể tồn tại để audit/backfill dữ liệu legacy, nhưng không nên thêm
flow production mới phụ thuộc vào txt files.

Backfill text content từ path legacy hiện có:

```bash
./viterbox/venv/bin/python scripts/story_pipeline/backfill_chapter_text_content.py --dry-run --limit 20
./viterbox/venv/bin/python scripts/story_pipeline/backfill_chapter_text_content.py --limit 100
```

Chạy từng batch bằng `--limit`; thêm `--overwrite` nếu muốn đọc lại file và replace content đã có.

`pipeline_runs`: log job crawl/check/translate/tts sau này.

## Import dữ liệu Qidian hiện có

Sau khi khảo sát Qidian free:

```bash
python scripts/story_pipeline/survey_qidian_free_books.py \
  --ranks free free_all free_completed \
  --limit-per-rank 20
```

Import survey vào DB:

```bash
python -m story_db.story_pipeline_db.cli import-qidian-survey \
  story_data/qidian/free_survey/books.json
```

Import catalog cụ thể:

```bash
python -m story_db.story_pipeline_db.cli import-qidian-catalog \
  story_data/qidian/catalogs/<book_id>/chapters.json
```

Sau khi download public chapters:

```bash
python scripts/story_pipeline/download_qidian_public_chapters.py \
  --manifest story_data/qidian/catalogs/<book_id>/chapters.json
```

Import download report:

```bash
python -m story_db.story_pipeline_db.cli import-qidian-download-report \
  story_data/raw_zh/<ten-truyen-qidian>/_download_report.json
```

## Discovery truyện hot

Discovery lấy metadata/ranking public rồi upsert vào `sources`/`stories`. Bước này chỉ thêm
candidate vào DB; crawler chapter sẽ xử lý sau theo từng source đã support.

```bash
./viterbox/venv/bin/python scripts/story_pipeline/discover_hot_stories.py \
  --sources truyenfull_today royalroad \
  --pages 2 \
  --limit-per-source 50
```

Nguồn production mặc định hiện là `truyenfull_today` và `royalroad`, vì hai nguồn này có list
public còn truy cập được bằng request thường và có crawler chapter public trong pipeline.
`hako`, `manhwatv`, `qidian`/site guard tương tự, và `naver_series` vẫn có thể chạy opt-in bằng
`--sources`, nhưng không nên để trong cron/Docker discovery mặc định khi chưa kiểm chứng lại
access/session.

Nguồn Hàn hiện hỗ trợ metadata top truyện từ Naver Series TOP 100 (`naver_series`), chưa hỗ trợ
tải chapter viewer public để merge tự động. Nguồn tiếng Anh hiện hỗ trợ Royal Road (`royalroad`)
và alternate-source merge/search cho `lightnovelpub`, `novelbin`, `novelhub`, `freewebnovel`.
Auto alternate search tự sinh alias tiếng Anh cơ bản từ title Việt; có thể tăng độ phủ bằng
`ALTERNATE_ALIAS_INFERENCE=both` nếu có Ollama/model local.

Nếu Qidian trả WAF/captcha cho `requests` nhưng browser vẫn access được, dùng discovery bằng
Playwright/browser profile:

```bash
./viterbox/venv/bin/python -m pip install playwright
./viterbox/venv/bin/python -m playwright install chromium

./viterbox/venv/bin/python scripts/story_pipeline/discover_qidian_playwright.py \
  --headful \
  --channel chrome \
  --pages 3 \
  --limit-per-page 40 \
  --profile-dir .browser/qidian
```

Lần đầu script sẽ mở Chrome thật nếu máy đã cài Google Chrome. Nếu Playwright không tìm thấy Chrome,
bỏ `--channel chrome` để dùng Chromium bundled, hoặc truyền `--executable-path /path/to/chrome`.
Nếu Qidian hiện captcha/login, xử lý thủ công trong browser đó;
profile `.browser/qidian` sẽ giữ session cho các lần chạy sau. Script không bypass captcha, chỉ reuse
browser session hợp lệ.

Hoặc dùng trực tiếp trong discovery tổng:

```bash
./viterbox/venv/bin/python scripts/story_pipeline/discover_hot_stories.py \
  --sources qidian hako naver_series \
  --qidian-browser \
  --qidian-headful \
  --qidian-channel chrome \
  --pages 3 \
  --limit-per-source 0 \
  --qidian-profile-dir .browser/qidian
```

Crawl metadata/catalog public cho các truyện Naver Series đã discover:

```bash
./viterbox/venv/bin/python scripts/story_pipeline/crawl_naver_series_catalog.py \
  --from-db \
  --limit-stories 50 \
  --retries 3 \
  --retry-sleep 2
```

Script Naver Series chỉ lấy metadata/catalog public và đánh dấu chapter là `metadata_only` nếu parse
được danh sách episode. Nó không bypass login/paywall và không tải nội dung từ viewer. Nếu HTML detail
không expose episode, mặc định script chỉ update metadata story; không tạo chapter giả. Chỉ dùng
`--max-placeholders` khi muốn tạo placeholder locked chapters để theo dõi tổng số chương.

Crawl chapter từ các story active trong DB:

```bash
./viterbox/venv/bin/python scripts/story_pipeline/crawl_stories_from_db.py \
  --sources truyenfull_today royalroad \
  --only-incomplete \
  --min-catalog-check-hours 6 \
  --workers 2 \
  --chapter-delay 1.5
```

Nếu một host lỗi DNS hoặc một story lỗi catalog, script sẽ log `[ERROR] story failed ...` và tiếp tục
crawl các story còn lại. Thêm `--stop-on-error` khi muốn debug và dừng ngay tại lỗi đầu tiên.

## Query trạng thái

Chapter đang lock/VIP, cần check lại:

```bash
python -m story_db.story_pipeline_db.cli pending-locked --limit 20
```

Chapter đã có text/dịch nhưng chưa tạo audio:

```bash
python -m story_db.story_pipeline_db.cli audio-pending --limit 20
```

## Merge chapter từ nguồn khác

Khi một story bị thiếu chapter ở nguồn chính, có thể crawl URL nguồn phụ rồi ghi chapter vào cùng
`story_id` bằng script:

```bash
./viterbox/venv/bin/python -m story_db.story_pipeline_db.cli find-stories \
  --title "Vĩnh Sinh Hiệp Sĩ" \
  --limit 10

./viterbox/venv/bin/python scripts/story_pipeline/crawl_story_alternate_sources.py \
  --target-story-id <story_id> \
  --alternate-url "<alternate_story_url>" \
  --from-chapter 527
```

Script hỗ trợ nguồn có crawler public hiện có như TruyenFull Today, TruyenYY, DocLN/Hako,
Wattpad VN, generic VN, Royal Road, Qidian. Raw tiếng Anh/Trung/Hàn sẽ được enqueue vào
`polish_worker` để dịch/polish tiếp.

## Cron Ý Tưởng

Cron nên chạy các bước nhỏ:

1. Survey free list Qidian.
2. Import survey/catalog vào DB.
3. Với chapter `is_locked=true`, check lại catalog/download public.
4. Chapter nào mới tải được thì dịch.
5. Chapter đã dịch nhưng chưa audio thì generate TTS.

Ví dụ crontab chạy mỗi 6 giờ:

```cron
0 */6 * * * cd /home/yuki/Desktop/python/BetterBox-TTS && /home/yuki/Desktop/python/BetterBox-TTS/viterbox/venv/bin/python scripts/story_pipeline/survey_qidian_free_books.py --ranks free free_all free_completed --limit-per-rank 20 >> story_db/logs/survey.log 2>&1
```

Nên thêm worker script riêng cho cron ở bước sau, thay vì nhét toàn bộ logic vào crontab.

## Ghi chú thiết kế

- DB không lưu full text/audio blob; chỉ lưu path.
- `is_locked` dùng để biết chapter đang VIP/locked/login/paywall.
- Khi chạy lại downloader, file đã có sẽ skip, chapter mới public sẽ được tải thêm.
- `is_audio_generated` là cờ để worker audio biết chapter nào còn pending.
