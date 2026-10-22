"""Общий доступ к LLM для всех модулей ML-части (ТЗ §7.3, §8, §10).

:class:`LLM` поверх :class:`news_analyzer.ChatClient` добавляет:

* **кэш** ответов в SQLite (``data/llm_cache.db``) по ключу задачи и версии
  промпта — одна и та же новость или пара (``user_id``, ``event_id``)
  не уходит в LLM повторно;
* **журнал** всех вызовов в ``data/logs/llm_calls.jsonl``: задача, промпт,
  ответ, время, ошибка;
* **запасной режим**: если сервер недоступен, :meth:`LLM.json` сразу
  поднимает :class:`LLMUnavailable` (проверка кэшируется на
  ``availability_ttl`` секунд), и модули продолжают работу без LLM;
* **проверку ответа**: JSON выделяется из текста, валидатор задачи может
  отклонить ответ — тогда запрос повторяется с текстом ошибки.

Параметры подключения — переменные ``REC_NEWS_LLM_*`` в ``.env`` (см. :mod:`config`).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from news_pipeline.config import (
    DATA_DIR,
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_MODEL,
    LLM_TEMPERATURE,
    LLM_TIMEOUT_S,
)
from news_pipeline.llm.news_analyzer import ChatClient, LLMError, _extract_json
from news_pipeline.settings import SETTINGS

CACHE_PATH = DATA_DIR / "llm_cache.db"
LOG_PATH = DATA_DIR / "logs" / "llm_calls.jsonl"

Validator = Callable[[dict[str, Any]], dict[str, Any]]


class LLMUnavailable(LLMError):
    """LLM-сервер недоступен — модуль должен перейти в запасной режим."""


class LLM:
    """LLM с кэшем, журналом и запасным режимом."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        cache_path: str | Path = CACHE_PATH,
        log_path: str | Path = LOG_PATH,
        availability_ttl: float = 30.0,
        enabled: bool = True,
    ) -> None:
        """``enabled=False`` — принудительно работать без LLM (для замеров и демо)."""
        self.base_url = base_url or LLM_BASE_URL
        self.model_name = model or LLM_MODEL
        self.api_key = api_key or LLM_API_KEY
        self.timeout = LLM_TIMEOUT_S
        self.temperature = LLM_TEMPERATURE
        self.enabled = enabled
        self.cache_path = Path(cache_path)
        self.log_path = Path(log_path)
        self.availability_ttl = availability_ttl
        self._client: ChatClient | None = None
        self._checked_at = 0.0
        self._available = False
        self._lock = threading.Lock()
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS llm_cache ("
                " key TEXT PRIMARY KEY, task TEXT NOT NULL, response TEXT NOT NULL,"
                " model TEXT, created_at TEXT NOT NULL)"
            )

    # --- доступность ----------------------------------------------------------

    def is_available(self) -> bool:
        """Отвечает ли сервер (результат проверки кэшируется)."""
        if not self.enabled:
            return False
        now = time.monotonic()
        with self._lock:
            if now - self._checked_at < self.availability_ttl:
                return self._available
            try:
                self._client = ChatClient(
                    self.base_url, api_key=self.api_key, model=self.model_name, timeout=self.timeout
                )
                self._available = True
            except LLMError:
                self._client = None
                self._available = False
            self._checked_at = now
            return self._available

    @property
    def model(self) -> str | None:
        return self._client.model if self._client else self.model_name

    # --- вызов ----------------------------------------------------------------

    def json(
        self,
        task: str,
        messages: list[dict[str, str]],
        *,
        schema: dict[str, Any] | None = None,
        cache_key: str | None = None,
        validator: Validator | None = None,
        max_tokens: int = 1500,
        retries: int = 1,
    ) -> dict[str, Any]:
        """Возвращает JSON-ответ LLM для задачи ``task``.

        ``cache_key`` — стабильный ключ входа (например, ID новости или пара
        пользователь — событие); вместе с задачей и версией промпта он задаёт
        запись кэша. Без ключа ответ не кэшируется. ``validator`` проверяет
        и нормализует ответ; исключение ``LLMError`` внутри него вызывает
        повторный запрос.
        """
        key = self._cache_key(task, cache_key, messages) if cache_key is not None else None
        if key is not None:
            cached = self._cache_get(key)
            if cached is not None:
                return cached
        if not self.is_available():
            raise LLMUnavailable(f"LLM недоступна ({self.base_url})")

        dialog = list(messages)
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            started = time.monotonic()
            content, error = "", None
            try:
                content = self._client.complete(
                    dialog, schema=schema, temperature=self.temperature, max_tokens=max_tokens
                )
                data = _extract_json(content)
                if validator is not None:
                    data = validator(data)
            except LLMError as exc:
                error = exc
            finally:
                self._log(task, dialog, content, time.monotonic() - started, error, attempt)
            if error is None:
                if key is not None:
                    self._cache_put(key, task, data)
                return data
            if "недоступен" in str(error):
                with self._lock:
                    self._available, self._checked_at = False, time.monotonic()
                raise LLMUnavailable(str(error)) from error
            last_error = error
            dialog = list(messages) + [
                {"role": "assistant", "content": content},
                {"role": "user", "content": f"Ответ не прошёл проверку: {error}. Верни исправленный JSON."},
            ]
        raise LLMError(f"{task}: LLM не вернула корректный ответ: {last_error}")

    # --- кэш и журнал ---------------------------------------------------------

    def _cache_key(self, task: str, cache_key: str, messages: list[dict[str, str]]) -> str:
        version = SETTINGS.prompts.get(task, "v0")
        digest = hashlib.sha1(json.dumps(messages, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]
        return f"{task}:{version}:{cache_key}:{digest}"

    def _connect(self) -> sqlite3.Connection:
        # Кэш пишут несколько потоков и процессов: ждём блокировку, а не падаем.
        return sqlite3.connect(self.cache_path, timeout=30)

    def _cache_get(self, key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT response FROM llm_cache WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def _cache_put(self, key: str, task: str, data: dict[str, Any]) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT OR REPLACE INTO llm_cache (key, task, response, model, created_at) VALUES (?, ?, ?, ?, ?)",
                    (key, task, json.dumps(data, ensure_ascii=False), self.model,
                     datetime.now(timezone.utc).isoformat(timespec="seconds")),
                )
        except sqlite3.OperationalError:
            # Сбой записи в кэш не должен ронять вызов: ответ уже получен.
            pass

    def _log(
        self,
        task: str,
        messages: list[dict[str, str]],
        response: str,
        seconds: float,
        error: Exception | None,
        attempt: int,
    ) -> None:
        record = {
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "task": task,
            "prompt_version": SETTINGS.prompts.get(task),
            "model": self.model,
            "attempt": attempt,
            "seconds": round(seconds, 2),
            "messages": messages,
            "response": response,
            "error": str(error) if error else None,
        }
        with self._lock, self.log_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")


_DEFAULT: LLM | None = None


def default_llm() -> LLM:
    """Общий экземпляр LLM для процесса."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = LLM()
    return _DEFAULT
