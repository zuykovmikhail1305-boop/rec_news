"""Celery + RabbitMQ: приложение Celery, очередь :class:`CeleryQueue` и приём событий других сервисов.

Запуск воркера (из ``services/ml``; на Windows и вообще для ML — пул потоков: модель эмбеддингов
загружается один раз на процесс):

    celery -A news_pipeline.tasks.celery_app worker --pool threads --concurrency 4 --loglevel info

Что делает воркер:

* выполняет задачи ML — очереди ``processing.run``, ``insights.generate``, ``analytics.analyze``,
  ``feeds.refresh``. Задача — имя очереди, аргумент — JSON-сообщение (``tasks/messages.py``);
  выполняет его тот же обработчик, что и очередь в процессе (``MLService`` подписывает их через
  :meth:`CeleryQueue.subscribe`). Подтверждение (ACK) — после обработки (``acks_late``): если воркер
  упал, сообщение получит другой. Обработчик просит повтор исключением ``RetryLater``; после всех
  повторов сообщение отклоняется и уходит в очередь отказов ``<очередь>.dlq``;
* слушает сообщения других сервисов (:class:`NewsEventsConsumer`): ``NewsDiscovered`` из exchange
  ``news.events`` (своя очередь ``analytics.news``, ключ ``news.discovered``) и ``AnalyzeNews`` из
  ``analytics.commands``. Каждое превращается в задачу ``analytics.analyze`` и подтверждается после
  того, как брокер принял задачу (publisher confirms). Нечитаемые сообщения — в ``.dlq``;
* отправляет итоги ``AnalysisCompleted`` / ``AnalysisFailed`` в exchange ``analytics.events``
  (очередь Backend ``analytics.events``) — :meth:`CeleryQueue.emit`.

Схема обмена и контракт сообщений — ``docs/broker.md``.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from celery import Celery, bootsteps
from celery.exceptions import Reject
from kombu import Consumer, Exchange, Queue

from news_pipeline.env import env_flag, env_str
from news_pipeline.tasks.messages import (
    ANALYTICS_COMMANDS_QUEUE, ANALYTICS_EVENTS_EXCHANGE, ANALYTICS_EVENTS_QUEUE, ANALYTICS_NEWS_QUEUE, ANALYZE_QUEUE,
    DEAD_LETTER_EXCHANGE, NEWS_DISCOVERED_KEY, NEWS_EVENT_TYPES, NEWS_EVENTS_EXCHANGE, TASK_QUEUES, AnalyzeNewsTask,
    NewsEvent,
)
from news_pipeline.tasks.queue import GiveUp, Handler, RetryLater, Subscription

log = logging.getLogger("rec_news")

BROKER_URL = env_str("REC_NEWS_BROKER_URL", "amqp://guest:guest@localhost:5672//")
# Слушать ли сообщения других сервисов (news.discovered, AnalyzeNews) в этом воркере.
CONSUME_NEWS_EVENTS = env_flag("REC_NEWS_CONSUME_NEWS_EVENTS", True)

# --- топология RabbitMQ ------------------------------------------------------------------------

dead_letters = Exchange(DEAD_LETTER_EXCHANGE, type="direct", durable=True)
news_events = Exchange(NEWS_EVENTS_EXCHANGE, type="topic", durable=True)
analytics_events = Exchange(ANALYTICS_EVENTS_EXCHANGE, type="topic", durable=True)


def _with_dead_letters(name: str, **kwargs: Any) -> Queue:
    """Durable-очередь, отказы которой уходят в ``<name>.dlq`` через :data:`DEAD_LETTER_EXCHANGE`."""
    return Queue(name, durable=True, queue_arguments={"x-dead-letter-exchange": DEAD_LETTER_EXCHANGE,
                                                      "x-dead-letter-routing-key": name}, **kwargs)


def _dead_letter_queue(name: str) -> Queue:
    return Queue(f"{name}.dlq", exchange=dead_letters, routing_key=name, durable=True)


TASK_QUEUE_OBJECTS = [_with_dead_letters(name, exchange=Exchange(name, type="direct", durable=True), routing_key=name)
                      for name in TASK_QUEUES]
ANALYTICS_NEWS = _with_dead_letters(ANALYTICS_NEWS_QUEUE, exchange=news_events, routing_key=NEWS_DISCOVERED_KEY)
ANALYTICS_COMMANDS = _with_dead_letters(ANALYTICS_COMMANDS_QUEUE)  # default exchange: ключ = имя очереди
ANALYTICS_EVENTS = Queue(ANALYTICS_EVENTS_QUEUE, exchange=analytics_events, routing_key="analysis.#", durable=True)
DEAD_LETTER_QUEUES = [_dead_letter_queue(name) for name in (*TASK_QUEUES, ANALYTICS_NEWS_QUEUE, ANALYTICS_COMMANDS_QUEUE)]


def declare_topology(connection) -> None:  # noqa: ANN001
    """Объявляет exchange и очереди обмена с другими сервисами и очереди отказов (повторно — безопасно)."""
    channel = connection.default_channel
    for queue in (ANALYTICS_NEWS, ANALYTICS_COMMANDS, ANALYTICS_EVENTS, *TASK_QUEUE_OBJECTS, *DEAD_LETTER_QUEUES):
        queue.bind(channel).declare()


# --- приложение Celery -------------------------------------------------------------------------

app = Celery("rec_news_ml", broker=BROKER_URL)
app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    task_ignore_result=True,                 # состояние задач — в базе (jobs, pipeline_runs, статусы новостей)
    task_acks_late=True,                     # ACK после обработки: упавший воркер не теряет задачу
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,            # задачи длинные (LLM): не забирать впрок
    task_queues=TASK_QUEUE_OBJECTS,
    task_routes={name: {"queue": name, "routing_key": name} for name in TASK_QUEUES},
    task_default_queue=TASK_QUEUES[0],
    broker_transport_options={"confirm_publish": True},   # publisher confirms: брокер подтвердил приём
    broker_connection_retry_on_startup=True,
    worker_hijack_root_logger=False,
)

# Обработчики очередей: заполняются, когда MLService подписывается через CeleryQueue.subscribe.
_SUBSCRIPTIONS: dict[str, Subscription] = {}
_service_lock = threading.Lock()
_service: Any | None = None


def service() -> Any:
    """``MLService`` воркера — создаётся при первой задаче (загрузка модели, подключение к базам)."""
    global _service
    with _service_lock:
        if _service is None:
            from news_pipeline.service import MLService

            logging.basicConfig(level=logging.INFO)
            _service = MLService()
            log.info("воркер: сервис готов, очереди %s", ", ".join(sorted(_SUBSCRIPTIONS)))
        return _service


# Сервис воркера не запустился (база, Qdrant недоступны): задача повторяется, а не теряется.
SERVICE_RETRY_DELAY = 30
SERVICE_RETRIES = 20


def _register(queue: str) -> None:
    @app.task(name=queue, bind=True)
    def run(self, body: str) -> None:  # noqa: ANN001
        try:
            service()
        except Exception as error:  # noqa: BLE001
            log.exception("воркер: сервис не запустился")
            if self.request.retries >= SERVICE_RETRIES:
                raise Reject(f"сервис не запустился: {error}", requeue=False) from error   # → <queue>.dlq
            raise self.retry(countdown=SERVICE_RETRY_DELAY, max_retries=SERVICE_RETRIES)
        subscription = _SUBSCRIPTIONS[queue]
        payload = body.encode("utf-8")
        try:
            subscription.handler(payload)
        except RetryLater as error:
            attempt = self.request.retries
            if attempt >= subscription.retries:
                subscription.give_up(queue, payload, error)
                raise Reject(str(error), requeue=False) from error   # → <queue>.dlq
            log.info("задача %s: повтор %d — %s", queue, attempt + 1, error)
            raise self.retry(countdown=subscription.delay(error, attempt), max_retries=subscription.retries)


for _queue in TASK_QUEUES:
    _register(_queue)


class CeleryQueue:
    """:class:`TaskQueue` поверх Celery: ``publish`` — ``send_task`` в очередь RabbitMQ,
    ``subscribe`` — обработчик для задачи с именем очереди (выполняется в воркере)."""

    in_process = False

    def publish(self, queue: str, body: bytes, *, delay: float = 0) -> None:
        app.send_task(queue, args=[body.decode("utf-8")], queue=queue, routing_key=queue,
                      countdown=delay or None)

    def subscribe(self, queue: str, handler: Handler, *, workers: int = 1, retries: int = 0,
                  retry_delay: float = 10.0, on_give_up: GiveUp | None = None) -> None:
        _SUBSCRIPTIONS[queue] = Subscription(handler, workers, retries, retry_delay, on_give_up)

    def emit(self, routing_key: str, body: bytes) -> None:
        with app.producer_pool.acquire(block=True) as producer:
            producer.publish(json.loads(body), exchange=analytics_events, routing_key=routing_key, serializer="json",
                             delivery_mode=2, declare=[analytics_events, ANALYTICS_EVENTS], retry=True)

    def close(self) -> None:
        pass


# --- сообщения других сервисов --------------------------------------------------------------------

# Типы содержимого, которые принимаем от других сервисов: JSON и «сырые» байты/текст (разбираем сами).
_ACCEPT = ["application/json", "text/plain", "application/data", "application/octet-stream"]


def parse_news_event(body: Any, headers: dict | None = None) -> NewsEvent:
    """Тело сообщения (dict, str или bytes) → :class:`NewsEvent`; тип — из тела или заголовка ``type``."""
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8")
    if isinstance(body, str):
        body = json.loads(body)
    if not isinstance(body, dict):
        raise ValueError("ожидается JSON-объект")
    data = dict(body)
    data.setdefault("type", data.get("event") or (headers or {}).get("type"))
    if not data.get("news_id") and data.get("id") is not None:
        data["news_id"] = data["id"]
    return NewsEvent.model_validate(data)


class NewsEventsConsumer(bootsteps.ConsumerStep):
    """Принимает ``NewsDiscovered`` и ``AnalyzeNews`` и ставит задачу ``analytics.analyze`` на каждую новость."""

    def get_consumers(self, channel):  # noqa: ANN001
        declare_topology(channel.connection.client)
        return [Consumer(channel, queues=[ANALYTICS_NEWS, ANALYTICS_COMMANDS], callbacks=[self.on_message],
                         accept=_ACCEPT, prefetch_count=10)]

    def on_message(self, body, message) -> None:  # noqa: ANN001
        try:
            event = parse_news_event(body, message.headers)
        except Exception as error:  # noqa: BLE001 - нечитаемое сообщение не повторяем
            log.error("сообщение %s отклонено (→ .dlq): %s", message.delivery_info.get("routing_key"), error)
            message.reject(requeue=False)
            return
        if event.type not in NEWS_EVENT_TYPES:
            message.ack()   # чужие события (ParsingFinished и др.) нам не нужны
            return
        try:
            CeleryQueue().publish(ANALYZE_QUEUE, AnalyzeNewsTask(news_id=event.news_id).dumps())
        except Exception:  # noqa: BLE001 - брокер не принял задачу: сообщение вернётся в очередь
            log.exception("не удалось поставить задачу для новости %s", event.news_id)
            message.requeue()
            return
        message.ack()


if CONSUME_NEWS_EVENTS:
    app.steps["consumer"].add(NewsEventsConsumer)
