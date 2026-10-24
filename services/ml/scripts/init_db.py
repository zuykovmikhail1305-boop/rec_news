"""Инициализация баз ML-части: создать всё, что нужно для работы, и проверить окружение.

    python scripts/init_db.py                       # создать базы, таблицы, коллекции Qdrant, аналитику
    python scripts/init_db.py --check               # только проверить, ничего не создавая
    python scripts/init_db.py --data-dir D:/ml_data # другой каталог баз (как REC_NEWS_DATA_DIR)
    python scripts/init_db.py --import-dataset      # заодно перенести CSV датасета в dataset/*.db

Что делает:

1. SQLite (``data/``): создаёт все базы с таблицами и индексами — ``news.db``, ``new_summary.db``,
   ``unique.db``, ``analytics.db``, ``profiles.db``, ``recommendations.db``, ``insight_cards.db``,
   ``jobs.db``, ``chat.db``, ``feedback.db``, ``llm_cache.db``. Базы ранних версий дополняются
   недостающими колонками, базы старого формата переводятся в новый.
2. Qdrant: коллекции ``news``, ``analytics_chunks``, ``news_audience`` (локальная база ``data/qdrant``
   или сервер ``REC_NEWS_QDRANT_URL``) и индексация аналитики из ``fixtures/analytics``, если её ещё
   нет, — тем же кодом, что сервис при старте (``MLService``). Нужна модель эмбеддингов.
3. Проверяет окружение: датасет ``dataset/*.db``, модель в ``models/``, ``.env`` и подключение к LLM,
   демо-время ``REC_NEWS_DEMO_NOW``; печатает, сколько новостей, профилей и материалов аналитики есть.

Повторный запуск безопасен: существующие данные не удаляются и не перезаписываются. Синтетические
новости и профили не загружаются (для демо — ``scripts/bootstrap_demo.py``). Пока запущен API, локальную
базу Qdrant открыть нельзя: остановите API или используйте ``--skip-vectors``.

Код возврата: 0 — всё готово; 1 — чего-то не хватает (подробности в отчёте).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="только проверить, ничего не создавая")
    parser.add_argument("--data-dir", type=Path, help="каталог баз вместо data/ (как REC_NEWS_DATA_DIR)")
    parser.add_argument("--skip-vectors", action="store_true",
                        help="не трогать Qdrant и аналитику (например, пока запущен API)")
    parser.add_argument("--import-dataset", action="store_true", help="перенести CSV датасета в dataset/*.db")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.data_dir:  # до импорта news_pipeline: каталог баз читается при импорте config
        os.environ["REC_NEWS_DATA_DIR"] = str(args.data_dir.resolve())
    if sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    from sqlalchemy import func, inspect, select

    from news_pipeline.agent.store import CHAT_DB, CHAT_FEEDBACK, FEEDS, MESSAGES, SESSIONS, ChatStore
    from news_pipeline.analytics.store import (
        ANALYTICS_DB, CHUNKS, EVENT_ANALYTICS, EVENT_RUNS, MATERIALS, AnalyticsStore,
    )
    from news_pipeline.config import (
        DATA_DIR, DEMO_NOW, EMBEDDING_MODEL, LLM_API_KEY, LLM_BASE_URL, MODELS_DIR, NEWS_DB, QDRANT_PATH,
        QDRANT_URL, SUMMARY_DB, UNIQUE_DB,
    )
    from news_pipeline.env import ENV_FILE
    from news_pipeline.insights.store import INSIGHT_CARDS, INSIGHT_CARDS_DB, InsightCardStore
    from news_pipeline.llm.client import CACHE_PATH, LLM, LLM_CACHE
    from news_pipeline.profile.store import PROFILES, PROFILES_DB, ProfileStore
    from news_pipeline.ranking.user_news import PIPELINE_RUNS, RECOMMENDATIONS_DB, USER_NEWS, UserNewsStore
    from news_pipeline.storage import db
    from news_pipeline.storage.dataset_db import TABLES as DATASET_TABLES
    from news_pipeline.storage.dataset_db import import_csv
    from news_pipeline.storage.feedback_db import FEEDBACK, FEEDBACK_DB, FeedbackStore
    from news_pipeline.storage.news_db import (
        NEWS, NEWS_FINGERPRINT, NEWS_SOURCES, NEWS_SUMMARY, ensure_fingerprint_schema, ensure_schema,
        ensure_summary_schema, status_counts,
    )
    from news_pipeline.tasks.store import ITEMS, JOBS, JOBS_DB, JobStore

    # база → (таблицы, как создать)
    databases = [
        (NEWS_DB, [NEWS], lambda: ensure_schema(NEWS_DB)),
        (SUMMARY_DB, [NEWS_SUMMARY, NEWS_SOURCES], lambda: ensure_summary_schema(SUMMARY_DB)),
        (UNIQUE_DB, [NEWS_SUMMARY, NEWS_SOURCES, NEWS_FINGERPRINT], lambda: ensure_fingerprint_schema(UNIQUE_DB)),
        (ANALYTICS_DB, [MATERIALS, CHUNKS, EVENT_ANALYTICS, EVENT_RUNS], AnalyticsStore),
        (PROFILES_DB, [PROFILES], ProfileStore),
        (RECOMMENDATIONS_DB, [USER_NEWS, PIPELINE_RUNS], UserNewsStore),
        (INSIGHT_CARDS_DB, [INSIGHT_CARDS], InsightCardStore),
        (JOBS_DB, [JOBS, ITEMS], JobStore),
        (CHAT_DB, [SESSIONS, MESSAGES, FEEDS, CHAT_FEEDBACK], ChatStore),
        (FEEDBACK_DB, [FEEDBACK], FeedbackStore),
        (CACHE_PATH, [LLM_CACHE], lambda: LLM(cache_path=CACHE_PATH)),
    ]
    problems: list[str] = []
    notes: list[str] = []

    print(f"Каталог баз: {DATA_DIR}" + (" (только проверка)" if args.check else ""))

    # --- 1. SQLite ----------------------------------------------------------------------
    print("\n1. Базы SQLite")
    for path, tables, create in databases:
        if not args.check:
            create()
        if not path.exists():
            print(f"   ✗ {path.name}: нет файла")
            problems.append(f"{path.name}: нет базы — запустите без --check")
            continue
        bind = db.engine(path)
        existing = set(inspect(bind).get_table_names())
        missing = [table.name for table in tables if table.name not in existing]
        with bind.connect() as connection:
            counts = {table.name: connection.scalar(select(func.count()).select_from(table))
                      for table in tables if table.name in existing}
        summary = ", ".join(f"{name} {count}" for name, count in counts.items())
        if missing:
            print(f"   ✗ {path.name}: нет таблиц {', '.join(missing)}")
            problems.append(f"{path.name}: нет таблиц {', '.join(missing)}")
        else:
            print(f"   ✓ {path.name}: {summary}")

    # --- 2. Qdrant и аналитика ----------------------------------------------------------
    print("\n2. Векторы (Qdrant) и аналитика")
    model_dir = MODELS_DIR / EMBEDDING_MODEL.replace("/", "--")
    if args.skip_vectors:
        print("   – пропущено (--skip-vectors)")
    elif args.check and not QDRANT_URL and not any(QDRANT_PATH.glob("*")):
        print(f"   ✗ локальной базы Qdrant нет: {QDRANT_PATH}")  # не открываем: клиент создал бы файлы
        problems.append("Qdrant: нет коллекций — запустите без --check")
    elif args.check:
        location = QDRANT_URL or QDRANT_PATH
        try:
            from qdrant_client import QdrantClient

            client = QdrantClient(url=QDRANT_URL) if QDRANT_URL else QdrantClient(path=str(QDRANT_PATH))
            names = {item.name for item in client.get_collections().collections}
            client.close()
            for name in ("news", "analytics_chunks", "news_audience"):
                print(f"   {'✓' if name in names else '✗'} коллекция {name}")
                if name not in names:
                    problems.append(f"Qdrant: нет коллекции {name} — запустите без --check")
        except Exception as error:  # noqa: BLE001 - отчёт, а не падение
            print(f"   ✗ Qdrant ({location}) не открылся: {error}")
            problems.append("Qdrant не открылся — локальную базу держит запущенный API? Остановите его")
    else:
        if not model_dir.exists():
            print(f"   … модели {EMBEDDING_MODEL} нет в {MODELS_DIR}: она скачается с HuggingFace (нужен интернет)")
        try:
            from news_pipeline.service import MLService

            service = MLService(use_llm=False)  # тот же код, что при старте API: коллекции и аналитика
            try:
                collections = {item.name for item in service.dedup.index.client.get_collections().collections}
                print(f"   ✓ коллекции Qdrant: {', '.join(sorted(collections))} ({QDRANT_URL or QDRANT_PATH})")
                print(f"   ✓ векторов новостей: {service.dedup.index.count()}")
                materials = service.analytics.store.material_count()
                indexed = " — проиндексированы сейчас из fixtures/analytics" if service.analytics_bootstrapped else ""
                print(f"   {'✓' if materials else '✗'} материалов аналитики: {materials}{indexed}")
                if not materials:
                    problems.append("аналитики нет: в fixtures/analytics нет материалов")
            finally:
                service.close()
        except Exception as error:  # noqa: BLE001
            print(f"   ✗ не удалось открыть Qdrant или модель: {error}")
            problems.append("Qdrant или модель недоступны — запущен API? Остановите его или --skip-vectors")

    # --- 3. Окружение --------------------------------------------------------------------
    print("\n3. Окружение")
    for spec in DATASET_TABLES:
        if args.import_dataset and not args.check and spec.csv_path.exists():
            print(f"   … {spec.csv} → {spec.database}: {import_csv(spec)} строк")
        if spec.path.exists():
            with db.engine(spec.path).connect() as connection:
                tables = inspect(connection).get_table_names()
                rows = connection.exec_driver_sql(f'SELECT COUNT(*) FROM "{spec.table}"').scalar() \
                    if spec.table in tables else None
            print(f"   {'✓' if rows is not None else '✗'} датасет {spec.database}: {rows if rows is not None else 'нет таблицы ' + spec.table}")
        else:
            hint = " (есть CSV — запустите с --import-dataset)" if spec.csv_path.exists() else ""
            print(f"   – датасет {spec.database}: нет{hint}. Профили строятся без данных компании из датасета")
            notes.append(f"нет {spec.database}{hint}")
    print(f"   {'✓' if model_dir.exists() else '–'} модель эмбеддингов: "
          + (str(model_dir) if model_dir.exists() else f"нет в {MODELS_DIR}, скачается с HuggingFace"))
    print(f"   {'✓' if ENV_FILE.exists() else '✗'} .env: {ENV_FILE if ENV_FILE.exists() else 'нет — скопируйте .env.example'}")
    if not ENV_FILE.exists():
        problems.append(".env нет — скопируйте .env.example и укажите подключение к LLM")
    print(f"   {'✓' if LLM_API_KEY else '–'} LLM: {LLM_BASE_URL}" + ("" if LLM_API_KEY else " (ключ не задан)"))
    if DEMO_NOW:
        print(f"   ! REC_NEWS_DEMO_NOW = {DEMO_NOW:%Y-%m-%d %H:%M} — «текущий момент» синтетики; "
              "для реальных новостей уберите его из .env")
        notes.append("задано демо-время REC_NEWS_DEMO_NOW")

    # --- данные -------------------------------------------------------------------------------
    print("\n4. Данные")
    if NEWS_DB.exists():
        print(f"   новостей: {status_counts(NEWS_DB, 'news')}")
    if PROFILES_DB.exists():
        with db.engine(PROFILES_DB).connect() as connection:
            profiles = connection.scalar(select(func.count()).select_from(PROFILES))
        print(f"   профилей: {profiles}" + ("" if profiles else " — ленты, карточки и чат будут отвечать 404"))

    print("\nИтог: " + ("всё готово" if not problems else "есть проблемы"))
    for problem in problems:
        print(f"   ✗ {problem}")
    for note in notes:
        print(f"   ! {note}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
