"""Схемы данных ML-части (ТЗ §2, §4, §5, §6, §9) — общий контракт модулей и API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from news_pipeline.insights.schemas import InsightCardSummary
from news_pipeline.references import canonical_role

# --- Входные потоки от бэкенда (§2) -----------------------------------------


class UserProfileIn(BaseModel):
    """Анкета пользователя. ``company_id`` — ID компании в датасете (``SYNTHETIC_U``),
    по нему профиль дополняется отраслью, контрагентами, ВЭД и продуктами банка."""

    user_id: str
    role: str
    company_name: str | None = None
    industry: str | None = None
    company_description: str | None = None
    company_id: str | None = None
    regions: list[str] = Field(default_factory=list)
    products: list[str] = Field(default_factory=list)
    competitors: list[str] = Field(default_factory=list)
    counterparties: list[str] = Field(default_factory=list)
    company_size: str | None = None

    @field_validator("role")
    @classmethod
    def _canonical_role(cls, role: str) -> str:
        """Роль приводится к названию из ``role_topics.json`` (регистр, дефисы, синонимы)."""
        return canonical_role(role)


class NewsIn(BaseModel):
    news_id: str
    title: str
    text: str
    source: str
    published_at: datetime
    url: str | None = None
    rubric: str | None = None


class AnalyticsIn(BaseModel):
    """Материал аналитики. ``sections`` — заголовки разделов для чанкинга (если известны)."""

    analytics_id: str
    title: str
    text: str
    published_at: datetime
    type: str | None = None
    sections: list[str] = Field(default_factory=list)
    url: str | None = None
    author: str | None = None


# --- Профиль интересов (§4) ---------------------------------------------------


class Aspect(BaseModel):
    """Аспект интереса: один тип новостей, важных пользователю."""

    aspect_id: str
    title: str
    description: str
    examples: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    topic: str
    weight: float = Field(ge=0.0, le=1.0)
    embedding: list[float] | None = None


class InterestProfile(BaseModel):
    user_id: str
    role: str
    aspects: list[Aspect]
    entities: dict[str, float]
    topic_weights: dict[str, float]
    source: Literal["llm", "rules"] = "rules"
    pipeline_version: str = ""


# --- Обогащённая новость (§5) -------------------------------------------------


class EnrichedNews(BaseModel):
    news_id: str
    event_id: str | None = None
    parent_event_id: str | None = None
    is_representative: bool = True
    title: str
    lead: str
    source: str | None = None
    url: str | None = None
    entities: list[str] = Field(default_factory=list)
    topics: dict[str, float] = Field(default_factory=dict)
    importance: int = Field(default=3, ge=1, le=5)
    published_at: datetime | None = None
    embedding: list[float] | None = None


# --- Аналитика (ТЗ «подбор аналитики к новостям») ---------------------------

Relation = Literal["причины", "прогноз", "исторический контекст", "противоречит", "общая тема"]


class AnalyticsChunk(BaseModel):
    """Чанк аналитики (§4.4)."""

    chunk_id: str
    analytics_id: str
    analytics_title: str
    section_title: str
    position: int
    text: str
    published_at: datetime | None = None
    type: str | None = None
    url: str | None = None
    entities: list[str] = Field(default_factory=list)
    topics: dict[str, float] = Field(default_factory=dict)


class EventAnalytics(BaseModel):
    """Фрагмент, прикреплённый к событию (§8)."""

    analytics_id: str
    chunk_id: str
    score: int = Field(ge=0, le=3)
    relation: Relation
    contradicts: bool = False
    reason: str
    rerank: float | None = None
    matched_at: datetime
    analytics_title: str | None = None
    section_title: str | None = None
    published_at: datetime | None = None
    url: str | None = None


class EventAnalyticsResult(BaseModel):
    event_id: str
    analytics: list[EventAnalytics]
    status: Literal["matched", "empty", "fallback", "no_corpus"] = "matched"
    pipeline_version: str = ""


class FeedAnalytics(BaseModel):
    """Блок «Аналитика по теме» в ленте (§9): один фрагмент, выбранный под пользователя."""

    analytics_id: str
    chunk_id: str
    title: str
    section_title: str | None = None
    published_at: datetime | None = None
    relation: Relation
    contradicts: bool = False
    summary: str
    url: str | None = None


# --- Лента (§7, §9) -----------------------------------------------------------


class FeedRequest(BaseModel):
    user_id: str
    limit: int = Field(default=15, ge=1, le=50)
    window_days: int = Field(default=7, ge=1, le=60)
    with_insights: bool = True


class Signals(BaseModel):
    sem: float
    ent: float
    topic: float
    imp: float
    fresh: float
    retrieval: float
    llm: int | None = None
    final: float


class FeedItem(BaseModel):
    news_id: str
    event_id: str | None
    title: str
    score: float
    why_relevant: str | None = None
    topics: list[str] = Field(default_factory=list)
    analytics: FeedAnalytics | None = None
    insight: str | None = None
    insight_sources: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    signals: Signals | None = None


class FeedResponse(BaseModel):
    user_id: str
    generated_at: datetime
    pipeline_version: str
    llm_available: bool
    items: list[FeedItem]


# --- Фидбэк (§9, §12) ---------------------------------------------------------


class FeedbackEvent(BaseModel):
    user_id: str
    news_id: str
    type: Literal["impression", "click", "read_time", "useful", "not_useful", "hide"]
    value: float | None = None
    position: int | None = None
    pipeline_version: str | None = None
    timestamp: datetime


# --- Обработка по запросу бэкенда и готовые рекомендации ----------------------


class PipelineRunRequest(BaseModel):
    """Запуск обработки. ``now`` — «текущий момент» для демо на синтетике (по умолчанию
    ``REC_NEWS_DEMO_NOW`` или реальное время); ``user_ids`` — пересчитать только этих
    пользователей (по умолчанию — всех с профилем); ``insights`` — после рекомендаций построить
    карточки инсайтов для первых ``insights_top`` новостей каждого пользователя (по умолчанию
    ``pipeline_top`` из ``[insight_card]``)."""

    now: datetime | None = None
    user_ids: list[str] | None = None
    insights: bool = False
    insights_top: int | None = Field(default=None, ge=1, le=50)


class PipelineRun(BaseModel):
    run_id: str
    status: Literal["queued", "running", "done", "failed"]
    stage: str | None = None
    requested_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    demo_now: datetime | None = None
    news_prepared: int = 0
    news_deduplicated: int = 0
    news_unique: int = 0
    events_matched: int = 0
    users_total: int = 0
    users_done: int = 0
    users_failed: int = 0
    insights_built: int = 0
    error: str | None = None


class UserNewsAnalytics(BaseModel):
    """Фрагмент аналитики Альфа-Банка, прикреплённый к новости для пользователя."""

    analytics_id: str
    chunk_id: str
    title: str | None = None
    section_title: str | None = None
    published_at: datetime | None = None
    relation: str | None = None
    contradicts: bool = False
    summary: str | None = None
    text: str | None = None
    url: str | None = None


class UserNewsItem(BaseModel):
    rank: int
    news_id: str
    event_id: str | None
    title: str
    lead: str
    text: str | None = None
    source: str | None = None
    url: str | None = None
    published_at: datetime | None = None
    sources: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    importance: int | None = None
    score: float
    why_relevant: str | None = None
    analytics: UserNewsAnalytics | None = None
    insight: InsightCardSummary | None = None   # карточка инсайта, если уже построена
    signals: Signals | None = None


class UserNewsResponse(BaseModel):
    user_id: str
    status: Literal["ready", "not_computed"]
    run_id: str | None = None
    computed_at: datetime | None = None
    pipeline_version: str | None = None
    total: int = 0
    items: list[UserNewsItem]
