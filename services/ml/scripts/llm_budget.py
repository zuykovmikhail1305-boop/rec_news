"""Расход и остаток дневного бюджета ключа LLM API хакатона.

    python scripts/llm_budget.py

Адрес и ключ — ``REC_NEWS_LLM_BASE_URL`` и ``REC_NEWS_LLM_API_KEY`` из ``.env``.
Данные — ручка ``/key/info`` прокси LiteLLM; сам ключ не печатается.
"""

import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from news_pipeline.config import LLM_API_KEY, LLM_BASE_URL  # noqa: E402

if __name__ == "__main__":
    if sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    root = LLM_BASE_URL.rstrip("/").removesuffix("/v1")
    request = urllib.request.Request(f"{root}/key/info", headers={"Authorization": f"Bearer {LLM_API_KEY}"})
    info = json.load(urllib.request.urlopen(request, timeout=30))["info"]
    budget, spend = info.get("max_budget"), info.get("spend") or 0.0
    reset = info.get("budget_reset_at")
    print(f"ключ:              {info.get('key_alias') or info.get('key_name')}")
    if budget is not None:
        print(f"лимит на период:   {budget:g} (период {info.get('budget_duration') or '—'})")
        print(f"потрачено:         {spend:.2f}")
        print(f"осталось:          {budget - spend:.2f} ({(budget - spend) / budget:.0%})")
    else:
        print(f"потрачено:         {spend:.2f} (лимит не задан)")
    if reset:
        print(f"обнуление:         {datetime.fromisoformat(reset).astimezone():%d.%m.%Y %H:%M} (местное время)")
    print(f"всего с создания:  {info.get('total_spend') or 0:.2f}")
