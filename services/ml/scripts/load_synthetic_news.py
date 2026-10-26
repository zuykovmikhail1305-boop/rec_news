"""Переносит размеченные синтетические новости в базу новостей PostgreSQL (``ml_news.news``).

    python scripts/load_synthetic_news.py                                   # корпус по умолчанию
    python scripts/load_synthetic_news.py fixtures/synthetic_news.jsonl     # другой корпус

Только вставка: новости кладутся со статусом «новая» и эталонной разметкой в колонках ``test_*``,
``document_id`` — ``syn_<key>``. Уже загруженные пропускаются, обработку не запускает.
База — ``REC_NEWS_DATABASE_URL`` из ``.env``; без PostgreSQL скрипт ничего не делает.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from news_pipeline.config import NEWS_DB  # noqa: E402
from news_pipeline.storage import db  # noqa: E402
from news_pipeline.test_data.synthetic_news import default_corpus, load_into_news_db  # noqa: E402

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("corpus", nargs="?", type=Path, default=None,
                        help="JSONL с синтетикой (по умолчанию fixtures/synthetic_news_v2.jsonl или synthetic_news.jsonl)")
    args = parser.parse_args()
    if sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    if not db.POSTGRES:
        sys.exit("REC_NEWS_DATABASE_URL не задан — нужна база PostgreSQL")
    corpus = args.corpus or default_corpus()
    added = load_into_news_db(NEWS_DB, corpus)
    print(f"{corpus.name}: добавлено {added} новостей в {db.SCHEMA_PREFIX}news.news")
