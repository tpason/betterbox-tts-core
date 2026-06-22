# Story Audio Pipeline cho BetterBox TTS

Bộ script này vận hành pipeline tạo audiobook từ nguồn truyện public/được phép dùng sang nội dung đọc trong Story Reader và audio.

Luồng production hiện tại:

1. Discover/crawl metadata và chapter từ DB/source adapters.
2. Lưu raw text vào PostgreSQL (`chapters.raw_text_content`).
3. Dịch/polish bằng Ollama qua `story_jobs` queue.
4. Lưu translated/polished/reader-formatted content trong DB.
5. Sinh audio bằng VieNeu-TTS v3.
6. Story Reader đọc DB + audio segment metadata.

Các script file-based vẫn còn để debug/legacy, nhưng không phải flow mặc định.

## 0. Chuẩn bị

Chạy trong root project BetterBox-TTS:

```bash
cd /home/yuki/Desktop/python/BetterBox-TTS
source viterbox/venv/bin/activate
pip install -r general/requirements.txt
```

Audio mặc định hiện dùng VieNeu-TTS v3 với voice profile:

```text
preset_binh_an -> Bình An (VieNeu v3 built-in preset)
```

Nếu muốn polish text bằng AI local, cài Ollama và pull model:

```bash
ollama pull qwen3:14b
ollama pull translategemma:12b
```

Model fallback:

```bash
ollama pull qwen2.5:14b-instruct
ollama pull qwen2.5:7b-instruct
```

Theo Ollama Library, `qwen3:14b` khoảng 9.3 GB và `translategemma:12b` khoảng 8.1 GB. `translategemma` là model chuyên dịch, còn Qwen thường hợp hơn cho rewrite/polish văn phong truyện.

## 1. Crawl danh sách chương

Ví dụ với URL truyện:

```bash
python scripts/story_pipeline/crawl_wattpad_chapters.py \
  "https://wattpad.com.vn/cau-tha-thanh-thanh-nhan-tien-quan-trieu-ta-cham-ngua"
```

Output:

```text
story_data/cau-tha-thanh-thanh-nhan-tien-quan-trieu-ta-cham-ngua/chapters.json
```

File này chứa `story_url`, tổng số chương và danh sách URL từng chương.

## 2. Tải text từng chương

Tải toàn bộ:

```bash
python scripts/story_pipeline/download_chapter_texts.py \
  --manifest story_data/cau-tha-thanh-thanh-nhan-tien-quan-trieu-ta-cham-ngua/chapters.json \
  --output-root story_data/text
```

Tải thử 3 chương đầu:

```bash
python scripts/story_pipeline/download_chapter_texts.py \
  --manifest story_data/cau-tha-thanh-thanh-nhan-tien-quan-trieu-ta-cham-ngua/chapters.json \
  --output-root story_data/text \
  --limit 3
```

Output sẽ có dạng:

```text
story_data/text/<slug-truyen>/chapter1.txt
story_data/text/<slug-truyen>/chapter2.txt
...
```

## 3. Crawl truyện hot từ Qidian

Script Qidian hiện chỉ crawl metadata/bảng xếp hạng công khai, không tải nội dung chương và không bypass paywall.

Ví dụ crawl bảng bán chạy:

```bash
python scripts/story_pipeline/crawl_qidian_rankings.py \
  --rank hotsales \
  --limit 30
```

Output:

```text
story_data/qidian/hotsales/books.json
```

Các rank có sẵn:

```text
free
free_all
free_completed
hotsales
readindex
yuepiao
recom
```

Ví dụ crawl bảng đọc nhiều:

```bash
python scripts/story_pipeline/crawl_qidian_rankings.py \
  --rank readindex \
  --limit 30
```

Sau khi chọn truyện, hãy dùng nội dung text mà bạn có quyền sử dụng, lưu thành:

```text
story_data/raw_zh/<slug-truyen>/chapter1.txt
story_data/raw_zh/<slug-truyen>/chapter2.txt
```

### Tìm truyện hệ thống/phản phái/thiên mệnh chi tử

