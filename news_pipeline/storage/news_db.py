"""SQLite-хранилище новостей.

Две таблицы:

* ``news`` — входящие новости в ``news.db``: ``document_id``, ``text``
  (полный текст) и ``metadata`` (JSON: ``title``, ``url``, ``source``,
  ``publication_time``, ``created_at`` и другие поля источника);
* ``news_summary`` — подготовленные новости в ``new_summary.db``
  и уникальные новости после дедупликации в ``unique.db``: полный текст,
  заголовок, первый абзац, шаблон аудитории (``audience_text``, пока
  пустой — заполнялся LLM) и метаданные.

В ``unique.db`` рядом лежит таблица ``news_fingerprint`` — отпечатки
уникальных новостей для дешёвых шагов дедупликации: SHA-1 нормализованного
текста и ключ URL (оба с индексом) и MinHash (см. ``fingerprints.py``).
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
    document_id     TEXT PRIMARY KEY,
    text            TEXT NOT NULL,
    title           TEXT NOT NULL,
    first_paragraph TEXT NOT NULL,
    audience_text   TEXT NOT NULL DEFAULT '',
    metadata        TEXT NOT NULL DEFAULT '{}'
)
"""
SUMMARY_COLUMNS = ("document_id", "text", "title", "first_paragraph", "audience_text", "metadata")


def first_paragraph(text: str) -> str:
    """Первый абзац новости — текст до первой пустой строки."""
    return text.strip().split("\n\n", 1)[0].strip()


@dataclass(frozen=True)
class ProcessedNews:
    """Подготовленная новость: полный текст, заголовок и первый абзац."""

    document_id: str
    text: str
    title: str
    first_paragraph: str
    audience_text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_stored(cls, news: StoredNews) -> "ProcessedNews":
        """Готовит новость из ``news.db``: заголовок — из метаданных, первый абзац — из текста."""
        return cls(
            document_id=news.document_id,
            text=news.text,
            title=str(news.metadata.get("title") or "").strip(),
            first_paragraph=first_paragraph(news.text),
            metadata=dict(news.metadata),
        )

    @property
    def lead(self) -> str:
        """Текст для эмбеддингов: лид из подготовки новости (заголовок + первые
        предложения), иначе — заголовок и первый абзац."""
        if self.metadata.get("lead"):
            return str(self.metadata["lead"])
        return "\n".join(part for part in (self.title, self.first_paragraph) if part)

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


def _summary_rows(items: Iterable[ProcessedNews]) -> list[tuple]:
    return [
        (
            item.document_id,
            item.text,
            item.title,
            item.first_paragraph,
            item.audience_text,
            json.dumps(item.metadata, ensure_ascii=False),
        )
        for item in items
    ]


def insert_summaries(
    database_path: str | Path,
    items: Iterable[ProcessedNews],
    *,
    replace: bool = True,
) -> int:
    """Записывает подготовленные новости.

    При ``replace=True`` запись с тем же ID заменяется, иначе поднимается
    ``sqlite3.IntegrityError`` и ничего не записывается.
    """
    ensure_summary_schema(database_path)
    verb = "INSERT OR REPLACE" if replace else "INSERT"
    with sqlite3.connect(database_path) as connection:
        before = connection.total_changes
        connection.executemany(
            f"{verb} INTO news_summary "
            "(document_id, text, title, first_paragraph, audience_text, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            _summary_rows(items),
        )
        return connection.total_changes - before


