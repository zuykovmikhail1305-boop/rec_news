"""Сообщения очередей — контракт между тем, кто ставит задачу, и тем, кто её выполняет.

Сообщение — JSON (UTF-8). Поле ``version`` позволяет менять формат, не ломая обработчики,
которые ещё работают со старым.

Внутренние задачи ML (Celery: имя задачи = имя очереди, аргумент — тело :meth:`Message.dumps`):

* :data:`PROCESSING_QUEUE` — :class:`ProcessingTask`, запуск обработки (``POST /processing.enqueue``);
* :data:`INSIGHT_QUEUE` — :class:`InsightTask`, одна карточка инсайта;
* :data:`ANALYZE_QUEUE` — :class:`AnalyzeNewsTask`, обработка одной новости по ``news_id``;
* :data:`FEEDS_QUEUE` — :class:`FeedsRefreshTask`, пересчёт лент после новых новостей.

Обмен с другими сервисами (обычные JSON-сообщения RabbitMQ, не задачи Celery; docs/broker.md):

* вход — :class:`NewsEvent`: ``NewsDiscovered`` из exchange :data:`NEWS_EVENTS_EXCHANGE`
  (ключ :data:`NEWS_DISCOVERED_KEY`, наша очередь :data:`ANALYTICS_NEWS_QUEUE`) и ``AnalyzeNews``
  из очереди :data:`ANALYTICS_COMMANDS_QUEUE`;
* выход — :class:`AnalysisEvent`: ``AnalysisCompleted`` / ``AnalysisFailed`` в exchange
  :data:`ANALYTICS_EVENTS_EXCHANGE` (очередь :data:`ANALYTICS_EVENTS_QUEUE` для Backend).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# --- внутренние очереди ML (задачи Celery) -------------------------------------------------
INSIGHT_QUEUE = "insights.generate"
PROCESSING_QUEUE = "processing.run"
ANALYZE_QUEUE = "analytics.analyze"
FEEDS_QUEUE = "feeds.refresh"
TASK_QUEUES = (PROCESSING_QUEUE, INSIGHT_QUEUE, ANALYZE_QUEUE, FEEDS_QUEUE)

# --- обмен с другими сервисами -------------------------------------------------------------
NEWS_EVENTS_EXCHANGE = "news.events"           # topic: события Parser о новостях
NEWS_DISCOVERED_KEY = "news.discovered"
ANALYTICS_NEWS_QUEUE = "analytics.news"        # наша копия news.discovered (схема 3)
ANALYTICS_COMMANDS_QUEUE = "analytics.commands"  # команды AnalyzeNews от Backend (схемы 1–2)
ANALYTICS_EVENTS_EXCHANGE = "analytics.events"  # topic: результаты обработки
ANALYTICS_EVENTS_QUEUE = "analytics.events"    # очередь Backend, привязана к analysis.#
DEAD_LETTER_EXCHANGE = "ml.dlx"                # отказы после всех повторов → очередь <имя>.dlq


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


class AnalyzeNewsTask(Message):
    """Обработать новость ``news_id`` (она уже в базе новостей ML со статусом ``new``):
    подготовка → дедупликация и события → подбор аналитики; итог — :class:`AnalysisEvent`."""

    news_id: str


class FeedsRefreshTask(Message):
    """Пересчитать ленты пользователей: после обработки новых новостей (запуск ``processing.run``)."""

    requested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# --- сообщения других сервисов ---------------------------------------------------------------

NEWS_EVENT_TYPES = ("NewsDiscovered", "AnalyzeNews")


class NewsEvent(BaseModel):
    """Входящее сообщение о новости: ``{"type": "NewsDiscovered" | "AnalyzeNews", "news_id": …}``.

    В сообщении только ID — текст ML читает из своей базы новостей. ``news_id`` может быть числом.
    Тип можно передать и заголовком сообщения ``type``; лишние поля (``run_id`` и др.) не мешают.
    """

    type: str
    news_id: str
    version: int = 1

    model_config = {"extra": "ignore"}

    @field_validator("news_id", mode="before")
    @classmethod
    def _as_text(cls, value: Any) -> str:
        if value is None or str(value).strip() == "":
            raise ValueError("пустой news_id")
        return str(value).strip()


class AnalysisEvent(Message):
    """Итог обработки новости для Backend.

    ``AnalysisCompleted``: новость обработана (статус ``ready``); ``event_id`` — событие, к которому
    она отнесена; ``unique`` — она представитель события (``false`` — дубль уже известной новости).
    ``AnalysisFailed``: обработать не удалось после всех повторов (статус ``failed``), ``error`` — причина.
    """

    type: Literal["AnalysisCompleted", "AnalysisFailed"]
    news_id: str
    status: str
    event_id: str | None = None
    unique: bool | None = None
    error: str | None = None
    finished_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def routing_key(self) -> str:
        return "analysis.completed" if self.type == "AnalysisCompleted" else "analysis.failed"
