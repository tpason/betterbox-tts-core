from __future__ import annotations

from pathlib import Path

from .story_pipeline_db.db import connect


def main() -> None:
    migrations_dir = Path(__file__).resolve().parent / "postgres" / "init"
    sql_files = sorted(migrations_dir.glob("*.sql"))
    if not sql_files:
        raise SystemExit(f"Không tìm thấy migration trong {migrations_dir}")

    with connect() as conn:
        for sql_path in sql_files:
            print(f"apply {sql_path}")
            sql = sql_path.read_text(encoding="utf-8")
            for statement in [item.strip() for item in sql.split(";") if item.strip()]:
                conn.execute(statement)

    print("Done.")


if __name__ == "__main__":
    main()