Script này chỉ crawl metadata công khai để lọc truyện hợp vibe: main có hệ thống,
phản phái hệ thống, tà đạo/ma đạo, main không hệ thống chống khí vận chi tử hoặc
thiên mệnh chi tử. Script cũng có seed cho một số bộ bạn đang thích như
`Ta! Thiên Mệnh Đại Nhân Vật Phản Phái`, `Toàn Trí Độc Giả`, `Re:Monster`,
`Hoa Sơn Tái Khởi`, `Thiên Ma Phi Thăng Truyện`.
Không tải nội dung chương.

```bash
python scripts/story_pipeline/discover_system_villain_stories.py \
  --max-stories 120 \
  --output story_data/discovery/system_villain_stories.json
```

Thêm seed URL riêng:

```bash
python scripts/story_pipeline/discover_system_villain_stories.py \
  --extra-url "https://example.com/truyen-hop-vibe" \
  --output story_data/discovery/system_villain_stories.json
```

Nếu muốn lưu metadata vào Postgres story DB:

```bash
python scripts/story_pipeline/discover_system_villain_stories.py --upsert-db
```

Khi upsert DB, script chỉ ghi các source mà `crawl_stories_from_db.py` hỗ trợ
để tránh tạo story không crawl tiếp được. Hiện có `truyenyy`, `hako`,
`docln`, `manhwatv`, `sttruyen`, `truyenchuhay`, `truyenhoangdung`,
`wattpad_vn`, `truyenfull_today`, `qidian`, `royalroad`.

Luồng production tự động discovery rồi crawl toàn bộ story URL đã tìm được,
không cần nhập `--title-contains`:

```bash
python scripts/story_pipeline/run_system_villain_story_pipeline.py \
  --max-stories 160 \
  --workers 2 \
  --chapter-delay 1.5
```

Mặc định production chỉ upsert/crawl source có khả năng lấy chapter chữ public và không bị chặn
ổn định trong môi trường hiện tại. Docker discovery mặc định chỉ dùng `truyenfull_today` và
`royalroad`; Docker crawler dùng allowlist source ổn định, không claim `hako`, `manhwatv`,
`qidian`/site guard tương tự. Các source này vẫn có thể chạy thủ công bằng `--source` sau khi
kiểm chứng access.

### Crawl LightNovelPub

Adapter LightNovelPub hỗ trợ trang ranking/popular và URL truyện dạng
`https://lightnovelpub.org/novel/<slug>/`. Catalog chapter được lấy từ trang
`/chapters/`, không đoán URL chương.

Crawl catalog cho 6 truyện mặc định:

```bash
python scripts/story_pipeline/crawl_lightnovelpub_chapters.py \
  --use-default-stories
```

Lấy story từ ranking hoặc popular rồi lưu metadata vào DB:

```bash
python scripts/story_pipeline/crawl_lightnovelpub_chapters.py \
  --discover-url "https://lightnovelpub.org/ranking/" \
  --discover-url "https://lightnovelpub.org/genre-all/?order=popular" \
  --discover-limit 20 \
  --upsert-db
```

Upsert riêng các URL bạn chọn:

```bash
python scripts/story_pipeline/crawl_lightnovelpub_chapters.py \
  "https://lightnovelpub.org/novel/shadow-slave/" \
  "https://lightnovelpub.org/novel/reverend-insanity/" \
  "https://lightnovelpub.org/novel/lord-of-the-mysteries/" \
  "https://lightnovelpub.org/novel/genetic-ascension/" \
  "https://lightnovelpub.org/novel/a-regressors-tale-of-cultivation/" \
  "https://lightnovelpub.org/novel/the-authors-pov/" \
  --upsert-db
```

Sau khi có story trong DB, tải chapter text tiếng Anh và enqueue translate/polish:

```bash
python scripts/story_pipeline/crawl_stories_from_db.py \
  --sources lightnovelpub \
  --story-url "https://lightnovelpub.org/novel/shadow-slave/" \
  --max-chapters 3
```

Chạy thử ít chương:

```bash
python scripts/story_pipeline/run_system_villain_story_pipeline.py \
  --max-crawl-stories 10 \
  --max-chapters 3
```

Nếu chỉ muốn crawl lại từ file discovery đã có:

```bash
python scripts/story_pipeline/run_system_villain_story_pipeline.py \
  --crawl-only \
  --output story_data/discovery/system_villain_stories.json
```

