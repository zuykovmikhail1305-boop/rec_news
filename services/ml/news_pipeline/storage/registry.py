"""Все базы ML-части: путь (файл SQLite или схема PostgreSQL, см. ``storage/db.py``), ORM-модели и как их создать.

Нужен скриптам, которые работают со всеми базами сразу: ``scripts/init_db.py`` (создать и проверить)
и ``scripts/migrate_to_postgres.py`` (перенести данные из SQLite в PostgreSQL).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from news_pipeline.storage import db


@dataclass(frozen=True)
class Database:
    path: Path
    models: list[type[db.Base]]
    create: Callable[[], object]


def databases() -> list[Database]:
    from news_pipeline.agent.store import CHAT_DB, MODELS as CHAT_MODELS, ChatStore
    from news_pipeline.analytics.store import ANALYTICS_DB, MODELS as ANALYTICS_MODELS, AnalyticsStore
    from news_pipeline.config import NEWS_DB, SUMMARY_DB, UNIQUE_DB
    from news_pipeline.insights.store import INSIGHT_CARDS_DB, InsightCardRow, InsightCardStore
    from news_pipeline.llm.client import CACHE_PATH, LLM, LLMCacheEntry
    from news_pipeline.profile.store import PROFILES_DB, Profile, ProfileStore
    from news_pipeline.ranking.user_news import MODELS as RECOMMENDATION_MODELS, RECOMMENDATIONS_DB, UserNewsStore
    from news_pipeline.storage.feedback_db import FEEDBACK_DB, Feedback, FeedbackStore
    from news_pipeline.storage.news_db import (
        News, NewsFingerprint, NewsSource, NewsSummary, ensure_fingerprint_schema, ensure_schema, ensure_summary_schema,
    )
    from news_pipeline.tasks.store import JOBS_DB, MODELS as JOB_MODELS, JobStore

    return [
        Database(NEWS_DB, [News], lambda: ensure_schema(NEWS_DB)),
        Database(SUMMARY_DB, [NewsSummary, NewsSource], lambda: ensure_summary_schema(SUMMARY_DB)),
        Database(UNIQUE_DB, [NewsSummary, NewsSource, NewsFingerprint], lambda: ensure_fingerprint_schema(UNIQUE_DB)),
        Database(ANALYTICS_DB, ANALYTICS_MODELS, AnalyticsStore),
        Database(PROFILES_DB, [Profile], ProfileStore),
        Database(RECOMMENDATIONS_DB, RECOMMENDATION_MODELS, UserNewsStore),
        Database(INSIGHT_CARDS_DB, [InsightCardRow], InsightCardStore),
        Database(JOBS_DB, JOB_MODELS, JobStore),
        Database(CHAT_DB, CHAT_MODELS, ChatStore),
        Database(FEEDBACK_DB, [Feedback], FeedbackStore),
        Database(CACHE_PATH, [LLMCacheEntry], lambda: LLM(cache_path=CACHE_PATH)),
    ]
