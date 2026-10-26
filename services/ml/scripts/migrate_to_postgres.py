"""Перенос данных ML-части из SQLite в PostgreSQL (и векторов из локального Qdrant в Qdrant-сервер).

    python scripts/migrate_to_postgres.py                 # data/*.db и dataset/*.db → PostgreSQL
    python scripts/migrate_to_postgres.py --qdrant        # и векторы data/qdrant → REC_NEWS_QDRANT_URL
                                                          # (только векторы — scripts/qdrant_to_server.py)
    python scripts/migrate_to_postgres.py --replace       # перед копированием очистить таблицы PostgreSQL
    python scripts/migrate_to_postgres.py --skip-dataset  # без датасета (платежи — 1,4 млн строк)
    python scripts/migrate_to_postgres.py --tables profiles analytics_materials analytics_chunks
                                                          # только эти таблицы, остальные — пустые

Куда — ``REC_NEWS_DATABASE_URL`` (и ``REC_NEWS_DB_SCHEMA_PREFIX``) из ``.env`` или окружения: каждая база
становится схемой (``data/news.db`` → ``ml_news``…, см. ``news_pipeline/storage/db.py``). Откуда — файлы
SQLite в ``--from-data`` (по умолчанию ``data/``) и ``--from-dataset`` (``dataset/``).

Всё через ORM: таблицы создаются тем же кодом, что у сервиса (``storage/registry.py``); строки читаются
теми же моделями из сессии SQLite и записываются ORM-вставкой в сессии PostgreSQL. Строки с естественным
ключом копируются без замены: уже существующие пропускаются, поэтому повторный запуск безопасен.
Таблицы с суррогатным ключом ``id`` (реакции, оценки ответов чата, датасет) копируются, только если в
PostgreSQL они пусты (или с ``--replace``). Сервис на время переноса лучше остановить.

Код возврата: 0 — перенесено; 1 — ошибка; 2 — не задан ``REC_NEWS_DATABASE_URL``.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BATCH = 5_000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--from-data", type=Path, default=ROOT / "data", help="каталог баз SQLite (data/)")
    parser.add_argument("--from-dataset", type=Path, default=ROOT / "dataset", help="каталог датасета (dataset/)")
    parser.add_argument("--replace", action="store_true", help="очистить таблицы PostgreSQL перед копированием")
    parser.add_argument("--skip-dataset", action="store_true", help="не переносить датасет")
    parser.add_argument("--qdrant", action="store_true", help="перенести векторы из data/qdrant в Qdrant-сервер")
    parser.add_argument("--tables", nargs="+", metavar="ТАБЛИЦА",
                        help="переносить строки только этих таблиц (profiles, analytics_chunks…); остальные таблицы "
                             "создаются пустыми. Датасет — отдельно (--skip-dataset)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    from sqlalchemy import create_engine, delete, func, insert, inspect, select
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import NullPool

    from news_pipeline.storage import db
    from news_pipeline.storage.dataset_db import TABLES as DATASET_TABLES
    from news_pipeline.storage.registry import databases

    if not db.POSTGRES:
        print("Не задан REC_NEWS_DATABASE_URL — куда переносить? Пример: "
              "postgresql+psycopg://user:password@localhost:5432/rec_news")
        return 2

    def clean(value):
        # PostgreSQL не хранит символ \x00 в тексте
        return value.replace("\x00", "") if isinstance(value, str) else value

    def surrogate(model) -> bool:  # noqa: ANN001
        """Ключ — порядковый id: строки не сравнить с уже перенесёнными, копируем только в пустую таблицу."""
        keys = list(model.__table__.primary_key.columns)
        return len(keys) == 1 and keys[0].name == "id"

    def copy_model(source_engine, model, target_bind) -> str:  # noqa: ANN001
        """Строки модели из SQLite (сессия источника) → PostgreSQL (ORM-вставка в сессии цели)."""
        present = {column["name"] for column in inspect(source_engine).get_columns(model.__tablename__)}
        names = [column.key for column in model.__table__.columns if column.name in present
                 and not (surrogate(model) and column.name == "id")]
        with db.session(target_bind) as target:
            if args.replace:
                target.execute(delete(model))
            elif surrogate(model) and target.scalar(select(func.count()).select_from(model)):
                return "уже перенесено — пропущено (--replace)"
            statement = insert(model) if surrogate(model) else db.insert_ignore(model)
            before = db.changes(target)
            with Session(source_engine) as source:
                result = source.execute(select(*(getattr(model, name) for name in names))
                                        .execution_options(yield_per=BATCH))
                for part in result.partitions(BATCH):
                    target.execute(statement, [{name: clean(value) for name, value in zip(names, row)} for row in part])
            return str(db.changes(target) - before)

    started = time.monotonic()
    print(f"Куда: {db.DATABASE_URL.split('@')[-1]} (схемы {db.SCHEMA_PREFIX}*)\n\n1. Базы из {args.from_data}")
    for database in databases():
        source_path = args.from_data / database.path.name
        database.create()
        bind = db.engine(database.path)
        if not source_path.exists():
            print(f"   – {source_path.name}: нет файла, только таблицы")
            continue
        source = create_engine(f"sqlite:///{source_path.resolve()}", poolclass=NullPool)
        present = set(inspect(source).get_table_names())
        report = [f"{model.__tablename__} {copy_model(source, model, bind)}" for model in database.models
                  if model.__tablename__ in present and (not args.tables or model.__tablename__ in args.tables)]
        source.dispose()
        print(f"   ✓ {source_path.name} → {db.schema_name(database.path)}: " + (", ".join(report) or "пусто"))

    if not args.skip_dataset:
        print(f"\n2. Датасет из {args.from_dataset}")
        for spec in DATASET_TABLES:
            source_path = args.from_dataset / spec.database
            if not source_path.exists():
                print(f"   – {spec.database}: нет файла")
                continue
            source = create_engine(f"sqlite:///{source_path.resolve()}", poolclass=NullPool)
            if spec.table not in inspect(source).get_table_names():
                print(f"   – {spec.database}: нет таблицы {spec.table}")
                continue
            bind = db.engine(spec.path)
            db.ensure_tables(bind, [spec.model])
            print(f"   ✓ {spec.database} → {db.schema_name(spec.path)}.{spec.table}: "
                  f"{copy_model(source, spec.model, bind)}", flush=True)
            source.dispose()

    if args.qdrant:
        print("\n3. Векторы Qdrant")
        from qdrant_to_server import copy_collections

        if not copy_collections(args.from_data / "qdrant" / "storage"):
            return 1
    print(f"\nГотово за {time.monotonic() - started:.0f} с. Проверка: python scripts/init_db.py --check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
