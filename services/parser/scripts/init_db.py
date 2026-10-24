"""Create/verify parser tables in the configured shared database without resetting data."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify without schema writes")
    args = parser.parse_args()
    from parser_app.database import engine
    from parser_app.schema import create_tables, schema_problems, target_schema
    try:
        print("Database:", engine.url.render_as_string(hide_password=True), "schema:", target_schema(engine) or "default")
        if args.check and engine.dialect.name == "sqlite" and engine.url.database != ":memory:":
            if not engine.url.database or not Path(engine.url.database).exists():
                print("Database does not exist; run init_db.py without --check.")
                return 1
        if not args.check:
            create_tables(engine)
        problems = schema_problems(engine)
        for problem in problems:
            print(problem)
        if not problems:
            print("OK: shared news tables and parser queue tables are ready")
        return int(bool(problems))
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
