#!/usr/bin/env python3
"""Phase 1 DB maintenance: reclaim TOAST bloat safely with VACUUM FULL.

Background: the chapters TOAST measured ~21% dead + ~13% free space (~3.65 GB
reclaimable) because autovacuum had never run on the big tables. Migration
022_autovacuum_tuning.sql prevents recurrence; this script does the one-time reclaim.

Design constraints (validated by Codex review rounds 1-3):
- VACUUM / VACUUM FULL / REINDEX cannot run inside a transaction block, so this script
  uses a dedicated autocommit connection (NOT story_db.db.connect(), which is transactional).
- Default mode is a read-only REPORT. Mutating actions require explicit gates:
  backup confirmation, writers-stopped confirmation, and a free-space check.
- pgstattuple is optional: degrade to pg_total_relation_size estimates if absent.
- This script does NOT prune story_jobs: 'done' rows act as enqueue idempotency
  tombstones (enqueue_chapter_job preserves done/running on conflict), and producers
  may re-enqueue from chapter state. Pruning is a separate, job-type-aware tool.

Usage:
  # report only (safe, no changes)
  viterbox/venv/bin/python scripts/story_pipeline/db_maintenance.py

  # actually reclaim (stop polish-worker/crawlers first, take a backup)
  viterbox/venv/bin/python scripts/story_pipeline/db_maintenance.py \
      --vacuum-full --writers-stopped --pg-dump-to /backups/betterbox.dump \
      --free-space-path /var/lib/docker
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from story_db.story_pipeline_db.db import database_url  # noqa: E402

# Tables worth reclaiming in Phase 1 (the bloat lives here).
TARGET_TABLES = ("chapters", "story_jobs")


def open_autocommit() -> psycopg.Connection:
    """VACUUM/REINDEX require autocommit (no surrounding transaction block)."""
    return psycopg.connect(database_url(), autocommit=True, row_factory=dict_row)


def fetch_all(conn: psycopg.Connection, sql: str, params=None) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params or ()).fetchall()]


def fetch_one(conn: psycopg.Connection, sql: str, params=None):
    row = conn.execute(sql, params or ()).fetchone()
    return dict(row) if row else None


def db_size_pretty(conn: psycopg.Connection) -> str:
    row = fetch_one(conn, "SELECT pg_size_pretty(pg_database_size(current_database())) AS s")
    return row["s"] if row else "?"


def db_size_bytes(conn: psycopg.Connection) -> int:
    row = fetch_one(conn, "SELECT pg_database_size(current_database()) AS b")
    return int(row["b"]) if row else 0


def table_sizes(conn: psycopg.Connection) -> list[dict]:
    return fetch_all(
        conn,
        """
        SELECT c.relname AS tbl,
               pg_total_relation_size(c.oid) AS total_bytes,
               pg_size_pretty(pg_total_relation_size(c.oid)) AS total,
               pg_size_pretty(pg_relation_size(c.oid)) AS heap,
               pg_size_pretty(COALESCE(pg_total_relation_size(c.reltoastrelid), 0)) AS toast
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'r' AND n.nspname = 'public'
        ORDER BY pg_total_relation_size(c.oid) DESC
        LIMIT 10
        """,
    )


def has_pgstattuple(conn: psycopg.Connection) -> bool:
    """Detection-only: never CREATE EXTENSION here.

    The report path must stay read-only (no catalog writes before the backup/writers
    gates). If pgstattuple is not already installed we degrade to size estimates.
    To enable it, install once out-of-band: CREATE EXTENSION pgstattuple;
    """
    row = fetch_one(
        conn,
        "SELECT 1 AS ok FROM pg_extension WHERE extname = 'pgstattuple'",
    )
    if row:
        return True
    avail = fetch_one(
        conn,
        "SELECT 1 AS ok FROM pg_available_extensions WHERE name = 'pgstattuple'",
    )
    if avail:
        print("[bloat] pgstattuple available but not installed; run 'CREATE EXTENSION pgstattuple' "
              "to get exact numbers. Using size estimates for now.")
    return False


def print_bloat(conn: psycopg.Connection) -> None:
    if has_pgstattuple(conn):
        print("\n=== TOAST bloat (pgstattuple) ===")
        for tbl in TARGET_TABLES:
            toast = fetch_one(
                conn,
                "SELECT reltoastrelid FROM pg_class WHERE relname = %s",
                (tbl,),
            )
            if not toast or not toast["reltoastrelid"]:
                continue
            stat = fetch_one(
                conn,
                "SELECT table_len, dead_tuple_percent, free_percent "
                "FROM pgstattuple(%s)",
                (toast["reltoastrelid"],),
            )
            if stat:
                reclaimable = stat["table_len"] * (
                    (stat["dead_tuple_percent"] + stat["free_percent"]) / 100.0
                )
                print(
                    f"  {tbl} TOAST: dead={stat['dead_tuple_percent']}% "
                    f"free={stat['free_percent']}% "
                    f"~reclaimable={reclaimable / 1e9:.2f} GB"
                )
    else:
        print("\n=== bloat (estimate; pgstattuple unavailable) ===")
        print("  Using pg_total_relation_size only; run VACUUM (FULL) to reclaim dead/free space.")


def print_sizes(conn: psycopg.Connection, title: str) -> int:
    total = db_size_bytes(conn)
    print(f"\n=== {title}: DB = {db_size_pretty(conn)} ===")
    for r in table_sizes(conn):
        print(f"  {r['tbl']:<26} total={r['total']:>9}  heap={r['heap']:>9}  toast={r['toast']:>9}")
    return total


def check_free_space(conn: psycopg.Connection, path: str, force: bool) -> bool:
    """VACUUM FULL rewrites a table -> needs free space ~= largest table size."""
    biggest = fetch_one(
        conn,
        """
        SELECT c.relname AS tbl, pg_total_relation_size(c.oid) AS bytes
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'r' AND n.nspname = 'public'
        ORDER BY pg_total_relation_size(c.oid) DESC LIMIT 1
        """,
    )
    need = int(biggest["bytes"]) if biggest else 0
    try:
        free = shutil.disk_usage(path).free
    except Exception as exc:
        print(f"[free-space] cannot stat '{path}': {exc}")
        if not force:
            print("[free-space] ABORT (use --force to skip this check, or set --free-space-path)")
            return False
        return True
    print(
        f"\n=== free space check ===\n  path={path} free={free / 1e9:.1f} GB  "
        f"largest_table={biggest['tbl'] if biggest else '?'} ({need / 1e9:.1f} GB)"
    )
    if free < need * 2 and not force:
        print(
            "[free-space] ABORT: free < 2x largest table (VACUUM FULL needs a full rewrite). "
            "Free up space, point --free-space-path at the real DB volume, or pass --force."
        )
        return False
    return True


def run_pg_dump(path: str) -> bool:
    print(f"\n=== backup: pg_dump -Fc -> {path} ===")
    try:
        subprocess.run(
            ["pg_dump", "-Fc", "-d", database_url(), "-f", path],
            check=True,
        )
    except FileNotFoundError:
        print("[backup] pg_dump not found on PATH. Install postgresql-client or use --backup-done.")
        return False
    except subprocess.CalledProcessError as exc:
        print(f"[backup] pg_dump failed: {exc}")
        return False
    size = Path(path).stat().st_size if Path(path).exists() else 0
    print(f"[backup] ok ({size / 1e6:.1f} MB)")
    return True


def ensure_pg_repack(conn: psycopg.Connection) -> bool:
    ext = fetch_one(conn, "SELECT 1 AS ok FROM pg_extension WHERE extname = 'pg_repack'")
    client = shutil.which("pg_repack")
    if ext and client:
        return True
    print(
        "[--online] pg_repack not available "
        f"(extension={'yes' if ext else 'NO'}, client={'yes' if client else 'NO'}). "
        "Install pg_repack or drop --online to use VACUUM FULL."
    )
    return False


def do_vacuum_full(conn: psycopg.Connection) -> None:
    for tbl in TARGET_TABLES:
        print(f"\n[vacuum] VACUUM (FULL, ANALYZE) {tbl} ... (this rewrites the table)")
        conn.execute(f"VACUUM (FULL, ANALYZE) {tbl}")
        print(f"[vacuum] done {tbl}")


def do_repack(tbl: str) -> bool:
    print(f"\n[repack] pg_repack --table {tbl} ...")
    try:
        subprocess.run(["pg_repack", "-d", database_url(), "--table", tbl], check=True)
    except subprocess.CalledProcessError as exc:
        print(f"[repack] failed for {tbl}: {exc}")
        return False
    print(f"[repack] done {tbl}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 1 DB maintenance (report + VACUUM FULL reclaim).")
    ap.add_argument("--vacuum-full", action="store_true", help="Run VACUUM (FULL, ANALYZE) to reclaim bloat (default tool).")
    ap.add_argument("--online", action="store_true", help="Use pg_repack instead of VACUUM FULL (only if pg_repack installed).")
    ap.add_argument("--writers-stopped", action="store_true", help="Confirm polish-worker/crawlers are stopped (required for reclaim).")
    ap.add_argument("--pg-dump-to", default="", help="Run pg_dump -Fc to this path before reclaiming.")
    ap.add_argument("--backup-done", action="store_true", help="Assert a backup already exists (skip pg_dump).")
    ap.add_argument("--free-space-path", default="/", help="Filesystem path of the DB volume to check free space on.")
    ap.add_argument("--force", action="store_true", help="Skip the free-space safety check.")
    args = ap.parse_args()

    reclaim = args.vacuum_full or args.online

    with open_autocommit() as conn:
        before = print_sizes(conn, "BEFORE")
        print_bloat(conn)

        if not reclaim:
            print("\n[report-only] No changes made. Add --vacuum-full (after stopping writers + backup) to reclaim.")
            return 0

        # --- gates ---
        if not args.writers_stopped:
            print("\n[ABORT] Reclaim requires --writers-stopped (stop polish-worker, crawlers, enqueuers first).")
            return 2
        if not args.backup_done:
            if not args.pg_dump_to:
                print("\n[ABORT] Reclaim requires a backup: pass --pg-dump-to PATH or --backup-done.")
                return 2
            if not run_pg_dump(args.pg_dump_to):
                return 2
        if not check_free_space(conn, args.free_space_path, args.force):
            return 2
        if args.online and not ensure_pg_repack(conn):
            return 2

        # --- reclaim ---
        if args.online:
            for tbl in TARGET_TABLES:
                if not do_repack(tbl):
                    return 3
            conn.execute("ANALYZE")
        else:
            do_vacuum_full(conn)

        after = print_sizes(conn, "AFTER")
        print(f"\n=== reclaimed: {(before - after) / 1e9:.2f} GB "
              f"({before / 1e9:.2f} -> {after / 1e9:.2f} GB) ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
