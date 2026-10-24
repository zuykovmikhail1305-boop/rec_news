"""Доступ к SQLite-базам проекта через SQLAlchemy (Core).

Каждая база — файл в ``data/`` или ``dataset/``; таблицы описаны объектами
:class:`sqlalchemy.Table` рядом с кодом, который с ними работает
(``storage/news_db.py``, ``analytics/store.py`` и т. д.).

* :func:`engine` — движок для файла базы (один на файл в процессе);
* :func:`ensure_tables` — создаёт таблицы и индексы, если их нет, и
  дополняет таблицы ранних версий недостающими колонками;
* :func:`insert_ignore`, :func:`upsert` — вставка с пропуском или заменой
  существующих строк (``ON CONFLICT`` SQLite);
* :func:`changes` — сколько строк изменили запросы соединения.

Соединения не держатся открытыми между операциями (``NullPool``): базы
пишут несколько процессов (API, скрипты), а на Windows открытый файл нельзя
удалить. ``timeout=30`` — ждать блокировку записи, а не падать сразу.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Iterable, Sequence

from sqlalchemy import Connection, Engine, Table, create_engine, event, inspect
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateColumn

_ENGINES: dict[tuple[str, bool], Engine] = {}
_ENSURED: set[tuple[str, str]] = set()
_LOCK = threading.Lock()
# SQLite ограничивает число параметров запроса — длинные списки ID делим на части.
CHUNK = 500


def engine(path: str | Path, *, wal: bool = False) -> Engine:
    """Движок SQLAlchemy для файла базы. ``wal`` — включить журнал WAL (несколько читателей и писатель)."""
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


def ensure_tables(bind: Engine, tables: Sequence[Table], *, key: str | None = None) -> None:
    """Создаёт таблицы и их индексы; таблицам ранних версий добавляет недостающие колонки.

    ``key`` — имя набора таблиц: в процессе проверка выполняется один раз, пока файл базы существует.
    """
    path = bind.url.database or ""
    cache_key = (path, key or ",".join(table.name for table in tables))
    if cache_key in _ENSURED and Path(path).exists():
        return
    with bind.begin() as connection:
        for table in tables:
            table.create(connection, checkfirst=True)
            existing = {column["name"] for column in inspect(connection).get_columns(table.name)}
            for column in table.columns:
                if column.name not in existing and not column.primary_key:
                    add_column(connection, table, column.name)
            for index in table.indexes:
                index.create(connection, checkfirst=True)
    _ENSURED.add(cache_key)


def is_ensured(bind: Engine, key: str) -> bool:
    """Набор таблиц ``key`` уже проверен в этом процессе и файл базы на месте."""
    path = bind.url.database or ""
    return (path, key) in _ENSURED and Path(path).exists()


def mark_ensured(bind: Engine, key: str) -> None:
    _ENSURED.add((bind.url.database or "", key))


def forget(bind: Engine) -> None:
    """Сбрасывает отметку «таблицы созданы» для базы (после пересоздания файла или переноса формата)."""
    path = bind.url.database or ""
    _ENSURED.difference_update({key for key in _ENSURED if key[0] == path})


def add_column(connection: Connection, table: Table, name: str) -> None:
    """``ALTER TABLE … ADD COLUMN``: колонка из описания таблицы. SQLite не умеет добавлять
    ``NOT NULL`` без значения по умолчанию — такая колонка добавляется допускающей ``NULL``."""
    column = table.c[name]
    ddl = str(CreateColumn(column).compile(dialect=connection.dialect))
    if column.server_default is None:
        ddl = ddl.replace(" NOT NULL", "")
    connection.exec_driver_sql(f"ALTER TABLE {table.name} ADD COLUMN {ddl}")


def columns(bind: Engine | Connection, table: str) -> list[str]:
    """Колонки таблицы в базе (пусто, если таблицы нет)."""
    inspector = inspect(bind)
    return [column["name"] for column in inspector.get_columns(table)] if inspector.has_table(table) else []


def changes(connection: Connection) -> int:
    """Сколько строк изменено, вставлено или удалено в этом соединении с момента открытия."""
    return connection.connection.driver_connection.total_changes


def insert_ignore(table: Table):
    """``INSERT … ON CONFLICT DO NOTHING`` — существующие строки остаются как есть."""
    return sqlite_insert(table).on_conflict_do_nothing()


def upsert(table: Table, update_columns: Iterable[str] | None = None):
    """``INSERT … ON CONFLICT DO UPDATE`` по первичному ключу: существующая строка заменяется.

    ``update_columns`` — какие колонки обновлять (по умолчанию все, кроме первичного ключа).
    """
    statement = sqlite_insert(table)
    keys = [column.name for column in table.primary_key.columns]
    names = list(update_columns) if update_columns is not None else \
        [column.name for column in table.columns if column.name not in keys]
    return statement.on_conflict_do_update(index_elements=keys, set_={name: statement.excluded[name] for name in names})


def chunks(items: Sequence[Any], size: int = CHUNK) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def row_dict(row: Any) -> dict[str, Any]:
    return dict(row._mapping)
