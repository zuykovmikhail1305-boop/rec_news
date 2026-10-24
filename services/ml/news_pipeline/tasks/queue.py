"""Очередь задач: интерфейс :class:`TaskQueue` и реализация в процессе :class:`LocalQueue`.

Остальной код знает только интерфейс: ``publish(очередь, тело)`` и
``subscribe(очередь, обработчик)``. Обработчик получает тело сообщения
(байты) — так же, как задача Celery получает свой аргумент. Чтобы перейти на
Celery, достаточно реализовать :class:`TaskQueue` поверх него (``publish`` —
``celery_app.send_task(очередь, args=[тело])``, ``subscribe`` — регистрация
задачи с именем очереди, которая вызывает обработчик) и выбрать реализацию
в :func:`create_queue`; обработчики и формат сообщений не меняются.
План перехода — ``docs/insight_jobs.md``.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Protocol

log = logging.getLogger("rec_news")

Handler = Callable[[bytes], None]


class TaskQueue(Protocol):
    # True — задачи выполняются внутри процесса API и пропадают при его остановке: после старта
    # сервис сам ставит незавершённые задачи заново. У брокера (Celery) — False: задачи хранит брокер.
    in_process: bool

    def publish(self, queue: str, body: bytes) -> None:
        """Поставить сообщение в очередь."""

    def subscribe(self, queue: str, handler: Handler, *, workers: int = 1) -> None:
        """Обрабатывать сообщения очереди: ``workers`` — сколько одновременно."""

    def close(self) -> None:
        """Остановить приём и обработку."""


class LocalQueue:
    """Очередь в памяти процесса: у каждой очереди свой пул потоков.

    Сообщения не переживают перезапуск — после старта сервис сам ставит
    незавершённые задачи заново (состояние заданий хранится в базе).
    Ошибка обработчика пишется в журнал и не останавливает очередь.
    """

    in_process = True

    def __init__(self) -> None:
        self._pools: dict[str, ThreadPoolExecutor] = {}
        self._handlers: dict[str, Handler] = {}
        self._lock = threading.Lock()
        self._closed = False

    def subscribe(self, queue: str, handler: Handler, *, workers: int = 1) -> None:
        with self._lock:
            self._handlers[queue] = handler
            self._pools[queue] = ThreadPoolExecutor(max(1, workers), thread_name_prefix=f"queue-{queue}")

    def publish(self, queue: str, body: bytes) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("очередь остановлена")
            pool = self._pools.get(queue)
        if pool is None:
            raise KeyError(f"на очередь {queue} никто не подписан")
        pool.submit(self._dispatch, queue, body)

    def _dispatch(self, queue: str, body: bytes) -> None:
        try:
            self._handlers[queue](body)
        except Exception:  # noqa: BLE001 - одно сообщение не останавливает очередь
            log.exception("ошибка обработки сообщения очереди %s", queue)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            pools = list(self._pools.values())
        for pool in pools:
            pool.shutdown(wait=False, cancel_futures=True)


def create_queue() -> TaskQueue:
    """Реализация очереди для сервиса. Сейчас — только в процессе; место для брокера — здесь."""
    return LocalQueue()
