"""Вопрос агенту бизнес-инсайтов из командной строки (без API).

    python scripts/ask_agent.py --user u_01 "Что значит для нас решение ЦБ по ставке?"
    python scripts/ask_agent.py --user u_01 --brief
    python scripts/ask_agent.py --user u_01 --session c_… "А для казначейства?"
    python scripts/ask_agent.py --user u_01 --trace "…"        # этапы: план, проверки, оценки

``--now`` по умолчанию — конец периода синтетических новостей (если он в прошлом).
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("question", nargs="?", default="")
    parser.add_argument("--user", required=True)
    parser.add_argument("--brief", action="store_true", help="главное за период вместо вопроса")
    parser.add_argument("--session", help="продолжить диалог")
    parser.add_argument("--now", type=datetime.fromisoformat, help="текущий момент для демо")
    parser.add_argument("--trace", action="store_true", help="печатать этапы")
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    if args.no_llm:
        os.environ["REC_NEWS_USE_LLM"] = "0"
    if args.data_dir:
        os.environ["REC_NEWS_DATA_DIR"] = str(args.data_dir.resolve())
    if sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    from news_pipeline.service import MLService
    from news_pipeline.test_data.synthetic_news import PERIOD_END

    now = args.now or (PERIOD_END if PERIOD_END < datetime.now(timezone.utc) else None)

    def trace(stage: str, data: dict) -> None:
        if not args.trace or stage == "answer":
            return
        if stage == "context":
            data = {"relevant": data["relevant"], "evidence": [f"{e['id']} {e['relevance']:.2f} {e['title'][:60]}"
                                                               for e in data["evidence"]]}
        print(f"\n--- {stage} ---\n{json.dumps(data, ensure_ascii=False, indent=1, default=str)[:3000]}")

    svc = MLService()
    try:
        if args.brief:
            answer = svc.agent.brief(args.user, session_id=args.session, now=now, on_event=trace)
        else:
            if not args.question:
                parser.error("нужен вопрос или --brief")
            answer = svc.agent.ask(args.user, args.question, session_id=args.session, now=now, on_event=trace)
    finally:
        svc.close()
    print(f"\n=== {answer.status} · сессия {answer.session_id} ===\n")
    print(answer.text)
    critic = answer.quality.critic
    print(f"\nитераций: {answer.quality.iterations}, перезапусков: {answer.quality.restarts}, "
          f"оценка: {critic['overall'] if critic else '—'} {critic['scores'] if critic else ''}")
    remaining = [c for c in answer.quality.checks]
    if remaining:
        print("замечания проверки:", "; ".join(f"{c['severity']} {c['code']} [{c['section_id']}]" for c in remaining))
    print("время, мс:", answer.timings_ms)