Lưu ý: `manhwatv` có thể chỉ sync được catalog/locked chapter vì nhiều chương
yêu cầu đăng nhập hoặc dùng ảnh thay vì text. Crawler không bypass login và chỉ
lưu nội dung chữ public. `sttruyen`/`truyenchuhay`/`truyenhoangdung` dùng adapter
HTML generic nên nên test vài chương bằng `--max-chapters` trước khi chạy rộng.

### Lấy catalog chapter Qidian

Sau khi mở `story_data/qidian/hotsales/books.json`, chọn một `book_url`, ví dụ:

```text
https://www.qidian.com/book/1043886198/
```

Crawl catalog:

```bash
python scripts/story_pipeline/crawl_qidian_catalog.py \
  "https://www.qidian.com/book/1043886198/"
```

Output:

```text
story_data/qidian/catalogs/1043886198/chapters.json
```

### Tải chapter public/free

Tải thử 3 chapter public đầu:

```bash
python scripts/story_pipeline/download_qidian_public_chapters.py \
  --manifest story_data/qidian/catalogs/1043886198/chapters.json \
  --limit 3
```

Output:

```text
story_data/raw_zh/<ten-truyen-qidian>/chapter1.txt
story_data/raw_zh/<ten-truyen-qidian>/chapter2.txt
```

Lưu ý: script này chỉ tải chương public mà HTML trả về trực tiếp. Chapter VIP/locked/login/paywall sẽ bị skip.

### Khảo sát truyện Qidian có nhiều chapter free

Nếu muốn tìm truyện free/limited-free trước:

```bash
python scripts/story_pipeline/survey_qidian_free_books.py \
  --ranks free free_all free_completed \
  --limit-per-rank 20
```

Output:

```text
story_data/qidian/free_survey/books.json
story_data/qidian/free_survey/books.csv
```

Script này sẽ:

- Crawl danh sách free/limited-free/completed-free.
- Crawl catalog từng book.
- Đếm `total_chapters`, `free_chapters`, `vip_chapters`.
- Lưu sẵn catalog manifest vào `story_data/qidian/catalogs/<book_id>/chapters.json`.
- Gợi ý `download_command` cho từng truyện.

Sau khi chọn truyện có `free_chapters` cao, tải chapter public:

```bash
python scripts/story_pipeline/download_qidian_public_chapters.py \
  --manifest story_data/qidian/catalogs/<book_id>/chapters.json
```

Downloader sẽ tạo report:

```text
story_data/raw_zh/<ten-truyen-qidian>/_download_report.json
```

Sau này nếu chapter VIP chuyển thành free, chạy lại cùng lệnh trên. File đã tải sẽ skip, chương mới public sẽ được tải thêm.

## 4. Crawl/update chapter mới từ database

Khi `stories` đã có trong database, dùng script tổng quát này để kiểm tra catalog mới, tải chapter mới, sync database và enqueue job polish nội dung:

```bash
python scripts/story_pipeline/crawl_stories_from_db.py \
  --sources truyenfull_today royalroad \
  --only-incomplete \
  --min-catalog-check-hours 6 \
  --workers 2 \
  --chapter-delay 1.5
```

Script này sẽ:

- Crawl lại catalog của story trong DB.
- `upsert_story()` để sync metadata.
- `upsert_chapter()` cho chapter đã có hoặc chapter mới.
- Tải file text mới vào `story_data/text` hoặc `story_data/raw_zh`.
- Enqueue `polish_chapter` job cho nội dung chapter.
- Tự nâng `stories.total_chapters` theo chapter number mới nhất đã crawl.

Nếu muốn ép check lại tất cả story, bỏ `--only-incomplete` và dùng:

```bash
--min-catalog-check-hours 0
```

Lưu ý: script này không polish title bằng Ollama. Title polish là bước riêng ở mục sau.

### Translate chapter nguồn không phải tiếng Việt còn thiếu bản dịch

Nếu database đã có chapter raw tiếng Trung/Anh/Hàn nhưng chưa có bản `translated`, hoặc có
`polished` nhưng nội dung polished vẫn không giống tiếng Việt, quét và translate trực tiếp từ DB:

