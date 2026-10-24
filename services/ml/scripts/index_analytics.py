"""Индексирует аналитику из fixtures/analytics и подбирает её к событиям.

    python scripts/index_analytics.py                 # индексация + подбор для событий без подбора
    python scripts/index_analytics.py --reindex       # переиндексировать корпус заново
    python scripts/index_analytics.py --event e_syn_monetary_policy_0   # подбор для одного события

Тексты PDF распознаются заранее: python scripts/ocr_analytics.py
"""

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--reindex", action="store_true", help="переиндексировать корпус")
    parser.add_argument("--event", help="подобрать аналитику только для этого события")
    parser.add_argument("--limit", type=int, help="максимум событий для подбора")
    parser.add_argument("--no-llm", action="store_true", help="без LLM-судьи (fallback по реранку)")
    parser.add_argument("--data-dir", type=Path, help="каталог баз вместо data/")
    args = parser.parse_args()
    if args.no_llm:
        os.environ["REC_NEWS_USE_LLM"] = "0"
    if args.data_dir:
        os.environ["REC_NEWS_DATA_DIR"] = str(args.data_dir.resolve())

    from news_pipeline.analytics.corpus import load_fixture_materials
    from news_pipeline.service import MLService

    svc = MLService()  # при пустой базе сам индексирует fixtures/analytics
    try:
        if args.reindex and not svc.analytics_bootstrapped:
            materials = load_fixture_materials()
            started = time.monotonic()
            print(f"чанков: {svc.analytics.index_materials(materials)} из {len(materials)} материалов "
                  f"({time.monotonic() - started:.0f} с)")
        print(f"материалов в базе: {svc.analytics.store.material_count()}")
        events = [args.event] if args.event else None
        started = time.monotonic()
        if events:
            for event_id in events:
                result = svc.rematch(event_id)
                print(f"{event_id}: {result.status}")
                for item in result.analytics:
                    print(f"  [{item.score}] {item.relation}: «{item.analytics_title}» / {item.section_title} — {item.reason}")
        else:
            count = svc.match_pending_events(limit=args.limit)
            print(f"подбор выполнен для {count} событий ({time.monotonic() - started:.0f} с)")
    finally:
        svc.close()
