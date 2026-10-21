"""Непрерывная дедупликация: data/new_summary.db -> data/unique.db + Qdrant.

    python scripts/run_dedup.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from news_pipeline.dedup.drop_dublicate import NewsDeduplicator  # noqa: E402


def main() -> None:
    """Запускает непрерывную дедупликацию подготовленных новостей.

    Новости с заголовком и первым абзацем в new_summary.db кладёт
    scripts/prepare_news.py (из news.db).
    """
    deduplicator = NewsDeduplicator(verbose=True)

    while True:
        results = deduplicator.process_pending_news()

        print(
            f"[main] Обработано: {len(results)}. "
            f"Следующая проверка через 10 секунд."
        )
        time.sleep(10)


if __name__ == "__main__":
    main()
