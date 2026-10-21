"""Синтетические бизнес-новости для разработки и gold-сетов.

Генерация идёт в четыре шага:

1. **Сценарии** — LLM придумывает по ``events_per_topic`` событий на каждую
   тему таксономии: заголовок, 3–5 фактов с числами, сущности, регион,
   важность, сдвиг даты в днях от конца периода.
2. **Статьи** — LLM пишет по сценарию новость на 900–1400 символов.
3. **Копии** — для части событий:
   * ``near_duplicate`` — перепечатка мелкими правками текста (без LLM);
   * ``semantic_duplicate`` — пересказ своими словами (LLM);
   * ``continuation`` — развитие события с новыми фактами через 2–6 дней (LLM).
4. **Разметка** — у каждой новости в ``metadata["test"]``: связь, исходная
   новость, ключ события, тема сценария и важность сценария.

Ответы LLM кэшируются (``data/llm_cache.db``), поэтому прерванную генерацию
можно запустить снова — готовые шаги не повторяются. Результат —
``fixtures/synthetic_news.jsonl``; загрузка в ``news.db`` — :func:`load_into_news_db`.
"""

from __future__ import annotations

import json
import random
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from news_pipeline.config import FIXTURES_DIR
from news_pipeline.llm.client import LLM, LLMError, default_llm
from news_pipeline.references import TOPICS, Topic
from news_pipeline.storage.news_db import StoredNews, insert_news

OUTPUT_PATH = FIXTURES_DIR / "synthetic_news.jsonl"
PERIOD_END = datetime(2026, 10, 21, 18, 0, tzinfo=timezone.utc)
SOURCES = (
    "interfax.example", "tass.example", "rbc.example", "kommersant.example", "vedomosti.example",
    "business-herald.example", "frank-media.example", "region-news.example", "news-aggregator.example",
)

_NO_THINK = "\n/no_think"


@dataclass
class SyntheticNews:
    key: str
    title: str
    text: str
    source: str
    published_at: datetime
    relation: str = "original"
    related_to: str | None = None
    event_key: str = ""
    topic: str = ""
    importance: int = 3
    entities: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key, "title": self.title, "text": self.text, "source": self.source,
            "published_at": self.published_at.isoformat(), "relation": self.relation,
            "related_to": self.related_to, "event_key": self.event_key, "topic": self.topic,
            "importance": self.importance, "entities": self.entities,
        }


# --- шаг 1: сценарии событий --------------------------------------------------

_SCENARIO_PROMPT = """Ты редактор деловых новостей в России. Придумай {n} разных правдоподобных \
событий по теме «{topic}» ({description}) за последние две недели октября 2026 года.

Каждое событие:
- title — заголовок новости;
- facts — 3–5 конкретных фактов с числами, датами и участниками (не меньше двух чисел);
- entities — организации, товары, страны, валюты из события;
- region — регион России, если событие региональное, иначе пустая строка;
- importance — масштаб для крупного бизнеса 1–5 (5 — решение ЦБ или правительства \
для всей экономики, 1 — локальная новость одной компании);
- days_ago — сколько дней назад вышла новость, 0–13.

События должны быть разными по масштабу и участникам. Компании выдуманные, но \
регуляторы (Банк России, Минфин, ФНС) и рынки — реальные. События не должны \
противоречить фону:
{world}
Ответ — JSON."""

# Общий фон синтетического мира — согласован с аналитикой Альфа-Банка в texts/.
WORLD = """- ключевая ставка Банка России — 14% годовых, совет директоров сохранил её 11 сентября \
2026 года после цикла снижений; следующее заседание — 24 октября 2026 года, в октябре решений \
по ставке не было;
- инфляция замедляется, но остаётся выше цели 4%; рост ВВП слабый, рынок труда перегрет;
- курс юаня около 11–12 рублей, доллара — около 80–90 рублей;
- действуют санкции, расчёты с зарубежными партнёрами идут в основном в юанях и через \
третьи страны."""

_SCENARIO_SCHEMA = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "facts": {"type": "array", "items": {"type": "string"}},
                    "entities": {"type": "array", "items": {"type": "string"}},
                    "region": {"type": "string"},
                    "importance": {"type": "integer"},
                    "days_ago": {"type": "integer"},
                },
                "required": ["title", "facts", "entities", "region", "importance", "days_ago"],
            },
        }
    },
    "required": ["events"],
}


def _validate_events(data: dict[str, Any]) -> dict[str, Any]:
    events = [
        event for event in data.get("events") or []
        if str(event.get("title") or "").strip() and len(event.get("facts") or []) >= 2
    ]
    if not events:
        raise LLMError("нет событий с заголовком и фактами")
    return {"events": events}