```bash
python scripts/story_pipeline/translate_chapters_from_db.py --dry-run --limit 20
python scripts/story_pipeline/translate_chapters_from_db.py --limit 20
```

Lọc theo truyện/source khi cần:

```bash
python scripts/story_pipeline/translate_chapters_from_db.py \
  --story-title "Tên truyện" \
  --from-chapter 1 \
  --to-chapter 20
```

Mặc định script chỉ xử lý chapter thiếu `translated`. Nếu muốn dịch lại chapter đã `polished`
nhưng polished output vẫn không phải tiếng Việt, thêm `--include-polished-not-vi --overwrite-polish`.
Mặc định script chỉ dịch và update `translated_text_path/content`, không ghi `polished`.
Nếu muốn copy bản translated sang polished cho reader/TTS dùng luôn, thêm `--write-polished-copy`.
Nếu vẫn muốn polish sau dịch, truyền `--post-translate polish`.

Script cũng dịch `stories.display_title` và `stories.description` cho các story liên quan theo
mặc định. Nếu chỉ muốn dịch chapter, thêm `--no-translate-story-metadata`. Nếu muốn ép dịch lại
metadata dù đã có tiếng Việt, thêm `--overwrite-story-metadata`.

Console log có timestamp và progress từng chapter; mặc định cũng ghi vào:

```text
story_data/logs/translate_chapters_from_db.log
```

Đổi file log bằng `--log-file <path>`, hoặc tắt ghi file bằng `--log-file ""`.

## 5. Polish title truyện bằng Ollama

Title truyện dùng luồng riêng với nội dung chapter. Script này chỉ update các cột:

```text
stories.display_title
stories.title_polished_at
stories.title_polish_model
```

Không ghi đè `stories.title` hoặc `stories.original_title`, để vẫn giữ title nguồn cho audit.

Chạy mặc định:

```bash
python scripts/story_pipeline/polish_story_titles_ollama.py
```

Mặc định:

- `--limit 50`
- `--batch-size 20`
- `--ollama-url http://127.0.0.1:11434`
- `--model qwen3:14b`
- Bỏ qua story đã có `display_title`.

Polish toàn bộ story chưa có `display_title`:

```bash
python scripts/story_pipeline/polish_story_titles_ollama.py \
  --limit 0 \
  --batch-size 20
```

Polish thử không ghi DB:

```bash
python scripts/story_pipeline/polish_story_titles_ollama.py \
  --limit 20 \
  --batch-size 10 \
  --dry-run
```

Nếu model trả JSON không ổn định, giảm batch hoặc bật fallback:

```bash
python scripts/story_pipeline/polish_story_titles_ollama.py \
  --limit 100 \
  --batch-size 10 \
  --fallback-single
```

Muốn polish lại cả các title đã có `display_title`:

```bash
--overwrite
```

Script sẽ loại các hậu tố metadata nguồn như `(Dịch)`, `(Convert)`, `(Full)`, `(Trọn Bộ)`, `- Truyện Chữ` trước và sau khi gọi Ollama.

## 6. Tạo description truyện từ chapter bằng Ollama

Nếu story đã có chapter text trong database nhưng `stories.description` còn trống, dùng script này để tự tạo mô tả:

```bash
python scripts/story_pipeline/backfill_story_descriptions_ollama.py \
  --dry-run \
  --limit 3
```

Chạy thật:

```bash
python scripts/story_pipeline/backfill_story_descriptions_ollama.py \
  --limit 20 \
  --model qwen3:14b
```

Script sẽ:

- Lấy story đang active nhưng thiếu `description`.
- Đọc chapter text theo thứ tự ưu tiên `polished_text_content`, `translated_text_content`, `raw_text_content`, rồi fallback sang path tương ứng.
- Tóm tắt từng chapter bằng Ollama theo prompt chống spoiler.
- Gộp các summary thành description kiểu webnovel/light novel, khoảng 90-140 từ.
- Update `stories.description`.
- Ghi audit summary/model vào `stories.metadata`.

Mặc định script chỉ lấy các chapter đầu để tránh lộ twist về sau:

```bash
--sample-strategy first --sample-chapters 8 --max-chapters 80
```

Nếu muốn lấy rải đều trong phần đã có, dùng:

```bash
--sample-strategy spread --sample-chapters 10
```

