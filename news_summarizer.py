"""Анализ входящих новостей LLM и перенос их в ``new_summary.db``.

Каждая новость из ``news.db`` проходит через :class:`news_analyzer.NewsAnalyzer`
и сохраняется в ``new_summary.db`` в таблицу ``news_summary``:

* ``document_id`` — идентификатор новости;
* ``text`` — полный текст новости;
* ``summary`` — краткая выжимка от LLM (для векторизации и дедупликации);
* ``audience_text`` — шаблон характеристики аудитории (см. ``audience_profile.py``);
* ``metadata`` — JSON: исходные метаданные новости и ключ ``analysis``
  с результатом анализа (``is_business_relevant``, структура ``audience``,
  ``model``, ``analyzed_at``).

Новость удаляется из ``news.db`` только после того, как результат записан
в ``new_summary.db``. Если LLM не справилась, новость остаётся в ``news.db``
и будет обработана при следующем запуске.

Запуск:

    python news_summarizer.py            # обработать всё, что есть в news.db
    python news_summarizer.py --watch    # обрабатывать постоянно, как main.py
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from news_analyzer import DEFAULT_BASE_URL, ChatClient, LLMError, NewsAnalyzer
from news_db import (
    StoredNews,
    SummarizedNews,
    delete_news,
    ensure_summary_schema,
    insert_summaries,
    read_news,
    summary_ids,
)

BASE_DIR = Path(__file__).resolve().parent

@dataclass(frozen=True)
class ProcessingReport:
    """Итог одного прохода по ``news.db``."""

    processed: list[str] = field(default_factory=list)
    already_done: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)


class NewsSummarizer:
    """Переносит новости из ``news.db`` в ``new_summary.db`` через LLM-анализ."""

    def __init__(
        self,
        *,
        source_database_path: str | Path = BASE_DIR / "news.db",
        summary_database_path: str | Path = BASE_DIR / "new_summary.db",
        analyzer: NewsAnalyzer | None = None,
        analyzer_factory: Callable[[], NewsAnalyzer] = NewsAnalyzer,
        max_attempts: int = 3,
        verbose: bool = True,
    ) -> None:
        """Создаёт обработчик.

        ``analyzer`` создаётся лениво через ``analyzer_factory`` — только когда
        действительно нужен вызов LLM. ``max_attempts`` — сколько раз за время
        жизни объекта пробовать новость, на которой LLM падает, прежде чем
        перестать тратить на неё токены (новость при этом остаётся в ``news.db``).
        """
        self.source_database_path = Path(source_database_path)
        self.summary_database_path = Path(summary_database_path)
        self._analyzer = analyzer
        self._analyzer_factory = analyzer_factory
        self.max_attempts = max_attempts
        self.verbose = verbose
        self._failures: dict[str, int] = {}
        ensure_summary_schema(self.summary_database_path)

    @property
    def analyzer(self) -> NewsAnalyzer:
        if self._analyzer is None:
            self._analyzer = self._analyzer_factory()
            self._print(f"LLM: {self._analyzer.client.model} ({self._analyzer.client.base_url})")
        return self._analyzer

    def process_pending(self, limit: int | None = None) -> ProcessingReport:
        """Обрабатывает новости из ``news.db`` по одной.

        Порядок — по времени публикации. Каждая новость сразу записывается
        в ``new_summary.db`` и удаляется из ``news.db``, поэтому прерывание
        посреди прохода ничего не теряет. ``limit`` ограничивает число
        вызовов LLM за проход.
        """
        report = ProcessingReport()
        if not self.source_database_path.exists():
            self._print(f"{self.source_database_path.name} не найдена — обрабатывать нечего.")
            return report

        pending = sorted(
            read_news(self.source_database_path),
            key=lambda item: item.metadata.get("publication_time") or "",
        )
        done = summary_ids(self.summary_database_path)
        self._print(f"Новостей в {self.source_database_path.name}: {len(pending)}")

        llm_calls = 0
        for item in pending:
            if item.document_id in done:
                # Уже обработана в прошлый раз, но не успела удалиться.
                delete_news(self.source_database_path, [item.document_id])
                report.already_done.append(item.document_id)
                self._print(f"{item.document_id}: уже есть в {self.summary_database_path.name}, удалена из источника")
                continue
            if self._failures.get(item.document_id, 0) >= self.max_attempts:
                report.skipped.append(item.document_id)
                continue
            if limit is not None and llm_calls >= limit:
                break

            try:
                analyzer = self.analyzer
            except LLMError as error:
                self._print(f"LLM недоступна, проход остановлен, новости остаются в источнике: {error}")
                break

            llm_calls += 1
            started = time.monotonic()
            try:
                summarized = self._analyze(item, analyzer)
            except (LLMError, ValueError) as error:
                self._failures[item.document_id] = self._failures.get(item.document_id, 0) + 1
                report.failed[item.document_id] = str(error)
                self._print(
                    f"{item.document_id}: ошибка LLM (попытка {self._failures[item.document_id]}"
                    f"/{self.max_attempts}), новость остаётся в источнике: {error}"
                )
                continue

            insert_summaries(self.summary_database_path, [summarized])
            delete_news(self.source_database_path, [item.document_id])
            report.processed.append(item.document_id)
            relevant = summarized.metadata["analysis"]["is_business_relevant"]
            self._print(
                f"{item.document_id}: готово за {time.monotonic() - started:.1f} с"
                f"{'' if relevant else ' (не бизнес-новость)'} — {item.metadata.get('title', '')}"
            )

        self._print(
            f"Итог: обработано {len(report.processed)}, уже было {len(report.already_done)}, "
            f"ошибок {len(report.failed)}, пропущено {len(report.skipped)}"
        )
        return report

    @staticmethod
    def _analyze(item: StoredNews, analyzer: NewsAnalyzer) -> SummarizedNews:
        """Вызывает LLM для одной новости и собирает запись для ``new_summary.db``."""
        analysis = analyzer.analyze(item.text, title=item.metadata.get("title"))
        return SummarizedNews(
            document_id=item.document_id,
            text=item.text,
            summary=analysis.summary,
            audience_text=analysis.audience_text,
            metadata={
                **item.metadata,
                "analysis": {
                    "is_business_relevant": analysis.is_business_relevant,
                    "audience": analysis.audience.to_dict(),
                    "model": analysis.model,
                    "analyzed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                },
            },
        )

    def _print(self, message: str) -> None:
        if self.verbose:
            print(f"[NewsSummarizer] {message}", flush=True)


def main(argv: Iterable[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--news-db", type=Path, default=BASE_DIR / "news.db")
    parser.add_argument("--summary-db", type=Path, default=BASE_DIR / "new_summary.db")
    parser.add_argument("--watch", action="store_true", help="обрабатывать новости постоянно")
    parser.add_argument("--interval", type=float, default=10.0, help="пауза между проверками в --watch, с")
    parser.add_argument("--limit", type=int, default=None, help="максимум вызовов LLM за проход")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="адрес OpenAI-совместимого API")
    parser.add_argument("--model", default=None, help="имя модели (по умолчанию — первая загруженная)")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--no-thinking", action="store_true", help="быстрее, но менее точно")
    args = parser.parse_args(list(argv) if argv is not None else None)

    summarizer = NewsSummarizer(
        source_database_path=args.news_db,
        summary_database_path=args.summary_db,
        analyzer_factory=lambda: NewsAnalyzer(
            ChatClient(args.base_url, api_key=args.api_key, model=args.model),
            thinking=not args.no_thinking,
        ),
    )
    while True:
        summarizer.process_pending(limit=args.limit)
        if not args.watch:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
