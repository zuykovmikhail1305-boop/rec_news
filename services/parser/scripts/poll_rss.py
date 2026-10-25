"""Collect RSS links once or periodically; article HTML is fetched by the queue worker."""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="discover URLs without database writes or dispatch")
    parser.add_argument("--force", action="store_true", help="ignore next-poll time and HTTP validators, but not an active lease")
    args = parser.parse_args()
    from parser_app.rss import poll_rss, rss_config
    config = rss_config()
    if not args.dry_run:
        from parser_app.database import engine
        from parser_app.schema import initialize_database
        initialize_database(engine)
    try:
        while True:
            result = poll_rss(config=config, dry_run=args.dry_run, force=args.force)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            if args.once:
                return int(any(feed["status"] == "failed" for feed in result["feeds"]))
            time.sleep(config.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        from parser_app.database import engine
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
