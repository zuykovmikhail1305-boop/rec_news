import time

from drop_dublicate import NewsDeduplicator


def main() -> None:
    """Запускает непрерывную обработку входящих новостей."""
    deduplicator = NewsDeduplicator(
        source_database_path="news.db",
        unique_database_path="unique.db",
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