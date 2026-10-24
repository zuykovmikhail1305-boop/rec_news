"""Доступ к базам проекта через SQLAlchemy ORM: SQLite-файлы или PostgreSQL.

Данные описаны ORM-моделями (наследники :class:`Base`, колонки — ``Mapped[...]``) рядом с кодом,
который с ними работает (``storage/news_db.py``, ``analytics/store.py`` и т. д.); работа с ними —
через :class:`sqlalchemy.orm.Session` (:func:`session`): ``session.get``, ``session.scalars(select(Модель))``,
``session.add``, ORM-запросы ``insert``/``update``/``delete`` по моделям.

Код работает с «базами» по пути файла (``data/news.db``, ``dataset/ur_lica.db``…). Где они лежат
на самом деле, решает этот модуль:

* ``REC_NEWS_DATABASE_URL`` не задан — каждая база — свой файл SQLite (локальная работа);
* задан (``postgresql+psycopg://user:pass@host:5432/db``) — все базы в одной базе PostgreSQL,
  каждая — своя схема: ``data/news.db`` → ``ml_news``, ``data/new_summary.db`` → ``ml_new_summary``
  (префикс — ``REC_NEWS_DB_SCHEMA_PREFIX``). В разных схемах могут быть таблицы с одним именем,
  как в разных файлах (``news_summary`` в ``new_summary.db`` и ``unique.db`` — одна модель).
  Схема подставляется через ``schema_translate_map`` движка: модели и запросы не меняются.

Функции:

* :func:`session` — сессия ORM для базы в транзакции (фиксируется в конце блока);
* :func:`engine` — движок для базы (один на базу в процессе);
* :func:`ensure_tables` — создаёт схему, таблицы моделей и индексы, если их нет; таблицы ранних
  версий дополняет недостающими колонками (без ключа — пересоздаёт с ключом, сохраняя строки);
* :func:`insert_ignore`, :func:`upsert` — ORM-вставка с пропуском или заменой существующих строк
  (``ON CONFLICT`` — одинаково в SQLite и PostgreSQL);
* :func:`changes` — сколько строк изменили запросы сессии;
* :func:`write_lock` — блокировка записи до конца транзакции (взять новости в работу, создать запуск);
* :func:`named_lock` — блокировка на время блока кода между процессами (API и воркеры Celery);
* :func:`exists` — есть ли база (файл или схема).

SQLite: соединения не держатся открытыми между операциями (``NullPool``) — базы пишут несколько
процессов, а на Windows открытый файл нельзя удалить; ``timeout=30`` — ждать блокировку записи,
а не падать сразу. PostgreSQL: общий пул соединений на все схемы.
"""

from __future__ import annotations

import re
import threading
import zlib
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from sqlalchemy import (
    Connection, Engine, MetaData, Table, create_engine, event, func, inspect, insert, select,
)
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import DeclarativeBase, Session
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateColumn, CreateSchema

from news_pipeline.env import env_int, env_str

DATABASE_URL = env_str("REC_NEWS_DATABASE_URL")
POSTGRES = bool(DATABASE_URL)
SCHEMA_PREFIX = env_str("REC_NEWS_DB_SCHEMA_PREFIX", "ml_")

_ENGINES: dict[tuple[str, bool], Engine] = {}
_ENSURED: set[tuple[str, str]] = set()
_SCHEMAS: set[str] = set()
_LOCK = threading.Lock()
_NAMED_LOCKS: dict[str, threading.Lock] = {}
_POSTGRES_ENGINE: Engine | None = None
# Длинные списки ID делим на части: у SQLite ограничено число параметров запроса.
CHUNK = 500


class Base(DeclarativeBase):
    """Базовый класс ORM-моделей всех баз ML-части. В какой базе (файле или схеме) лежит таблица модели,
    определяет сессия (:func:`session`), а не модель."""


def table_of(model: Any) -> Table:
    """Таблица ORM-модели (или сама таблица)."""
    return model.__table__ if hasattr(model, "__table__") else model


def as_dict(obj: Base) -> dict[str, Any]:
    """Значения колонок объекта модели по именам колонок."""
    return {column.name: getattr(obj, column.key) for column in obj.__table__.columns}


# --- движки и сессии ---------------------------------------------------------------------


def schema_name(path: str | Path) -> str:
    """Схема PostgreSQL для базы: префикс + имя файла без расширения (``data/news.db`` → ``ml_news``)."""
    return SCHEMA_PREFIX + re.sub(r"\W", "_", Path(path).stem.lower())


def _postgres() -> Engine:
    global _POSTGRES_ENGINE
    if _POSTGRES_ENGINE is None:
        _POSTGRES_ENGINE = create_engine(
            DATABASE_URL, pool_pre_ping=True,
            pool_size=env_int("REC_NEWS_DB_POOL_SIZE", 5), max_overflow=env_int("REC_NEWS_DB_MAX_OVERFLOW", 10),
        )

        @event.listens_for(_POSTGRES_ENGINE, "after_cursor_execute")
        def _count_changes(conn, cursor, _statement, _parameters, context, _executemany) -> None:  # noqa: ANN001
            # Аналог total_changes SQLite: сумма строк, изменённых INSERT/UPDATE/DELETE соединения.
            if context is not None and (context.isinsert or context.isupdate or context.isdelete) \
                    and cursor.rowcount and cursor.rowcount > 0:
                conn.info["changes"] = conn.info.get("changes", 0) + cursor.rowcount
    return _POSTGRES_ENGINE