Lọc theo truyện/source khi cần:

```bash
python scripts/story_pipeline/backfill_story_descriptions_ollama.py \
  --story-title "Tên truyện" \
  --sample-chapters 6 \
  --dry-run
```

Ghi đè description đã có:

```bash
--overwrite
```

Console log mặc định được mirror vào:

```text
story_data/logs/backfill_story_descriptions_ollama.log
```

## 7. Dịch hoặc polish text chapter bằng Ollama

Workflow production khuyến nghị là chạy qua DB queue:

```bash
viterbox/venv/bin/python scripts/story_pipeline/polish_worker.py \
  --once --workers 1 --batch-size 1 \
  --vi-model qwen3:14b \
  --translate-model translategemma:12b \
  --post-translate polish
```

Behavior hiện tại:

- Không cần truyền `--genre`; genre tự suy từ DB metadata/source/language/char map.
- Không cần `--no-save-files`; DB-only là mặc định.
- Raw tiếng Việt được polish trực tiếp.
- Raw không phải tiếng Việt được translate sang Việt rồi polish.

### Legacy/debug file-based

```bash
python scripts/story_pipeline/translate_chapter_texts_ollama.py \
  --input-dir story_data/raw_zh/<slug-truyen> \
  --chapter 1 \
  --model translategemma:12b
```

Sau đó polish bản dịch bằng Qwen:

```bash
python scripts/story_pipeline/polish_chapter_texts_ollama.py \
  --input-dir story_data/translated/<slug-truyen> \
  --chapter 1 \
  --model qwen3:14b
```

Output legacy/file-based:

```text
story_data/translated/<slug-truyen>/chapter1.txt
story_data/polished/<slug-truyen>/chapter1.txt
```

Nếu text đã là tiếng Việt dịch máy, dùng bước polish:

Chạy thử chương 1 trước:

```bash
python scripts/story_pipeline/polish_chapter_texts_ollama.py \
  --input-dir story_data/text/cau-tha-thanh-thanh-nhan-tien-quan-trieu-ta-cham-ngua \
  --chapter 1 \
  --model qwen2.5:14b-instruct
```

Output:

```text
story_data/polished/cau-tha-thanh-thanh-nhan-tien-quan-trieu-ta-cham-ngua/chapter1.txt
```

Polish toàn bộ chương:

```bash
python scripts/story_pipeline/polish_chapter_texts_ollama.py \
  --input-dir story_data/text/cau-tha-thanh-thanh-nhan-tien-quan-trieu-ta-cham-ngua \
  --all \
  --model qwen2.5:14b-instruct
```

Nếu muốn dùng 7B:

```bash
--model qwen2.5:7b-instruct
```

Polish nhanh hơn nhưng giữ prompt chi tiết như cũ bằng cách tăng chunk:

```bash
--prompt-profile full --max-chars-per-chunk 5000
```

Nếu chấp nhận đánh đổi một phần độ kỹ để chạy nhanh hơn nữa, có thể dùng prompt ngắn:

```bash
--prompt-profile fast --max-chars-per-chunk 5000
```

Nếu text đã khá ổn và chỉ cần chuẩn hóa cho TTS, bỏ qua Ollama:

```bash
--polish-mode clean
```

Script polish nội dung sẽ skip file polished đã tồn tại. Muốn polish lại:

```bash
--overwrite
```

## 8. Sinh audio từng chương bằng VieNeu-TTS v3

Audio production hiện dùng VieNeu-TTS v3. Default voice cho truyện tiên hiệp/system:

```text
preset_binh_an -> Bình An (VieNeu v3 built-in preset)
```

### Test một audio ngắn

```bash
viterbox/venv/bin/python scripts/story_pipeline/generate_chapter_audio_vieneu.py \
  --input-dir /tmp/betterbox-vieneu-smoke-input \
  --output-root /tmp/betterbox-vieneu-smoke-output \
  --chapter 1 \
  --overwrite \
  --voice-profile preset_binh_an \
  --max-chars-per-unit 120 \
  --min-chars-per-unit 20 \
  --max-new-frames 180 \
  --sentence-pause-ms 250 \
  --crossfade-ms 0 \
  --no-watermark
```

### Generate thử một chapter

