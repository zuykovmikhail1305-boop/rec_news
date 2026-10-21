"""Запускает API ML-части: http://127.0.0.1:8000/docs

    python scripts/run_api.py
    python scripts/run_api.py --port 8080 --no-llm
"""

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-llm", action="store_true", help="работать без LLM")
    parser.add_argument("--data-dir", type=Path, help="каталог баз вместо data/")
    args = parser.parse_args()
    if args.no_llm:
        os.environ["REC_NEWS_USE_LLM"] = "0"
    if args.data_dir:
        os.environ["REC_NEWS_DATA_DIR"] = str(args.data_dir.resolve())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    import uvicorn

    from news_pipeline.api import app

    uvicorn.run(app, host=args.host, port=args.port)