def engine(path: str | Path, *, wal: bool = False) -> Engine:
    """Движок SQLAlchemy для базы ``path``. ``wal`` — для SQLite включить журнал WAL
    (несколько читателей и писатель); в PostgreSQL не нужен."""
    if POSTGRES:
        schema = schema_name(path)
        with _LOCK:
            cached = _ENGINES.get((schema, False))
            if cached is None:
                cached = _postgres().execution_options(schema_translate_map={None: schema})
                _ENGINES[(schema, False)] = cached
            return cached
    resolved = str(Path(path).resolve())
    with _LOCK:
        cached = _ENGINES.get((resolved, wal))
        if cached is not None:
            return cached
        Path(resolved).parent.mkdir(parents=True, exist_ok=True)
        created = create_engine(f"sqlite:///{resolved}", poolclass=NullPool,
                                connect_args={"timeout": 30, "check_same_thread": False})
        if wal:
            @event.listens_for(created, "connect")
            def _journal(dbapi_connection, _record) -> None:  # noqa: ANN001
                dbapi_connection.execute("PRAGMA journal_mode=WAL")
        _ENGINES[(resolved, wal)] = created
        return created


@contextmanager
def session(bind: str | Path | Engine) -> Iterator[Session]:
    """Сессия ORM для базы (путь или движок) в одной транзакции: фиксируется в конце блока,
    при исключении откатывается. Объекты после фиксации не устаревают (``expire_on_commit=False``)."""
    target = bind if isinstance(bind, Engine) else engine(bind)
    with Session(target, expire_on_commit=False) as current, current.begin():
        yield current


def schema_of(bind: Engine | Connection | Session) -> str | None:
    """Схема PostgreSQL базы (``None`` для SQLite) — для рефлексии, которая не знает о подстановке."""
    if isinstance(bind, Session):
        bind = bind.connection()
    mapping = bind.get_execution_options().get("schema_translate_map") or {}
    return mapping.get(None)


def _key(bind: Engine) -> str:
    return schema_of(bind) or bind.url.database or ""


def _alive(bind: Engine) -> bool:
    """База на месте: файл SQLite существует (его могли удалить), схема PostgreSQL создана."""
    return bool(schema_of(bind)) or Path(bind.url.database or "").exists()


def exists(path: str | Path) -> bool:
    """Есть ли база: файл SQLite или схема PostgreSQL (пустая база ещё не создана)."""
    if not POSTGRES:
        return Path(path).exists()
    schema = schema_name(path)
    if schema in _SCHEMAS:
        return True
    with _postgres().connect() as connection:
        found = schema in inspect(connection).get_schema_names()
    if found:
        _SCHEMAS.add(schema)
    return found


def location(path: str | Path) -> str:
    """Где лежит база — для сообщений: имя файла или схема PostgreSQL."""
    return f"схема {schema_name(path)} (PostgreSQL)" if POSTGRES else Path(path).name


# --- таблицы (схема данных) ------------------------------------------------------------------


def ensure_tables(bind: Engine, models: Sequence[Any], *, key: str | None = None) -> None:
    """Создаёт схему (PostgreSQL), таблицы моделей и их индексы; таблицам ранних версий добавляет
    недостающие колонки, а таблицу без первичного ключа модели пересоздаёт с ним (строки сохраняются).

    ``models`` — ORM-модели (или их таблицы). ``key`` — имя набора: в процессе проверка выполняется
    один раз, пока база на месте.
    """
    tables = [table_of(model) for model in models]
    cache_key = (_key(bind), key or ",".join(table.name for table in tables))
    if cache_key in _ENSURED and _alive(bind):
        return
    schema = schema_of(bind)
    with bind.begin() as connection:
        if schema:
            connection.execute(CreateSchema(schema, if_not_exists=True))
            _SCHEMAS.add(schema)
        for table in tables:
            existing = set(columns(connection, table.name))
            if existing and any(column.primary_key and column.name not in existing for column in table.columns):
                _rebuild(connection, table, existing)
                continue
            table.create(connection, checkfirst=True)
            for column in table.columns:
                if existing and column.name not in existing:
                    add_column(connection, table, column.name)
            for index in table.indexes:
                index.create(connection, checkfirst=True)
    _ENSURED.add(cache_key)


def is_ensured(bind: Engine, key: str) -> bool:
    """Набор таблиц ``key`` уже проверен в этом процессе и база на месте."""
    return (_key(bind), key) in _ENSURED and _alive(bind)


def mark_ensured(bind: Engine, key: str) -> None:
    _ENSURED.add((_key(bind), key))


def _qualified(connection: Connection, name: str) -> str:
    schema = schema_of(connection)
    return f'"{schema}"."{name}"' if schema else f'"{name}"'