```bash
viterbox/venv/bin/python scripts/story_pipeline/generate_chapter_audio_vieneu.py \
  --input-dir story_data/polished/<slug-truyen> \
  --output-root story_audio \
  --chapter 1 \
  --voice-profile preset_binh_an \
  --sentence-pause-ms 250 \
  --crossfade-ms 0 \
  --overwrite
```

### Generate toàn bộ folder legacy/file-based

```bash
viterbox/venv/bin/python scripts/story_pipeline/generate_chapter_audio_vieneu.py \
  --input-dir story_data/polished/<slug-truyen> \
  --output-root story_audio \
  --all \
  --voice-profile preset_binh_an \
  --sentence-pause-ms 250 \
  --crossfade-ms 0
```

Output:

```text
story_audio/<slug-truyen>/chapter1.wav
story_audio/<slug-truyen>/chapter2.wav
...
```

### Tham số nên chỉnh trước

```bash
--voice-profile
--sentence-pause-ms
--max-new-frames
--max-chars-per-unit
--min-chars-per-unit
```

Các script Viterbox (`preview_text_viterbox.py`, `generate_chapter_audio_viterbox.py`) vẫn còn để so sánh/legacy, nhưng không phải default production.

### Khảo sát/chọn voice tự động

Script này chấm điểm VieNeu v3 preset voices và voice clone trong `voice_bank/vieneu` theo tiêu chí audiobook tiên hiệp/system:

```bash
viterbox/venv/bin/python scripts/story_pipeline/survey_vieneu_voices.py --top 12
```

Output mặc định:

```text
/tmp/betterbox-vieneu-voice-survey/vieneu_voice_survey.json
/tmp/betterbox-vieneu-voice-survey/vieneu_voice_survey.md
```

Kết quả hiện tại chọn `preset_binh_an` vì đây là built-in speaker token nam điềm đạm, ổn định hơn single-WAV cloning và hợp nghe dài.

Nếu bị đọc sai chữ, thử giảm độ dài block:

```bash
--max-chars-per-block 600
```

Nếu muốn đọc chính xác từng từ hơn nhưng chậm hơn nhiều, có thể bật:

```bash
--advance-tts
```

## 9. Merge audio chương

Merge thử 5 chương đầu:

```bash
python scripts/story_pipeline/merge_chapter_audio.py \
  --folder story_audio/cau-tha-thanh-thanh-nhan-tien-quan-trieu-ta-cham-ngua \
  --number 5 \
  --silence-ms 1000
```

Merge toàn bộ:

```bash
python scripts/story_pipeline/merge_chapter_audio.py \
  --folder story_audio/cau-tha-thanh-thanh-nhan-tien-quan-trieu-ta-cham-ngua \
  --silence-ms 1000
```

Output mặc định:

```text
story_audio_merged/<slug-truyen>_first_5.wav
story_audio_merged/<slug-truyen>_all.wav
```

Hoặc chỉ định output:

```bash
python scripts/story_pipeline/merge_chapter_audio.py \
  --folder story_audio/cau-tha-thanh-thanh-nhan-tien-quan-trieu-ta-cham-ngua \
  --output story_audio_merged/truyen_tien_hiep.wav
```

## 10. Backfill ảnh bìa truyện thiếu cover

Kiểm tra dry-run trước:

```bash
python scripts/story_pipeline/backfill_story_covers.py \
  --sources hako wattpad_vn qidian naver_series royalroad truyenfull_today \
  --limit 50
```

Mặc định script sẽ thử theo thứ tự:

- Trang gốc của story (`catalog_url` hoặc `source_url`) nếu còn access được.
- Search bên ngoài theo `original_title`, `title`, `display_title`, `author`, rồi lấy ảnh từ trang kết quả có title/author khớp tương đối.

Ghi vào DB sau khi preview ổn:

```bash
python scripts/story_pipeline/backfill_story_covers.py \
  --sources hako wattpad_vn qidian naver_series royalroad truyenfull_today \
  --limit 200 \
  --write
```

Nếu chỉ muốn dùng trang gốc, không search nguồn khác:

```bash
python scripts/story_pipeline/backfill_story_covers.py \
  --limit 100 \
  --no-external-search
```

