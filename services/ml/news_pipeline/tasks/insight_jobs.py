"""Генерация всех карточек инсайтов пользователя через очередь задач.

    start(user_id)  → задание в jobs.db + сообщение InsightTask на каждую новость → очередь insights.generate
    handle(body)    ← обработчик сообщения: строит одну карточку и записывает результат в jobs.db

:meth:`InsightJobs.handle` не зависит от того, кто доставил сообщение: сейчас
его вызывает :class:`queue.LocalQueue`, при переходе на Celery — воркер, выполняющий задачу
очереди ``insights.generate``. Обработчик идемпотентен: повторно доставленное
сообщение (карточка уже построена или задание отменено) пропускается.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from news_pipeline.config import UNIQUE_DB
from news_pipeline.env import env_int
from news_pipeline.storage.news_db import read_summaries
from news_pipeline.tasks.messages import INSIGHT_QUEUE, InsightTask
from news_pipeline.tasks.queue import TaskQueue
from news_pipeline.tasks.schemas import InsightJob
from news_pipeline.tasks.store import JobStore

if TYPE_CHECKING:
    from news_pipeline.service import MLService

log = logging.getLogger("rec_news")


class NothingToGenerate(ValueError):
    """У пользователя нет новостей для карточек (ещё не было обработки)."""


class InsightJobs:
    def __init__(self, service: "MLService", queue: TaskQueue, store: JobStore | None = None) -> None:
        self.service = service
        self.queue = queue
        self.store = store or JobStore()
        self._start_lock = threading.Lock()
        # Сколько карточек строится одновременно; каждая сама делает до 3 вызовов LLM параллельно.
        queue.subscribe(INSIGHT_QUEUE, self.handle, workers=env_int("REC_NEWS_INSIGHT_WORKERS", 2))

    # --- постановка -------------------------------------------------------------------

    def start(self, user_id: str, *, news_ids: list[str] | None = None, limit: int | None = None,
              refresh: bool = False) -> tuple[InsightJob, bool]:
        """Ставит задание и возвращает его; ``False`` во втором значении — у пользователя уже идёт задание
        (новое не создаётся, возвращается текущее).

        ``KeyError`` — нет профиля, :class:`NothingToGenerate` — нет новостей.
        """
        if self.service.profiles.get(user_id) is None:
            raise KeyError(user_id)
        with self._start_lock:
            return self._start(user_id, news_ids, limit, refresh)

    def _start(self, user_id: str, news_ids: list[str] | None, limit: int | None,
               refresh: bool) -> tuple[InsightJob, bool]:
        active = self.store.latest(user_id, active_only=True)
        if active is not None:
            return active, False
        if news_ids:
            news_ids = list(dict.fromkeys(news_ids))
            known = {item.document_id for item in read_summaries(UNIQUE_DB, news_ids)}
            unknown = [news_id for news_id in news_ids if news_id not in known]
            if unknown:
                raise LookupError(", ".join(unknown))
        else:
            news_ids = [row.news_id for row in self.service.user_news_store.for_user(user_id, limit=limit)]
            if not news_ids:
                raise NothingToGenerate(user_id)
        job_id = self.store.create(user_id, news_ids, refresh=refresh)
        for news_id in news_ids:
            self.publish(InsightTask(job_id=job_id, user_id=user_id, news_id=news_id, refresh=refresh))
        return self.store.job(job_id), True

    def publish(self, task: InsightTask) -> None:
        self.queue.publish(INSIGHT_QUEUE, task.dumps())

    def resume_unfinished(self) -> int:
        """После перезапуска: незавершённые карточки снова в очередь.

        Нужно только очереди в памяти — брокер сам хранит недоставленные сообщения.
        """
        self.store.requeue_running()
        items = self.store.unfinished()
        for item in items:
            self.publish(InsightTask(job_id=item["job_id"], user_id=item["user_id"], news_id=item["news_id"],
                                     refresh=bool(item["refresh"])))
        if items:
            log.info("незавершённые карточки снова в очереди: %d", len(items))
        return len(items)

    # --- обработка --------------------------------------------------------------------

    def handle(self, body: bytes) -> None:
        """Обработчик сообщения очереди: одна карточка."""
        task = InsightTask.loads(body)
        if not self.store.start_item(task.job_id, task.news_id):
            return  # уже обработана, отменена или задания нет — повторная доставка
        try:
            card = self.service.insight_card(task.user_id, task.news_id, refresh=task.refresh)
        except KeyError:
            self.store.finish_item(task.job_id, task.news_id, status="failed", error="профиль пользователя не найден")
        except LookupError:
            self.store.finish_item(task.job_id, task.news_id, status="failed", error="новость не найдена")
        except Exception as error:  # noqa: BLE001 - ошибка одной карточки не роняет задание
            log.exception("карточка %s/%s", task.user_id, task.news_id)
            self.store.finish_item(task.job_id, task.news_id, status="failed",
                                   error=f"{type(error).__name__}: {error}"[:500])
        else:
            self.store.finish_item(task.job_id, task.news_id, status="done", card_id=card.card_id,
                                   card_status=card.status, headline=card.headline.text if card.headline else None)

    # --- состояние --------------------------------------------------------------------

    def job(self, job_id: str) -> InsightJob | None:
        return self.store.job(job_id)

    def latest(self, user_id: str) -> InsightJob | None:
        return self.store.latest(user_id)

    def cancel(self, job_id: str) -> InsightJob | None:
        if self.store.job(job_id, with_items=False) is None:
            return None
        self.store.cancel(job_id)
        return self.store.job(job_id)