def add_column(connection: Connection, table: Table, name: str) -> None:
    """``ALTER TABLE … ADD COLUMN``: колонка из модели. ``NOT NULL`` без значения по умолчанию
    к таблице со строками не добавить — такая колонка добавляется допускающей ``NULL``."""
    column = table.c[name]
    ddl = str(CreateColumn(column).compile(dialect=connection.dialect))
    if column.server_default is None:
        ddl = ddl.replace(" NOT NULL", "")
    connection.exec_driver_sql(f"ALTER TABLE {_qualified(connection, table.name)} ADD COLUMN {ddl}")


def _rebuild(connection: Connection, table: Table, existing: set[str]) -> None:
    """Таблица ранней версии без первичного ключа модели (например, без ``id``) → новая таблица модели;
    строки переносятся ``INSERT … SELECT``."""
    schema = schema_of(connection)
    old_name = f"{table.name}_old_format"
    for index in inspect(connection).get_indexes(table.name, schema=schema):
        connection.exec_driver_sql(f"DROP INDEX IF EXISTS {_qualified(connection, index['name'])}")
    connection.exec_driver_sql(f"ALTER TABLE {_qualified(connection, table.name)} RENAME TO \"{old_name}\"")
    table.create(connection)
    old = Table(old_name, MetaData(), schema=schema, autoload_with=connection)
    names = [column.name for column in table.columns if column.name in existing]
    connection.execute(insert(table).from_select(names, select(*(old.c[name] for name in names))))
    old.drop(connection)


def columns(bind: Engine | Connection, table: str) -> list[str]:
    """Колонки таблицы в базе (пусто, если таблицы нет)."""
    schema = schema_of(bind)
    inspector = inspect(bind)
    if not inspector.has_table(table, schema=schema):
        return []
    return [column["name"] for column in inspector.get_columns(table, schema=schema)]


# --- запись -----------------------------------------------------------------------------


def changes(target: Session | Connection) -> int:
    """Сколько строк изменено, вставлено или удалено в соединении сессии (счётчик растёт с открытия;
    нужна разница «после − до»)."""
    connection = target.connection() if isinstance(target, Session) else target
    if POSTGRES:
        return connection.info.get("changes", 0)
    return connection.connection.driver_connection.total_changes


def _insert(model: Any):
    return postgres_insert(model) if POSTGRES else sqlite_insert(model)


def insert_ignore(model: Any):
    """ORM-вставка ``INSERT … ON CONFLICT DO NOTHING`` — существующие строки остаются как есть.
    Выполняется ``session.execute(insert_ignore(Модель), [строки])``."""
    return _insert(model).on_conflict_do_nothing()


def upsert(model: Any, update_columns: Iterable[str] | None = None):
    """ORM-вставка ``INSERT … ON CONFLICT DO UPDATE`` по первичному ключу: существующая строка заменяется.

    ``update_columns`` — какие колонки обновлять (по умолчанию все, кроме первичного ключа).
    """
    table = table_of(model)
    statement = _insert(model)
    keys = [column.name for column in table.primary_key.columns]
    names = list(update_columns) if update_columns is not None else \
        [column.name for column in table.columns if column.name not in keys]
    return statement.on_conflict_do_update(index_elements=keys, set_={name: statement.excluded[name] for name in names})


# --- блокировки -------------------------------------------------------------------------


def _lock_id(name: str) -> int:
    return zlib.crc32(f"{SCHEMA_PREFIX}{name}".encode("utf-8"))


def write_lock(target: Session | Connection, name: str) -> None:
    """Начинает транзакцию с блокировкой записи ``name`` до её конца: два процесса не возьмут одни и те же
    строки. SQLite — ``BEGIN IMMEDIATE`` (блокируется вся база), PostgreSQL — ``pg_advisory_xact_lock``."""
    if POSTGRES:
        target.execute(select(func.pg_advisory_xact_lock(_lock_id(name))))
    else:
        connection = target.connection() if isinstance(target, Session) else target
        connection.exec_driver_sql("BEGIN IMMEDIATE")


@contextmanager
def named_lock(name: str) -> Iterator[None]:
    """Блокировка на время блока: в процессе — потоки, в PostgreSQL — ещё и все процессы
    (``pg_advisory_lock`` на отдельном соединении). Например, обработка новостей: дедупликация
    сравнивает новость с уже обработанными, и два воркера не должны делать её одновременно."""
    with _LOCK:
        local = _NAMED_LOCKS.setdefault(name, threading.Lock())
    with local:
        if not POSTGRES:
            yield
            return
        with _postgres().connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(select(func.pg_advisory_lock(_lock_id(name))))
            try:
                yield
            finally:
                connection.execute(select(func.pg_advisory_unlock(_lock_id(name))))


# --- прочее -----------------------------------------------------------------------------


def vacuum(bind: Engine) -> None:
    """Сжать файл SQLite после пересоздания таблиц; PostgreSQL делает это сам (autovacuum)."""
    if POSTGRES:
        return
    with bind.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.exec_driver_sql("VACUUM")


def chunks(items: Sequence[Any], size: int = CHUNK) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]
