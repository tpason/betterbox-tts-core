from __future__ import annotations

from pathlib import Path

from .story_pipeline_db.db import connect


def collect_migration_files() -> list[Path]:
    """Return SQL migrations in global numeric filename order (001 before 024)."""
    base = Path(__file__).resolve().parent
    migration_dirs = [base / "postgres" / "init", base / "migrations"]
    by_name: dict[str, Path] = {}
    for migrations_dir in migration_dirs:
        if not migrations_dir.is_dir():
            continue
        for sql_path in migrations_dir.glob("*.sql"):
            # Tracked `migrations/` wins over gitignored duplicate in postgres/init.
            if sql_path.name not in by_name or migrations_dir.name == "migrations":
                by_name[sql_path.name] = sql_path
    return [by_name[name] for name in sorted(by_name)]


def main() -> None:
    sql_files = collect_migration_files()
    if not sql_files:
        raise SystemExit("Không tìm thấy migration trong story_db/migrations hoặc story_db/postgres/init")

    with connect() as conn:
        for sql_path in sql_files:
            print(f"apply {sql_path}")
            sql = sql_path.read_text(encoding="utf-8")
            for statement in [item.strip() for item in sql.split(";") if item.strip()]:
                conn.execute(statement)

    print("Done.")


if __name__ == "__main__":
    main()