Nếu DB đã có `cover_image_url` nhưng ảnh đó nằm trên host chết, chạy dry-run kiểm tra và thay ảnh hỏng:

```bash
python scripts/story_pipeline/backfill_story_covers.py \
  --limit 200 \
  --replace-broken
```

Sau khi preview ổn:

```bash
python scripts/story_pipeline/backfill_story_covers.py \
  --limit 200 \
  --replace-broken \
  --write
```

Script chỉ update story đang thiếu `cover_image_url`, không overwrite ảnh đã có.
Riêng khi dùng `--replace-broken`, script validate ảnh hiện tại trước; ảnh còn truy cập được sẽ bị skip, ảnh hỏng mới được thay.
Với ảnh lấy từ nguồn ngoài, script lưu thêm `cover_backfill_method` và `cover_backfill_page_url` vào `stories.metadata` để sau này audit được ảnh đến từ trang nào.

## Quy trình khuyến nghị

1. Crawl chapter list hoặc chạy `crawl_stories_from_db.py` để sync DB.
2. Polish title bằng `polish_story_titles_ollama.py` nếu cần tên hiển thị đẹp.
3. Tải thử `--limit 3`.
4. Nếu nguồn Trung: dịch thử `--chapter 1`. Nếu nguồn Việt dịch máy: polish thử `--chapter 1`.
5. Preview chương đã dịch/polish.
6. Nghe kiểm tra giọng, tốc độ, pitch.
7. Dịch/polish nội dung `--all` hoặc chạy `polish_worker.py`.
8. Generate audio `--all`.
9. Backfill ảnh bìa nếu reader còn truyện thiếu cover.
10. Merge `--number 5` để kiểm tra.
11. Merge toàn bộ.

## Reader realtime broadcast

Sau khi crawl hoặc polish chapter, pipeline có thể push thông báo live tới Story Reader qua WebSocket.

1. Generate token (một lần):

```bash
bash docker/scripts/generate-reader-realtime-token.sh
```

2. Thêm vào root `.env`:

```env
READER_REALTIME_TOKEN=<token>
READER_REALTIME_URL=http://story-reader:3000
# Dev UI trên host :3003 (workers ưu tiên DEV URL khi set):
# READER_REALTIME_DEV_URL=http://host.docker.internal:3003
```

3. Docker `story-reader` phải chạy `start:ws` (mặc định trong `Dockerfile`). Dev UI không rebuild: `bash docker/scripts/dev-story-reader.sh` hoặc `make reader-dev`.

`crawl_stories_from_db.py` và `polish_worker.py` gọi `scripts/story_pipeline/reader_realtime_broadcast.py` tự động khi env có `READER_REALTIME_URL` hoặc `READER_REALTIME_DEV_URL`. Broadcast thất bại không làm fail job crawl/polish.

Kiểm tra:

```bash
curl http://localhost:3000/api/health
bash docker/scripts/smoke-reader-realtime.sh
curl -X POST http://localhost:3000/api/realtime/broadcast \
  -H "Authorization: Bearer $READER_REALTIME_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"type":"notification_update"}'

# Hoặc qua Python CLI:
READER_REALTIME_URL=http://localhost:3000 READER_REALTIME_TOKEN=$READER_REALTIME_TOKEN \
  viterbox/venv/bin/python scripts/story_pipeline/reader_realtime_broadcast.py \
  --type chapter_update --story-id <uuid> --chapter-number 128
```

## Lưu ý

- Script crawl hiện tối ưu cho cấu trúc `wattpad.com.vn`, cụ thể selector `#chapter-list` và `#vungdoc > div.truyen`.
- Nếu website đổi HTML, cần chỉnh selector trong `crawl_wattpad_chapters.py` hoặc `download_chapter_texts.py`.
- Qidian script chỉ lấy metadata/bảng xếp hạng công khai. Không dùng pipeline này để tải lậu nội dung có bản quyền hoặc bypass paywall.
- BetterBox đang chạy Hugging Face offline, nên model local trong `viterbox/modelViterboxLocal` phải có sẵn.
- Khi build voice profile, `viterbox/pretrained` chỉ nên chứa dữ liệu của một giọng.
- Audio generated từ script này không mở UI, nhưng dùng cùng core Viterbox model như app.
