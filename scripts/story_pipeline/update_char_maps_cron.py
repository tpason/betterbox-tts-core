#!/usr/bin/env python3
"""
Cron script: cập nhật char-map cho story có chapter mới.

Tự động chạy extract_char_map.py cho các story thoả mãn:
- Có ít nhất --min-polished chapter đã polish
- Chưa có char_map HOẶC char_map cũ hơn --new-chapter-threshold chapter

Resource checks (VRAM / RAM / CPU) được thực hiện trước mỗi story.
Nếu tài nguyên không đủ, script chờ (poll) rồi thử lại — không crash.

Resume: DB-based tự động. Sau khi extract xong, metadata story được cập nhật.
Nếu container stop giữa chừng, lần restart sẽ bỏ qua story đã xong và tiếp tục
từ story còn lại (vì chúng vẫn thoả điều kiện query).

Usage:
    python scripts/story_pipeline/update_char_maps_cron.py
    python scripts/story_pipeline/update_char_maps_cron.py --dry-run
    python scripts/story_pipeline/update_char_maps_cron.py --story-id <uuid>
    python scripts/story_pipeline/update_char_maps_cron.py --limit 5 \\
        --min-free-vram-gb 4 --min-free-ram-gb 2 --max-cpu-pct 80
"""
import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXTRACT_SCRIPT = PROJECT_ROOT / "scripts" / "story_pipeline" / "extract_char_map.py"
PYTHON = sys.executable


# ---------------------------------------------------------------------------
# Resource helpers (same pattern as polish_worker.py)
# ---------------------------------------------------------------------------

