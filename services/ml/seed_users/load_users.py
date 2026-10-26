"""Загрузка пользователей (профилей) в базу ML — всё нужное лежит в этой папке.

    uv run --env-file .env seed_users/load_users.py                  # загрузить всех из users.json
    uv run --env-file .env seed_users/load_users.py demo_cfo_build   # только указанных
    uv run --env-file .env seed_users/load_users.py --force          # перезаписать уже загруженных
    uv run --env-file .env seed_users/load_users.py --no-build       # без LLM: только готовые профили
    uv run --env-file .env seed_users/load_users.py --export         # обновить файлы папки из базы

Файлы папки:

* ``users.json`` — анкеты пользователей (``UserProfileIn``): их можно править и добавлять новые;
* ``profiles.jsonl.gz`` — готовые профили интересов к анкетам (аспекты с векторами), по строке на пользователя.

Загрузка: анкета с готовым профилем (совпадает хэш анкеты) кладётся в базу как есть — быстро и без LLM;
анкета без готового профиля (новая или изменённая) строится заново (LLM + датасет компаний), если не задан
``--no-build``. Пользователи, уже загруженные с той же анкетой, пропускаются (кроме ``--force``).
База — из ``.env`` (``REC_NEWS_DATABASE_URL`` — PostgreSQL, иначе SQLite в ``data/``).
Код возврата: 0 — все загружены; 1 — кого-то загрузить не удалось.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

USERS_FILE = HERE / "users.json"
PROFILES_FILE = HERE / "profiles.jsonl.gz"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("users", nargs="*", help="user_id для загрузки (по умолчанию — все из users.json)")
    parser.add_argument("--force", action="store_true", help="перезаписать пользователей, которые уже в базе")
    parser.add_argument("--no-build", action="store_true", help="не строить профили через LLM: только готовые")
    parser.add_argument("--export", action="store_true", help="выгрузить пользователей из базы в файлы папки")
    return parser.parse_args()


def read_users() -> list:
    from news_pipeline.schemas import UserProfileIn

    return [UserProfileIn.model_validate(row) for row in json.loads(USERS_FILE.read_text(encoding="utf-8"))]


def read_profiles() -> dict[str, dict]:
    """``user_id`` → ``{"input_hash", "interests"}`` из ``profiles.jsonl.gz``."""
    if not PROFILES_FILE.exists():
        return {}
    with gzip.open(PROFILES_FILE, "rt", encoding="utf-8") as file:
        rows = [json.loads(line) for line in file if line.strip()]
    return {row["user_id"]: row for row in rows}


def export(store, user_ids: list[str]) -> int:
    """Анкеты и профили из базы → ``users.json`` и ``profiles.jsonl.gz`` (по умолчанию — все пользователи)."""
    ids = user_ids or store.all_ids()
    users, rows = [], []
    for user_id in ids:
        user, interests = store.get_input(user_id), store.get(user_id)
        if user is None or interests is None:
            print(f"  ✗ {user_id}: нет в базе")
            continue
        users.append(json.loads(user.model_dump_json(exclude_defaults=True)))
        rows.append({"user_id": user_id, "input_hash": store.input_hash(user),
                     "interests": json.loads(interests.model_dump_json())})
        print(f"  ✓ {user_id}: {user.role} — {user.company_name or user.company_id}")
    USERS_FILE.write_text(json.dumps(users, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with gzip.open(PROFILES_FILE, "wt", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"Выгружено {len(rows)}: {USERS_FILE.name}, {PROFILES_FILE.name} "
          f"({PROFILES_FILE.stat().st_size / 1e6:.1f} МБ)")
    return 0 if len(rows) == len(ids) else 1


def main() -> int:
    args = parse_args()
    if args.no_build:
        os.environ["REC_NEWS_USE_LLM"] = "0"
    if sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    from news_pipeline.profile.store import ProfileStore
    from news_pipeline.schemas import InterestProfile
    from news_pipeline.storage import db

    store = ProfileStore()
    print(f"База: {'PostgreSQL ' + db.SCHEMA_PREFIX + 'profiles' if db.POSTGRES else 'SQLite ' + str(store.path)}")
    if args.export:
        return export(store, args.users)

    users = read_users()
    if args.users:
        unknown = sorted(set(args.users) - {user.user_id for user in users})
        if unknown:
            print(f"нет в {USERS_FILE.name}: {', '.join(unknown)}")
            return 1
        users = [user for user in users if user.user_id in args.users]
    ready = read_profiles()
    print(f"Пользователей: {len(users)}, готовых профилей в {PROFILES_FILE.name}: {len(ready)}\n")

    service, counts, failed = None, {"готовый": 0, "построен": 0, "уже в базе": 0}, []
    started = time.monotonic()
    try:
        for user in users:
            if not args.force and store.is_fresh(user):
                counts["уже в базе"] += 1
                print(f"  · {user.user_id}: уже в базе")
                continue
            row = ready.get(user.user_id)
            if row and row["input_hash"] == store.input_hash(user):
                store.put(user, InterestProfile.model_validate(row["interests"]))
                counts["готовый"] += 1
                print(f"  ✓ {user.user_id}: готовый профиль ({user.role} — {user.company_name})")
                continue
            if args.no_build:
                failed.append(user.user_id)
                print(f"  ✗ {user.user_id}: нет готового профиля к этой анкете (уберите --no-build)")
                continue
            if service is None:
                from news_pipeline.service import MLService

                service = MLService()
            try:
                profile = service.build_profile(user)
            except Exception as error:  # noqa: BLE001 — отчитаться и продолжить с остальными
                failed.append(user.user_id)
                print(f"  ✗ {user.user_id}: {type(error).__name__}: {error}")
                continue
            counts["построен"] += 1
            print(f"  ✓ {user.user_id}: построен ({len(profile.aspects)} аспектов, {profile.source})", flush=True)
    finally:
        if service is not None:
            service.close()

    summary = ", ".join(f"{name} — {count}" for name, count in counts.items() if count)
    print(f"\nГотово за {time.monotonic() - started:.0f} с: {summary or 'ничего'}"
          + (f"; не загружены: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
