"""Run: python -m parser_app.worker [--once] [--limit 20]."""
import argparse
import json
import logging
import time

import httpx

from .database import Base, SessionLocal, engine
from .queue import claim_next, process_claim
from .models import NewsRaw

logger = logging.getLogger(__name__)


def run_batch(factory=SessionLocal, *, limit=20):
    counts = {"ready": 0, "retry": 0, "failed": 0, "lease_lost": 0}
    with httpx.Client(timeout=30, follow_redirects=False,
                     headers={"User-Agent": "RecNewsHackathonCollector/0.1"}) as client:
        for _ in range(limit):
            with factory() as db:
                claim = claim_next(db)
            if claim is None:
                break
            try:
                with factory() as db:
                    status = process_claim(db, client, claim)
                    error = db.get(NewsRaw, claim.id).last_error
                counts[status] += 1
                logger.info("job=%s attempt=%s status=%s url=%s error=%s", claim.id, claim.attempt, status, claim.url, error)
            except Exception:
                # Leave the claim leased; another worker can recover it after expiry.
                logger.exception("Unexpected error job=%s url=%s", claim.id, claim.url)
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Process currently eligible jobs and exit")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--poll-interval", type=float, default=2)
    args = parser.parse_args()
    if args.limit < 1 or args.poll_interval < 0.1:
        parser.error("--limit >= 1, --poll-interval >= 0.1")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from .broker import queue_backend
    if queue_backend() != "local":
        parser.error("Use scripts/run_celery.py worker and beat when PARSER_QUEUE_BACKEND=celery")
    from .schema import initialize_database
    initialize_database(engine)
    try:
        while True:
            counts = run_batch(limit=args.limit)
            if args.once:
                print(json.dumps(counts))
                return 0
            time.sleep(args.poll_interval)
    except KeyboardInterrupt:
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