def read_summaries(
    database_path: str | Path,
    document_ids: Iterable[str] | None = None,
) -> list[ProcessedNews]:
    """Читает новости из таблицы ``news_summary``: все или только с заданными ID."""
    ensure_summary_schema(database_path)
    query = (
        "SELECT document_id, text, title, first_paragraph, audience_text, metadata "
        "FROM news_summary"
    )
    with sqlite3.connect(database_path) as connection:
        if document_ids is None:
            rows = connection.execute(query).fetchall()
        else:
            rows = [
                row
                for document_id in dict.fromkeys(document_ids)
                for row in connection.execute(f"{query} WHERE document_id = ?", (document_id,))
            ]
    return [
        ProcessedNews(
            document_id=row[0],
            text=row[1],
            title=row[2],
            first_paragraph=row[3],
            audience_text=row[4],
            metadata=json.loads(row[5] or "{}"),
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


# --- Отпечатки уникальных новостей (unique.db) ---------------------------

FINGERPRINT_SCHEMA = """
CREATE TABLE IF NOT EXISTS news_fingerprint (
    document_id TEXT PRIMARY KEY,
    text_hash   TEXT NOT NULL,
    url_key     TEXT,
    minhash     BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS news_fingerprint_text_hash ON news_fingerprint (text_hash);
CREATE INDEX IF NOT EXISTS news_fingerprint_url_key ON news_fingerprint (url_key);
"""


@dataclass(frozen=True)
class StoredFingerprint:
    """Отпечатки уникальной новости: SHA-1 нормализованного текста, ключ URL, MinHash."""

    document_id: str
    text_hash: str
    url_key: str | None
    minhash: bytes


def ensure_fingerprint_schema(database_path: str | Path) -> None:
    """Создаёт таблицу ``news_fingerprint`` с индексами по хешу текста и URL."""
    ensure_summary_schema(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.executescript(FINGERPRINT_SCHEMA)


def insert_unique_news(
    database_path: str | Path,
    items: Iterable[ProcessedNews],
    fingerprints: Iterable[StoredFingerprint],
) -> int:
    """Одной транзакцией добавляет уникальные новости и их отпечатки.

    Существующие записи не перезаписываются: при совпадении ``document_id``
    поднимается ``sqlite3.IntegrityError`` и не записывается ничего.
    """
    ensure_fingerprint_schema(database_path)
    with sqlite3.connect(database_path) as connection:
        before = connection.total_changes
        connection.executemany(
            "INSERT INTO news_summary "
            "(document_id, text, title, first_paragraph, audience_text, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            _summary_rows(items),
        )
        inserted = connection.total_changes - before
        connection.executemany(
            "INSERT INTO news_fingerprint (document_id, text_hash, url_key, minhash) "
            "VALUES (?, ?, ?, ?)",
            [(fp.document_id, fp.text_hash, fp.url_key, fp.minhash) for fp in fingerprints],
        )
        return inserted


def upsert_fingerprints(database_path: str | Path, fingerprints: Iterable[StoredFingerprint]) -> None:
    """Записывает отпечатки уже сохранённых новостей (для старых баз без них)."""
    ensure_fingerprint_schema(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.executemany(
            "INSERT OR REPLACE INTO news_fingerprint (document_id, text_hash, url_key, minhash) "
            "VALUES (?, ?, ?, ?)",
            [(fp.document_id, fp.text_hash, fp.url_key, fp.minhash) for fp in fingerprints],
        )


def read_fingerprints(database_path: str | Path) -> list[StoredFingerprint]:
    """Читает отпечатки всех уникальных новостей."""
    ensure_fingerprint_schema(database_path)
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT document_id, text_hash, url_key, minhash FROM news_fingerprint"
        ).fetchall()
    return [StoredFingerprint(*row) for row in rows]


def find_by_fingerprint(
    database_path: str | Path,
    *,
    document_id: str | None = None,
    text_hash: str | None = None,
    url_key: str | None = None,
) -> str | None:
    """``document_id`` уникальной новости с тем же ID, хешем текста или ключом URL.

    Задаётся ровно одно условие; поиск идёт по первичному ключу или индексу.
    """
    conditions = {"document_id": document_id, "text_hash": text_hash, "url_key": url_key}
    given = [(column, value) for column, value in conditions.items() if value is not None]
    if not given:
        return None
    if len(given) > 1:
        raise ValueError("Передайте одно условие: document_id, text_hash или url_key.")
    column, value = given[0]
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            f"SELECT document_id FROM news_fingerprint WHERE {column} = ? LIMIT 1", (value,)
        ).fetchone()
    return row[0] if row else None


def append_sources(database_path: str | Path, sources: dict[str, list[dict[str, Any]]]) -> int:
    """Дописывает в ``metadata["sources"]`` уникальных новостей источники их дублей.

    ``sources`` — ``{document_id оригинала: [запись источника, ...]}``.
    Запись с той же парой ``document_id`` и ``url``, что уже есть в списке,
    повторно не добавляется (повторная обработка пачки после сбоя).
    Возвращает число обновлённых новостей.
    """
    updated = 0
    with sqlite3.connect(database_path) as connection:
        for document_id, entries in sources.items():
            row = connection.execute(
                "SELECT metadata FROM news_summary WHERE document_id = ?", (document_id,)
            ).fetchone()
            if row is None:
                continue
            metadata = json.loads(row[0] or "{}")
            current = metadata.setdefault("sources", [])
            known = {(entry.get("document_id"), entry.get("url")) for entry in current}
            fresh = [
                entry for entry in entries if (entry.get("document_id"), entry.get("url")) not in known
            ]
            if not fresh:
                continue
            current.extend(fresh)
            connection.execute(
                "UPDATE news_summary SET metadata = ? WHERE document_id = ?",
                (json.dumps(metadata, ensure_ascii=False), document_id),
            )
            updated += 1
    return updated
