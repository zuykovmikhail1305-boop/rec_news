"""Запуск HTTP API парсера; по умолчанию http://127.0.0.1:8001/docs."""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.getenv("PARSER_API_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=os.getenv("PARSER_API_PORT", "8001"))
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    import uvicorn
    from parser_app.main import app

    # Synchronous endpoints share a process-local rate limiter: one worker only.
    uvicorn.run(app, host=args.host, port=args.port, workers=1)


if __name__ == "__main__":
    main()
