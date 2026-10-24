import argparse
import json
import sys

import httpx

from .collector import CollectionError, collect, result
from .database import Base, SessionLocal, engine


def main():
    parser = argparse.ArgumentParser(description="Collect one Central Bank publication")
    parser.add_argument("url")
    args = parser.parse_args()
    Base.metadata.create_all(engine)
    try:
        with SessionLocal() as db, httpx.Client(timeout=30, follow_redirects=False,
                headers={"User-Agent": "RecNewsHackathonCollector/0.1"}) as client:
            status, item = collect(db, client, args.url)
            print(json.dumps(result(status, item), ensure_ascii=False, indent=2))
        return 0
    except (ValueError, CollectionError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
