"""Обогащение новости (ТЗ §5.3, §5.4): сущности, темы, важность.

* :func:`extract_entities` — словарь алиасов (регуляторы, сырьё, валюты,
  страны), регионы и NER natasha (ORG, PER, LOC) для имён вне словаря;
* :func:`classify` — темы таксономии с вероятностями, важность 1–5 и тип
  события одним вызовом LLM на новость (ответ кэшируется по ID новости);
* без LLM — :func:`topics_by_embedding` (zero-shot по косинусу лида с
  прототипами тем: среднее эмбеддингов описания и примеров заголовков)
  и :func:`importance_heuristic`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import numpy as np

from news_pipeline.index.embeddig import embed
from news_pipeline.llm.client import LLM, LLMError, LLMUnavailable, default_llm
from news_pipeline.prompts import prompt
from news_pipeline.recommendation.audience_profile import regions_in_text
from news_pipeline.references import ENTITIES_BY_ID, TOPICS, TOPICS_BY_ID, find_entities, source_rating

EVENT_TYPES = ("отчётность", "сделка", "регуляторика", "решение регулятора", "рыночная динамика",
               "авария или ЧС", "прогноз", "другое")
_MIN_TOPIC_PROBABILITY = 0.15


@dataclass(frozen=True)
class Enrichment:
    """Признаки новости, которые считаются один раз при поступлении."""

    lead: str
    entities: list[str]
    topics: dict[str, float]          # название темы -> вероятность
    importance: int
    event_type: str
    source: str = "rules"             # llm / rules
    numbers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lead": self.lead, "entities": self.entities, "topics": self.topics,
            "importance": self.importance, "event_type": self.event_type,
            "source": self.source, "numbers": self.numbers,
        }


# --- сущности -----------------------------------------------------------------

@lru_cache(maxsize=1)
def _natasha():
    from natasha import Doc, NewsEmbedding, NewsNERTagger, Segmenter

    embedding = NewsEmbedding()
    return Segmenter(), NewsNERTagger(embedding), Doc


def _ner_spans(text: str) -> list[tuple[str, str]]:
    segmenter, tagger, doc_class = _natasha()
    doc = doc_class(text)
    doc.segment(segmenter)
    doc.tag_ner(tagger)
    return [(span.type, span.text) for span in doc.spans]


def extract_entities(title: str, text: str, *, use_ner: bool = True) -> list[str]:
    """Канонические имена сущностей новости.

    Сначала словарь алиасов и регионы; затем NER для ORG и PER, не найденных
    словарём (LOC не добавляются отдельно: страны и регионы уже распознаны
    словарём, а падежные формы городов без морфологии дали бы дубли).
    """
    full = f"{title}\n{text}"
    names = [ENTITIES_BY_ID[entity_id].name for entity_id in find_entities(full)]
    names += sorted(regions_in_text(full))
    if use_ner:
        known = {name.casefold() for name in names}
        for kind, span in _ner_spans(full[:5000]):
            if kind not in ("ORG", "PER"):
                continue
            if find_entities(span):  # алиас словаря — уже учтён
                continue
            name = _clean_entity_name(span)
            if len(name) > 2 and name.casefold() not in known:
                known.add(name.casefold())
                names.append(name)
    return names


_QUOTED_RE = re.compile(r"[«\"]([^»\"]+)")
_LEGAL_FORM_RE = re.compile(
    r"^(?:компания|компании|группа|группы|холдинг|концерн|завод|ооо|оао|зао|пао|ао)\s+", re.IGNORECASE
)


def _clean_entity_name(span: str) -> str:
    """Имя организации без кавычек и организационно-правовой формы."""
    quoted = _QUOTED_RE.search(span)
    name = quoted.group(1) if quoted else span
    name = _LEGAL_FORM_RE.sub("", re.sub(r"\s+", " ", name).strip(" «»\"'.,"))
    return name.strip()


_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")


def key_numbers(text: str) -> list[str]:
    """Числа текста в едином виде (запятая → точка) — для подтверждения склейки и проверки инсайтов."""
    return sorted({number.replace(",", ".") for number in _NUMBER_RE.findall(text)})


# --- темы без LLM -------------------------------------------------------------

@lru_cache(maxsize=1)
def topic_prototypes() -> np.ndarray:
    """Прототип темы — нормированное среднее эмбеддингов описания и примеров заголовков."""
    rows = []
    for topic in TOPICS:
        vectors = embed([topic.description, *topic.examples])
        mean = vectors.mean(axis=0)
        rows.append(mean / (np.linalg.norm(mean) or 1.0))
    return np.vstack(rows)


def topics_by_embedding(lead_vector: np.ndarray, temperature: float = 0.03) -> dict[str, float]:
    """Zero-shot темы: softmax по косинусу лида с прототипами тем, отсечение слабых."""
    scores = topic_prototypes() @ np.asarray(lead_vector)
    weights = np.exp((scores - scores.max()) / temperature)
    probabilities = weights / weights.sum()
    order = np.argsort(-probabilities)
    result = {}
    for index in order[:3]:
        if probabilities[index] >= _MIN_TOPIC_PROBABILITY or not result:
            result[TOPICS[index].name] = round(float(probabilities[index]), 3)
    return result


def importance_heuristic(entities: list[str], topics: dict[str, float], source: str | None) -> int:
    """Важность без LLM: регуляторы, макро-темы, авторитетность источника."""
    score = 2
    regulators = {e.name for e in ENTITIES_BY_ID.values() if e.type == "regulator"}
    if regulators & set(entities):
        score += 1
    from news_pipeline.references import TOPICS_BY_NAME

    if any(TOPICS_BY_NAME[name].kind == "macro" and p >= 0.5 for name, p in topics.items() if name in TOPICS_BY_NAME):
        score += 1
    if source_rating(source) >= 4:
        score += 1
    return max(1, min(5, score))


# --- темы и важность через LLM ------------------------------------------------

_CLASSIFY_PROMPT = prompt("news.classify")

_CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "topics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "enum": [topic.id for topic in TOPICS]},
                    "probability": {"type": "number"},
                },
                "required": ["id", "probability"],
            },
        },
        "importance": {"type": "integer"},
        "event_type": {"type": "string", "enum": list(EVENT_TYPES)},
    },
    "required": ["topics", "importance", "event_type"],
}


def _validate_classification(data: dict[str, Any]) -> dict[str, Any]:
    topics = {}
    for item in data.get("topics") or []:
        topic_id = item.get("id")
        if topic_id in TOPICS_BY_ID:
            topics[TOPICS_BY_ID[topic_id].name] = round(min(max(float(item.get("probability") or 0), 0.0), 1.0), 3)
    if not topics:
        raise LLMError("нет ни одной темы из таксономии")
    importance = int(data.get("importance") or 0)
    if not 1 <= importance <= 5:
        raise LLMError("importance должна быть от 1 до 5")
    event_type = data.get("event_type") if data.get("event_type") in EVENT_TYPES else "другое"
    return {"topics": dict(sorted(topics.items(), key=lambda kv: -kv[1])[:3]),
            "importance": importance, "event_type": event_type}


def classify_with_llm(news_id: str, title: str, lead: str, llm: LLM) -> dict[str, Any]:
    prompt = _CLASSIFY_PROMPT.format(
        event_types=", ".join(EVENT_TYPES),
        topics="\n".join(f"- {t.id} — {t.name} — {t.description}" for t in TOPICS),
    )
    return llm.json(
        "topics",
        [{"role": "system", "content": prompt + "\n/no_think"},
         {"role": "user", "content": f"Заголовок: {title}\n\n{lead}"}],
        schema=_CLASSIFY_SCHEMA, cache_key=news_id, validator=_validate_classification, max_tokens=800,
    )


def enrich(
    news_id: str,
    title: str,
    text: str,
    lead: str,
    *,
    source: str | None = None,
    lead_vector: np.ndarray | None = None,
    llm: LLM | None = None,
    use_llm: bool = True,
    use_ner: bool = True,
) -> Enrichment:
    """Все признаки новости: сущности, темы, важность, тип события, ключевые числа."""
    entities = extract_entities(title, text, use_ner=use_ner)
    numbers = key_numbers(lead)
    if use_llm:
        try:
            data = classify_with_llm(news_id, title, lead, llm or default_llm())
            return Enrichment(lead=lead, entities=entities, topics=data["topics"], importance=data["importance"],
                              event_type=data["event_type"], source="llm", numbers=numbers)
        except (LLMUnavailable, LLMError):
            pass
    vector = lead_vector if lead_vector is not None else embed([lead])[0]
    topics = topics_by_embedding(vector)
    return Enrichment(lead=lead, entities=entities, topics=topics,
                      importance=importance_heuristic(entities, topics, source),
                      event_type="другое", source="rules", numbers=numbers)
