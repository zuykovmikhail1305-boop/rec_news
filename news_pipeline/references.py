"""Справочники ML-команды (ТЗ §2) из ``config/references/``.

* таксономия тем — :data:`TOPICS`, :func:`topics_for_industry`;
* матрица «роль × тема» — :func:`role_topic_weight`;
* словарь сущностей и алиасов — :data:`ENTITIES`, :func:`find_entities`;
* рейтинг источников — :func:`source_rating`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache

from news_pipeline.config import ROOT_DIR

REFERENCES_DIR = ROOT_DIR / "config" / "references"


def _load(name: str) -> dict:
    with (REFERENCES_DIR / name).open(encoding="utf-8") as file:
        return json.load(file)


@dataclass(frozen=True)
class Topic:
    """Тема таксономии."""

    id: str
    name: str
    description: str
    kind: str  # macro / market / sector — от него зависит скорость устаревания аналитики
    industries: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()


@dataclass(frozen=True)
class Entity:
    """Сущность словаря: каноническое имя, тип и алиасы."""

    id: str
    name: str
    type: str
    aliases: tuple[str, ...]
    industries: tuple[str, ...] = ()
    group: str | None = None
    patterns: tuple[re.Pattern, ...] = field(default=(), compare=False, repr=False)


def _alias_pattern(alias: str) -> re.Pattern:
    """Регулярка алиаса: каждое слово — основа с начала слова.

    Пробел в конце алиаса означает, что последнее слово должно закончиться
    (``"газ "`` не совпадёт с «газета»).
    """
    exact_end = alias.endswith(" ")
    words = [re.escape(word) for word in alias.split()]
    pattern = r"(?<!\w)" + r"\w*\s+".join(words) + (r"(?!\w)" if exact_end else "")
    return re.compile(pattern, re.IGNORECASE)


_TAXONOMY = _load("taxonomy.json")
TAXONOMY_VERSION: str = _TAXONOMY["version"]
TOPICS: tuple[Topic, ...] = tuple(
    Topic(
        id=item["id"],
        name=item["name"],
        description=item["description"],
        kind=item["kind"],
        industries=tuple(item.get("industries", ())),
        examples=tuple(item.get("examples", ())),
    )
    for item in _TAXONOMY["topics"]
)
TOPICS_BY_ID = {topic.id: topic for topic in TOPICS}
TOPICS_BY_NAME = {topic.name: topic for topic in TOPICS}

_ROLE_TOPICS = _load("role_topics.json")
ROLE_TOPICS_VERSION: str = _ROLE_TOPICS["version"]
ROLES: tuple[str, ...] = tuple(_ROLE_TOPICS["roles"])

_ENTITIES = _load("entities.json")
ENTITIES_VERSION: str = _ENTITIES["version"]
ENTITIES: tuple[Entity, ...] = tuple(
    Entity(
        id=item["id"],
        name=item["name"],
        type=item["type"],
        aliases=tuple(item["aliases"]),
        industries=tuple(item.get("industries", ())),
        group=item.get("group"),
        patterns=tuple(_alias_pattern(alias) for alias in item["aliases"]),
    )
    for item in _ENTITIES["entities"]
)
ENTITIES_BY_ID = {entity.id: entity for entity in ENTITIES}
ENTITIES_BY_NAME = {entity.name: entity for entity in ENTITIES}

_SOURCES = _load("sources.json")


def topics_for_industry(industry: str) -> list[Topic]:
    """Темы, связанные с отраслью шаблона ``audience_profile.INDUSTRIES``."""
    return [topic for topic in TOPICS if industry in topic.industries]


def role_topic_weight(role: str | None, topic_id: str) -> float:
    """Априорная важность темы для роли (для неизвестной роли — значение по умолчанию)."""
    weights = _ROLE_TOPICS["roles"].get(role or "", {})
    return float(weights.get(topic_id, _ROLE_TOPICS["default"]))


def role_topic_weights(role: str | None) -> dict[str, float]:
    """Веса всех тем таксономии для роли: ``{topic_id: вес}``."""
    return {topic.id: role_topic_weight(role, topic.id) for topic in TOPICS}


@lru_cache(maxsize=4096)
def find_entities(text: str) -> tuple[str, ...]:
    """ID сущностей словаря, упомянутых в тексте (в порядке словаря)."""
    return tuple(
        entity.id for entity in ENTITIES if any(p.search(text) for p in entity.patterns)
    )


def entities_for_industry(industry: str) -> list[Entity]:
    """Сырьё и товары, важные для отрасли (поле ``industries`` словаря)."""
    return [entity for entity in ENTITIES if industry in entity.industries]


def entity_for_country_group(group: str) -> Entity | None:
    """Сущность-страна для группы стран ВЭД из шаблона."""
    for entity in ENTITIES:
        if entity.type == "country" and entity.group == group:
            return entity
    return None


def entity_key(name: str, prefix: int = 5) -> str:
    """Ключ сущности для сравнения без морфологии: первые буквы каждого слова.

    «Аркадий Ветров» и «Аркадия Ветрова» дают один ключ «аркад ветро».
    """
    words = re.findall(r"\w+", name.casefold().replace("ё", "е"))
    return " ".join(word[:prefix] for word in words)


def source_rating(source: str | None) -> int:
    """Авторитетность источника 1–5."""
    return int(_SOURCES["sources"].get((source or "").lower(), _SOURCES["default"]))
