"""Данные для демо в текущей базе: профили компаний демо-входа и аналитика банка из PDF.

    python scripts/setup_demo_data.py                  # профили + аналитика из texts/
    python scripts/setup_demo_data.py --build-demo     # и сразу 9 профилей «роль + компания» (вход без ожидания)
    python scripts/setup_demo_data.py --reindex        # переиндексировать всю аналитику заново
    python scripts/setup_demo_data.py --skip-profiles  # только аналитика

База — из ``.env`` (``REC_NEWS_DATABASE_URL`` — PostgreSQL, иначе SQLite в ``data/``), векторы — Qdrant
(``REC_NEWS_QDRANT_URL`` или локальная база). Что делает:

1. Профили. Компании демо-входа берутся из готовых профилей (``services/backend/backend/demo.py``,
   ``COMPANIES``: ``u_01``, ``u_02``, ``u_07``); если их нет в базе, они копируются из SQLite
   ``--profiles-from`` (по умолчанию ``data/profiles.db``). С ``--build-demo`` строятся и профили
   «роль + компания» для всех пар ``USERS`` × ``COMPANIES`` (иначе — при первом входе).
2. PDF аналитики из ``texts/`` → текст ``fixtures/analytics/<имя>.txt`` (текстовый слой PDF или OCR,
   ``scripts/ocr_analytics.py``); уже распознанные файлы не пересчитываются. Дата и название материала —
   ``fixtures/analytics/materials.json``: без даты материал пропускается (иначе возможна утечка будущего).
3. Индексация: материалы → база аналитики (материалы, фрагменты) и Qdrant (``analytics_chunks``), затем
   подбор аналитики к событиям. Уже проиндексированные материалы пропускаются (кроме ``--reindex``).

Повторный запуск безопасен. Код возврата: 0 — готово; 1 — что-то не получилось.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
DEMO_CONFIG = ROOT.parent / "backend" / "backend" / "demo.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--profiles-from", type=Path, default=ROOT / "data" / "profiles.db",
                        help="SQLite с готовыми профилями (по умолчанию data/profiles.db)")
    parser.add_argument("--build-demo", action="store_true", help="сразу построить профили «роль + компания»")
    parser.add_argument("--skip-profiles", action="store_true", help="не трогать профили")
    parser.add_argument("--skip-analytics", action="store_true", help="не трогать аналитику")
    parser.add_argument("--reindex", action="store_true", help="переиндексировать всю аналитику")
    parser.add_argument("--no-llm", action="store_true", help="без LLM (профили по правилам, подбор без LLM-судьи)")
    return parser.parse_args()


def load_demo_config():
    spec = importlib.util.spec_from_file_location("backend_demo", DEMO_CONFIG)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def copy_profiles(user_ids: list[str], source: Path) -> list[str]:
    """Профили ``user_ids``, которых нет в базе, — из SQLite-файла ``source`` (ORM-модель Profile)."""
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import NullPool

    from news_pipeline.profile.store import Profile, ProfileStore
    from news_pipeline.schemas import InterestProfile, UserProfileIn

    store = ProfileStore()
    missing = [user_id for user_id in user_ids if store.get(user_id) is None]
    if not missing:
        return []
    if not source.exists():
        raise SystemExit(f"нет профилей {', '.join(missing)} в базе и нет файла {source}")
    engine = create_engine(f"sqlite:///{source.resolve()}", poolclass=NullPool)
    with Session(engine) as session:
        rows = {row.user_id: row for row in session.scalars(select(Profile).where(Profile.user_id.in_(missing)))}
    engine.dispose()
    lost = sorted(set(missing) - set(rows))
    if lost:
        raise SystemExit(f"профилей {', '.join(lost)} нет и в {source}")
    for row in rows.values():
        store.put(UserProfileIn.model_validate_json(row.input), InterestProfile.model_validate_json(row.interests))
    return sorted(rows)


def main() -> int:
    args = parse_args()
    if args.no_llm:
        os.environ["REC_NEWS_USE_LLM"] = "0"
    if sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    from news_pipeline.storage import db

    demo = load_demo_config()
    print(f"База: {'PostgreSQL ' + db.SCHEMA_PREFIX + '*' if db.POSTGRES else 'SQLite'}")

    if not args.skip_profiles:
        sources = list(demo.COMPANIES.values())
        copied = copy_profiles(sources, args.profiles_from)
        print(f"\n1. Профили компаний демо ({', '.join(sources)}): "
              + (f"скопированы {', '.join(copied)}" if copied else "уже в базе"))

    if args.skip_analytics and not args.build_demo:
        return 0

    from news_pipeline.service import MLService

    started = time.monotonic()
    service = MLService(use_llm=not args.no_llm)   # пустую базу аналитики сервис сам заполнит из fixtures
    try:
        if args.build_demo and not args.skip_profiles:
            from news_pipeline.schemas import UserProfileIn

            print("\n   профили «роль + компания»:")
            for user_key, user in demo.USERS.items():
                for company_key, source_id in demo.COMPANIES.items():
                    company = service.profiles.get_input(source_id)
                    user_id = demo.profile_id(user_key, company_key)
                    service.build_profile(UserProfileIn(
                        user_id=user_id, role=user["role"], company_id=company.company_id,
                        company_name=company.company_name, industry=company.industry,
                        company_description=company.company_description))
                    print(f"   ✓ {user_id}: {user['role']} — {company.company_name}", flush=True)

        if not args.skip_analytics:
            import ocr_analytics
            from sqlalchemy import select

            from news_pipeline.analytics.corpus import load_fixture_materials
            from news_pipeline.analytics.store import AnalyticsMaterial

            pdfs = sorted(ocr_analytics.TEXTS_DIR.glob("*.pdf"))
            print(f"\n2. PDF аналитики в {ocr_analytics.TEXTS_DIR}: {len(pdfs)}")
            recognized = ocr_analytics.recognize()
            print(f"   распознано сейчас: {len(recognized)}")

            materials = load_fixture_materials()
            texts = {path.stem for path in ocr_analytics.OUT_DIR.glob("*.txt")}
            with db.session(service.analytics.store.engine) as session:
                indexed = set(session.scalars(select(AnalyticsMaterial.analytics_id)))
            todo = materials if args.reindex else [m for m in materials if m.analytics_id not in indexed]
            skipped = len(texts) - len(materials)
            print(f"\n3. Индексация: материалов с датой {len(materials)}"
                  + (f" (без даты в materials.json пропущено {skipped})" if skipped else "")
                  + f", к индексации {len(todo)}", flush=True)
            if todo:
                chunks = service.index_analytics(todo)
                print(f"   ✓ фрагментов: {chunks}; подбор для свежих событий пересчитан")
            matched = service.match_pending_events()
            print(f"   подбор аналитики для новых событий: {matched}")
            print(f"   материалов в базе: {service.analytics.store.material_count()}")
    finally:
        service.close()
    print(f"\nГотово за {time.monotonic() - started:.0f} с")
    return 0


if __name__ == "__main__":
    sys.exit(main())
