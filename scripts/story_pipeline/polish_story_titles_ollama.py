#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PROMPT_TEMPLATE = """Bạn là biên tập viên tên truyện tiếng Việt.

Hãy chỉnh tên truyện sau thành một tên tiếng Việt tự nhiên, dễ đọc cho web đọc truyện.
Yêu cầu:
- Chỉ trả về đúng một tên truyện.
- Không thêm giải thích.
- Không thêm dấu ngoặc kép.
- Giữ đúng nghĩa chính, không bịa thể loại mới.
- Nếu tên đã ổn, chỉ chuẩn hóa chính tả/viết hoa.

Tên nguồn: {title}
"""

BATCH_PROMPT_TEMPLATE = """Bạn là biên tập viên tên truyện tiếng Việt.

Hãy chỉnh các tên truyện sau thành tên tiếng Việt tự nhiên, dễ đọc cho web đọc truyện.
Yêu cầu bắt buộc:
- Chỉ trả về JSON hợp lệ, không markdown, không giải thích.
- JSON là một object có key "items".
- "items" là một mảng object.
- Mỗi object trong "items" giữ nguyên id và có display_title.
- Không bỏ sót id nào.
- Không thêm id mới.
- display_title chỉ là một tên truyện, không thêm dấu ngoặc kép trong nội dung.
- Giữ đúng nghĩa chính, không bịa thể loại mới.
- Nếu tên đã ổn, chỉ chuẩn hóa chính tả/viết hoa.

Input JSON:
{items_json}

