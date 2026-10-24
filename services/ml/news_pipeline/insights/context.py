"""Входные данные карточки инсайта: что видят агенты блоков и проверщик.

* ``N1…`` — публикации о событии: ``N1`` — представитель события из
  ``unique.db`` (полный текст), остальные — дубли и пересказы из
  ``new_summary.db`` (лид), по одной на издание, по убыванию надёжности;
* ``A1`` — фрагмент аналитики Альфа-Банка, выбранный под пользователя
  (тот же, что в ``user_news``; если рекомендаций ещё нет — подбирается сейчас);
* ``F1…`` — данные о компании (:mod:`insights.facts`);
* профиль: роль, компания, интересы.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from news_pipeline.config import SUMMARY_DB, UNIQUE_DB
from news_pipeline.insights.facts import CompanyFact
from news_pipeline.insights.numbers import numbers_in
from news_pipeline.preprocessing.cleaning import sentences
from news_pipeline.references import source_rating
from news_pipeline.schemas import FeedAnalytics, InterestProfile, UserProfileIn
from news_pipeline.settings import SETTINGS
from news_pipeline.storage.news_db import read_summaries

if TYPE_CHECKING:
    from news_pipeline.service import MLService


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _date_forms(value: datetime | None) -> str:
    """Дата в видах, в которых её может написать модель: 2026-10-15 15.10.2026 15."""
    return f"{value:%Y-%m-%d} {value:%d.%m.%Y} {value.day} {value.year}" if value else ""


@dataclass
class SourceDoc:
    id: str
    type: str                      # news | analytics
    title: str
    text: str
    outlet: str | None = None
    url: str | None = None
    published_at: datetime | None = None
    reliability: int = 3
    section_title: str | None = None
    document_id: str | None = None

    def prompt(self, chars: int) -> str:
        date = f", {self.published_at:%d.%m.%Y}" if self.published_at else ""
        where = f"{self.outlet}{date}" if self.outlet else date.lstrip(", ")
        text = self.text if len(self.text) <= chars else self.text[:chars].rsplit(" ", 1)[0] + "…"
        return f"[{self.id}] «{self.title}» ({where}; надёжность {self.reliability}/5)\n{text}"


@dataclass
class CardContext:
    user_id: str
    news_id: str
    event_id: str | None
    title: str
    published_at: datetime | None
    profile: InterestProfile
    user: UserProfileIn | None
    news: list[SourceDoc]
    analytics: SourceDoc | None
    analytics_match: FeedAnalytics | None
    facts: list[CompanyFact]
    why_relevant: str | None = None
    rank: int | None = None
    score: float | None = None
    importance: int | None = None
    methods: dict[str, str] = field(default_factory=dict)   # document_id → original | exact | semantic …

    # --- доступ к источникам -----------------------------------------------------

    @property
    def company(self) -> str | None:
        return self.user.company_name if self.user else None

    @property
    def outlets(self) -> list[str]:
        return list(dict.fromkeys(doc.outlet for doc in self.news if doc.outlet))

    def source(self, source_id: str) -> SourceDoc | None:
        if self.analytics is not None and source_id == self.analytics.id:
            return self.analytics
        return next((doc for doc in self.news if doc.id == source_id), None)

    def fact(self, fact_id: str) -> CompanyFact | None:
        return next((item for item in self.facts if item.id == fact_id), None)

    def ids(self, groups: tuple[str, ...]) -> set[str]:
        result: set[str] = set()
        if "event" in groups:
            result.add(self.news[0].id)
        if "news" in groups:
            result |= {doc.id for doc in self.news}
        if "analytics" in groups and self.analytics is not None:
            result.add(self.analytics.id)
        if "facts" in groups:
            result |= {item.id for item in self.facts}
        return result

    def numbers(self, groups: tuple[str, ...]) -> set[str]:
        """Числа, которые агент блока может использовать: из видимых ему источников.

        Готовые блоки (``blocks``) построены по всем источникам — с ними доступны все числа.
        """
        if "blocks" in groups:
            groups = ("news", "analytics", "facts")
        parts: list[str] = []
        if "event" in groups and "news" not in groups:
            parts.append(self.event_text())
        if "news" in groups:
            parts += [f"{doc.title}\n{doc.text}\n{_date_forms(doc.published_at)}" for doc in self.news]
        if "analytics" in groups and self.analytics is not None:
            parts.append(f"{self.analytics.title}\n{self.analytics.text}\n{_date_forms(self.analytics.published_at)}")
        if "facts" in groups:
            parts += [item.text for item in self.facts]
        parts.append(self.profile_text())
        return numbers_in("\n".join(parts))

    # --- тексты для промптов -----------------------------------------------------

    def profile_text(self) -> str:
        lines = [f"Роль читателя: {self.profile.role}"]
        if self.company:
            lines.append(f"Компания: {self.company}")
        if self.profile.aspects:
            lines.append("Интересы: " + "; ".join(aspect.title for aspect in self.profile.aspects[:8]))
        return "\n".join(lines)

    def facts_text(self) -> str:
        if not self.facts:
            return "Данных о компании нет — выводы только по роли и интересам."
        return "\n".join(f"[{item.id}] {item.text}" for item in self.facts)

    def news_text(self, chars_main: int, chars_other: int) -> str:
        return "\n\n".join(doc.prompt(chars_main if index == 0 else chars_other) for index, doc in enumerate(self.news))

    def event_text(self) -> str:
        """О каком событии речь: заголовок и лид N1 — для блоков, которым не нужен полный текст."""
        doc = self.news[0]
        date = f" ({doc.published_at:%d.%m.%Y})" if doc.published_at else ""
        return f"[{doc.id}] «{doc.title}»{date}\n" + " ".join(sentences(doc.text)[:3])

    def analytics_text(self, chars: int) -> str:
        if self.analytics is None:
            return "Аналитики Альфа-Банка по теме нет."
        note = ""
        if self.analytics_match is not None:
            note = f"\nСвязь с новостью: {self.analytics_match.relation}."
            if self.analytics_match.contradicts:
                note += " Аналитика банка расходится с новостью — это нужно сказать прямо."
        return self.analytics.prompt(chars) + note

    def number_sources(self, text: str) -> dict[str, list[str]]:
        """Где встречается каждое число текста: ID публикаций, аналитики и фактов о компании."""
        docs = [(doc.id, f"{doc.title}\n{doc.text}\n{_date_forms(doc.published_at)}")
                for doc in [*self.news, *([self.analytics] if self.analytics else [])]]
        docs += [(item.id, item.text) for item in self.facts]
        found = {doc_id: numbers_in(body) for doc_id, body in docs}
        return {number: [doc_id for doc_id, numbers in found.items() if number in numbers]
                for number in sorted(numbers_in(text))}

    def fingerprint(self) -> str:
        """Меняется, если поменялись входные данные карточки: тогда кэш карточки не используется."""
        payload = {
            "profile": self.profile.model_dump(exclude={"aspects": {"__all__": {"embedding"}}}),
            "user": self.user.model_dump() if self.user else None,
            "news": [doc.document_id for doc in self.news],
            "analytics": self.analytics.document_id if self.analytics else None,
            "facts": [item.text for item in self.facts],
            "prompts": {k: v for k, v in SETTINGS.prompts.items() if k.startswith("insight")},
            "pipeline": SETTINGS.pipeline_version,
        }
        return hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def build_context(service: "MLService", user_id: str, news_id: str) -> CardContext:
    """Собирает входные данные карточки. ``KeyError`` — нет профиля или новости."""
    profile = service.profiles.get(user_id)
    if profile is None:
        raise KeyError(user_id)
    rows = read_summaries(UNIQUE_DB, [news_id])
    if not rows:
        raise LookupError(news_id)
    row = rows[0]
    config = SETTINGS.insight_card
    event_id = row.metadata.get("event_id") or f"e_{news_id}"

    # публикации о событии: представитель + по одной на издание из дублей
    news = [SourceDoc(
        id="N1", type="news", title=row.title, text=row.text, outlet=row.metadata.get("source"), url=row.url,
        published_at=_parse_time(row.publication_time), reliability=source_rating(row.metadata.get("source")),
        document_id=news_id,
    )]
    methods = {news_id: "original"}
    entries = [entry for entry in row.metadata.get("sources") or [] if entry.get("document_id") != news_id]
    duplicates = {item.document_id: item for item in read_summaries(SUMMARY_DB, [e["document_id"] for e in entries])}
    seen = {news[0].outlet}
    entries.sort(key=lambda entry: -source_rating(entry.get("source")))
    for entry in entries:
        outlet = entry.get("source")
        duplicate = duplicates.get(entry["document_id"])
        if duplicate is None or outlet in seen or len(news) >= config["max_sources"]:
            continue
        seen.add(outlet)
        methods[duplicate.document_id] = str(entry.get("method") or "")
        news.append(SourceDoc(
            id=f"N{len(news) + 1}", type="news", title=duplicate.title, text=duplicate.lead or duplicate.text,
            outlet=outlet, url=entry.get("url") or duplicate.url,
            published_at=_parse_time(entry.get("publication_time") or duplicate.publication_time),
            reliability=source_rating(outlet), document_id=duplicate.document_id,
        ))

    # аналитика: та же, что выбрана в рекомендациях; иначе подбор сейчас
    user_row = service.user_news_store.row(user_id, news_id)
    match: FeedAnalytics | None = None
    if user_row is not None and user_row.analytics_chunk_id:
        chunks = service.analytics.store.chunks([user_row.analytics_chunk_id])
        if chunks:
            chunk = chunks[0]
            match = FeedAnalytics(
                analytics_id=chunk.analytics_id, chunk_id=chunk.chunk_id, title=chunk.analytics_title,
                section_title=chunk.section_title, published_at=chunk.published_at,
                relation=user_row.analytics_relation or "общая тема",
                contradicts=bool(user_row.analytics_contradicts), summary=user_row.analytics_summary or "",
                url=chunk.url,
            )
    elif user_row is None:
        match = service.user_analytics(profile, event_id, row)
    analytics = None
    if match is not None:
        chunks = service.analytics.store.chunks([match.chunk_id])
        if chunks:
            chunk = chunks[0]
            analytics = SourceDoc(
                id="A1", type="analytics", title=chunk.analytics_title, text=chunk.text, outlet="Альфа Ресерч",
                url=chunk.url, published_at=chunk.published_at, reliability=5, section_title=chunk.section_title,
                document_id=chunk.chunk_id,
            )

    user = service.profiles.get_input(user_id)
    facts = service.company_facts(user) if user else []
    return CardContext(
        user_id=user_id, news_id=news_id, event_id=event_id, title=row.title,
        published_at=_parse_time(row.publication_time), profile=profile, user=user, news=news,
        analytics=analytics, analytics_match=match if analytics else None, facts=facts,
        why_relevant=user_row.why_relevant if user_row else None, rank=user_row.rank if user_row else None,
        score=user_row.score if user_row else None,
        importance=(row.metadata.get("enrichment") or {}).get("importance"), methods=methods,
    )
