import time

from drop_dublicate import NewsDeduplicator


def main() -> None:
    """Запускает непрерывную дедупликацию новостей, уже обработанных LLM.

    Новости в new_summary.db кладёт news_summarizer.py (из news.db).
    """
    deduplicator = NewsDeduplicator(
        source_database_path="new_summary.db",
        unique_database_path="unique.db",
        qdrant_path="qdrant_data",
        verbose=True,
    )

    while True:
        results = deduplicator.process_pending_news()

        print(
            f"[main] Обработано: {len(results)}. "
            f"Следующая проверка через 10 секунд."
        )
        time.sleep(10)


if __name__ == "__main__":
    main()