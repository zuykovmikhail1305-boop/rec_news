"""Хранилище новостей на SQLAlchemy ORM. Описание всех колонок — ``docs/news_db.md``.

ORM-модели (базы — файлы SQLite или схемы PostgreSQL, см. ``storage/db.py``):

* :class:`News` (``news``) — входящие новости в ``news.db``;
* :class:`NewsSummary` (``news_summary``) — подготовленные новости в ``new_summary.db`` и уникальные
  новости после дедупликации в ``unique.db`` (одна модель — в какой базе строка, решает сессия);
* :class:`NewsSource` (``news_sources``) — источники уникальной новости: она сама и её дубли
  (по строке на источник, ``unique.db``);
* :class:`NewsFingerprint` (``news_fingerprint``) — отпечатки уникальных новостей для дешёвых шагов
  дедупликации (``unique.db``): SHA-1 нормализованного текста, ключ URL (оба с индексом) и MinHash
  (см. ``fingerprints.py``).

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
  дедупликация — в ``unique.db``. В ``unique.db`` у всех записей ``ready``;
* ``failed`` — только ``news``: не обработана после всех повторов (задача ``analytics.analyze``).

Новости после обработки не удаляются — меняется только статус. Запись,
зависшая в ``processing`` дольше ``STALE_MINUTES`` (процесс упал), снова
получает ``new`` (:func:`release_stale`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import Engine, Float, Index, Integer, LargeBinary, Text, delete, func, insert, or_, select, update
from sqlalchemy.orm import Mapped, Session, mapped_column

from news_pipeline.storage import db

# failed — новость не удалось обработать после всех повторов (задача analytics.analyze); заново в работу
# её возвращает новая команда AnalyzeNews или смена статуса на new.
NEW, PROCESSING, READY, FAILED = "new", "processing", "ready", "failed"
STATUSES = (NEW, PROCESSING, READY, FAILED)
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


def _meta_columns(table: str) -> list[str]:
    columns = list(_FLAT[table])
    if table == "news_summary":
        columns += list(_ENRICHMENT.values())
    return columns + [f"test_{key}" for key in _TEST] + ["extra"]


# --- модели --------------------------------------------------------------------


class _SourceColumns:
    """Поля метаданных, общие для входящей и подготовленной новости."""

    url: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(Text)
    rubric: Mapped[str | None] = mapped_column(Text)
    publication_time: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str | None] = mapped_column(Text)


class _TestColumns:
    """Эталонная разметка синтетических новостей (metadata["test"])."""

    test_relation: Mapped[str | None] = mapped_column(Text)
    test_related_to: Mapped[str | None] = mapped_column(Text)
    test_event_key: Mapped[str | None] = mapped_column(Text)
    test_topic: Mapped[str | None] = mapped_column(Text)
    test_importance: Mapped[int | None] = mapped_column(Integer)
    test_genre: Mapped[str | None] = mapped_column(Text)
    test_event_type: Mapped[str | None] = mapped_column(Text)


class _StatusColumns:
    """Прочие поля метаданных (JSON) и статус обработки."""

    extra: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=NEW, default=NEW)
    status_updated_at: Mapped[str | None] = mapped_column(Text)


class News(_SourceColumns, _TestColumns, _StatusColumns, db.Base):
    """Входящая новость (``news.db``)."""

    __tablename__ = "news"
    __table_args__ = (Index("news_publication_time", "publication_time"), Index("news_status", "status"))

    document_id: Mapped[str] = mapped_column(Text, primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)


class NewsSummary(_SourceColumns, _TestColumns, _StatusColumns, db.Base):
    """Подготовленная (``new_summary.db``) или уникальная (``unique.db``) новость."""

    __tablename__ = "news_summary"
    __table_args__ = tuple(Index(f"news_summary_{column}", column)
                           for column in ("publication_time", "source", "event_id", "status"))

    document_id: Mapped[str] = mapped_column(Text, primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    first_paragraph: Mapped[str] = mapped_column(Text)
    audience_text: Mapped[str] = mapped_column(Text, server_default="", default="")
    lead: Mapped[str | None] = mapped_column(Text)
    prepared_at: Mapped[str | None] = mapped_column(Text)
    deduplicated_at: Mapped[str | None] = mapped_column(Text)
    event_id: Mapped[str | None] = mapped_column(Text)
    parent_event_id: Mapped[str | None] = mapped_column(Text)
    entities: Mapped[str | None] = mapped_column(Text)
    topics: Mapped[str | None] = mapped_column(Text)
    importance: Mapped[int | None] = mapped_column(Integer)
    event_type: Mapped[str | None] = mapped_column(Text)
    numbers: Mapped[str | None] = mapped_column(Text)
    enrichment_source: Mapped[str | None] = mapped_column(Text)


class NewsSource(db.Base):
    """Источник уникальной новости: она сама или её дубль (``unique.db``)."""

    __tablename__ = "news_sources"
    __table_args__ = (Index("news_sources_source_document_id", "source_document_id"),)

    document_id: Mapped[str] = mapped_column(Text, primary_key=True)
    position: Mapped[int] = mapped_column(Integer)
    source_document_id: Mapped[str] = mapped_column(Text, primary_key=True)
    url: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(Text)
    publication_time: Mapped[str | None] = mapped_column(Text)
    method: Mapped[str] = mapped_column(Text)
    score: Mapped[float | None] = mapped_column(Float)
    via: Mapped[str | None] = mapped_column(Text)

    def entry(self) -> dict[str, Any]:
        entry = {"document_id": self.source_document_id, "url": self.url, "source": self.source,
                 "publication_time": self.publication_time, "method": self.method}
        if self.score is not None:
            entry["score"] = self.score
        if self.via is not None:
            entry["via"] = self.via
        return entry


class NewsFingerprint(db.Base):
    """Отпечатки уникальной новости (``unique.db``)."""

    __tablename__ = "news_fingerprint"
    __table_args__ = (Index("news_fingerprint_text_hash", "text_hash"), Index("news_fingerprint_url_key", "url_key"))

    document_id: Mapped[str] = mapped_column(Text, primary_key=True)
    text_hash: Mapped[str] = mapped_column(Text)
    url_key: Mapped[str | None] = mapped_column(Text)
    minhash: Mapped[bytes] = mapped_column(LargeBinary)


_MODELS = {"news": News, "news_summary": NewsSummary}


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


def _ensure(path: str | Path, model: type[db.Base], extra: list[type[db.Base]], key: str) -> Engine:
    """Создаёт таблицы базы (модель и дополнительные)."""
    bind = db.engine(path)
    if db.is_ensured(bind, key):
        return bind
    existing = db.columns(bind, model.__tablename__)
    if existing and "extra" not in existing:
        raise RuntimeError(f"В {db.location(path)} таблица {model.__tablename__} прежнего формата {existing}. "
                           "Удалите базу, она будет создана заново.")
    db.ensure_tables(bind, [model, *extra])
    db.mark_ensured(bind, key)
    return bind


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _execute_counting(session: Session, statement, rows: list[dict[str, Any]]) -> int:
    """ORM-вставка пачкой; возвращает число вставленных строк (без пропущенных ``ON CONFLICT DO NOTHING``)."""
    if not rows:
        return 0
    before = db.changes(session)
    session.execute(statement, rows)
    return db.changes(session) - before


# --- входящие новости (news.db) -------------------------------------------------


@dataclass(frozen=True)
class StoredNews:
    """Новость в хранилище: текст и метаданные."""

    document_id: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: News) -> "StoredNews":
        return cls(row.document_id, row.text, _unflatten(db.as_dict(row), "news"))


def ensure_schema(database_path: str | Path) -> None:
    """Создаёт таблицу ``news``."""
    _ensure(database_path, News, [], "news")


def _news_row(item: StoredNews) -> dict[str, Any]:
    return {**_flatten(item.metadata, "news"), "document_id": item.document_id, "text": item.text}


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
    statement = db.insert_ignore(News) if ignore_existing else insert(News)
    with db.session(database_path) as session:
        return _execute_counting(session, statement, [_news_row(item) for item in items])


def _status_clause(model: type[News] | type[NewsSummary], statuses: Iterable[str] | None):
    return model.status.in_(tuple(statuses)) if statuses is not None else None


def read_news(database_path: str | Path, statuses: Iterable[str] | None = None) -> list[StoredNews]:
    """Читает новости из базы: все или только с заданными статусами."""
    ensure_schema(database_path)
    query = select(News)
    if (clause := _status_clause(News, statuses)) is not None:
        query = query.where(clause)
    with db.session(database_path) as session:
        return [StoredNews.from_row(row) for row in session.scalars(query)]


def read_ids(database_path: str | Path, statuses: Iterable[str] | None = None) -> set[str]:
    """``document_id`` новостей базы (все или с заданными статусами); пустое множество, если базы нет."""
    if not db.exists(database_path):
        return set()
    ensure_schema(database_path)
    return _ids(database_path, News, statuses)


def _ids(database_path: str | Path, model: type[News] | type[NewsSummary], statuses: Iterable[str] | None) -> set[str]:
    query = select(model.document_id)
    if (clause := _status_clause(model, statuses)) is not None:
        query = query.where(clause)
    with db.session(database_path) as session:
        return set(session.scalars(query))


def claim_news(database_path: str | Path, document_ids: Iterable[str] | None = None) -> list[StoredNews]:
    """Берёт в работу новости со статусом ``new`` (все или только ``document_ids``): ставит им
    ``processing`` и возвращает их."""
    ensure_schema(database_path)
    ids = _claim(database_path, News, document_ids)
    return [item for item in read_news(database_path, (PROCESSING,)) if item.document_id in ids]


def news_status(database_path: str | Path, document_id: str) -> str | None:
    """Статус новости ``news.db``; ``None`` — такой новости нет."""
    if not db.exists(database_path):
        return None
    ensure_schema(database_path)
    with db.session(database_path) as session:
        return session.scalar(select(News.status).where(News.document_id == document_id))


def set_news_status(database_path: str | Path, document_ids: Iterable[str], status: str) -> int:
    """Меняет статус новостей ``news.db``; возвращает число изменённых записей."""
    return _set_status(database_path, News, document_ids, status)


def delete_news(database_path: str | Path, document_ids: Iterable[str]) -> int:
    """Удаляет новости по ``document_id`` и возвращает число удалённых записей."""
    return _delete(database_path, News, list(document_ids))


def _delete(database_path: str | Path, model: type[db.Base], ids: list[str]) -> int:
    with db.session(database_path) as session:
        return sum(session.execute(delete(model).where(model.document_id.in_(part))
                                   .execution_options(synchronize_session=False)).rowcount
                   for part in db.chunks(ids))


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
    """Создаёт таблицы ``news_summary`` и ``news_sources``."""
    _ensure(database_path, NewsSummary, [NewsSource], "news_summary")


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


def _write_summaries(session: Session, statement, items: list[ProcessedNews], status: str = NEW) -> int:
    now = _now()
    inserted = _execute_counting(session, statement,
                                 [{**_summary_row(item), "status": status, "status_updated_at": now} for item in items])
    for item in items:
        sources = item.metadata.get("sources")
        if sources:
            session.execute(delete(NewsSource).where(NewsSource.document_id == item.document_id))
            _execute_counting(session, db.insert_ignore(NewsSource), _source_rows(item.document_id, sources))
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
    statement = db.upsert(NewsSummary) if replace else insert(NewsSummary)
    with db.session(database_path) as session:
        return _write_summaries(session, statement, list(items))


def _read_sources(session: Session, ids: list[str] | None) -> dict[str, list[dict[str, Any]]]:
    order = (NewsSource.document_id, NewsSource.position)
    if ids is None:
        queries = [select(NewsSource).order_by(*order)]
    else:
        queries = [select(NewsSource).where(NewsSource.document_id.in_(part)).order_by(*order)
                   for part in db.chunks(ids)]
    result: dict[str, list[dict[str, Any]]] = {}
    for query in queries:
        for source in session.scalars(query):
            result.setdefault(source.document_id, []).append(source.entry())
    return result


def read_summaries(
    database_path: str | Path,
    document_ids: Iterable[str] | None = None,
) -> list[ProcessedNews]:
    """Читает новости из таблицы ``news_summary``: все или только с заданными ID (в их порядке)."""
    ensure_summary_schema(database_path)
    ids = list(dict.fromkeys(document_ids)) if document_ids is not None else None
    with db.session(database_path) as session:
        if ids is None:
            rows = list(session.scalars(select(NewsSummary)))
        else:
            found = {}
            for part in db.chunks(ids):
                for row in session.scalars(select(NewsSummary).where(NewsSummary.document_id.in_(part))):
                    found[row.document_id] = row
            rows = [found[document_id] for document_id in ids if document_id in found]
        sources = _read_sources(session, None if ids is None else [row.document_id for row in rows])
    result = []
    for row in rows:
        meta = {"title": row.title, **_unflatten(db.as_dict(row), "news_summary")}
        if row.document_id in sources:
            meta["sources"] = sources[row.document_id]
        result.append(ProcessedNews(row.document_id, row.text, row.title, row.first_paragraph, row.audience_text, meta))
    return result


def event_representative(database_path: str | Path, document_id: str) -> str | None:
    """Уникальная новость (представитель события) ``unique.db``, к которой дедупликация отнесла
    ``document_id``: она сама или новость, в источниках которой ``document_id`` записан как дубль."""
    if not db.exists(database_path):
        return None
    ensure_fingerprint_schema(database_path)
    with db.session(database_path) as session:
        if session.get(NewsSummary, document_id) is not None:
            return document_id
        return session.scalar(select(NewsSource.document_id)
                              .where(NewsSource.source_document_id == document_id).limit(1))


def summary_ids(database_path: str | Path, statuses: Iterable[str] | None = None) -> set[str]:
    """``document_id`` новостей ``news_summary`` (все или с заданными статусами); пусто, если базы нет."""
    if not db.exists(database_path):
        return set()
    ensure_summary_schema(database_path)
    return _ids(database_path, NewsSummary, statuses)


def claim_summaries(database_path: str | Path) -> list[ProcessedNews]:
    """Берёт в работу подготовленные новости со статусом ``new``: ставит ``processing`` и возвращает их."""
    ensure_summary_schema(database_path)
    return read_summaries(database_path, sorted(_claim(database_path, NewsSummary)))


def set_summary_status(database_path: str | Path, document_ids: Iterable[str], status: str) -> int:
    """Меняет статус записей ``news_summary``; возвращает число изменённых записей."""
    return _set_status(database_path, NewsSummary, document_ids, status)


def status_counts(database_path: str | Path, table: str) -> dict[str, int]:
    """Сколько записей таблицы в каждом статусе (``news`` или ``news_summary``)."""
    if not db.exists(database_path):
        return {status: 0 for status in STATUSES}
    (ensure_schema if table == "news" else ensure_summary_schema)(database_path)
    model = _MODELS[table]
    with db.session(database_path) as session:
        counts = dict(session.execute(select(model.status, func.count()).group_by(model.status)).all())
    return {status: counts.get(status, 0) for status in STATUSES}


def release_stale(database_path: str | Path, table: str, minutes: int = STALE_MINUTES) -> int:
    """Записи, зависшие в ``processing`` дольше ``minutes``, снова получают ``new``."""
    if not db.exists(database_path):
        return 0
    (ensure_schema if table == "news" else ensure_summary_schema)(database_path)
    model = _MODELS[table]
    border = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    with db.session(database_path) as session:
        return session.execute(
            update(model)
            .where(model.status == PROCESSING, or_(model.status_updated_at.is_(None), model.status_updated_at < border))
            .values(status=NEW, status_updated_at=_now())
            .execution_options(synchronize_session=False)
        ).rowcount


def _claim(database_path: str | Path, model: type[News] | type[NewsSummary],
           document_ids: Iterable[str] | None = None) -> set[str]:
    """Атомарно переводит записи ``new`` (все или только ``document_ids``) в ``processing``; возвращает их ID.

    Блокировка записи (:func:`db.write_lock`) берётся сразу: два процесса не возьмут одну новость.
    """
    with db.session(database_path) as session:
        db.write_lock(session, f"claim:{db.schema_name(database_path)}.{model.__tablename__}")
        condition = model.status == NEW
        if document_ids is not None:
            condition = condition & model.document_id.in_(list(document_ids))
        ids = set(session.scalars(select(model.document_id).where(condition)))
        session.execute(update(model).where(condition).values(status=PROCESSING, status_updated_at=_now())
                        .execution_options(synchronize_session=False))
    return ids


def _set_status(database_path: str | Path, model: type[News] | type[NewsSummary], document_ids: Iterable[str],
                status: str) -> int:
    if status not in STATUSES:
        raise ValueError(f"статус должен быть одним из {STATUSES}")
    now = _now()
    ids = list(document_ids)
    with db.session(database_path) as session:
        return sum(session.execute(update(model).where(model.document_id.in_(part))
                                   .values(status=status, status_updated_at=now)
                                   .execution_options(synchronize_session=False)).rowcount
                   for part in db.chunks(ids))


def delete_summaries(database_path: str | Path, document_ids: Iterable[str]) -> int:
    """Удаляет новости из ``news_summary`` (и их источники) и возвращает число удалённых записей."""
    ids = list(document_ids)
    deleted = _delete(database_path, NewsSummary, ids)
    _delete(database_path, NewsSource, ids)
    return deleted


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
    db.ensure_tables(bind, [NewsFingerprint])
    with db.session(bind) as session:
        session.execute(
            update(NewsSummary).where(NewsSummary.status != READY)
            .values(status=READY, status_updated_at=func.coalesce(NewsSummary.status_updated_at, _now()))
            .execution_options(synchronize_session=False)
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
    with db.session(database_path) as session:
        inserted = _write_summaries(session, insert(NewsSummary), list(items), status=READY)
        session.add_all(NewsFingerprint(**row) for row in _fingerprint_rows(fingerprints))
        return inserted


def upsert_fingerprints(database_path: str | Path, fingerprints: Iterable[StoredFingerprint]) -> None:
    """Записывает отпечатки уже сохранённых новостей (для старых баз без них)."""
    ensure_fingerprint_schema(database_path)
    rows = _fingerprint_rows(fingerprints)
    if rows:
        with db.session(database_path) as session:
            session.execute(db.upsert(NewsFingerprint), rows)


def read_fingerprints(database_path: str | Path) -> list[StoredFingerprint]:
    """Читает отпечатки всех уникальных новостей."""
    ensure_fingerprint_schema(database_path)
    with db.session(database_path) as session:
        return [StoredFingerprint(row.document_id, row.text_hash, row.url_key, row.minhash)
                for row in session.scalars(select(NewsFingerprint))]


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
    with db.session(database_path) as session:
        return session.scalar(select(NewsFingerprint.document_id)
                              .where(getattr(NewsFingerprint, column) == value).limit(1))


def append_sources(database_path: str | Path, sources: dict[str, list[dict[str, Any]]]) -> int:
    """Дописывает в ``news_sources`` уникальных новостей источники их дублей.

    ``sources`` — ``{document_id оригинала: [запись источника, ...]}``.
    Источник, который уже записан у этой новости (повторная обработка пачки
    после сбоя), повторно не добавляется. Возвращает число обновлённых новостей.
    """
    ensure_summary_schema(database_path)
    updated = 0
    with db.session(database_path) as session:
        for document_id, entries in sources.items():
            if session.get(NewsSummary, document_id) is None:
                continue
            start = session.scalar(select(func.coalesce(func.max(NewsSource.position) + 1, 0))
                                   .where(NewsSource.document_id == document_id))
            if _execute_counting(session, db.insert_ignore(NewsSource), _source_rows(document_id, entries, start)):
                updated += 1
    return updated
