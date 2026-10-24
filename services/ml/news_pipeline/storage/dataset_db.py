"""Датасет хакатона в SQLite: компании, платежи, физлица.

    dataset/ur_lica.db   таблица companies    — ``ur_lica.csv`` (индекс по SYNTHETIC_U)
    dataset/payment.db   таблица payments     — ``payment.csv`` (индексы по BENEFICIARY, PAYER)
    dataset/fiz_lica.db  таблица individuals  — ``fiz_lica.csv`` (индексы по SYNTHETIC_F, SYNTHETIC_U)

Перенос из CSV — :func:`import_csv` (``scripts/import_dataset.py``). Колонки и
типы те же, что даёт ``pandas.read_csv``: код, работавший с CSV, получает из
:func:`read_table` такой же ``DataFrame``. Подключение — движок SQLAlchemy
(``storage/db.py``), через него же работают ``pandas.to_sql`` и ``read_sql``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Index, MetaData, Table, select

from news_pipeline.config import DATASET_DIR
from news_pipeline.storage import db


@dataclass(frozen=True)
class DatasetTable:
    csv: str
    database: str
    table: str
    indexes: tuple[str, ...]

    @property
    def csv_path(self) -> Path:
        return DATASET_DIR / self.csv

    @property
    def path(self) -> Path:
        return DATASET_DIR / self.database


COMPANIES = DatasetTable("ur_lica.csv", "ur_lica.db", "companies", ("SYNTHETIC_U", "GROUP_ID"))
PAYMENTS = DatasetTable("payment.csv", "payment.db", "payments", ("BENEFICIARY", "PAYER"))
INDIVIDUALS = DatasetTable("fiz_lica.csv", "fiz_lica.db", "individuals", ("SYNTHETIC_F", "SYNTHETIC_U"))
TABLES = (COMPANIES, PAYMENTS, INDIVIDUALS)


def read_csv(path: Path) -> Any:
    """Читает CSV датасета, определяя разделитель (``,`` или ``;``) по заголовку."""
    import pandas as pd

    with path.open(encoding="utf-8") as file:
        header = file.readline()
    return pd.read_csv(path, sep=";" if header.count(";") > header.count(",") else ",", low_memory=False)


def import_csv(spec: DatasetTable, csv_path: Path | None = None) -> int:
    """Переносит CSV в SQLite (таблица пересоздаётся); возвращает число строк."""
    frame = read_csv(csv_path or spec.csv_path)
    spec.path.parent.mkdir(parents=True, exist_ok=True)
    bind = db.engine(spec.path)
    with bind.begin() as connection:
        frame.to_sql(spec.table, connection, if_exists="replace", index=False, chunksize=50_000)
        table = Table(spec.table, MetaData(), autoload_with=connection)
        for column in spec.indexes:
            Index(f"idx_{spec.table}_{column.lower()}", table.c[column]).create(connection, checkfirst=True)
    with bind.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.exec_driver_sql("VACUUM")
    return len(frame)


def read_table(spec: DatasetTable) -> Any:
    """Таблица датасета целиком как ``DataFrame``."""
    import pandas as pd

    if not spec.path.exists():
        raise FileNotFoundError(f"{spec.path} нет — перенесите CSV: python scripts/import_dataset.py")
    bind = db.engine(spec.path)
    with bind.connect() as connection:
        table = Table(spec.table, MetaData(), autoload_with=connection)
        return pd.read_sql_query(select(table), connection)


def exists(spec: DatasetTable) -> bool:
    return spec.path.exists()
