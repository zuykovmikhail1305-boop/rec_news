"""Передать ранее собранные ready-публикации в ML в той же PostgreSQL-БД."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--after-id", type=int, default=0)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    if args.after_id < 0 or not 1 <= args.limit <= 1000:
        parser.error("--after-id >= 0, 1 <= --limit <= 1000")
    from sqlalchemy import select
    from parser_app.database import SessionLocal, engine
    from parser_app.models import NewsRaw
    from parser_app.ml_export import export_publication
    from parser_app.schema import initialize_database
    from parser_app.settings import ml_export_enabled
    if not ml_export_enabled(engine.dialect.name):
        parser.error("Enable ML export on the shared PostgreSQL database")
    try:
        initialize_database(engine)
        with SessionLocal() as db:
            rows = list(db.scalars(select(NewsRaw).where(NewsRaw.status == "ready",
                NewsRaw.id > args.after_id).order_by(NewsRaw.id).limit(args.limit)))
            for row in rows:
                export_publication(db, row)
            db.commit()
            print({"processed": len(rows), "last_id": rows[-1].id if rows else args.after_id})
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
