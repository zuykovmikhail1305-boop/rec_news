"""Данные о компании для карточки инсайта — пронумерованные факты ``F1…``.

Источники: анкета пользователя (``UserProfileIn``) и датасет по ``company_id``
(``dataset/ur_lica.db`` — отрасль, регион, выручка, продукты в банке, ВЭД;
``dataset/payment.db`` — отрасли контрагентов и концентрация платежей).
Агенты карточки опираются только на эти факты, а блок «Вывод AI» показывает,
какие из них учтены. Числа в фактах (доли платежей) — посчитаны кодом, поэтому
агенты могут их приводить.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from news_pipeline.recommendation.audience_profile import AudienceProfile, counterparty_industries
from news_pipeline.schemas import UserProfileIn


@dataclass(frozen=True)
class CompanyFact:
    id: str
    text: str
    kind: str       # industry, region, size, product, trade, suppliers, customers, concentration, profile
    origin: str     # dataset | анкета | платежи


def _concentration(company_id: str, payments: Any, column: str, counterparty: str) -> tuple[float, int] | None:
    """Доля крупнейшего контрагента в платежах компании и число контрагентов."""
    edges = payments[(payments[column] == company_id) & (payments["PAYER"] != payments["BENEFICIARY"])]
    if edges.empty:
        return None
    amounts = edges.groupby(counterparty)["PAY_AMT"].sum()
    total = float(amounts.sum())
    if total <= 0:
        return None
    return float(amounts.max()) / total, int(len(amounts))


def company_facts(user: UserProfileIn, *, companies: Any | None = None, payments: Any | None = None) -> list[CompanyFact]:
    """Факты о компании пользователя в фиксированном порядке."""
    items: list[tuple[str, str, str]] = []
    base = AudienceProfile()
    company_id = user.company_id
    if company_id and companies is not None:
        rows = companies[companies["SYNTHETIC_U"] == company_id]
        if not rows.empty:
            suppliers, customers = (counterparty_industries(company_id, payments, companies)
                                    if payments is not None else ((), ()))
            base = AudienceProfile.from_company(rows.iloc[0], role=user.role, supplier_industries=suppliers,
                                                customer_industries=customers)

    industry = user.industry or (base.industries[0] if base.industries else None)
    if industry:
        items.append(("industry", f"отрасль — {industry}", "анкета" if user.industry else "dataset"))
    for region in dict.fromkeys([*user.regions, *base.regions]):
        items.append(("region", f"регион — {region}", "анкета" if region in user.regions else "dataset"))
    if base.revenues:
        items.append(("size", f"выручка {base.revenues[0]}", "dataset"))
    elif user.company_size:
        items.append(("size", f"размер компании — {user.company_size}", "анкета"))
    if base.employees:
        items.append(("size", f"сотрудников — {base.employees[0]}", "dataset"))

    products = list(dict.fromkeys([*base.bank_products, *user.products]))
    if "кредиты" in products:
        items.append(("product", "есть кредиты", "dataset" if "кредиты" in base.bank_products else "анкета"))
    for product in products:
        if product != "кредиты":
            items.append(("product", f"использует {product}", "dataset" if product in base.bank_products else "анкета"))

    if base.foreign_trade and base.foreign_trade[0] == "есть":
        countries = ", ".join(base.trade_countries)
        items.append(("trade", "ведёт внешнеэкономическую деятельность" + (f": {countries}" if countries else ""),
                      "dataset"))
    if base.supplier_industries:
        items.append(("suppliers", "ключевые поставщики — " + ", ".join(base.supplier_industries), "платежи"))
    if base.customer_industries:
        items.append(("customers", "ключевые покупатели — " + ", ".join(base.customer_industries), "платежи"))

    if company_id and payments is not None:
        incoming = _concentration(company_id, payments, "BENEFICIARY", "PAYER")
        if incoming and incoming[0] >= 0.25 and incoming[1] > 1:
            items.append(("concentration", f"{round(incoming[0] * 100)}% поступлений — от одного покупателя", "платежи"))
        outgoing = _concentration(company_id, payments, "PAYER", "BENEFICIARY")
        if outgoing and outgoing[0] >= 0.25 and outgoing[1] > 1:
            items.append(("concentration", f"{round(outgoing[0] * 100)}% платежей поставщикам — одному контрагенту",
                          "платежи"))

    if user.competitors:
        items.append(("profile", "конкуренты — " + ", ".join(user.competitors), "анкета"))
    if user.counterparties:
        items.append(("profile", "контрагенты — " + ", ".join(user.counterparties), "анкета"))

    return [CompanyFact(id=f"F{number}", text=text, kind=kind, origin=origin)
            for number, (kind, text, origin) in enumerate(items, 1)]
