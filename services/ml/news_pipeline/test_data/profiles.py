"""Тестовые профили пользователей (ТЗ §2): разные роли и отрасли из датасета.

Берутся компании с выручкой больше 15 млрд ₽ (крупный бизнес по условиям
кейса) — по одной на отрасль шаблона, роли раздаются по кругу. Описание
компании собирается из её данных: отрасль, регион, ВЭД, продукты банка,
отрасли контрагентов. Результат — ``fixtures/test_profiles.json``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from news_pipeline.config import FIXTURES_DIR
from news_pipeline.recommendation.audience_profile import industry_by_okved
from news_pipeline.recommendation.recommender import profile_for_company
from news_pipeline.storage.dataset_db import COMPANIES, PAYMENTS, read_table
from news_pipeline.references import ROLES
from news_pipeline.schemas import UserProfileIn

PROFILES_PATH = FIXTURES_DIR / "test_profiles.json"
MIN_REVENUE = 15e9


def _description(profile: Any) -> str:
    parts = []
    if profile.industries:
        parts.append(f"Основная деятельность — {profile.industries[0]}")
    if profile.regions:
        parts.append(f"регион — {profile.regions[0]}")
    if profile.revenues:
        parts.append(f"выручка {profile.revenues[0]}")
    text = ", ".join(parts) + "."
    if "есть" in profile.foreign_trade:
        countries = ", ".join(profile.trade_countries) or "зарубежные страны"
        text += f" Ведёт внешнеэкономическую деятельность: {countries}."
    if profile.supplier_industries:
        text += f" Основные поставщики — {', '.join(profile.supplier_industries)}."
    if profile.customer_industries:
        text += f" Основные покупатели — {', '.join(profile.customer_industries)}."
    if profile.bank_products:
        text += f" Продукты в банке: {', '.join(profile.bank_products)}."
    return text


def build_test_profiles(count: int = 24, *, companies: Any | None = None, payments: Any | None = None) -> list[UserProfileIn]:
    """``count`` профилей: одна крупная компания на отрасль, роли по кругу."""
    companies = companies if companies is not None else read_table(COMPANIES)
    payments = payments if payments is not None else read_table(PAYMENTS)
    big = companies[companies["REVENUE_COMPANY"] > MIN_REVENUE].copy()
    big["industry"] = big["MAINOKVED_CODE"].map(industry_by_okved)
    big = big.sort_values("REVENUE_COMPANY", ascending=False)
    picked = big.drop_duplicates("industry")
    # Если отраслей меньше, чем нужно профилей, добираем следующими по выручке.
    rest = big[~big["SYNTHETIC_U"].isin(picked["SYNTHETIC_U"])]
    rows = list(picked.itertuples()) + list(rest.itertuples())
    users = []
    for number, row in enumerate(rows[:count]):
        role = ROLES[number % len(ROLES)]
        base = profile_for_company(row.SYNTHETIC_U, role, companies=companies, payments=payments)
        users.append(
            UserProfileIn(
                user_id=f"u_{number + 1:02d}",
                role=role,
                company_name=f"Компания {row.SYNTHETIC_U}",
                industry=row.industry,
                company_id=row.SYNTHETIC_U,
                company_description=_description(base),
            )
        )
    return users


def save_profiles(users: list[UserProfileIn], path: Path = PROFILES_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([user.model_dump() for user in users], ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_profiles(path: Path = PROFILES_PATH) -> list[UserProfileIn]:
    return [UserProfileIn(**item) for item in json.loads(path.read_text(encoding="utf-8"))]
