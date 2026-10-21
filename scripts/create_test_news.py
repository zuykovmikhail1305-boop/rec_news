"""Пересоздаёт data/news.db с 20 тестовыми новостями.

    python scripts/create_test_news.py
    python scripts/create_test_news.py --db путь/к/news.db
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from news_pipeline.test_data.create_news_db import DB_PATH, NEWS, build  # noqa: E402

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", type=Path, default=DB_PATH)
    args = parser.parse_args()
    build(args.db)
    print(f"Создана база {args.db} ({len(NEWS)} новостей)")
