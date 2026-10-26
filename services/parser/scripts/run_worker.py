"""Запуск воркера очереди: --once, --limit 20, --poll-interval 2."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __name__ == "__main__":
    from parser_app.worker import main

    raise SystemExit(main())
