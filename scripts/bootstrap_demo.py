"""Наполняет данные для демо: синтетические новости, конвейер, профили, пример ленты.

    python scripts/bootstrap_demo.py                    # 5 профилей, аспекты через LLM
    python scripts/bootstrap_demo.py --profiles 24 --rules   # все профили, аспекты по правилам
    python scripts/bootstrap_demo.py --no-llm --data-dir tmp/demo

Затем: python scripts/run_api.py (с теми же --data-dir / --no-llm).
"""

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--profiles", type=int, default=5, help="сколько тестовых профилей построить")
    parser.add_argument("--rules", action="store_true", help="аспекты профиля по правилам, без LLM")
    parser.add_argument("--no-llm", action="store_true", help="вообще без LLM")
    parser.add_argument("--data-dir", type=Path, help="каталог баз вместо data/")
    args = parser.parse_args()
    if args.no_llm:
        os.environ["REC_NEWS_USE_LLM"] = "0"
    if args.data_dir:
        os.environ["REC_NEWS_DATA_DIR"] = str(args.data_dir.resolve())

    from news_pipeline.config import NEWS_DB, SUMMARY_DB, UNIQUE_DB
    from news_pipeline.schemas import FeedRequest
    from news_pipeline.service import MLService
    from news_pipeline.storage.news_db import read_ids, summary_ids
    from news_pipeline.test_data.profiles import PROFILES_PATH, build_test_profiles, load_profiles, save_profiles
    from news_pipeline.test_data.synthetic_news import OUTPUT_PATH, PERIOD_END, load_into_news_db

    svc = MLService()
    try:
        if not (summary_ids(UNIQUE_DB) or summary_ids(SUMMARY_DB) or read_ids(NEWS_DB)):
            print(f"новости: загружено {load_into_news_db(NEWS_DB, OUTPUT_PATH)} из {OUTPUT_PATH}")
        started = time.monotonic()
        print("конвейер:", svc.run_pipeline_once(), f"({time.monotonic() - started:.0f} с)")

        users = load_profiles() if PROFILES_PATH.exists() else build_test_profiles(24)
        if not PROFILES_PATH.exists():
            save_profiles(users)
        for user in users[: args.profiles]:
            started = time.monotonic()
            if args.rules:
                from news_pipeline.profile.interests import build_interest_profile

                companies, payments = svc._dataset()
                profile = build_interest_profile(user, companies=companies, payments=payments, use_llm=False)
                svc.profiles.put(user, profile)
            else:
                profile = svc.build_profile(user)
            print(f"профиль {user.user_id} ({user.role}): {len(profile.aspects)} аспектов, "
                  f"{profile.source}, {time.monotonic() - started:.0f} с")

        user = users[0]
        started = time.monotonic()
        feed = svc.recommend(FeedRequest(user_id=user.user_id, limit=10, with_insights=False),
                             now=PERIOD_END if PERIOD_END < datetime.now(timezone.utc) else None)
        print(f"\nлента {user.user_id} ({user.role}), {time.monotonic() - started:.1f} с, LLM: {feed.llm_available}")
        for number, item in enumerate(feed.items, 1):
            print(f"{number:2}. [{item.score:.3f}] {item.title}\n      {item.why_relevant}")
    finally:
        svc.close()
