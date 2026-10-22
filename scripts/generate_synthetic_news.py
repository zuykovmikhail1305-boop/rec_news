"""Генерирует синтетический корпус бизнес-новостей через LLM.

    python scripts/generate_synthetic_news.py                          # 30 тем × 6 событий + 15 «шумовых»
    python scripts/generate_synthetic_news.py --topics 2 --events 2 --noise 1 --output tmp/pilot.jsonl
    python scripts/generate_synthetic_news.py --load data/news.db      # загрузить готовый корпус

Новости разных жанров (молния, новость, обзор, аналитика) и изданий, период —
4 недели до 21.10.2026. Повторный запуск берёт готовые ответы из кэша LLM.
Расход токенов — поле ``usage`` в ``data/logs/llm_calls.jsonl``.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from news_pipeline.references import TOPICS  # noqa: E402
from news_pipeline.test_data.synthetic_news import (  # noqa: E402
    OUTPUT_PATH,
    PERIOD_DAYS,
    default_corpus,
    generate,
    load_into_news_db,
)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--topics", type=int, default=len(TOPICS), help="сколько тем таксономии взять")
    parser.add_argument("--events", type=int, default=6, help="событий на тему")
    parser.add_argument("--noise", type=int, default=15, help="новостей без влияния на бизнес")
    parser.add_argument("--days", type=int, default=PERIOD_DAYS, help="длина периода в днях")
    parser.add_argument("--workers", type=int, default=4, help="параллельных запросов к LLM")
    parser.add_argument("--output", type=Path, default=None, help=f"куда писать (по умолчанию {OUTPUT_PATH.name})")
    parser.add_argument("--load", type=Path, help="не генерировать, а загрузить корпус в эту news.db")
    args = parser.parse_args()
    if sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    if args.load:
        path = args.output or default_corpus()
        print(f"Загружено в {args.load}: {load_into_news_db(args.load, path)} из {path}")
    else:
        generate(events_per_topic=args.events, noise_events=args.noise, period_days=args.days,
                 workers=args.workers, output_path=args.output or OUTPUT_PATH, topics=TOPICS[: args.topics])
