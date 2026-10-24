"""SQLite-хранилище новостей на SQLAlchemy. Описание всех колонок — ``docs/news_db.md``.

Таблицы (объекты :class:`sqlalchemy.Table` ниже):

* ``news`` — входящие новости в ``news.db``;
* ``news_summary`` — подготовленные новости в ``new_summary.db`` и уникальные
  новости после дедупликации в ``unique.db``;
* ``news_sources`` — источники уникальной новости: она сама и её дубли
  (по строке на источник, ``unique.db``);
* ``news_fingerprint`` — отпечатки уникальных новостей для дешёвых шагов
  дедупликации (``unique.db``): SHA-1 нормализованного текста, ключ URL
  (оба с индексом) и MinHash (см. ``fingerprints.py``).

У каждого поля метаданных — своя колонка. В коде метаданные по-прежнему
словарь ``metadata`` той же формы, что и раньше (``title``, ``source``,
``enrichment``, ``test``, ``sources``, …): слой хранения раскладывает его
по колонкам при записи и собирает обратно при чтении. Ключи, для которых
колонки нет (поля внешнего источника), сохраняются в колонке ``extra``
(JSON) и тоже возвращаются в ``metadata``.

У ``news`` и ``news_summary`` есть статус обработки ``status``:

* ``new`` — ждёт обработки;
* ``processing`` — взята в работу (:func:`claim_news`, :func:`claim_summaries`);
* ``ready`` — обработана: подготовка записала её в ``new_summary.db``,
  дедупликация — в ``unique.db``. В ``unique.db`` у всех записей ``ready``.

Новости после обработки не удаляются — меняется только статус. Запись,
зависшая в ``processing`` дольше ``STALE_MINUTES`` (процесс упал), снова
получает ``new`` (:func:`release_stale`).

Базы прежнего формата (одна JSON-колонка ``metadata``) и базы без статуса
переводятся в новый формат автоматически при первом открытии.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import (
    Column, Connection, Engine, Float, Index, Integer, LargeBinary, MetaData, Table, Text, delete, func, inspect,
    or_, select, update,
)

from news_pipeline.storage import db

NEW, PROCESSING, READY = "new", "processing", "ready"
STATUSES = (NEW, PROCESSING, READY)
STALE_MINUTES = 30

# --- раскладка метаданных по колонкам ------------------------------------------

# Плоские ключи metadata → колонки (имя колонки = ключ).
_FLAT = {
    "news": ("title", "url", "source", "rubric", "publication_time", "created_at"),
    "news_summary": ("url", "source", "rubric", "publication_time", "created_at", "lead", "prepared_at",
                     "deduplicated_at", "event_id", "parent_event_id"),
}
# metadata["enrichment"] → колонки (ключ обогащения → колонка).
_ENRICHMENT = {"entities": "entities", "topics": "topics", "importance": "importance",
               "event_type": "event_type", "numbers": "numbers", "source": "enrichment_source"}
# metadata["test"] (эталонная разметка синтетики) → колонки test_*.
_TEST = ("relation", "related_to", "event_key", "topic", "importance", "genre", "event_type")
_JSON_COLUMNS = {"entities", "topics", "numbers", "extra"}
_INTEGER_COLUMNS = {"importance", "test_importance"}


def _meta_columns(table: str) -> list[str]:
    columns = list(_FLAT[table])
    if table == "news_summary":
        columns += list(_ENRICHMENT.values())
    return columns + [f"test_{key}" for key in _TEST] + ["extra"]


# --- схема ---------------------------------------------------------------------

metadata = MetaData()


def _table(name: str, base: list[Column], indexed: tuple[str, ...]) -> Table:
    """Таблица новостей: базовые колонки, колонка на каждое поле метаданных, статус."""
    names = {column.name for column in base}
    meta = [Column(column, Integer if column in _INTEGER_COLUMNS else Text)
            for column in _meta_columns(name) if column not in names]
    status = [Column("status", Text, nullable=False, server_default=NEW), Column("status_updated_at", Text)]
    table = Table(name, metadata, *base, *meta, *status)
    for column in indexed:
        Index(f"{name}_{column}", table.c[column])
    return table


NEWS = _table("news", [
    Column("document_id", Text, primary_key=True),
    Column("text", Text, nullable=False),
    Column("title", Text),
], indexed=("publication_time", "status"))

NEWS_SUMMARY = _table("news_summary", [
    Column("document_id", Text, primary_key=True),
    Column("text", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("first_paragraph", Text, nullable=False),
    Column("audience_text", Text, nullable=False, server_default=""),
], indexed=("publication_time", "source", "event_id", "status"))

NEWS_SOURCES = Table(
    "news_sources", metadata,
    Column("document_id", Text, primary_key=True),
    Column("position", Integer, nullable=False),
    Column("source_document_id", Text, primary_key=True),
    Column("url", Text),
    Column("source", Text),
    Column("publication_time", Text),
    Column("method", Text, nullable=False),
    Column("score", Float),
    Column("via", Text),
    Index("news_sources_source_document_id", "source_document_id"),
)

NEWS_FINGERPRINT = Table(
    "news_fingerprint", metadata,
    Column("document_id", Text, primary_key=True),
    Column("text_hash", Text, nullable=False),
    Column("url_key", Text),
    Column("minhash", LargeBinary, nullable=False),
    Index("news_fingerprint_text_hash", "text_hash"),
    Index("news_fingerprint_url_key", "url_key"),
)

_TABLES = {"news": NEWS, "news_summary": NEWS_SUMMARY}


def _flatten(meta_in: dict[str, Any], table: str) -> dict[str, Any]:
    """Словарь metadata → значения колонок таблицы (``sources`` пишется отдельно)."""
    meta = dict(meta_in)
    meta.pop("sources", None)
    values: dict[str, Any] = {}
    for key in _FLAT[table]:
        values[key] = meta.pop(key, None)
    if table == "news_summary":
        meta.pop("title", None)  # у подготовленной новости заголовок — отдельная колонка title
    enrichment = meta.pop("enrichment", None) if table == "news_summary" else None
    if isinstance(enrichment, dict):
        enrichment = dict(enrichment)
        if enrichment.get("lead") and not values.get("lead"):
            values["lead"] = enrichment["lead"]
        enrichment.pop("lead", None)
        for key, column in _ENRICHMENT.items():
            values[column] = enrichment.pop(key, None)
        if enrichment:  # неизвестные поля обогащения не теряем
            meta["enrichment"] = enrichment
    test = meta.pop("test", None)
    if isinstance(test, dict):
        test = dict(test)
        for key in _TEST:
            values[f"test_{key}"] = test.pop(key, None)
        if test:
            meta["test"] = test
    values["extra"] = meta or None
    for column in _JSON_COLUMNS:
        if values.get(column) is not None:
            values[column] = json.dumps(values[column], ensure_ascii=False)
    # у news нет колонок обогащения — в строке только колонки таблицы
    return {column: values.get(column) for column in _meta_columns(table)}


def _unflatten(row: dict[str, Any], table: str) -> dict[str, Any]:
    """Значения колонок → словарь metadata прежней формы."""
    values = {column: (json.loads(row[column]) if column in _JSON_COLUMNS and row.get(column) else row.get(column))
              for column in _meta_columns(table)}
    meta: dict[str, Any] = dict(values.pop("extra") or {})
    for key in _FLAT[table]:
        if values.get(key) is not None:
            meta[key] = values[key]
    if table == "news_summary" and any(values.get(column) is not None for column in _ENRICHMENT.values()):
        enrichment = dict(meta.get("enrichment") or {})
        enrichment["lead"] = values.get("lead") or ""
        for key, column in _ENRICHMENT.items():
            enrichment[key] = values.get(column)
        meta["enrichment"] = enrichment
    if any(values.get(f"test_{key}") is not None for key in _TEST):
        test = dict(meta.get("test") or {})
        test.update({key: values.get(f"test_{key}") for key in _TEST})
        meta["test"] = test
    return meta


def _ensure(path: str | Path, table: Table, extra: list[Table], key: str) -> Engine:
    """Создаёт таблицы базы; таблицу прежнего формата (JSON-колонка ``metadata``) переводит в новый."""
    bind = db.engine(path)
    if db.is_ensured(bind, key):
        return bind
    for other in extra:
        db.ensure_tables(bind, [other])
    existing = db.columns(bind, table.name)
    if existing and "metadata" in existing and (table is NEWS or "first_paragraph" in existing):
        _migrate(bind, table)
    elif existing and "extra" not in existing:
        raise RuntimeError(f"В {path} таблица {table.name} неизвестного формата {existing}. "
                           "Удалите файл, он будет создан заново.")
    db.ensure_tables(bind, [table, *extra])
    db.mark_ensured(bind, key)
    return bind


# --- входящие новости (news.db) -------------------------------------------------


@dataclass(frozen=True)
class StoredNews:
    """Новость в хранилище: текст и метаданные."""

    document_id: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


def ensure_schema(database_path: str | Path) -> None:
    """Создаёт таблицу ``news``; базу прежнего формата (JSON-колонка ``metadata``) переводит в новый."""
    _ensure(database_path, NEWS, [], "news")


def _news_row(item: StoredNews) -> dict[str, Any]:
    return {**_flatten(item.metadata, "news"), "document_id": item.document_id, "text": item.text}


def _insert(connection: Connection, statement, rows: list[dict[str, Any]]) -> int:
    """Вставка пачкой; возвращает число вставленных строк."""
    if not rows:
        return 0
    before = db.changes(connection)
    connection.execute(statement, rows)
    return db.changes(connection) - before


def insert_news(
    database_path: str | Path,
    items: Iterable[StoredNews],
    *,
    ignore_existing: bool = False,
) -> int:
    """Добавляет новости и возвращает число добавленных записей.

    При ``ignore_existing=True`` новости с уже существующим ``document_id``
    пропускаются, иначе поднимается ``sqlalchemy.exc.IntegrityError``.
    """
    ensure_schema(database_path)
    statement = db.insert_ignore(NEWS) if ignore_existing else NEWS.insert()
    with db.engine(database_path).begin() as connection:
        return _insert(connection, statement, [_news_row(item) for item in items])


def _status_clause(table: Table, statuses: Iterable[str] | None):
    return table.c.status.in_(tuple(statuses)) if statuses is not None else None


def read_news(database_path: str | Path, statuses: Iterable[str] | None = None) -> list[StoredNews]:
    """Читает новости из базы: все или только с заданными статусами."""
    ensure_schema(database_path)
    query = select(NEWS)
    if (clause := _status_clause(NEWS, statuses)) is not None:
        query = query.where(clause)
    with db.engine(database_path).connect() as connection:
        rows = [db.row_dict(row) for row in connection.execute(query)]
    return [StoredNews(row["document_id"], row["text"], _unflatten(row, "news")) for row in rows]


def read_ids(database_path: str | Path, statuses: Iterable[str] | None = None) -> set[str]:
    """``document_id`` новостей базы (все или с заданными статусами); пустое множество, если файла нет."""
    if not Path(database_path).exists():
        return set()
    ensure_schema(database_path)
    return _ids(database_path, NEWS, statuses)


def _ids(database_path: str | Path, table: Table, statuses: Iterable[str] | None) -> set[str]:
    query = select(table.c.document_id)
    if (clause := _status_clause(table, statuses)) is not None:
        query = query.where(clause)
    with db.engine(database_path).connect() as connection:
        return set(connection.scalars(query))


def claim_news(database_path: str | Path) -> list[StoredNews]:
    """Берёт в работу все новости со статусом ``new``: ставит им ``processing`` и возвращает их."""
    ensure_schema(database_path)
    ids = _claim(database_path, NEWS)
    return [item for item in read_news(database_path, (PROCESSING,)) if item.document_id in ids]


def set_news_status(database_path: str | Path, document_ids: Iterable[str], status: str) -> int:
    """Меняет статус новостей ``news.db``; возвращает число изменённых записей."""
    return _set_status(database_path, NEWS, document_ids, status)


def delete_news(database_path: str | Path, document_ids: Iterable[str]) -> int:
    """Удаляет новости по ``document_id`` и возвращает число удалённых записей."""
    return _delete(database_path, NEWS, list(document_ids))


def _delete(database_path: str | Path, table: Table, ids: list[str]) -> int:
    with db.engine(database_path).begin() as connection:
        before = db.changes(connection)
        for part in db.chunks(ids):
            connection.execute(delete(table).where(table.c.document_id.in_(part)))
        return db.changes(connection) - before


# --- подготовленные и уникальные новости (new_summary.db, unique.db) -------------


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
    """Создаёт таблицы ``news_summary`` и ``news_sources``; прежний формат переводит в новый."""
    _ensure(database_path, NEWS_SUMMARY, [NEWS_SOURCES], "news_summary")


def _summary_row(item: ProcessedNews) -> dict[str, Any]:
    return {**_flatten(item.metadata, "news_summary"), "document_id": item.document_id, "text": item.text,
            "title": item.title, "first_paragraph": item.first_paragraph, "audience_text": item.audience_text}


def _source_rows(document_id: str, entries: Iterable[dict[str, Any]], start: int = 0) -> list[dict[str, Any]]:
    return [
        {"document_id": document_id, "position": start + offset,
         "source_document_id": str(entry.get("document_id") or document_id), "url": entry.get("url"),
         "source": entry.get("source"), "publication_time": entry.get("publication_time"),
         "method": entry.get("method") or "original", "score": entry.get("score"), "via": entry.get("via")}
        for offset, entry in enumerate(entries)
    ]


def _write_summaries(connection: Connection, statement, items: list[ProcessedNews], status: str = NEW) -> int:
    now = _now()
    inserted = _insert(connection, statement,
                       [{**_summary_row(item), "status": status, "status_updated_at": now} for item in items])
    for item in items:
        sources = item.metadata.get("sources")
        if sources:
            connection.execute(delete(NEWS_SOURCES).where(NEWS_SOURCES.c.document_id == item.document_id))
            _insert(connection, db.insert_ignore(NEWS_SOURCES), _source_rows(item.document_id, sources))
    return inserted


def insert_summaries(
    database_path: str | Path,
    items: Iterable[ProcessedNews],
    *,
    replace: bool = True,
) -> int:
    """Записывает подготовленные новости.

    При ``replace=True`` запись с тем же ID заменяется, иначе поднимается
    ``sqlalchemy.exc.IntegrityError`` и ничего не записывается.
    """
    ensure_summary_schema(database_path)
    statement = db.upsert(NEWS_SUMMARY) if replace else NEWS_SUMMARY.insert()
    with db.engine(database_path).begin() as connection:
        return _write_summaries(connection, statement, list(items))


def _read_sources(connection: Connection, ids: list[str] | None) -> dict[str, list[dict[str, Any]]]:
    order = (NEWS_SOURCES.c.document_id, NEWS_SOURCES.c.position)
    if ids is None:
        queries = [select(NEWS_SOURCES).order_by(*order)]
    else:
        queries = [select(NEWS_SOURCES).where(NEWS_SOURCES.c.document_id.in_(part)).order_by(*order)
                   for part in db.chunks(ids)]
    result: dict[str, list[dict[str, Any]]] = {}
    for query in queries:
        for row in connection.execute(query):
            row = db.row_dict(row)
            entry = {"document_id": row["source_document_id"], "url": row["url"], "source": row["source"],
                     "publication_time": row["publication_time"], "method": row["method"]}
            if row["score"] is not None:
                entry["score"] = row["score"]
            if row["via"] is not None:
                entry["via"] = row["via"]
            result.setdefault(row["document_id"], []).append(entry)
    return result


def read_summaries(
    database_path: str | Path,
    document_ids: Iterable[str] | None = None,
) -> list[ProcessedNews]:
    """Читает новости из таблицы ``news_summary``: все или только с заданными ID (в их порядке)."""
    ensure_summary_schema(database_path)
    ids = list(dict.fromkeys(document_ids)) if document_ids is not None else None
    with db.engine(database_path).connect() as connection:
        if ids is None:
            rows = [db.row_dict(row) for row in connection.execute(select(NEWS_SUMMARY))]
        else:
            found = {}
            for part in db.chunks(ids):
                for row in connection.execute(select(NEWS_SUMMARY).where(NEWS_SUMMARY.c.document_id.in_(part))):
                    found[row.document_id] = db.row_dict(row)
            rows = [found[document_id] for document_id in ids if document_id in found]
        sources = _read_sources(connection, None if ids is None else [row["document_id"] for row in rows])
    result = []
    for row in rows:
        meta = {"title": row["title"], **_unflatten(row, "news_summary")}
        if row["document_id"] in sources:
            meta["sources"] = sources[row["document_id"]]
        result.append(ProcessedNews(row["document_id"], row["text"], row["title"], row["first_paragraph"],
                                    row["audience_text"], meta))
    return result


def summary_ids(database_path: str | Path, statuses: Iterable[str] | None = None) -> set[str]:
    """``document_id`` новостей ``news_summary`` (все или с заданными статусами); пусто, если файла нет."""
    if not Path(database_path).exists():
        return set()
    ensure_summary_schema(database_path)
    return _ids(database_path, NEWS_SUMMARY, statuses)


def claim_summaries(database_path: str | Path) -> list[ProcessedNews]:
    """Берёт в работу подготовленные новости со статусом ``new``: ставит ``processing`` и возвращает их."""
    ensure_summary_schema(database_path)
    return read_summaries(database_path, sorted(_claim(database_path, NEWS_SUMMARY)))


def set_summary_status(database_path: str | Path, document_ids: Iterable[str], status: str) -> int:
    """Меняет статус записей ``news_summary``; возвращает число изменённых записей."""
    return _set_status(database_path, NEWS_SUMMARY, document_ids, status)


def status_counts(database_path: str | Path, table: str) -> dict[str, int]:
    """Сколько записей таблицы в каждом статусе (``news`` или ``news_summary``)."""
    if not Path(database_path).exists():
        return {status: 0 for status in STATUSES}
    (ensure_schema if table == "news" else ensure_summary_schema)(database_path)
    target = _TABLES[table]
    with db.engine(database_path).connect() as connection:
        counts = dict(connection.execute(select(target.c.status, func.count()).group_by(target.c.status)).all())
    return {status: counts.get(status, 0) for status in STATUSES}


def release_stale(database_path: str | Path, table: str, minutes: int = STALE_MINUTES) -> int:
    """Записи, зависшие в ``processing`` дольше ``minutes``, снова получают ``new``."""
    if not Path(database_path).exists():
        return 0
    (ensure_schema if table == "news" else ensure_summary_schema)(database_path)
    target = _TABLES[table]
    border = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    with db.engine(database_path).begin() as connection:
        before = db.changes(connection)
        connection.execute(
            update(target)
            .where(target.c.status == PROCESSING,
                   or_(target.c.status_updated_at.is_(None), target.c.status_updated_at < border))
            .values(status=NEW, status_updated_at=_now())
        )
        return db.changes(connection) - before


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _claim(database_path: str | Path, table: Table) -> set[str]:
    """Атомарно переводит все записи ``new`` в ``processing``; возвращает их ID.

    ``BEGIN IMMEDIATE`` сразу берёт блокировку записи: два процесса не возьмут одну новость.
    """
    with db.engine(database_path).connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        ids = set(connection.scalars(select(table.c.document_id).where(table.c.status == NEW)))
        connection.execute(update(table).where(table.c.status == NEW)
                           .values(status=PROCESSING, status_updated_at=_now()))
        connection.commit()
    return ids


def _set_status(database_path: str | Path, table: Table, document_ids: Iterable[str], status: str) -> int:
    if status not in STATUSES:
        raise ValueError(f"статус должен быть одним из {STATUSES}")
    now = _now()
    ids = list(document_ids)
    with db.engine(database_path).begin() as connection:
        before = db.changes(connection)
        for part in db.chunks(ids):
            connection.execute(update(table).where(table.c.document_id.in_(part))
                               .values(status=status, status_updated_at=now))
        return db.changes(connection) - before


def delete_summaries(database_path: str | Path, document_ids: Iterable[str]) -> int:
    """Удаляет новости из ``news_summary`` (и их источники) и возвращает число удалённых записей."""
    ids = list(document_ids)
    deleted = _delete(database_path, NEWS_SUMMARY, ids)
    with db.engine(database_path).begin() as connection:
        for part in db.chunks(ids):
            connection.execute(delete(NEWS_SOURCES).where(NEWS_SOURCES.c.document_id.in_(part)))
    return deleted


# --- перевод из прежнего формата ------------------------------------------------


def _migrate(bind: Engine, table: Table) -> None:
    """Таблица с JSON-колонкой ``metadata`` → таблица с колонкой на каждое поле."""
    name = table.name
    with bind.begin() as connection:
        rows = [db.row_dict(row) for row in connection.exec_driver_sql(f"SELECT * FROM {name}")]
        connection.exec_driver_sql(f"ALTER TABLE {name} RENAME TO {name}_old_format")
        for index in inspect(connection).get_indexes(f"{name}_old_format"):
            connection.exec_driver_sql(f"DROP INDEX IF EXISTS {index['name']}")
        table.create(connection)
        if table is NEWS:
            items = [StoredNews(row["document_id"], row["text"], json.loads(row["metadata"] or "{}")) for row in rows]
            _insert(connection, NEWS.insert(), [{**_news_row(item), "status": NEW} for item in items])
        else:
            items = [ProcessedNews(row["document_id"], row["text"], row["title"], row["first_paragraph"],
                                   row["audience_text"], json.loads(row["metadata"] or "{}")) for row in rows]
            _write_summaries(connection, NEWS_SUMMARY.insert(), items)
        connection.exec_driver_sql(f"DROP TABLE {name}_old_format")
    db.forget(bind)


# --- Отпечатки уникальных новостей (unique.db) ---------------------------


@dataclass(frozen=True)
class StoredFingerprint:
    """Отпечатки уникальной новости: SHA-1 нормализованного текста, ключ URL, MinHash."""

    document_id: str
    text_hash: str
    url_key: str | None
    minhash: bytes


def ensure_fingerprint_schema(database_path: str | Path) -> None:
    """Создаёт таблицу ``news_fingerprint`` с индексами по хешу текста и URL.

    Это база уникальных новостей: все её записи обработаны, статус — ``ready``.
    """
    bind = db.engine(database_path)
    if db.is_ensured(bind, "unique"):
        return
    ensure_summary_schema(database_path)
    db.ensure_tables(bind, [NEWS_FINGERPRINT])
    with bind.begin() as connection:
        connection.execute(
            update(NEWS_SUMMARY).where(NEWS_SUMMARY.c.status != READY)
            .values(status=READY, status_updated_at=func.coalesce(NEWS_SUMMARY.c.status_updated_at, _now()))
        )
    db.mark_ensured(bind, "unique")


def _fingerprint_rows(fingerprints: Iterable[StoredFingerprint]) -> list[dict[str, Any]]:
    return [{"document_id": fp.document_id, "text_hash": fp.text_hash, "url_key": fp.url_key, "minhash": fp.minhash}
            for fp in fingerprints]


def insert_unique_news(
    database_path: str | Path,
    items: Iterable[ProcessedNews],
    fingerprints: Iterable[StoredFingerprint],
) -> int:
    """Одной транзакцией добавляет уникальные новости, их источники и отпечатки.

    Существующие записи не перезаписываются: при совпадении ``document_id``
    поднимается ``sqlalchemy.exc.IntegrityError`` и не записывается ничего.
    """
    ensure_fingerprint_schema(database_path)
    with db.engine(database_path).begin() as connection:
        inserted = _write_summaries(connection, NEWS_SUMMARY.insert(), list(items), status=READY)
        rows = _fingerprint_rows(fingerprints)
        if rows:
            connection.execute(NEWS_FINGERPRINT.insert(), rows)
        return inserted


def upsert_fingerprints(database_path: str | Path, fingerprints: Iterable[StoredFingerprint]) -> None:
    """Записывает отпечатки уже сохранённых новостей (для старых баз без них)."""
    ensure_fingerprint_schema(database_path)
    rows = _fingerprint_rows(fingerprints)
    if rows:
        with db.engine(database_path).begin() as connection:
            connection.execute(db.upsert(NEWS_FINGERPRINT), rows)


def read_fingerprints(database_path: str | Path) -> list[StoredFingerprint]:
    """Читает отпечатки всех уникальных новостей."""
    ensure_fingerprint_schema(database_path)
    with db.engine(database_path).connect() as connection:
        rows = connection.execute(select(NEWS_FINGERPRINT.c.document_id, NEWS_FINGERPRINT.c.text_hash,
                                         NEWS_FINGERPRINT.c.url_key, NEWS_FINGERPRINT.c.minhash)).all()
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
    with db.engine(database_path).connect() as connection:
        return connection.scalar(select(NEWS_FINGERPRINT.c.document_id)
                                 .where(NEWS_FINGERPRINT.c[column] == value).limit(1))


def append_sources(database_path: str | Path, sources: dict[str, list[dict[str, Any]]]) -> int:
    """Дописывает в ``news_sources`` уникальных новостей источники их дублей.

    ``sources`` — ``{document_id оригинала: [запись источника, ...]}``.
    Источник, который уже записан у этой новости (повторная обработка пачки
    после сбоя), повторно не добавляется. Возвращает число обновлённых новостей.
    """
    ensure_summary_schema(database_path)
    updated = 0
    with db.engine(database_path).begin() as connection:
        for document_id, entries in sources.items():
            exists = connection.scalar(select(NEWS_SUMMARY.c.document_id)
                                       .where(NEWS_SUMMARY.c.document_id == document_id))
            if exists is None:
                continue
            start = connection.scalar(select(func.coalesce(func.max(NEWS_SOURCES.c.position) + 1, 0))
                                      .where(NEWS_SOURCES.c.document_id == document_id))
            if _insert(connection, db.insert_ignore(NEWS_SOURCES), _source_rows(document_id, entries, start)):
                updated += 1
    return updated
