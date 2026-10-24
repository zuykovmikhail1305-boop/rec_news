"""Обработка новостей по одной через очередь — ML как сервис Analytics (docs/broker.md).

    NewsDiscovered / AnalyzeNews {news_id}   ← другие сервисы (RabbitMQ, celery_app.NewsEventsConsumer)
      → задача analytics.analyze {news_id}   → :meth:`NewsAnalysis.handle`
      → AnalysisCompleted / AnalysisFailed   → exchange analytics.events (Backend)
      → задача feeds.refresh                 → :meth:`NewsAnalysis.handle_feeds`: пересчёт лент (processing.run)

Новость уже лежит в базе новостей ML со статусом ``new`` (её пишет Parser/Backend). Обработка — тот же
код, что у ``POST /processing.enqueue``: подготовка новости (LLM; параллельно с другими новостями), затем
под общей блокировкой ``pipeline`` (одна на все процессы) — дедупликация и события, подбор аналитики.
Статус: ``new → processing → ready``; после всех повторов — ``failed``.

Обработчик идемпотентен: повторная доставка новости, которая уже ``ready``, только заново отправляет
``AnalysisCompleted`` (получатели тоже обрабатывают события по ID идемпотентно).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

from news_pipeline.config import NEWS_DB, UNIQUE_DB
from news_pipeline.settings import SETTINGS
from news_pipeline.storage.news_db import (
    FAILED, NEW, PROCESSING, READY, event_representative, news_status, read_summaries, set_news_status,
)
from news_pipeline.tasks.messages import (
    ANALYZE_QUEUE, FEEDS_QUEUE, AnalysisEvent, AnalyzeNewsTask, FeedsRefreshTask,
)
from news_pipeline.tasks.queue import RetryLater, TaskQueue

if TYPE_CHECKING:
    from news_pipeline.service import MLService

log = logging.getLogger("rec_news")


class NewsAnalysis:
    def __init__(self, service: "MLService", queue: TaskQueue) -> None:
        self.service = service
        self.queue = queue
        self.settings = SETTINGS.queue
        self._feeds_requested_at = 0.0
        self._feeds_lock = threading.Lock()
        queue.subscribe(ANALYZE_QUEUE, self.handle, workers=2,
                        retries=int(self.settings.get("analyze_retries", 6)),
                        retry_delay=float(self.settings.get("analyze_retry_delay_s", 30)),
                        on_give_up=self.give_up)
        queue.subscribe(FEEDS_QUEUE, self.handle_feeds, workers=1,
                        retries=int(self.settings.get("feeds_refresh_retries", 30)),
                        retry_delay=float(self.settings.get("feeds_refresh_retry_s", 120)))

    # --- постановка -------------------------------------------------------------------

    def request(self, news_id: str) -> None:
        """Поставить новость в обработку (то же, что сообщение ``AnalyzeNews``)."""
        self.queue.publish(ANALYZE_QUEUE, AnalyzeNewsTask(news_id=news_id).dumps())

    # --- обработка новости --------------------------------------------------------------

    def handle(self, body: bytes) -> None:
        news_id = AnalyzeNewsTask.loads(body).news_id
        status = news_status(NEWS_DB, news_id)
        if status is None:
            # Сообщение могло прийти раньше, чем запись новости стала видна, — подождать.
            raise RetryLater(f"новости {news_id} нет в базе новостей ML")
        if status == FAILED:  # новая команда на новость, которую не удалось обработать, — ещё попытка
            set_news_status(NEWS_DB, [news_id], NEW)
        try:
            # Обработка идёт и тогда, когда новость уже ready: дожидаемся, пока завершится начатый
            # кем-то проход (дедупликация, аналитика), — только после этого новость готова.
            self.service.process_news([news_id])
        except Exception as error:  # noqa: BLE001 - LLM, база, Qdrant: повторить позже
            log.exception("обработка новости %s", news_id)
            raise RetryLater(f"{type(error).__name__}: {error}") from error
        status = news_status(NEWS_DB, news_id)
        if status != READY:
            # processing — новость взял другой воркер (или он упал: зависшие через 30 мин снова new)
            raise RetryLater(f"новость {news_id} в статусе {status}")
        self.emit(self.completed(news_id))
        self.request_feeds()

    def completed(self, news_id: str) -> AnalysisEvent:
        representative = event_representative(UNIQUE_DB, news_id)
        event_id = None
        if representative is not None:
            stored = read_summaries(UNIQUE_DB, [representative])
            event_id = (stored[0].metadata.get("event_id") if stored else None) or f"e_{representative}"
        return AnalysisEvent(type="AnalysisCompleted", news_id=news_id, status=READY, event_id=event_id,
                             unique=representative == news_id)

    def give_up(self, body: bytes, error: Exception) -> None:
        """Все повторы исчерпаны: новость ``failed`` (если её не обработали), событие ``AnalysisFailed``."""
        news_id = AnalyzeNewsTask.loads(body).news_id
        status = news_status(NEWS_DB, news_id)
        if status in (NEW, PROCESSING):
            set_news_status(NEWS_DB, [news_id], FAILED)
            status = FAILED
        self.emit(AnalysisEvent(type="AnalysisFailed", news_id=news_id, status=status or "missing",
                                error=str(error)[:500]))

    def emit(self, event: AnalysisEvent) -> None:
        self.queue.emit(event.routing_key, event.dumps())

    # --- ленты ----------------------------------------------------------------------------

    def request_feeds(self) -> None:
        """Пересчитать ленты после новых новостей — с задержкой, одним запуском на пачку новостей."""
        if not self.settings.get("refresh_feeds", True):
            return
        delay = float(self.settings.get("feeds_refresh_delay_s", 60))
        with self._feeds_lock:
            if time.monotonic() - self._feeds_requested_at < delay:
                return   # задача на пересчёт уже стоит — она увидит и эту новость
            self._feeds_requested_at = time.monotonic()
        self.queue.publish(FEEDS_QUEUE, FeedsRefreshTask().dumps(), delay=delay)

    def handle_feeds(self, body: bytes) -> None:
        """Запуск обработки (``processing.run``): ленты всех пользователей. Если запуск уже ждёт в очереди —
        он и учтёт новые новости; если идёт — новые новости он мог не увидеть, проверить позже."""
        FeedsRefreshTask.loads(body)
        run, created = self.service.start_run()
        if created or run.status == "queued":
            return
        raise RetryLater(f"идёт обработка {run.run_id} — ленты пересчитаем после неё",
                         delay=float(self.settings.get("feeds_refresh_retry_s", 120)))
