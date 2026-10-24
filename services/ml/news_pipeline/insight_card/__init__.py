"""Персональная карточка-инсайт по новости: факты в фоне, карточка при открытии (docs/insight_card.md)."""

from news_pipeline.insight_card.builder import InsightCardBuilder
from news_pipeline.insight_card.facts import FactExtractor, SourceVerifier, event_articles
from news_pipeline.insight_card.store import InsightStore

__all__ = ["FactExtractor", "InsightCardBuilder", "InsightStore", "SourceVerifier", "event_articles"]