Output JSON mẫu:
{{"items":[{{"id":"...","display_title":"..."}}]}}
"""


def clean_title(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", value or "").strip().strip("\"'“”‘’")
    cleaned = re.sub(r"\s+([,.:;!?])", r"\1", cleaned)
    cleaned = strip_source_title_noise(cleaned)
    return cleaned[:180]


def strip_source_title_noise(value: str) -> str:
    cleaned = value
    noise_patterns = [
        r"\s*[-–—]\s*truyện\s+chữ\s*$",
        r"\s*\((?:dịch|convert|full|trọn\s+bộ|bản\s+chuẩn\s+mới\s*[-–—]?\s*full)\)\s*$",
        r"\s*\[(?:dịch|convert|full|trọn\s+bộ)\]\s*$",
    ]
    previous = None
    while previous != cleaned:
        previous = cleaned
        for pattern in noise_patterns:
            cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE).strip()
    return cleaned


def chunked(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [items[index:index + size] for index in range(0, len(items), size)]


def strip_json_response(value: str) -> str:
    cleaned = (value or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    object_start = cleaned.find("{")
    object_end = cleaned.rfind("}")
    if object_start >= 0 and object_end > object_start:
        return cleaned[object_start:object_end + 1]
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start >= 0 and end > start:
        return cleaned[start:end + 1]
    return cleaned


def parse_batch_response(value: str, expected_ids: set[str]) -> dict[str, str]:
    parsed = json.loads(strip_json_response(value))
    if isinstance(parsed, dict):
        parsed_items = parsed.get("items") or parsed.get("titles") or parsed.get("results")
    else:
        parsed_items = parsed

    if not isinstance(parsed_items, list):
        raise ValueError(f"Ollama response does not contain an items array: {str(value)[:300]}")

    result: dict[str, str] = {}
    for item in parsed_items:
        if not isinstance(item, dict):
            continue
        story_id = str(item.get("id") or "").strip()
        display_title = clean_title(str(item.get("display_title") or item.get("title") or ""))
        if story_id in expected_ids and display_title:
            result[story_id] = display_title

    missing_ids = expected_ids - set(result)
    if missing_ids:
        raise ValueError(f"Ollama response missing ids: {', '.join(sorted(missing_ids))}")
    return result


def call_ollama(base_url: str, model: str, title: str, timeout: int) -> str:
    response = requests.post(
        f"{base_url.rstrip('/')}/api/generate",
        json={
            "model": model,
            "prompt": PROMPT_TEMPLATE.format(title=title),
            "stream": False,
            "options": {"temperature": 0.15, "num_ctx": 2048},
        },
        timeout=timeout,
    )
    response.raise_for_status()
    return clean_title(str(response.json().get("response") or ""))


def call_ollama_batch(
    base_url: str,
    model: str,
    stories: list[dict[str, Any]],
    timeout: int,
    *,
    json_mode: bool = False,
) -> dict[str, str]:
    items = [
        {
            "id": str(story["id"]),
            "title": strip_source_title_noise(str(story.get("original_title") or story["title"])),
        }
        for story in stories
    ]
    expected_ids = {str(item["id"]) for item in items}
    prompt = BATCH_PROMPT_TEMPLATE.format(items_json=json.dumps(items, ensure_ascii=False, indent=2))
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.12, "num_ctx": 4096},
    }
    if json_mode:
        payload["format"] = "json"

    response = requests.post(
        f"{base_url.rstrip('/')}/api/generate",
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()
    first_response = str(response.json().get("response") or "")
    try:
        return parse_batch_response(first_response, expected_ids)
    except ValueError as exc:
        if not json_mode or first_response.strip() not in {"{}", "[]"}:
            raise
        print(f"[WARN] empty JSON response, retry without Ollama json mode: {exc}")

    retry_response = requests.post(
        f"{base_url.rstrip('/')}/api/generate",
        json={
            "model": model,
            "prompt": (
                f"{prompt}\n\n"
                "Nhắc lại: trả về duy nhất JSON object có dạng "
                "{\"items\":[{\"id\":\"id gốc\",\"display_title\":\"tên đã chỉnh\"}]}. "
                "Không trả về object rỗng."
            ),
            "stream": False,
            "options": {"temperature": 0.08, "num_ctx": 4096},
        },
        timeout=timeout,
    )
    retry_response.raise_for_status()
    return parse_batch_response(str(retry_response.json().get("response") or ""), expected_ids)


def list_candidate_stories(limit: int, source_code: str | None, overwrite: bool) -> list[dict[str, Any]]:
    from story_db.story_pipeline_db.db import connect

    where = ["s.is_active = TRUE"]
    params: list[Any] = []
    if source_code:
        params.append(source_code)
        where.append(f"src.code = %s")
    if not overwrite:
        where.append("(s.display_title IS NULL OR btrim(s.display_title) = '')")

    limit_sql = ""
    if limit > 0:
        params.append(limit)
        limit_sql = "LIMIT %s"

    query = f"""
        SELECT s.id, s.title, s.original_title, s.display_title, src.code AS source_code
        FROM stories s
        JOIN sources src ON src.id = s.source_id
        WHERE {' AND '.join(where)}
        ORDER BY s.updated_at DESC, s.created_at DESC
        {limit_sql}
    """
    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]


def main() -> None:
    parser = argparse.ArgumentParser(description="Polish story display_title bằng Ollama, không đổi title gốc.")
    parser.add_argument("--limit", type=int, default=50, help="Số story tối đa cần polish. 0 = toàn bộ story pending.")
    parser.add_argument("--source-code", default="")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default="qwen3:14b")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--json-mode", action="store_true", help="Bật Ollama format=json. Mặc định tắt vì một số model trả {}.")
    parser.add_argument(
        "--no-fallback-single",
        action="store_true",
        help="Tắt fallback gọi từng title khi batch JSON lỗi.",
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    stories = list_candidate_stories(max(0, args.limit), args.source_code or None, args.overwrite)
    if not stories:
        print("Không có story cần polish title.")
        return

    for batch_index, batch in enumerate(chunked(stories, max(1, args.batch_size)), start=1):
        print(f"\n[BATCH {batch_index}] stories={len(batch)}")
        try:
            polished_by_id = call_ollama_batch(
                args.ollama_url,
                args.model,
                batch,
                args.timeout,
                json_mode=args.json_mode,
            )
        except Exception as exc:
            if args.no_fallback_single:
                print(f"[ERROR] batch failed: {exc}")
                continue

            print(f"[WARN] batch failed, fallback single: {exc}")
            polished_by_id = {}
            for story in batch:
                source_title = strip_source_title_noise(str(story.get("original_title") or story["title"]))
                polished_by_id[story["id"]] = call_ollama(args.ollama_url, args.model, source_title, args.timeout)

        for story in batch:
            story_id = str(story["id"])
            polished_title = clean_title(polished_by_id.get(story_id, ""))
            if not polished_title:
                print(f"[SKIP] empty title: {story['title']}")
                continue

            print(f"{story['title']} -> {polished_title}")
            if not args.dry_run:
                from story_db.story_pipeline_db import repository as repo

                repo.update_story_display_title(story_id, polished_title, model=args.model)


if __name__ == "__main__":
    main()
