"""Подключение к Qdrant — одно место для сервиса и скриптов.

* ``REC_NEWS_QDRANT_URL`` задан — сервер Qdrant (Docker: ``docker compose up -d qdrant``, порт 6333).
  С сервером работают сразу несколько процессов: API, воркеры Celery, скрипты;
* не задан — локальная база ``data/qdrant/storage`` (её открывает только один процесс).

Явно переданный ``path`` — всегда локальная база (скрипты оценки на ``data_eval/``, перенос векторов).
"""

from __future__ import annotations

from pathlib import Path

from qdrant_client import QdrantClient

from news_pipeline.config import QDRANT_API_KEY, QDRANT_PATH, QDRANT_URL


def connect(path: str | Path | None = None) -> QdrantClient:
    """Клиент Qdrant: сервер ``REC_NEWS_QDRANT_URL`` или локальная база ``path`` / ``data/qdrant/storage``."""
    if path is None and QDRANT_URL:
        return QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)
    return QdrantClient(path=str(path or QDRANT_PATH))


def location(path: str | Path | None = None) -> str:
    """Где векторы — для сообщений."""
    return f"сервер {QDRANT_URL}" if path is None and QDRANT_URL else f"локальная база {path or QDRANT_PATH}"