def scenarios(topic: Topic, n: int, llm: LLM) -> list[dict[str, Any]]:
    data = llm.json(
        "synthetic_scenarios",
        [{"role": "system", "content": _SCENARIO_PROMPT.format(n=n, topic=topic.name, description=topic.description, world=WORLD) + _NO_THINK},
         {"role": "user", "content": f"Тема: {topic.name}"}],
        schema=_SCENARIO_SCHEMA, cache_key=f"{topic.id}:{n}", validator=_validate_events, max_tokens=3000,
    )
    return data["events"][:n]


# --- шаг 2–3: тексты ----------------------------------------------------------

_ARTICLE_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "text": {"type": "string"}},
    "required": ["title", "text"],
}

_ARTICLE_PROMPT = """Ты журналист делового издания. Напиши новость на русском языке по фактам. \
Объём текста 900–1400 символов, 3–5 абзацев, абзацы разделяй пустой строкой. Первый абзац — \
главное: кто, что, когда, ключевые числа. Дальше — детали, комментарии участников, \
последствия для рынка. Используй все факты и не противоречь им. Новость \
опубликована {date} — даты в тексте не позже неё. Ответ — JSON с полями title и text."""

_PARAPHRASE_PROMPT = """Ты журналист другого издания. Перескажи новость своими словами: \
другой заголовок, другая структура и формулировки, но те же факты и числа, без новых \
фактов. Объём 800–1300 символов, абзацы разделяй пустой строкой. Ответ — JSON с полями \
title и text."""

_CONTINUATION_PROMPT = """Ты журналист делового издания. Напиши продолжение сюжета: что \
произошло через {days} дн. после этой новости. Нужны новые факты и новые числа (развитие \
событий, реакция рынка, решения участников), а не пересказ. Объём 800–1300 символов, абзацы \
разделяй пустой строкой. Ответ — JSON с полями title и text."""


def _validate_article(data: dict[str, Any]) -> dict[str, Any]:
    title, text = str(data.get("title") or "").strip(), str(data.get("text") or "").strip()
    if not title or len(text) < 400:
        raise LLMError("статья пустая или короче 400 символов")
    text = re.sub(r"\n{3,}", "\n\n", text.replace("\\n", "\n"))
    if "\n\n" not in text:  # модель написала одним абзацем — делим по предложениям
        parts = re.split(r"(?<=[.!?])\s+", text)
        text = "\n\n".join(" ".join(parts[i:i + 2]) for i in range(0, len(parts), 2))
    return {"title": title, "text": text}


def _write(task: str, prompt: str, content: str, key: str, llm: LLM) -> dict[str, str]:
    return llm.json(
        task, [{"role": "system", "content": prompt + _NO_THINK}, {"role": "user", "content": content}],
        schema=_ARTICLE_SCHEMA, cache_key=key, validator=_validate_article, max_tokens=2500,
    )


_NEAR_EDITS = (
    ("«", '"'), ("»", '"'), (" процентов", "%"), (" миллиардов", " млрд"), (" миллионов", " млн"),
    (" рублей", " руб."), ("сообщил", "заявил"), ("Банк России", "ЦБ"),
)


def near_copy(text: str, rng: random.Random) -> str:
    """Перепечатка: мелкие правки оформления и одна-две замены слов."""
    for old, new in _NEAR_EDITS:
        if old in text and rng.random() < 0.7:
            text = text.replace(old, new)
    paragraphs = text.split("\n\n")
    if len(paragraphs) > 3 and rng.random() < 0.5:
        paragraphs = paragraphs[:-1]  # перепечатки часто обрезают последний абзац
    return "\n\n".join(paragraphs)


# --- сборка -------------------------------------------------------------------


