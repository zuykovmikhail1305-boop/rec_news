"""Карточка инсайта — контракт для бэкенда и фронтенда (docs/insight_card.md).

Карточка состоит из блоков в порядке показа: шапка (чипы), заголовок, факт,
позиция Альфа-Банка, вывод AI, риски и возможности, гипотеза, что можно
сделать, предложение банка, на чём основан вывод. Каждый блок — отдельная
модель; блок, который не удалось построить или проверить, равен ``null``,
а причина — в ``verification.blocks``.

ID источников внутри карточки: ``N1…`` — публикации о событии, ``A1`` —
фрагмент аналитики Альфа-Банка, ``F1…`` — данные о компании.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

BlockName = Literal[
    "header", "headline", "fact", "bank_view", "ai_conclusion", "risks", "hypothesis", "actions", "bank_offer",
    "evidence",
]
BlockStatus = Literal["ok", "fixed", "unverified", "failed", "skipped", "fallback"]


# --- шапка ----------------------------------------------------------------------


class Chip(BaseModel):
    value: str
    label: str
    reason: str | None = None


class Header(BaseModel):
    """Чипы: влияние, приоритет, горизонт, надёжность."""

    impact: Chip            # risk | opportunity | neutral
    priority: Chip          # high | medium | low
    horizon: Chip           # 1-3m | 3-6m | 6-12m
    reliability: Chip       # confirmed | single_source
    sources_count: int


class Headline(BaseModel):
    """Главная фраза: что событие значит для компании читателя (не заголовок новости).

    Строится LLM из новости, аналитики Альфа-Банка и профиля; на что она опирается — в полях ниже.
    """

    text: str
    company: str | None = None
    role: str
    event_date: datetime | None = None
    aspect: str | None = None                    # интерес читателя из профиля, который задевает фраза
    company_features: list["CompanyFeature"] = Field(default_factory=list)   # особенности компании
    source_ids: list[str] = Field(default_factory=list)                     # N1 и A1, если аналитика есть
    based_on_analytics: bool = False             # фраза учитывает оценку аналитиков банка


# --- содержательные блоки ------------------------------------------------------------


class KeyFigure(BaseModel):
    value: str
    label: str
    source_ids: list[str] = Field(default_factory=list)


class Fact(BaseModel):
    """Что произошло: сухо, без оценок."""

    text: str
    decided: list[str] = Field(default_factory=list)      # уже решено
    discussed: list[str] = Field(default_factory=list)    # пока обсуждается
    key_figures: list[KeyFigure] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)


class BankView(BaseModel):
    """Позиция Альфа-Банка. ``available = false`` — аналитики по теме нет, вместо блока ``warning``."""

    available: bool
    text: str | None = None
    material_title: str | None = None
    material_date: datetime | None = None
    section_title: str | None = None
    url: str | None = None
    relation: str | None = None
    contradicts: bool = False
    source_ids: list[str] = Field(default_factory=list)
    warning: str | None = None


class CompanyFeature(BaseModel):
    id: str                 # F1…
    text: str               # «есть кредиты», «44% поступлений — от одного покупателя»


class AIConclusion(BaseModel):
    """Вывод AI: как событие затронет компанию и на какие её особенности опирается вывод."""

    text: str
    channels: list[str] = Field(default_factory=list)   # кредиты, отрасль, регион, поставщики, покупатели…
    company_features: list[CompanyFeature] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)


class RiskItem(BaseModel):
    text: str
    source_ids: list[str] = Field(default_factory=list)
    feature_ids: list[str] = Field(default_factory=list)


class RisksOpportunities(BaseModel):
    risks: list[RiskItem] = Field(default_factory=list)          # 1–3
    opportunities: list[RiskItem] = Field(default_factory=list)  # 1–3


class Hypothesis(BaseModel):
    condition: str          # «если …»
    consequence: str        # «то …»
    text: str               # «Если …, то …»
    disclaimer: str = "Это предположение, а не прогноз."


Headline.model_rebuild()


class Actions(BaseModel):
    steps: list[str] = Field(default_factory=list)   # 2–4 шага под роль
    question: str                                    # вопрос для обсуждения внутри компании


class CallToAction(BaseModel):
    label: str = "Обсудить с менеджером"
    action: str = "contact_manager"


class BankOffer(BaseModel):
    product_id: str
    name: str
    description: str
    reason: str             # чем помогает с названным риском
    risk_index: int | None = None   # номер риска из risks.risks (с 0)
    cta: CallToAction = Field(default_factory=CallToAction)


# --- на чём основан вывод ------------------------------------------------------------


class EvidenceSource(BaseModel):
    id: str                 # N1…, A1
    type: Literal["news", "analytics"]
    title: str
    outlet: str | None = None
    url: str | None = None
    published_at: datetime | None = None
    reliability: int = Field(ge=1, le=5)
    reliability_label: str
    quote: str | None = None


class TraceStep(BaseModel):
    step: int
    title: str
    detail: str


class Evidence(BaseModel):
    sources: list[EvidenceSource] = Field(default_factory=list)
    analytics_material: EvidenceSource | None = None
    company_data: list[CompanyFeature] = Field(default_factory=list)
    trace: list[TraceStep] = Field(default_factory=list)   # кнопка «Как получен вывод»


# --- проверка ---------------------------------------------------------------------------


class BlockIssue(BaseModel):
    code: str
    severity: Literal["error", "warning"]
    message: str
    by: Literal["code", "verifier"] = "code"


class BlockVerification(BaseModel):
    status: BlockStatus
    attempts: int = 0
    model: str | None = None
    issues: list[BlockIssue] = Field(default_factory=list)


class Verification(BaseModel):
    verdict: Literal["passed", "passed_with_fixes", "partial", "not_verified"]
    rounds: int = 0
    comment: str | None = None
    blocks: dict[str, BlockVerification] = Field(default_factory=dict)


# --- карточка ----------------------------------------------------------------------------


class InsightCard(BaseModel):
    card_id: str
    user_id: str
    news_id: str
    event_id: str | None = None
    status: Literal["ready", "partial", "degraded"]
    generated_at: datetime
    pipeline_version: str
    header: Header
    headline: Headline | None = None
    fact: Fact
    bank_view: BankView
    ai_conclusion: AIConclusion | None = None
    risks: RisksOpportunities | None = None
    hypothesis: Hypothesis | None = None
    actions: Actions | None = None
    bank_offer: BankOffer | None = None
    evidence: Evidence
    verification: Verification
    timings_ms: dict[str, int] = Field(default_factory=dict)


class InsightCardSummary(BaseModel):
    """Краткая карточка для ленты: заголовок и чипы."""

    card_id: str
    status: str
    headline: str | None = None
    impact: str
    priority: str
    horizon: str
    reliability: str
