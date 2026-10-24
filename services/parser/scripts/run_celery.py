"""Запуск Celery worker или Beat: python scripts/run_celery.py worker|beat."""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("worker", "beat"))
    parser.add_argument("--pool", choices=("solo", "prefork", "threads"),
                        default="solo" if os.name == "nt" else "prefork")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    if args.concurrency < 1:
        parser.error("--concurrency must be positive")
    os.environ.setdefault("PARSER_QUEUE_BACKEND", "celery")
    from parser_app.broker import queue_backend
    if queue_backend() != "celery":
        parser.error("Set PARSER_QUEUE_BACKEND=celery for the API, worker and Beat")
    from parser_app.celery_app import celery_app
    from parser_app.database import Base, engine
    from parser_app import models
    Base.metadata.create_all(engine)
    engine.dispose()
    if args.mode == "worker":
        celery_app.worker_main(["worker", "--loglevel", args.log_level, "--pool", args.pool,
            "--concurrency", str(args.concurrency), "--queues", "parser.publications,parser.dispatch"])
    else:
        state = ROOT / "data"
        state.mkdir(exist_ok=True)
        celery_app.start(["beat", "--loglevel", args.log_level, "--schedule", str(state / "celerybeat-schedule")])


if __name__ == "__main__":
    main()
