"""SQLite-хранилище новостей.

Две таблицы:

* ``news`` — входящие новости в ``news.db``: ``document_id``, ``text``
  (полный текст) и ``metadata`` (JSON: ``title``, ``url``, ``source``,
  ``publication_time``, ``created_at`` и другие поля источника);
* ``news_summary`` — новости после LLM-анализа в ``new_summary.db``
  и уникальные новости после дедупликации в ``unique.db``: те же поля
  плюс ``summary`` и ``audience_text``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS news (
    document_id TEXT PRIMARY KEY,
    text        TEXT NOT NULL,
    metadata    TEXT NOT NULL DEFAULT '{}'
)
"""
COLUMNS = ("document_id", "text", "metadata")


@dataclass(frozen=True)
class StoredNews:
    """Новость в хранилище: текст и метаданные."""

    document_id: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


def ensure_schema(database_path: str | Path) -> None:
    """Создаёт таблицу ``news``, если её нет, и проверяет её формат.

    Если в файле осталась таблица старого формата (с колонками ``title``,
    ``first_paragraph`` и т. д.), поднимает ``RuntimeError``: такую базу
    нужно удалить и создать заново.
    """
    with sqlite3.connect(database_path) as connection:
        connection.execute(SCHEMA)
        columns = tuple(row[1] for row in connection.execute("PRAGMA table_info(news)"))
    if columns != COLUMNS:
        raise RuntimeError(
            f"В {database_path} таблица news старого формата {columns}, "
            f"ожидается {COLUMNS}. Удалите файл, он будет создан заново."
        )


def insert_news(
    database_path: str | Path,
    items: Iterable[StoredNews],
    *,
    ignore_existing: bool = False,
) -> int:
    """Добавляет новости и возвращает число добавленных записей.

    При ``ignore_existing=True`` новости с уже существующим ``document_id``
    пропускаются, иначе поднимается ``sqlite3.IntegrityError``.
    """
    ensure_schema(database_path)
    verb = "INSERT OR IGNORE" if ignore_existing else "INSERT"
    with sqlite3.connect(database_path) as connection:
        before = connection.total_changes
        connection.executemany(
            f"{verb} INTO news (document_id, text, metadata) VALUES (?, ?, ?)",
            [
                (item.document_id, item.text, json.dumps(item.metadata, ensure_ascii=False))
                for item in items
            ],
        )
        return connection.total_changes - before


def read_news(database_path: str | Path) -> list[StoredNews]:
    """Читает все новости из базы."""
    ensure_schema(database_path)
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute("SELECT document_id, text, metadata FROM news").fetchall()
    return [
        StoredNews(document_id=row[0], text=row[1], metadata=json.loads(row[2] or "{}"))
        for row in rows
    ]


def read_ids(database_path: str | Path) -> set[str]:
    """Возвращает ``document_id`` всех новостей базы (пустое множество, если файла нет)."""
    if not Path(database_path).exists():
        return set()
    ensure_schema(database_path)
    with sqlite3.connect(database_path) as connection:
        return {row[0] for row in connection.execute("SELECT document_id FROM news")}


def delete_news(database_path: str | Path, document_ids: Iterable[str]) -> int:
    """Удаляет новости по ``document_id`` и возвращает число удалённых записей."""
    with sqlite3.connect(database_path) as connection:
        before = connection.total_changes
        connection.executemany(
            "DELETE FROM news WHERE document_id = ?",
            [(document_id,) for document_id in document_ids],
        )
        return connection.total_changes - before


SUMMARY_SCHEMA = """
CREATE TABLE IF NOT EXISTS news_summary (
    document_id   TEXT PRIMARY KEY,
    text          TEXT NOT NULL,
    summary       TEXT NOT NULL,
    audience_text TEXT NOT NULL,
    metadata      TEXT NOT NULL DEFAULT '{}'
)
"""
SUMMARY_COLUMNS = ("document_id", "text", "summary", "audience_text", "metadata")


@dataclass(frozen=True)
class SummarizedNews:
    """Новость с результатом LLM-анализа: выжимкой и шаблоном аудитории."""

    document_id: str
    text: str
    summary: str
    audience_text: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def title(self) -> str | None:
        return self.metadata.get("title")

    @property
    def url(self) -> str | None:
        return self.metadata.get("url")

    @property
    def publication_time(self) -> str | None:
        return self.metadata.get("publication_time")


def ensure_summary_schema(database_path: str | Path) -> None:
    """Создаёт таблицу ``news_summary``, если её нет, и проверяет её формат."""
    with sqlite3.connect(database_path) as connection:
        connection.execute(SUMMARY_SCHEMA)
        columns = tuple(
            row[1] for row in connection.execute("PRAGMA table_info(news_summary)")
        )
    if columns != SUMMARY_COLUMNS:
        raise RuntimeError(
            f"В {database_path} таблица news_summary формата {columns}, "
            f"ожидается {SUMMARY_COLUMNS}. Удалите файл, он будет создан заново."
        )


def insert_summaries(database_path: str | Path, items: Iterable[SummarizedNews]) -> int:
    """Записывает обработанные новости (запись с тем же ID заменяется)."""
    ensure_summary_schema(database_path)
    with sqlite3.connect(database_path) as connection:
        before = connection.total_changes
        connection.executemany(
            "INSERT OR REPLACE INTO news_summary "
            "(document_id, text, summary, audience_text, metadata) VALUES (?, ?, ?, ?, ?)",
            [
                (
                    item.document_id,
                    item.text,
                    item.summary,
                    item.audience_text,
                    json.dumps(item.metadata, ensure_ascii=False),
                )
                for item in items
            ],
        )
        return connection.total_changes - before


def read_summaries(database_path: str | Path) -> list[SummarizedNews]:
    """Читает все новости из таблицы ``news_summary``."""
    ensure_summary_schema(database_path)
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT document_id, text, summary, audience_text, metadata FROM news_summary"
        ).fetchall()
    return [
        SummarizedNews(
            document_id=row[0],
            text=row[1],
            summary=row[2],
            audience_text=row[3],
            metadata=json.loads(row[4] or "{}"),
        )
        for row in rows
    ]


def summary_ids(database_path: str | Path) -> set[str]:
    """``document_id`` всех новостей таблицы ``news_summary`` (пусто, если файла нет)."""
    if not Path(database_path).exists():
        return set()
    ensure_summary_schema(database_path)
    with sqlite3.connect(database_path) as connection:
        return {row[0] for row in connection.execute("SELECT document_id FROM news_summary")}


def delete_summaries(database_path: str | Path, document_ids: Iterable[str]) -> int:
    """Удаляет новости из ``news_summary`` и возвращает число удалённых записей."""
    with sqlite3.connect(database_path) as connection:
        before = connection.total_changes
        connection.executemany(
            "DELETE FROM news_summary WHERE document_id = ?",
            [(document_id,) for document_id in document_ids],
        )
        return connection.total_changes - before
