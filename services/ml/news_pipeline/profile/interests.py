"""Профиль интересов пользователя (ТЗ §4).

Анкета (:class:`schemas.UserProfileIn`) превращается в
:class:`schemas.InterestProfile`: аспекты интересов с векторами, сущности
с весами и веса тем таксономии.

1. **База** — детерминированный профиль компании (:class:`AudienceProfile`):
   из датасета по ``company_id`` (отрасль, контрагенты по платежам, ВЭД,
   продукты банка, регион) и из полей анкеты.
2. **Аспекты** — LLM по роли, описанию компании и базе (8–15 штук, JSON-схема).
   Валидация: тема из таксономии, есть описание, темы не повторяются, косинус
   между аспектами < ``max_aspect_cosine``. Если LLM недоступна или вернула
   мало годных аспектов, аспекты достраиваются по правилам из базы, матрицы
   «роль × тема» и примеров заголовков таксономии.
3. **Векторы** — среднее эмбеддингов описания и примеров заголовков.
4. **Сущности** — с весами из ``[profile] entity_weights`` конфига.
5. **Веса тем** — максимум из матрицы «роль × тема» и весов аспектов темы.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import numpy as np

from news_pipeline.index.embeddig import embed
from news_pipeline.llm.client import LLM, LLMError, LLMUnavailable, default_llm
from news_pipeline.prompts import prompt
from news_pipeline.recommendation.audience_profile import INDUSTRIES, AudienceProfile
from news_pipeline.references import (
    ENTITIES_BY_ID,
    TOPICS,
    TOPICS_BY_ID,
    entities_for_industry,
    entity_for_country_group,
    role_topic_weights,
    topics_for_industry,
)
from news_pipeline.schemas import Aspect, InterestProfile, UserProfileIn
from news_pipeline.settings import SETTINGS

_CONFIG = SETTINGS.profile


@dataclass(frozen=True)
class _TopicScore:
    topic_id: str
    weight: float
    reason: str


def base_profile(
    user: UserProfileIn,
    *,
    companies: Any | None = None,
    payments: Any | None = None,
) -> AudienceProfile:
    """Детерминированный профиль компании: из датасета по ``company_id`` и из анкеты."""
    profile = AudienceProfile(roles=(user.role,) if user.role else ())
    if user.company_id and companies is not None:
        from news_pipeline.recommendation.recommender import profile_for_company

        profile = profile_for_company(user.company_id, user.role, companies=companies, payments=payments)
    updates: dict[str, list[str]] = {}
    if user.industry and not profile.industries:
        industry = _closest_industry(user.industry)
        if industry:
            updates["industries"] = [industry]
    if user.regions and not profile.regions:
        updates["regions"] = user.regions
    if user.products and not profile.bank_products:
        updates["bank_products"] = user.products
    return profile.with_values(**updates) if updates else profile


def _closest_industry(text: str) -> str | None:
    """Отрасль шаблона, ближайшая по смыслу к свободному описанию отрасли."""
    if text in INDUSTRIES:
        return text
    vectors = embed([text, *INDUSTRIES])
    scores = vectors[1:] @ vectors[0]
    best = int(np.argmax(scores))
    return INDUSTRIES[best] if scores[best] >= 0.5 else None


def _candidate_topics(user: UserProfileIn, base: AudienceProfile) -> dict[str, _TopicScore]:
    """Темы с весами и причиной: матрица ролей, отрасли, ВЭД, продукты, регион."""
    scores: dict[str, _TopicScore] = {}

    def add(topic_id: str, weight: float, reason: str) -> None:
        if topic_id not in scores or weight > scores[topic_id].weight:
            scores[topic_id] = _TopicScore(topic_id, round(weight, 3), reason)

    for topic_id, weight in role_topic_weights(user.role).items():
        add(topic_id, weight, f"роль «{user.role}»")
    for industry in base.industries:
        for topic in topics_for_industry(industry):
            add(topic.id, 1.0, f"отрасль компании «{industry}»")
    for industry in (*base.supplier_industries, *base.customer_industries):
        for topic in topics_for_industry(industry):
            side = "поставщиков" if industry in base.supplier_industries else "покупателей"
            add(topic.id, 0.7, f"отрасль {side} «{industry}»")
    if "есть" in base.foreign_trade:
        countries = ", ".join(base.trade_countries) or "зарубежные страны"
        add("foreign_trade", 0.9, f"ВЭД: {countries}")
        add("fx", 0.85, f"валютные расчёты по ВЭД: {countries}")
        add("sanctions", 0.75, f"платежи и поставки по ВЭД: {countries}")
    products = set(base.bank_products)
    if products & {"кредиты", "лизинг"}:
        add("monetary_policy", 0.9, "кредиты и лизинг — стоимость долга зависит от ставки")
        add("banking", 0.85, "кредиты и лизинг в банке")
    if "депозиты" in products:
        add("monetary_policy", 0.7, "депозиты — доходность зависит от ставки")
    if "банковские гарантии" in products:
        add("budget", 0.6, "банковские гарантии — участие в госзаказе")
    if base.regions:
        add("regions", 0.5, f"регион: {', '.join(base.regions)}")
    return scores


def profile_entities(user: UserProfileIn, base: AudienceProfile) -> dict[str, float]:
    """Сущности профиля с весами (ТЗ §4, шаг 4)."""
    weights = _CONFIG["entity_weights"]
    entities: dict[str, float] = {}

    def add(name: str, weight: float) -> None:
        if name and weight > entities.get(name, 0.0):
            entities[name] = weight

    if user.company_name:
        add(user.company_name, weights["company"])
    for name in user.competitors:
        add(name, weights["competitor"])
    for name in user.counterparties:
        add(name, weights["counterparty"])
    for industry in base.industries:
        for entity in entities_for_industry(industry):
            add(entity.name, weights["commodity"])
    for industry in (*base.supplier_industries, *base.customer_industries):
        for entity in entities_for_industry(industry):
            add(entity.name, weights["commodity"] * 0.8)
    for group in base.trade_countries:
        entity = entity_for_country_group(group)
        if entity is not None:
            add(entity.name, weights["country"])
            for currency in (e for e in ENTITIES_BY_ID.values() if e.type == "currency" and e.group == group):
                add(currency.name, weights["country"] * 0.8)
    if set(base.bank_products) & {"кредиты", "лизинг", "депозиты"}:
        add(ENTITIES_BY_ID["key_rate"].name, weights["commodity"])
    for region in base.regions:
        add(region, weights["region"])
    return dict(sorted(entities.items(), key=lambda item: -item[1]))


# --- аспекты по правилам -----------------------------------------------------


def _rule_aspects(candidates: dict[str, _TopicScore], base: AudienceProfile, limit: int) -> list[Aspect]:
    """Аспекты без LLM: темы с наибольшим весом, описание и примеры — из таксономии."""
    chosen = sorted(candidates.values(), key=lambda item: -item.weight)[:limit]
    aspects = []
    for item in chosen:
        topic = TOPICS_BY_ID[item.topic_id]
        aspects.append(
            Aspect(
                aspect_id=f"{topic.id}",
                title=topic.name,
                description=f"{topic.description} Важно, потому что {item.reason}.",
                examples=list(topic.examples),
                keywords=_topic_keywords(topic.id, base),
                entities=[],
                topic=topic.name,
                weight=item.weight,
            )
        )
    return aspects


def _topic_keywords(topic_id: str, base: AudienceProfile) -> list[str]:
    topic = TOPICS_BY_ID[topic_id]
    words = [topic.name, *[industry for industry in topic.industries if industry in base.industries]]
    return list(dict.fromkeys(words))


# --- аспекты через LLM -------------------------------------------------------

_ASPECTS_PROMPT = prompt("profile.aspects")

_ASPECTS_SCHEMA = {
    "type": "object",
    "properties": {
        "aspects": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "aspect_id": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "examples": {"type": "array", "items": {"type": "string"}},
                    "keywords": {"type": "array", "items": {"type": "string"}},
                    "entities": {"type": "array", "items": {"type": "string"}},
                    "topic": {"type": "string", "enum": [topic.id for topic in TOPICS]},
                    "weight": {"type": "number"},
                },
                "required": ["aspect_id", "title", "description", "examples", "keywords", "entities", "topic", "weight"],
            },
        }
    },
    "required": ["aspects"],
}


def _user_description(user: UserProfileIn, base: AudienceProfile) -> str:
    lines = [f"Должность: {user.role}"]
    if user.company_name:
        lines.append(f"Компания: {user.company_name}")
    if user.company_description:
        lines.append(f"Описание компании: {user.company_description}")
    if user.competitors:
        lines.append(f"Конкуренты: {', '.join(user.competitors)}")
    if user.counterparties:
        lines.append(f"Контрагенты: {', '.join(user.counterparties)}")
    lines.append("Данные банка о компании:\n" + base.with_values(roles=[]).to_embedding_text())
    return "\n".join(lines)


def _validate_aspects(data: dict[str, Any]) -> dict[str, Any]:
    """Проверка ответа LLM: тема из таксономии, есть описание и примеры, темы не повторяются."""
    aspects = data.get("aspects")
    if not isinstance(aspects, list):
        raise LLMError("нет списка aspects")
    by_topic: dict[str, dict[str, Any]] = {}
    for item in aspects:
        topic = item.get("topic")
        description = str(item.get("description") or "").strip()
        if topic not in TOPICS_BY_ID or not description:
            continue
        examples = [str(x).strip() for x in item.get("examples") or [] if str(x).strip()][:3]
        if not examples:
            continue
        weight = min(max(float(item.get("weight") or 0.5), 0.0), 1.0)
        candidate = {
            "aspect_id": re.sub(r"[^a-z0-9_]+", "_", str(item.get("aspect_id") or topic).lower()).strip("_") or topic,
            "title": str(item.get("title") or TOPICS_BY_ID[topic].name).strip(),
            "description": description,
            "examples": examples,
            "keywords": [str(x) for x in item.get("keywords") or []][:6],
            "entities": [str(x) for x in item.get("entities") or []][:8],
            "topic": topic,
            "weight": weight,
        }
        if topic not in by_topic or weight > by_topic[topic]["weight"]:
            by_topic[topic] = candidate
    if not by_topic:
        raise LLMError("ни одного аспекта с темой из таксономии и описанием")
    return {"aspects": list(by_topic.values())}


def _llm_aspects(user: UserProfileIn, base: AudienceProfile, llm: LLM) -> list[Aspect]:
    prompt = _ASPECTS_PROMPT.format(
        min_n=_CONFIG["min_aspects"],
        max_n=_CONFIG["max_aspects"],
        topics="\n".join(f"- {topic.id} — {topic.name}" for topic in TOPICS),
    )
    description = _user_description(user, base)
    data = llm.json(
        "aspects",
        [{"role": "system", "content": prompt}, {"role": "user", "content": description}],
        schema=_ASPECTS_SCHEMA,
        cache_key=user.user_id,  # хеш промпта добавляет сам LLM-клиент
        validator=_validate_aspects,
        max_tokens=4000,
    )
    return [
        Aspect(**{**item, "topic": TOPICS_BY_ID[item["topic"]].name}) for item in data["aspects"]
    ]


# --- сборка профиля ----------------------------------------------------------


def _embed_aspects(aspects: list[Aspect]) -> list[Aspect]:
    """Вектор аспекта — нормированное среднее эмбеддингов описания и примеров."""
    texts, owners = [], []
    for index, aspect in enumerate(aspects):
        for text in (aspect.description, *aspect.examples):
            texts.append(text)
            owners.append(index)
    vectors = embed(texts)
    result = []
    for index, aspect in enumerate(aspects):
        mean = vectors[[i for i, owner in enumerate(owners) if owner == index]].mean(axis=0)
        mean = mean / (np.linalg.norm(mean) or 1.0)
        result.append(aspect.model_copy(update={"embedding": mean.round(6).tolist()}))
    return result


def _drop_similar(aspects: list[Aspect], max_cosine: float) -> list[Aspect]:
    """Оставляет аспекты в порядке убывания веса, пропуская слишком похожие на уже взятые."""
    kept: list[Aspect] = []
    for aspect in sorted(aspects, key=lambda item: -item.weight):
        vector = np.asarray(aspect.embedding)
        if all(float(vector @ np.asarray(other.embedding)) < max_cosine for other in kept):
            kept.append(aspect)
    return kept


def build_interest_profile(
    user: UserProfileIn,
    *,
    companies: Any | None = None,
    payments: Any | None = None,
    llm: LLM | None = None,
    use_llm: bool = True,
) -> InterestProfile:
    """Строит профиль интересов: аспекты (LLM или правила), сущности, веса тем."""
    base = base_profile(user, companies=companies, payments=payments)
    candidates = _candidate_topics(user, base)
    min_n, max_n = _CONFIG["min_aspects"], _CONFIG["max_aspects"]

    aspects: list[Aspect] = []
    source = "rules"
    if use_llm:
        try:
            aspects = _llm_aspects(user, base, llm or default_llm())
            source = "llm"
        except (LLMUnavailable, LLMError):
            aspects = []
    aspects = _drop_similar(_embed_aspects(aspects), _CONFIG["max_aspect_cosine"]) if aspects else []
    if len(aspects) < min_n:
        # Достраиваем правилами темы, которых у LLM-аспектов нет.
        have = {aspect.topic for aspect in aspects}
        extra = {
            topic_id: score
            for topic_id, score in candidates.items()
            if TOPICS_BY_ID[topic_id].name not in have
        }
        rule = _embed_aspects(_rule_aspects(extra, base, max_n - len(aspects)))
        aspects = _drop_similar(aspects + rule, _CONFIG["max_aspect_cosine"])
    aspects = sorted(aspects, key=lambda item: -item.weight)[:max_n]

    topic_weights = {TOPICS_BY_ID[topic_id].name: score.weight for topic_id, score in candidates.items()}
    for aspect in aspects:
        topic_weights[aspect.topic] = max(topic_weights.get(aspect.topic, 0.0), aspect.weight)
    return InterestProfile(
        user_id=user.user_id,
        role=user.role,
        aspects=aspects,
        entities=profile_entities(user, base),
        topic_weights=dict(sorted(topic_weights.items(), key=lambda item: -item[1])),
        source=source,
        pipeline_version=SETTINGS.pipeline_version,
    )