def generate(
    *,
    events_per_topic: int = 5,
    near_share: float = 0.25,
    paraphrase_share: float = 0.2,
    continuation_share: float = 0.2,
    workers: int = 3,
    seed: int = 42,
    llm: LLM | None = None,
    output_path: Path = OUTPUT_PATH,
    topics: tuple[Topic, ...] = TOPICS,
    log: Any = print,
) -> list[SyntheticNews]:
    """Генерирует корпус и пишет его в ``output_path`` (JSON Lines)."""
    llm = llm or default_llm()
    rng = random.Random(seed)

    with ThreadPoolExecutor(workers) as pool:
        per_topic = list(pool.map(lambda topic: (topic, scenarios(topic, events_per_topic, llm)), topics))
    seeds = [(topic, index, event) for topic, events in per_topic for index, event in enumerate(events)]
    log(f"сценариев: {len(seeds)}")

    def article(item: tuple[Topic, int, dict[str, Any]]) -> SyntheticNews | None:
        topic, index, event = item
        key = f"{topic.id}_{index}"
        facts = "\n".join(f"- {fact}" for fact in event["facts"])
        # Дата и источник зависят только от ключа события — повторный запуск даёт тот же корпус.
        local = random.Random(key)
        days = min(max(int(event.get("days_ago") or 0), 0), 13)
        published = PERIOD_END - timedelta(days=days, hours=local.randint(0, 9), minutes=local.randint(0, 59))
        prompt = _ARTICLE_PROMPT.format(date=published.strftime("%d.%m.%Y"))
        try:
            written = _write("synthetic_article", prompt, f"Заголовок: {event['title']}\nФакты:\n{facts}", key, llm)
        except LLMError as error:
            log(f"{key}: статья не получилась: {error}")
            return None
        return SyntheticNews(
            key=key, title=written["title"], text=written["text"], source=local.choice(SOURCES),
            published_at=published, event_key=key, topic=topic.name,
            importance=min(max(int(event.get("importance") or 3), 1), 5),
            entities=[str(x) for x in event.get("entities") or []],
        )

    with ThreadPoolExecutor(workers) as pool:
        originals = [news for news in pool.map(article, seeds) if news is not None]
    log(f"оригиналов: {len(originals)}")

    plan = []
    for news in originals:
        roll = rng.random()
        if roll < near_share:
            plan.append(("near_duplicate", news))
        elif roll < near_share + paraphrase_share:
            plan.append(("semantic_duplicate", news))
        if rng.random() < continuation_share:
            plan.append(("continuation", news))

    def derived(item: tuple[str, SyntheticNews]) -> SyntheticNews | None:
        relation, base = item
        other_source = rng.choice([s for s in SOURCES if s != base.source])
        key = f"{base.key}_{relation.split('_')[0]}"
        if relation == "near_duplicate":
            return SyntheticNews(
                key=key, title=base.title, text=near_copy(base.text, random.Random(key)), source=other_source,
                published_at=base.published_at + timedelta(hours=random.Random(key).randint(1, 6)),
                relation=relation, related_to=base.key, event_key=base.event_key, topic=base.topic,
                importance=base.importance, entities=base.entities,
            )
        try:
            if relation == "semantic_duplicate":
                written = _write("synthetic_paraphrase", _PARAPHRASE_PROMPT,
                                 f"{base.title}\n\n{base.text}", key, llm)
                published = base.published_at + timedelta(hours=random.Random(key).randint(2, 20))
            else:
                days = random.Random(key).randint(2, 6)
                written = _write("synthetic_continuation", _CONTINUATION_PROMPT.format(days=days),
                                 f"{base.title}\n\n{base.text}", key, llm)
                published = base.published_at + timedelta(days=days, hours=random.Random(key).randint(0, 8))
        except LLMError as error:
            log(f"{key}: не получилось: {error}")
            return None
        return SyntheticNews(
            key=key, title=written["title"], text=written["text"], source=other_source,
            published_at=min(published, PERIOD_END), relation=relation, related_to=base.key,
            event_key=base.event_key if relation != "continuation" else key, topic=base.topic,
            importance=base.importance, entities=base.entities,
        )

    with ThreadPoolExecutor(workers) as pool:
        copies = [news for news in pool.map(derived, plan) if news is not None]
    corpus = sorted(originals + copies, key=lambda news: news.published_at)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as file:
        for news in corpus:
            file.write(json.dumps(news.to_json(), ensure_ascii=False) + "\n")
    counts = {r: sum(n.relation == r for n in corpus) for r in ("original", "near_duplicate", "semantic_duplicate", "continuation")}
    log(f"готово: {len(corpus)} новостей {counts} -> {output_path}")
    return corpus


def read_corpus(path: Path = OUTPUT_PATH) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as file:
        return [json.loads(line) for line in file if line.strip()]


def load_into_news_db(news_db: Path, path: Path = OUTPUT_PATH) -> int:
    """Загружает корпус в ``news.db`` в формате входящих новостей (эталон — в ``metadata.test``)."""
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    items = [
        StoredNews(
            document_id=f"syn_{row['key']}",
            text=row["text"],
            metadata={
                "title": row["title"],
                "url": f"https://{row['source']}/news/{row['key']}",
                "source": row["source"],
                "publication_time": row["published_at"],
                "created_at": created_at,
                "test": {
                    "relation": row["relation"],
                    "related_to": f"syn_{row['related_to']}" if row["related_to"] else None,
                    "event_key": row["event_key"],
                    "topic": row["topic"],
                    "importance": row["importance"],
                },
            },
        )
        for row in read_corpus(path)
    ]
    return insert_news(news_db, items, ignore_existing=True)
