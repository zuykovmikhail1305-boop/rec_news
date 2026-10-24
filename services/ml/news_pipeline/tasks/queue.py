"""Очередь задач: интерфейс :class:`TaskQueue` и две реализации.

* :class:`LocalQueue` — потоки внутри процесса API (по умолчанию, локальная работа);
* :class:`news_pipeline.tasks.celery_app.CeleryQueue` — Celery + RabbitMQ (``REC_NEWS_QUEUE=celery``):
  API только ставит задачи, выполняют их воркеры Celery (docs/broker.md).

Остальной код знает только интерфейс: ``publish(очередь, тело)`` и ``subscribe(очередь, обработчик)``.
Обработчик получает тело сообщения (байты) — так же, как задача Celery получает свой аргумент.
Обработчик может попросить повторить сообщение позже — исключением :class:`RetryLater`; после
``retries`` неудачных повторов вызывается ``on_give_up`` (а у брокера сообщение уходит в очередь
отказов ``<очередь>.dlq``). ``emit`` — событие для других сервисов (``AnalysisCompleted`` и т. п.).
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Protocol

from news_pipeline.env import env_str

log = logging.getLogger("rec_news")

Handler = Callable[[bytes], None]
GiveUp = Callable[[bytes, Exception], None]


class RetryLater(Exception):
    """Сообщение нельзя обработать сейчас (новости ещё нет в базе, LLM недоступна, идёт другой запуск) —
    повторить через ``delay`` секунд (по умолчанию — ``retry_delay`` подписки)."""

    def __init__(self, reason: str, delay: float | None = None) -> None:
        super().__init__(reason)
        self.delay = delay


@dataclass(frozen=True)
class Subscription:
    handler: Handler
    workers: int = 1
    retries: int = 0               # сколько раз повторить после RetryLater
    retry_delay: float = 10.0      # пауза перед повтором, с (растёт вдвое с каждой попыткой)
    on_give_up: GiveUp | None = None

    def delay(self, error: RetryLater, attempt: int) -> float:
        return error.delay if error.delay is not None else self.retry_delay * 2 ** attempt

    def give_up(self, queue: str, body: bytes, error: Exception) -> None:
        log.error("очередь %s: сообщение не обработано после %d повторов: %s", queue, self.retries, error)
        if self.on_give_up is not None:
            self.on_give_up(body, error)


class TaskQueue(Protocol):
    # True — задачи выполняются внутри процесса API и пропадают при его остановке: после старта
    # сервис сам ставит незавершённые задачи заново. У брокера (Celery) — False: задачи хранит брокер.
    in_process: bool

    def publish(self, queue: str, body: bytes, *, delay: float = 0) -> None:
        """Поставить сообщение в очередь; ``delay`` — выполнить не раньше чем через столько секунд."""

    def subscribe(self, queue: str, handler: Handler, *, workers: int = 1, retries: int = 0,
                  retry_delay: float = 10.0, on_give_up: GiveUp | None = None) -> None:
        """Обрабатывать сообщения очереди: ``workers`` — сколько одновременно (в воркере Celery
        одновременность задаёт ``--concurrency``)."""

    def emit(self, routing_key: str, body: bytes) -> None:
        """Отправить событие другим сервисам (exchange ``analytics.events``)."""

    def close(self) -> None:
        """Остановить приём и обработку."""


class LocalQueue:
    """Очередь в памяти процесса: у каждой очереди свой пул потоков.

    Сообщения не переживают перезапуск — после старта сервис сам ставит
    незавершённые задачи заново (состояние заданий хранится в базе).
    Ошибка обработчика пишется в журнал и не останавливает очередь.
    События (``emit``) только пишутся в журнал: других сервисов без брокера нет.
    """

    in_process = True

    def __init__(self) -> None:
        self._pools: dict[str, ThreadPoolExecutor] = {}
        self._subscriptions: dict[str, Subscription] = {}
        self._timers: set[threading.Timer] = set()
        self._lock = threading.Lock()
        self._closed = False

    def subscribe(self, queue: str, handler: Handler, *, workers: int = 1, retries: int = 0,
                  retry_delay: float = 10.0, on_give_up: GiveUp | None = None) -> None:
        with self._lock:
            self._subscriptions[queue] = Subscription(handler, workers, retries, retry_delay, on_give_up)
            self._pools[queue] = ThreadPoolExecutor(max(1, workers), thread_name_prefix=f"queue-{queue}")

    def publish(self, queue: str, body: bytes, *, delay: float = 0) -> None:
        self._submit(queue, body, 0, delay)

    def _submit(self, queue: str, body: bytes, attempt: int, delay: float) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("очередь остановлена")
            pool = self._pools.get(queue)
            if pool is None:
                raise KeyError(f"на очередь {queue} никто не подписан")
            if delay > 0:
                timer = threading.Timer(delay, self._fire, (queue, body, attempt))
                timer.daemon = True
                self._timers.add(timer)
                timer.start()
                return
        pool.submit(self._dispatch, queue, body, attempt)

    def _fire(self, queue: str, body: bytes, attempt: int) -> None:
        with self._lock:
            self._timers = {timer for timer in self._timers if timer.is_alive() and timer is not threading.current_thread()}
            if self._closed:
                return
            pool = self._pools[queue]
        pool.submit(self._dispatch, queue, body, attempt)

    def _dispatch(self, queue: str, body: bytes, attempt: int) -> None:
        subscription = self._subscriptions[queue]
        try:
            subscription.handler(body)
        except RetryLater as error:
            if attempt >= subscription.retries:
                subscription.give_up(queue, body, error)
                return
            log.info("очередь %s: повтор %d через %.0f с — %s", queue, attempt + 1,
                     subscription.delay(error, attempt), error)
            self._submit(queue, body, attempt + 1, subscription.delay(error, attempt))
        except Exception:  # noqa: BLE001 - одно сообщение не останавливает очередь
            log.exception("ошибка обработки сообщения очереди %s", queue)

    def emit(self, routing_key: str, body: bytes) -> None:
        log.info("событие %s: %s", routing_key, body.decode("utf-8", "replace"))

    def close(self) -> None:
        with self._lock:
            self._closed = True
            pools = list(self._pools.values())
            timers = list(self._timers)
        for timer in timers:
            timer.cancel()
        for pool in pools:
            pool.shutdown(wait=False, cancel_futures=True)


QUEUE_BACKEND = (env_str("REC_NEWS_QUEUE", "local") or "local").lower()


def create_queue() -> TaskQueue:
    """Реализация очереди: ``REC_NEWS_QUEUE=local`` (по умолчанию) — потоки в процессе,
    ``celery`` — Celery + RabbitMQ (``REC_NEWS_BROKER_URL``)."""
    if QUEUE_BACKEND == "celery":
        from news_pipeline.tasks.celery_app import CeleryQueue

        return CeleryQueue()
    if QUEUE_BACKEND != "local":
        raise ValueError(f"REC_NEWS_QUEUE={QUEUE_BACKEND}: ожидается local или celery")
    return LocalQueue()