def _free_vram_mb() -> int:
    """Free VRAM in MB from nvidia-smi. Returns -1 if unavailable."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheaders,nounits"],
            timeout=10, text=True,
        )
        values = [int(v.strip()) for v in out.strip().splitlines() if v.strip().isdigit()]
        return min(values) if values else -1
    except Exception:
        return -1


def _free_ram_mb() -> int:
    """Free system RAM in MB. Returns -1 if unavailable."""
    try:
        import psutil
        return psutil.virtual_memory().available // (1024 * 1024)
    except Exception:
        return -1


def _cpu_pct() -> float:
    """1-second CPU usage percent. Returns -1.0 if unavailable."""
    try:
        import psutil
        return psutil.cpu_percent(interval=1)
    except Exception:
        return -1.0


def _ollama_loaded_models(base_url: str) -> list[str]:
    try:
        import requests
        resp = requests.get(base_url.rstrip("/") + "/api/ps", timeout=10)
        if resp.ok:
            return [m.get("name", "") for m in resp.json().get("models", [])]
    except Exception:
        pass
    return []


def wait_for_resources(
    base_url: str,
    *,
    label: str = "",
    min_vram_mb: int = 0,
    max_cpu_pct: float = 90.0,
    min_ram_mb: int = 0,
    max_wait: int = 1800,
    poll: int = 30,
) -> bool:
    """
    Block until VRAM / RAM / CPU are within limits.

    Returns True when resources are OK.
    Returns False if max_wait seconds elapsed without recovery (caller decides
    whether to skip or abort — we never crash the process).
    """
    prefix = f"[RESOURCE:{label}] " if label else "[RESOURCE] "
    deadline = time.monotonic() + max_wait
    first = True

    while True:
        reasons: list[str] = []

        vram = _free_vram_mb()
        if min_vram_mb > 0 and vram != -1 and vram < min_vram_mb:
            reasons.append(f"VRAM free {vram}MB < {min_vram_mb}MB")

        ram = _free_ram_mb()
        if min_ram_mb > 0 and ram != -1 and ram < min_ram_mb:
            reasons.append(f"RAM free {ram}MB < {min_ram_mb}MB")

        cpu = _cpu_pct()
        if max_cpu_pct < 100 and cpu != -1.0 and cpu > max_cpu_pct:
            reasons.append(f"CPU {cpu:.0f}% > {max_cpu_pct:.0f}%")

        if not reasons:
            parts: list[str] = []
            if vram != -1:
                parts.append(f"VRAM {vram}MB free")
            if ram != -1:
                parts.append(f"RAM {ram}MB free")
            if cpu != -1.0:
                parts.append(f"CPU {cpu:.0f}%")
            if not first and parts:
                log.info("%sOK — %s", prefix, ", ".join(parts))
            return True

        if time.monotonic() >= deadline:
            log.warning(
                "%stimeout %ds waiting for resources: %s — skipping story",
                prefix, max_wait, "; ".join(reasons),
            )
            return False

        elapsed = time.monotonic() - (deadline - max_wait)
        log.info("%swaiting %ds — %s (elapsed %.0fs)", prefix, poll, "; ".join(reasons), elapsed)
        first = False
        time.sleep(poll)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_db_url() -> str:
    return os.environ.get(
        "STORY_DATABASE_URL",
        "postgresql://betterbox:betterbox@127.0.0.1:54329/betterbox_story",
    )


def find_stories_needing_update(
    conn: psycopg.Connection,
    min_polished: int,
    new_chapter_threshold: int,
    limit: int,
    story_id: str | None,
) -> list[dict]:
    """
    Trả về stories cần cập nhật char-map.

    Điều kiện:
    - Có >= min_polished chapter đã polish
    - Chưa có char_map (metadata->>'char_map_path' IS NULL) HOẶC
      char_map_updated_to_chapter + threshold < max_polished_chapter

    Order: số chapter polish nhiều nhất trước (ưu tiên story lớn).
    """
    extra_filter = ""
    params: list = [min_polished, new_chapter_threshold]

    if story_id:
        extra_filter = "AND s.id = %s"
        params.append(story_id)

    params.append(limit)

    sql = f"""
        WITH polished_counts AS (
            SELECT
                story_id,
                COUNT(*)           AS polished_count,
                MAX(chapter_number) AS max_polished_chapter
            FROM chapters
            WHERE is_polished = TRUE
            GROUP BY story_id
        )
        SELECT
            s.id,
            COALESCE(NULLIF(s.display_title, ''), s.title)  AS title,
            pc.polished_count,
            pc.max_polished_chapter,
            (s.metadata->>'char_map_path')                           AS char_map_path,
            (s.metadata->>'char_map_updated_to_chapter')::int        AS char_map_updated_to_chapter,
            (s.metadata->>'char_map_updated_at')                     AS char_map_updated_at
        FROM stories s
        JOIN polished_counts pc ON pc.story_id = s.id
        WHERE
            s.is_active = TRUE
            AND pc.polished_count >= %s
            AND (
                s.metadata->>'char_map_path' IS NULL
                OR (s.metadata->>'char_map_updated_to_chapter')::int + %s
                    < pc.max_polished_chapter
            )
            {extra_filter}
        ORDER BY pc.polished_count DESC
        LIMIT %s
    """

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


# ---------------------------------------------------------------------------
# Per-story runner
# ---------------------------------------------------------------------------

def run_extract_for_story(
    story: dict,
    *,
    ollama_url: str,
    model: str,
    sample_chapters: int,
    append_only: bool,
    dry_run: bool,
    timeout: int,
) -> bool:
    story_id = str(story["id"])
    title = story["title"]
    max_ch = story["max_polished_chapter"]
    from_ch = max(1, max_ch - sample_chapters + 1)

    log.info(
        "Xử lý: %s (id=%s) — chapters %d-%d",
        title, story_id, from_ch, max_ch,
    )

    if dry_run:
        log.info("  [DRY RUN] Bỏ qua")
        return True

    cmd = [
        PYTHON, str(EXTRACT_SCRIPT),
        "--story-id", story_id,
        "--from-chapter", str(from_ch),
        "--to-chapter", str(max_ch),
        "--ollama-url", ollama_url,
        "--model", model,
        "--sample-chapters", str(min(sample_chapters, max_ch - from_ch + 1)),
    ]
    if append_only:
        cmd.append("--append-only")

    try:
        result = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            timeout=timeout,
        )
        if result.returncode != 0:
            log.error("  extract_char_map.py failed (returncode=%d): %s", result.returncode, title)
            return False
        log.info("  Hoàn thành: %s", title)
        return True
    except subprocess.TimeoutExpired:
        log.error("  Timeout sau %ds: %s", timeout, title)
        return False
    except Exception as exc:
        log.error("  Lỗi: %s — %s", title, exc)
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Cron: update char-maps for stories with new chapters",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    # Story selection
    parser.add_argument("--story-id", default="", help="Chỉ update 1 story cụ thể (UUID)")
    parser.add_argument("--limit", type=int,
                        default=int(os.environ.get("CHAR_MAP_LIMIT", "20")),
                        help="Số story tối đa mỗi lần chạy (default: 20)")
    parser.add_argument("--min-polished", type=int,
                        default=int(os.environ.get("CHAR_MAP_MIN_POLISHED", "10")),
                        help="Số chapter polish tối thiểu để build char-map (default: 10)")
    parser.add_argument("--new-chapter-threshold", type=int,
                        default=int(os.environ.get("CHAR_MAP_NEW_CHAPTER_THRESHOLD", "50")),
                        help="Số chapter mới tối thiểu để trigger update (default: 50)")
    # Extract params
    parser.add_argument("--sample-chapters", type=int,
                        default=int(os.environ.get("CHAR_MAP_SAMPLE_CHAPTERS", "30")),
                        help="Số chapter sample để extract (default: 30)")
    parser.add_argument("--append-only", action="store_true",
                        default=bool(os.environ.get("CHAR_MAP_APPEND_ONLY", "")),
                        help="Chỉ thêm nhân vật mới, không ghi đè entry cũ")
    parser.add_argument("--ollama-url",
                        default=os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434"))
    parser.add_argument("--model",
                        default=os.environ.get("CHAR_MAP_MODEL", "qwen3:14b"))
    parser.add_argument("--timeout", type=int,
                        default=int(os.environ.get("CHAR_MAP_TIMEOUT", "600")),
                        help="Timeout (giây) cho mỗi story (default: 600)")
    parser.add_argument("--delay", type=float,
                        default=float(os.environ.get("CHAR_MAP_DELAY", "3.0")),
                        help="Delay giữa các story (giây, default: 3.0)")
    # Resource limits
    parser.add_argument("--min-free-vram-gb", type=float,
                        default=float(os.environ.get("CHAR_MAP_MIN_FREE_VRAM_GB", "0")),
                        help="VRAM tối thiểu cần có (GB, 0=không kiểm tra)")
    parser.add_argument("--min-free-ram-gb", type=float,
                        default=float(os.environ.get("CHAR_MAP_MIN_FREE_RAM_GB", "1.5")),
                        help="RAM tối thiểu cần có (GB, default: 1.5)")
    parser.add_argument("--max-cpu-pct", type=float,
                        default=float(os.environ.get("CHAR_MAP_MAX_CPU_PCT", "85")),
                        help="CPU% tối đa cho phép (default: 85)")
    parser.add_argument("--resource-wait", type=int,
                        default=int(os.environ.get("CHAR_MAP_RESOURCE_WAIT", "1800")),
                        help="Thời gian tối đa chờ tài nguyên (giây, default: 1800)")
    parser.add_argument("--resource-poll", type=int,
                        default=int(os.environ.get("CHAR_MAP_RESOURCE_POLL", "30")),
                        help="Interval poll tài nguyên (giây, default: 30)")
    parser.add_argument("--no-resource-check", action="store_true",
                        help="Bỏ qua resource check")
    # Misc
    parser.add_argument("--dry-run", action="store_true", help="Chỉ xem, không chạy")
    args = parser.parse_args()

    min_vram_mb = int(args.min_free_vram_gb * 1024)
    min_ram_mb  = int(args.min_free_ram_gb  * 1024)

    # --- Query DB ---
    with psycopg.connect(get_db_url()) as conn:
        stories = find_stories_needing_update(
            conn,
            min_polished=args.min_polished,
            new_chapter_threshold=args.new_chapter_threshold,
            limit=args.limit,
            story_id=args.story_id or None,
        )

    if not stories:
        log.info("Không có story nào cần cập nhật char-map.")
        return

    total = len(stories)
    log.info("Tìm thấy %d story cần cập nhật:", total)
    for i, s in enumerate(stories, 1):
        log.info(
            "  [%d/%d] %s — %d polished, updated_to=%s, updated_at=%s",
            i, total, s["title"], s["polished_count"],
            s["char_map_updated_to_chapter"], s["char_map_updated_at"],
        )

    # --- Process each story ---
    success = fail = skipped = 0

    for idx, story in enumerate(stories, 1):
        log.info("--- [%d/%d] %s ---", idx, total, story["title"])

        # Resource check before each story
        if not args.no_resource_check:
            ok = wait_for_resources(
                args.ollama_url,
                label=f"{idx}/{total}",
                min_vram_mb=min_vram_mb,
                min_ram_mb=min_ram_mb,
                max_cpu_pct=args.max_cpu_pct,
                max_wait=args.resource_wait,
                poll=args.resource_poll,
            )
            if not ok:
                log.warning("  Bỏ qua vì tài nguyên không đủ sau %ds chờ.", args.resource_wait)
                skipped += 1
                continue

        ok = run_extract_for_story(
            story,
            ollama_url=args.ollama_url,
            model=args.model,
            sample_chapters=args.sample_chapters,
            append_only=args.append_only,
            dry_run=args.dry_run,
            timeout=args.timeout,
        )

        if ok:
            success += 1
        else:
            fail += 1

        if args.delay > 0 and idx < total:
            time.sleep(args.delay)

    log.info(
        "Tổng kết: %d thành công, %d lỗi, %d bỏ qua vì tài nguyên.",
        success, fail, skipped,
    )


if __name__ == "__main__":
    main()
