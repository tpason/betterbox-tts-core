#!/usr/bin/env python3
"""Phase 2 PoC: benchmark zstd vs the current pglz TOAST compression on REAL chapter text.

Read-only. Samples chapter text columns (stratified by content type and raw language),
then measures, per row, the compressed size under:
  - current on-disk pglz  (via pg_column_size, what Postgres stores today)
  - zstd levels 3 / 9 / 19 (no dictionary)
  - zstd level 19 + a trained dictionary (helps short rows)
  - lz4 (Codex asked to compare; usually worse ratio than pglz)

It also times zstd encode/decode throughput and projects the DB-wide reduction by
applying the measured zstd/pglz ratio to the current on-disk text totals.

Goal: decide if Phase 2 (store bytea = zstd(text)) is worth the read/write rewrite,
and pick a compression level + whether a dictionary is worthwhile.

Usage:
  viterbox/venv/bin/python scripts/story_pipeline/db_compression_benchmark.py [--sample 200] [--no-projection]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import lz4.frame
import psycopg
import zstandard as zstd
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from story_db.story_pipeline_db.db import database_url  # noqa: E402

# (label, sql to pull text + its current on-disk pglz size)
GROUPS: list[tuple[str, str]] = [
    ("raw:zh", "SELECT raw_text_content AS t, pg_column_size(raw_text_content) AS pglz FROM chapters WHERE raw_language='zh' AND raw_text_content IS NOT NULL LIMIT %(n)s"),
    ("raw:en", "SELECT raw_text_content AS t, pg_column_size(raw_text_content) AS pglz FROM chapters WHERE raw_language='en' AND raw_text_content IS NOT NULL LIMIT %(n)s"),
    ("raw:ko", "SELECT raw_text_content AS t, pg_column_size(raw_text_content) AS pglz FROM chapters WHERE raw_language='ko' AND raw_text_content IS NOT NULL LIMIT %(n)s"),
    ("raw:vi", "SELECT raw_text_content AS t, pg_column_size(raw_text_content) AS pglz FROM chapters WHERE raw_language='vi' AND raw_text_content IS NOT NULL LIMIT %(n)s"),
    ("polished", "SELECT polished_text_content AS t, pg_column_size(polished_text_content) AS pglz FROM chapters WHERE polished_text_content IS NOT NULL LIMIT %(n)s"),
    ("reader_fmt", "SELECT reader_formatted_text_content AS t, pg_column_size(reader_formatted_text_content) AS pglz FROM chapters WHERE reader_formatted_text_content IS NOT NULL LIMIT %(n)s"),
]


def fmt_mb(b: float) -> str:
    return f"{b / 1e6:.1f}MB" if b >= 1e6 else f"{b / 1e3:.1f}KB"


def sample_group(conn, sql: str, n: int) -> list[tuple[str, int]]:
    rows = conn.execute(sql, {"n": n}).fetchall()
    return [(r["t"], int(r["pglz"] or 0)) for r in rows if r["t"]]


def bench_group(label: str, rows: list[tuple[str, int]]) -> dict | None:
    if not rows:
        print(f"  {label:<12} (no rows)")
        return None
    texts = [t for t, _ in rows]
    blobs = [t.encode("utf-8") for t in texts]

    raw_total = sum(len(b) for b in blobs)
    pglz_total = sum(p for _, p in rows)

    # train a dictionary on this group's blobs (helps short, similar rows)
    try:
        dict_data = zstd.train_dictionary(64 * 1024, blobs)
        cdict = dict_data
    except Exception:
        cdict = None

    def zstd_total(level: int, use_dict: bool) -> tuple[int, float, float]:
        if use_dict and cdict is not None:
            c = zstd.ZstdCompressor(level=level, dict_data=cdict)
            d = zstd.ZstdDecompressor(dict_data=cdict)
        else:
            c = zstd.ZstdCompressor(level=level)
            d = zstd.ZstdDecompressor()
        t0 = time.perf_counter()
        comp = [c.compress(b) for b in blobs]
        enc_s = time.perf_counter() - t0
        total = sum(len(x) for x in comp)
        t1 = time.perf_counter()
        for x in comp:
            d.decompress(x)
        dec_s = time.perf_counter() - t1
        return total, enc_s, dec_s

    z3, z3e, _ = zstd_total(3, False)
    z9, z9e, _ = zstd_total(9, False)
    z19, z19e, z19d = zstd_total(19, False)
    z19dict, _, _ = zstd_total(19, True)
    lz4_total = sum(len(lz4.frame.compress(b)) for b in blobs)

    mbps = (raw_total / 1e6) / enc if (enc := z19e) else 0
    dmbps = (raw_total / 1e6) / z19d if z19d else 0

    print(f"  {label:<12} rows={len(rows):>4} raw={fmt_mb(raw_total):>8} "
          f"pglz={fmt_mb(pglz_total):>8} | "
          f"z3={fmt_mb(z3):>8} z9={fmt_mb(z9):>8} z19={fmt_mb(z19):>8} "
          f"z19+dict={fmt_mb(z19dict):>8} lz4={fmt_mb(lz4_total):>8}")
    print(f"  {'':<12} ratio vs pglz: z9={pglz_total/z9:.2f}x z19={pglz_total/z19:.2f}x "
          f"z19+dict={pglz_total/z19dict:.2f}x lz4={pglz_total/lz4_total:.2f}x  "
          f"| z19 enc={mbps:.0f}MB/s dec={dmbps:.0f}MB/s")
    return {"pglz": pglz_total, "z19": z19, "z19dict": z19dict, "z9": z9}


def project(conn) -> None:
    print("\n=== DB-wide projection (scanning current on-disk pglz totals; ~10-20s) ===")
    row = conn.execute(
        """
        SELECT
          COALESCE(sum(pg_column_size(raw_text_content)),0) AS raw,
          COALESCE(sum(pg_column_size(translated_text_content)),0) AS translated,
          COALESCE(sum(pg_column_size(polished_text_content)),0) AS polished,
          COALESCE(sum(pg_column_size(reader_formatted_text_content)),0) AS reader
        FROM chapters
        """
    ).fetchone()
    total_pglz = sum(int(v) for v in row.values())
    print(f"  current text on-disk (pglz): raw={fmt_mb(row['raw'])} translated={fmt_mb(row['translated'])} "
          f"polished={fmt_mb(row['polished'])} reader={fmt_mb(row['reader'])} "
          f"=> total {total_pglz/1e9:.2f} GB")
    print("  (apply the per-group z19/z19+dict 'ratio vs pglz' above to estimate the new total.)")


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 2 zstd vs pglz benchmark (read-only).")
    ap.add_argument("--sample", type=int, default=200, help="rows per group")
    ap.add_argument("--no-projection", action="store_true", help="skip the DB-wide pglz scan")
    args = ap.parse_args()

    with psycopg.connect(database_url(), row_factory=dict_row) as conn:
        print(f"=== zstd vs pglz benchmark (sample={args.sample}/group) ===")
        agg = {"pglz": 0, "z19": 0, "z19dict": 0, "z9": 0}
        for label, sql in GROUPS:
            rows = sample_group(conn, sql, args.sample)
            res = bench_group(label, rows)
            if res:
                for k in agg:
                    agg[k] += res[k]
        if agg["pglz"]:
            print(f"\n=== sampled totals ===\n  pglz={fmt_mb(agg['pglz'])} "
                  f"z9={fmt_mb(agg['z9'])} ({agg['pglz']/agg['z9']:.2f}x) "
                  f"z19={fmt_mb(agg['z19'])} ({agg['pglz']/agg['z19']:.2f}x) "
                  f"z19+dict={fmt_mb(agg['z19dict'])} ({agg['pglz']/agg['z19dict']:.2f}x)")
        if not args.no_projection:
            project(conn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
