"""Датасет хакатона в базе (SQLite или схемы PostgreSQL, см. ``storage/db.py``): компании, платежи, физлица.

ORM-модели (колонки — как в CSV хакатона, плюс суррогатный ключ ``id``):

    dataset/ur_lica.db   :class:`Company`     (companies)    — ``ur_lica.csv`` (индексы по SYNTHETIC_U, GROUP_ID)
    dataset/payment.db   :class:`Payment`     (payments)     — ``payment.csv`` (индексы по BENEFICIARY, PAYER)
    dataset/fiz_lica.db  :class:`Individual`  (individuals)  — ``fiz_lica.csv`` (индексы по SYNTHETIC_F, SYNTHETIC_U)

Перенос из CSV — :func:`import_csv` (``scripts/import_dataset.py``), ORM-вставкой пачками. Код, работавший
с CSV, получает из :func:`read_table` такой же ``DataFrame`` (колонки CSV, без ``id``) — из результата
ORM-запроса ``select`` по атрибутам модели в сессии.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Float, Index, Integer, Text, delete, insert, select
from sqlalchemy.orm import Mapped, mapped_column

from news_pipeline.config import DATASET_DIR
from news_pipeline.storage import db


class Company(db.Base):
    __tablename__ = "companies"
    __table_args__ = (Index("idx_companies_synthetic_u", "SYNTHETIC_U"), Index("idx_companies_group_id", "GROUP_ID"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    SYNTHETIC_U: Mapped[str | None] = mapped_column(Text)
    ORG_TYPE: Mapped[str | None] = mapped_column(Text)
    OKFS_NAME: Mapped[str | None] = mapped_column(Text)
    COMPANY_AGE_YEARS_GRADATION: Mapped[str | None] = mapped_column(Text)
    ECO_MARK_LABEL: Mapped[str | None] = mapped_column(Text)
    MAINOKVED_CODE: Mapped[str | None] = mapped_column(Text)
    MAINOKVED_NAME: Mapped[str | None] = mapped_column(Text)
    NEGATIVEMARK: Mapped[float | None] = mapped_column(Float)
    AMOUNT_WORKERS_FNS_GRADATION: Mapped[str | None] = mapped_column(Text)
    CL_TYPE: Mapped[str | None] = mapped_column(Text)
    CL_PRODUCT_RKO_MARK: Mapped[float | None] = mapped_column(Float)
    CL_PRODUCT_GAR_MARK: Mapped[float | None] = mapped_column(Float)
    CL_PRODUCT_LOA_MARK: Mapped[float | None] = mapped_column(Float)
    CL_PRODUCT_LEA_MARK: Mapped[float | None] = mapped_column(Float)
    CL_PRODUCT_DEP_MARK: Mapped[float | None] = mapped_column(Float)
    CL_PRODUCT_ZPP_MARK: Mapped[float | None] = mapped_column(Float)
    FTS_FLAG: Mapped[float | None] = mapped_column(Float)
    FTS_COUNTRY_NAME: Mapped[str | None] = mapped_column(Text)
    FACTORING_MARK: Mapped[float | None] = mapped_column(Float)
    REVENUE_COMPANY: Mapped[float | None] = mapped_column(Float)
    GROUP_ID: Mapped[str | None] = mapped_column(Text)
    CITY: Mapped[str | None] = mapped_column(Text)
    REGION: Mapped[str | None] = mapped_column(Text)


class Payment(db.Base):
    __tablename__ = "payments"
    __table_args__ = (Index("idx_payments_beneficiary", "BENEFICIARY"), Index("idx_payments_payer", "PAYER"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    BENEFICIARY: Mapped[str | None] = mapped_column(Text)
    PAYER: Mapped[str | None] = mapped_column(Text)
    PAY_AMT: Mapped[float | None] = mapped_column(Float)
    PAY_CNT: Mapped[int | None] = mapped_column(Integer)


class Individual(db.Base):
    __tablename__ = "individuals"
    __table_args__ = (Index("idx_individuals_synthetic_f", "SYNTHETIC_F"),
                      Index("idx_individuals_synthetic_u", "SYNTHETIC_U"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    SYNTHETIC_F: Mapped[str | None] = mapped_column(Text)
    CITY_NAME: Mapped[str | None] = mapped_column(Text)
    RC_NAME: Mapped[str | None] = mapped_column(Text)
    SYNTHETIC_U: Mapped[str | None] = mapped_column(Text)


@dataclass(frozen=True)
class DatasetTable:
    csv: str
    database: str
    model: type[db.Base]

    @property
    def table(self) -> str:
        return self.model.__tablename__

    @property
    def columns(self) -> list[str]:
        """Колонки CSV (все колонки модели, кроме суррогатного ключа ``id``)."""
        return [column.name for column in self.model.__table__.columns if column.name != "id"]

    @property
    def csv_path(self) -> Path:
        return DATASET_DIR / self.csv

    @property
    def path(self) -> Path:
        return DATASET_DIR / self.database


COMPANIES = DatasetTable("ur_lica.csv", "ur_lica.db", Company)
PAYMENTS = DatasetTable("payment.csv", "payment.db", Payment)
INDIVIDUALS = DatasetTable("fiz_lica.csv", "fiz_lica.db", Individual)
TABLES = (COMPANIES, PAYMENTS, INDIVIDUALS)
BATCH = 20_000


def read_csv(path: Path) -> Any:
    """Читает CSV датасета, определяя разделитель (``,`` или ``;``) по заголовку."""
    import pandas as pd

    with path.open(encoding="utf-8") as file:
        header = file.readline()
    return pd.read_csv(path, sep=";" if header.count(";") > header.count(",") else ",", low_memory=False)


def _clean(value: Any) -> Any:
    """Значение pandas → значение колонки: NaN — NULL, числа numpy — числа Python."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return value.item() if hasattr(value, "item") else value


def import_csv(spec: DatasetTable, csv_path: Path | None = None) -> int:
    """Переносит CSV в базу (строки таблицы заменяются); возвращает число строк."""
    frame = read_csv(csv_path or spec.csv_path)
    names = [name for name in spec.columns if name in frame.columns]
    bind = db.engine(spec.path)
    db.ensure_tables(bind, [spec.model])
    rows = frame[names].itertuples(index=False, name=None)
    with db.session(bind) as session:
        session.execute(delete(spec.model))
        batch: list[dict[str, Any]] = []
        for row in rows:
            batch.append({name: _clean(value) for name, value in zip(names, row)})
            if len(batch) >= BATCH:
                session.execute(insert(spec.model), batch)
                batch = []
        if batch:
            session.execute(insert(spec.model), batch)
    db.vacuum(bind)
    return len(frame)


def read_table(spec: DatasetTable) -> Any:
    """Таблица датасета целиком как ``DataFrame`` (колонки CSV)."""
    import pandas as pd

    if not exists(spec):
        raise FileNotFoundError(f"{db.location(spec.path)}: нет таблицы {spec.table} — перенесите CSV: "
                                "python scripts/import_dataset.py")
    bind = db.engine(spec.path)
    db.ensure_tables(bind, [spec.model])
    query = select(*(getattr(spec.model, name) for name in spec.columns)).order_by(spec.model.id)
    with db.session(bind) as session:
        rows = session.execute(query.execution_options(yield_per=50_000)).all()
    # Как pandas.read_sql: числа с пропусками — float с NaN
    return pd.DataFrame.from_records(rows, columns=spec.columns, coerce_float=True)


def exists(spec: DatasetTable) -> bool:
    """Таблица датасета перенесена в базу."""
    return db.exists(spec.path) and bool(db.columns(db.engine(spec.path), spec.table))
