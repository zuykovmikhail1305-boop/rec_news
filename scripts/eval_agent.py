"""Прогон агента на контрольном наборе вопросов (docs/agent_tz.md §10, П12).

    python scripts/eval_agent.py                       # первые 3 профиля × все вопросы
    python scripts/eval_agent.py --users u_01 u_07 --only rate_impact digest

Вопросы — ``fixtures/agent_questions.json`` (диалоги из одной или нескольких
реплик). Каждый ответ — в ``data/logs/agent_eval.jsonl``; в конце — сводка:
статусы, доля принятых с первой итерации, средние оценки оценщика по
критериям, частота кодов проверки, время ответа.
"""

import argparse
import json
import os
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--users", nargs="*", help="ID профилей (по умолчанию первые 3)")
    parser.add_argument("--only", nargs="*", help="ID диалогов из набора")
    parser.add_argument("--now", type=datetime.fromisoformat)
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    if args.no_llm:
        os.environ["REC_NEWS_USE_LLM"] = "0"
    if args.data_dir:
        os.environ["REC_NEWS_DATA_DIR"] = str(args.data_dir.resolve())
    if sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    from news_pipeline.config import DATA_DIR
    from news_pipeline.service import MLService
    from news_pipeline.test_data.synthetic_news import PERIOD_END

    now = args.now or (PERIOD_END if PERIOD_END < datetime.now(timezone.utc) else None)
    dialogs = json.loads((ROOT / "fixtures" / "agent_questions.json").read_text(encoding="utf-8"))["dialogs"]
    if args.only:
        dialogs = [d for d in dialogs if d["id"] in args.only]
    out_path = DATA_DIR / "logs" / "agent_eval.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    svc = MLService()
    rows = []
    try:
        users = args.users or svc.profiles.all_ids()[:3]
        for user_id in users:
            for dialog in dialogs:
                session = None
                for turn, question in enumerate(dialog["turns"], 1):
                    started = time.monotonic()
                    answer = svc.agent.ask(user_id, question, session_id=session, now=now)
                    session = answer.session_id
                    seconds = time.monotonic() - started
                    critic = answer.quality.critic or {}
                    row = {
                        "user_id": user_id, "dialog": dialog["id"], "type": dialog["type"], "turn": turn,
                        "question": question, "status": answer.status, "answer_type": (answer.plan or {}).get("answer_type"),
                        "iterations": answer.quality.iterations, "restarts": answer.quality.restarts,
                        "overall": critic.get("overall"), "scores": critic.get("scores"),
                        "errors": [c["code"] for c in answer.quality.checks if c["severity"] == "error"],
                        "warnings": [c["code"] for c in answer.quality.checks if c["severity"] == "warning"],
                        "seconds": round(seconds, 1), "summary": answer.summary,
                    }
                    rows.append(row)
                    with out_path.open("a", encoding="utf-8") as file:
                        file.write(json.dumps(row, ensure_ascii=False) + "\n")
                    print(f"{user_id} {dialog['id']}#{turn}: {answer.status:12} {row['answer_type'] or '—':13} "
                          f"оценка {row['overall'] or '—'}  {seconds:5.1f} с  {answer.summary[:80]}")
    finally:
        svc.close()

    print(f"\nответов: {len(rows)}")
    print("статусы:", dict(Counter(r["status"] for r in rows)))
    generated = [r for r in rows if r["status"] in ("ok", "best_effort", "degraded")]
    if generated:
        first = sum(r["status"] == "ok" and r["iterations"] == 1 and r["restarts"] == 0 for r in generated)
        print(f"приняты с первой итерации: {first}/{len(generated)}")
        scored = [r for r in generated if r["scores"]]
        if scored:
            by_criterion = defaultdict(list)
            for r in scored:
                for name, value in r["scores"].items():
                    by_criterion[name].append(value)
            print("средняя оценка:", round(statistics.mean(r["overall"] for r in scored), 2),
                  {name: round(statistics.mean(v), 2) for name, v in by_criterion.items()})
        print("ошибки проверки в итоговых ответах:", dict(Counter(c for r in generated for c in r["errors"])))
        print("предупреждения:", dict(Counter(c for r in generated for c in r["warnings"])))
    seconds = sorted(r["seconds"] for r in rows)
    if seconds:
        print(f"время: медиана {statistics.median(seconds):.1f} с, максимум {seconds[-1]:.1f} с")
    print(f"подробно: {out_path}")
