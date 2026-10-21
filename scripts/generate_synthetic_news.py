"""Генерирует синтетический корпус бизнес-новостей через LLM.

    python scripts/generate_synthetic_news.py                   # все 30 тем, по 5 событий
    python scripts/generate_synthetic_news.py --topics 2 --events 2   # быстрый пробный прогон
    python scripts/generate_synthetic_news.py --load data/news.db     # загрузить готовый корпус

Повторный запуск берёт готовые ответы из кэша LLM.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from news_pipeline.references import TOPICS  # noqa: E402
from news_pipeline.test_data.synthetic_news import OUTPUT_PATH, generate, load_into_news_db  # noqa: E402

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--topics", type=int, default=len(TOPICS), help="сколько тем таксономии взять")
    parser.add_argument("--events", type=int, default=5, help="событий на тему")
    parser.add_argument("--workers", type=int, default=3, help="параллельных запросов к LLM")
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--load", type=Path, help="не генерировать, а загрузить корпус в эту news.db")
    args = parser.parse_args()
    if args.load:
        print(f"Загружено в {args.load}: {load_into_news_db(args.load, args.output)}")
    else:
        generate(events_per_topic=args.events, workers=args.workers, output_path=args.output,
                 topics=TOPICS[: args.topics])
