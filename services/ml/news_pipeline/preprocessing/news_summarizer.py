"""Подготовка входящих новостей и перенос их в ``new_summary.db``.

Каждая новость из ``news.db`` очищается (ТЗ §5.1), получает лид — заголовок
и первые предложения — и обогащение (§5.3–5.4: сущности, темы, важность, тип
события, ключевые числа; см. ``enrichment/news.py``) и превращается в запись
таблицы ``news_summary`` (см. ``news_db.py``):

* ``document_id`` — идентификатор новости;
* ``text`` — полный текст новости;
* ``title`` — заголовок (из ``metadata["title"]``);
* ``first_paragraph`` — первый абзац (текст до первой пустой строки);
* ``audience_text`` — шаблон аудитории; LLM сейчас не вызывается, поле пустое;
* ``metadata`` — исходные метаданные новости, ``lead``, ``enrichment`` и время
  подготовки ``prepared_at``.

Новости не удаляются — у них меняется статус: подготовка берёт из
``news.db`` новости ``new`` (ставит ``processing``), записывает их в
``new_summary.db`` со статусом ``new`` и только после этого ставит
в ``news.db`` статус ``ready``. При ошибке пачка возвращается в ``new``.
Дальше новость забирает дедупликация (``scripts/run_dedup.py``).

Запуск:

    python scripts/prepare_news.py            # обработать всё, что есть в news.db
    python scripts/prepare_news.py --watch    # обрабатывать постоянно
"""

from __future__ import annotations

import argparse
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from news_pipeline.config import NEWS_DB, SUMMARY_DB
from news_pipeline.enrichment.news import enrich
from news_pipeline.preprocessing.cleaning import clean_text, lead
from news_pipeline.storage.news_db import (
    ProcessedNews,
    NEW,
    READY,
    StoredNews,
    claim_news,
    ensure_summary_schema,
    first_paragraph,
    insert_summaries,
    release_stale,
    set_news_status,
    summary_ids,
)



@dataclass(frozen=True)
class ProcessingReport:
    """Итог одного прохода по ``news.db``."""

    processed: list[str] = field(default_factory=list)
    already_done: list[str] = field(default_factory=list)


class NewsSummarizer:
    """Переносит новости из ``news.db`` в ``new_summary.db``: очистка, лид, обогащение."""

    def __init__(
        self,
        *,
        source_database_path: str | Path = NEWS_DB,
        summary_database_path: str | Path = SUMMARY_DB,
        use_llm: bool = True,
        workers: int = 3,
        batch_size: int = 20,
        verbose: bool = True,
    ) -> None:
        """``use_llm=False`` — темы и важность без LLM (эмбеддинги и эвристика).

        Новости обрабатываются пачками по ``batch_size``: после каждой пачки
        результат записывается и подготовленные новости получают статус ``ready``,
        поэтому прерывание не теряет уже сделанную работу.
        """
        self.source_database_path = Path(source_database_path)
        self.summary_database_path = Path(summary_database_path)
        self.use_llm = use_llm
        self.workers = workers
        self.batch_size = batch_size
        self.verbose = verbose
        ensure_summary_schema(self.summary_database_path)

    def prepare(self, news: StoredNews, prepared_at: str) -> ProcessedNews:
        """Очистка (§5.1), лид из первых предложений и обогащение (§5.3–5.4) одной новости."""
        title = str(news.metadata.get("title") or "").strip()
        text = clean_text(news.text)
        news_lead = lead(title, text)
        enrichment = enrich(
            news.document_id, title, text, news_lead,
            source=news.metadata.get("source"), use_llm=self.use_llm,
        )
        return ProcessedNews(
            document_id=news.document_id,
            text=text,
            title=title,
            first_paragraph=first_paragraph(text),
            metadata={
                **news.metadata,
                "lead": news_lead,
                "enrichment": enrichment.to_dict(),
                "prepared_at": prepared_at,
            },
        )

    def process_pending(self) -> ProcessingReport:
        """Готовит новости ``news.db`` со статусом ``new`` и ставит им ``ready``.

        Новости, которые уже есть в ``new_summary.db`` (процесс упал между
        записью и сменой статуса), повторно не записываются — только получают
        ``ready``. Зависшие в ``processing`` дольше 30 минут снова берутся в работу.
        """
        report = ProcessingReport()
        if not self.source_database_path.exists():
            self._print(f"{self.source_database_path.name} не найдена — обрабатывать нечего.")
            return report

        release_stale(self.source_database_path, "news")
        pending = claim_news(self.source_database_path)
        done = summary_ids(self.summary_database_path)
        self._print(f"Новостей в {self.source_database_path.name}: {len(pending)}")

        prepared_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        already = [item.document_id for item in pending if item.document_id in done]
        if already:
            set_news_status(self.source_database_path, already, READY)
            report.already_done.extend(already)
        todo = [item for item in pending if item.document_id not in done]
        started = time.monotonic()
        with ThreadPoolExecutor(self.workers) as pool:
            for start in range(0, len(todo), self.batch_size):
                batch = todo[start : start + self.batch_size]
                try:
                    fresh = list(pool.map(lambda item: self.prepare(item, prepared_at), batch))
                    insert_summaries(self.summary_database_path, fresh)
                except BaseException:
                    # пачка и всё, что не успели взять, снова ждут обработки
                    set_news_status(self.source_database_path, [item.document_id for item in todo[start:]], NEW)
                    raise
                set_news_status(self.source_database_path, [item.document_id for item in batch], READY)
                report.processed.extend(news.document_id for news in fresh)
                for news in fresh:
                    if not news.title:
                        self._print(f"{news.document_id}: в метаданных нет заголовка")
                self._print(
                    f"подготовлено {len(report.processed)}/{len(todo)} "
                    f"({time.monotonic() - started:.0f} с)"
                )
        self._print(
            f"Итог: перенесено {len(report.processed)}, уже было {len(report.already_done)}"
        )
        return report

    def _print(self, message: str) -> None:
        if self.verbose:
            print(f"[NewsSummarizer] {message}", flush=True)


def main(argv: Iterable[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--news-db", type=Path, default=NEWS_DB)
    parser.add_argument("--summary-db", type=Path, default=SUMMARY_DB)
    parser.add_argument("--watch", action="store_true", help="обрабатывать новости постоянно")
    parser.add_argument("--interval", type=float, default=10.0, help="пауза между проверками в --watch, с")
    parser.add_argument("--no-llm", action="store_true", help="темы и важность без LLM")
    parser.add_argument("--workers", type=int, default=3, help="параллельных запросов к LLM")
    args = parser.parse_args(list(argv) if argv is not None else None)

    summarizer = NewsSummarizer(
        source_database_path=args.news_db,
        summary_database_path=args.summary_db,
        use_llm=not args.no_llm,
        workers=args.workers,
    )
    while True:
        summarizer.process_pending()
        if not args.watch:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
