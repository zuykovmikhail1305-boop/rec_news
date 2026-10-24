"""Сборщик контекста — пакет доказательств для агента (docs/agent_tz.md §4.0).

Лента целиком не помещается в контекст модели, поэтому перед планировщиком
отбираются самые подходящие к вопросу источники:

* новости — лента пользователя + поиск по вопросу во всей базе (Qdrant,
  окно ``window_days``); релевантность — косинус вопроса и лида, новостям
  ленты — бонус ``feed_bonus``; одна новость на событие;
* аналитика — фрагменты, прикреплённые к событиям выбранных новостей,
  + поиск по вопросу в коллекции аналитики; релевантность — косинус вопроса
  и фрагмента + бонус за оценку LLM-судьи;
* профиль — ``P``.

Для дайджеста (вопроса нет) порядок задаёт сама лента. Пакет фиксируется на
сессию; в уточнениях к нему добавляются новые источники со следующими ID.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
from qdrant_client import models

from news_pipeline.agent.schemas import Evidence, EvidencePack
from news_pipeline.analytics import AnalyticsMatcher
from news_pipeline.config import UNIQUE_DB
from news_pipeline.index.embeddig import QdrantIndex
from news_pipeline.schemas import FeedItem, InterestProfile, UserProfileIn
from news_pipeline.settings import SETTINGS
from news_pipeline.storage.news_db import read_summaries

PUBLICATION_TS_FIELD = "publication_ts"


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def profile_text(profile: InterestProfile, user: UserProfileIn | None) -> str:
    """Профиль пользователя и компании одним текстом (источник ``P``)."""
    lines = [f"Роль: {profile.role}"]
    if user is not None:
        fields = [
            ("Компания", user.company_name), ("Отрасль", user.industry), ("Размер", user.company_size),
            ("Описание", user.company_description), ("Регионы", ", ".join(user.regions)),
            ("Продукты", ", ".join(user.products)), ("Конкуренты", ", ".join(user.competitors)),
            ("Контрагенты", ", ".join(user.counterparties)),
        ]
        lines += [f"{name}: {value}" for name, value in fields if value]
    if profile.aspects:
        lines.append("Интересы: " + "; ".join(aspect.title for aspect in profile.aspects[:8]))
    entities = sorted(profile.entities, key=profile.entities.get, reverse=True)[:10]
    if entities:
        lines.append("Важные сущности: " + ", ".join(entities))
    return "\n".join(lines)


def _analytics_title(material: str, section: str | None) -> str:
    """«Материал — раздел»; раздел опускается, если совпадает с названием материала."""
    if not section or section.strip() == material.strip() or section.strip() in material:
        return material
    return f"{material} — {section}"


class EvidenceBuilder:
    def __init__(self, news_index: QdrantIndex, analytics: AnalyticsMatcher, *, config: dict[str, Any] | None = None):
        self.index = news_index
        self.analytics = analytics
        self.config = config or SETTINGS.agent

    def build(
        self,
        question: str,
        *,
        profile: InterestProfile,
        user: UserProfileIn | None,
        feed: list[FeedItem],
        now: datetime | None = None,
        brief: bool = False,
        previous: EvidencePack | None = None,
    ) -> EvidencePack:
        now = _utc(now or datetime.now(timezone.utc))
        vector = self.index.encode([question])[0]
        news = self._news(vector, feed, now, brief)
        analytics = self._analytics(vector, news, now, brief)
        profile_item = Evidence(id="P", type="profile", title="Профиль", text=profile_text(profile, user), relevance=1.0)
        relevances = [item.relevance for item in news + analytics]
        best = max(relevances, default=0.0)
        if previous is not None:
            return self._merge(previous, news, analytics, best)
        for number, item in enumerate(news, 1):
            item.id = f"N{number}"
        for number, item in enumerate(analytics, 1):
            item.id = f"A{number}"
        relevant = brief or best >= self.config["min_relevance"]
        return EvidencePack([*news, *analytics, profile_item], relevant=relevant, best_relevance=round(best, 4))

    # --- новости --------------------------------------------------------------

    def _news(self, vector: np.ndarray, feed: list[FeedItem], now: datetime, brief: bool) -> list[Evidence]:
        candidates: dict[str, dict[str, Any]] = {
            item.news_id: {"origin": "feed", "rank": rank, "why": item.why_relevant}
            for rank, item in enumerate(feed)
        }
        if not brief:
            window = models.Filter(must=[models.FieldCondition(
                key=PUBLICATION_TS_FIELD,
                range=models.Range(gte=(now - timedelta(days=self.config["window_days"])).timestamp(),
                                   lte=now.timestamp()),
            )])
            for doc in self.index.find_similar(vector, limit=self.config["search_news"], query_filter=window):
                candidates.setdefault(str(doc.id), {"origin": "search"})
        rows = [row for row in read_summaries(UNIQUE_DB, list(candidates)) if row.document_id in candidates]
        if not rows:
            return []
        similarity = self.index.encode([row.lead for row in rows]) @ vector
        items = []
        for row, cosine in zip(rows, similarity):
            meta = candidates[row.document_id]
            if brief:
                relevance = 1.0 - 0.01 * meta.get("rank", 99)
            else:
                relevance = float(cosine) + (self.config["feed_bonus"] if meta["origin"] == "feed" else 0.0)
            items.append(Evidence(
                id="", type="news", title=row.title, text=row.text[: self.config["news_chars"]].strip(),
                relevance=round(relevance, 4), source=row.metadata.get("source"),
                published_at=row.publication_time, url=row.url, news_id=row.document_id,
                event_id=str(row.metadata.get("event_id") or f"e_{row.document_id}"),
                origin=meta["origin"], why=meta.get("why"),
            ))
        items.sort(key=lambda item: item.relevance, reverse=True)
        chosen, events = [], set()
        for item in items:
            if item.event_id in events:
                continue
            events.add(item.event_id)
            chosen.append(item)
            if len(chosen) >= self.config["max_news"]:
                break
        return chosen

    # --- аналитика ------------------------------------------------------------

    def _analytics(self, vector: np.ndarray, news: list[Evidence], now: datetime, brief: bool) -> list[Evidence]:
        candidates: dict[str, dict[str, Any]] = {}
        for item in news:
            _, attached = self.analytics.store.event(item.event_id or "")
            for match in attached:
                candidates.setdefault(match.chunk_id, {
                    "origin": "event", "score": match.score, "relation": match.relation,
                    "contradicts": match.contradicts, "reason": match.reason,
                })
        if not brief:
            for doc in self.analytics.index.find_similar(vector, limit=self.config["search_analytics"]):
                candidates.setdefault(str(doc.id), {"origin": "search", "score": 0, "relation": "общая тема"})
        chunks = [chunk for chunk in self.analytics.store.chunks(list(candidates))
                  if chunk.published_at is None or _utc(chunk.published_at) <= now]
        if not chunks:
            return []
        similarity = self.index.encode([f"{c.section_title}\n{c.text}" for c in chunks]) @ vector
        items = []
        for chunk, cosine in zip(chunks, similarity):
            meta = candidates[chunk.chunk_id]
            bonus = 0.1 * meta["score"] / 3
            relevance = (meta["score"] / 3 + 0.01 * float(cosine)) if brief else float(cosine) + bonus
            items.append(Evidence(
                id="", type="analytics", title=_analytics_title(chunk.analytics_title, chunk.section_title),
                text=chunk.text[: self.config["analytics_chars"]].strip(), relevance=round(relevance, 4),
                source="Аналитика Альфа-Банка",
                published_at=chunk.published_at.isoformat() if chunk.published_at else None,
                url=chunk.url, chunk_id=chunk.chunk_id, origin=meta["origin"],
                relation=meta.get("relation"), contradicts=bool(meta.get("contradicts")), why=meta.get("reason"),
            ))
        items.sort(key=lambda item: item.relevance, reverse=True)
        return items[: self.config["max_analytics"]]

    # --- уточнения ------------------------------------------------------------

    def _merge(self, previous: EvidencePack, news: list[Evidence], analytics: list[Evidence],
               best: float) -> EvidencePack:
        """Старые ID сохраняются; новые источники получают следующие номера."""
        items = list(previous.items)
        known_news = {item.news_id for item in items if item.news_id}
        known_chunks = {item.chunk_id for item in items if item.chunk_id}
        limit_news = self.config["max_news"] + 3
        limit_analytics = self.config["max_analytics"] + 2
        for item in news:
            count = len([i for i in items if i.type == "news"])
            if item.news_id not in known_news and count < limit_news and item.relevance >= self.config["min_relevance"]:
                item.id = f"N{count + 1}"
                items.insert(count, item)
        for item in analytics:
            count = len([i for i in items if i.type == "analytics"])
            if item.chunk_id not in known_chunks and count < limit_analytics and item.relevance >= self.config["min_relevance"]:
                item.id = f"A{count + 1}"
                items.append(item)
        items.sort(key=lambda item: ({"news": 0, "analytics": 1, "profile": 2}[item.type], int(item.id[1:] or 0)))
        return EvidencePack(items, relevant=True, best_relevance=round(max(best, previous.best_relevance), 4))
