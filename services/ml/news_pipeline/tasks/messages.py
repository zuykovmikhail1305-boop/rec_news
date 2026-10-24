"""Сообщения очереди задач — контракт между тем, кто ставит задачу, и тем, кто её выполняет.

Сообщение — JSON (UTF-8). Тот же формат пойдёт в брокер (Celery): имя задачи — имя очереди
(:data:`INSIGHT_QUEUE`, :data:`PROCESSING_QUEUE`), аргумент — тело :meth:`Message.dumps`.
Поле ``version`` позволяет менять формат, не ломая обработчики, которые ещё работают со старым.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

INSIGHT_QUEUE = "insights.generate"
PROCESSING_QUEUE = "processing.run"


class Message(BaseModel):
    version: int = 1

    def dumps(self) -> bytes:
        return self.model_dump_json().encode("utf-8")

    @classmethod
    def loads(cls, body: bytes | str):
        return cls.model_validate_json(body)


class InsightTask(Message):
    """Построить карточку инсайта для пары пользователь — новость в рамках задания ``job_id``."""

    job_id: str
    user_id: str
    news_id: str
    refresh: bool = False


class ProcessingTask(Message):
    """Обработать новости из базы в рамках запуска ``run_id`` (``POST /processing.enqueue``):
    подготовка → дедупликация → подбор аналитики → ленты пользователей."""

    run_id: str
    now: datetime | None = None
    user_ids: list[str] | None = None
    insights_top: int = 0
