"""Создать таблицы парсера или проверить существующую схему без изменений."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="проверить существующую БД без записи")
    args = parser.parse_args()

    from sqlalchemy import inspect
    from parser_app import models  # register all tables
    from parser_app.database import Base, engine

    try:
        # SQLite connect would create a missing file even for a read-only check.
        if args.check and engine.dialect.name == "sqlite" and engine.url.database != ":memory:":
            if not engine.url.database or not Path(engine.url.database).exists():
                print("Database does not exist; run init_db.py without --check.")
                return 1
        if not args.check:
            Base.metadata.create_all(engine)
        inspector = inspect(engine)
        problems = []
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                problems.append(f"Missing table: {table.name}")
                continue
            existing = {column["name"] for column in inspector.get_columns(table.name)}
            missing = set(table.columns.keys()) - existing
            if missing:
                problems.append(f"Missing columns in {table.name}: {', '.join(sorted(missing))}")
            else:
                print(f"OK: {table.name}")
        for problem in problems:
            print(problem)
        if problems:
            print("Existing schemas are not migrated automatically. Check the database configuration.")
        return int(bool(problems))
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
