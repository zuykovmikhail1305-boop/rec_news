"""Подбор аналитики к событиям (ТЗ «подбор аналитики к новостям», §5–§9).

:class:`AnalyticsMatcher` — отдельный класс, который:

* **индексирует** материалы (:meth:`index_materials`): чанки → сущности, темы,
  эмбеддинги → SQLite (``analytics_chunks``) + Qdrant (коллекция
  ``analytics_chunks``) + BM25 в памяти (собирается из SQLite);
* **подбирает** фрагменты к событию (:meth:`match_event`):
  предфильтр по дате → dense top-20 + BM25 top-20 → RRF (k = 60) →
  бонусы за сущности и темы → ≤ 2 чанка на материал, склейка соседних →
  свежесть ``exp(−Δt/τ_темы)``, отсев < 0.1 → top-10 → реранк → top-5 →
  один вызов LLM-судьи → правила прикрепления (score ≥ 2, не устаревшие,
  ≤ 3, по одному на материал) → ``event_analytics``;
* **персонализирует** (:meth:`for_user`): выбирает фрагмент, ближайший
  к аспектам пользователя, и пишет связку 2–3 предложения с проверкой чисел.

Реранк: cross-encoder ``bge-reranker-v2-m3`` в окружении не установлен,
поэтому используется косинус эмбеддингов «заголовок + лид» новости и чанка
(та же модель bge-m3). Без LLM прикрепляется top-1 по реранку, если его
косинус не ниже ``fallback_min_rerank``; тип связи — «общая тема».
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import numpy as np
from qdrant_client import QdrantClient, models
from sentence_transformers import SentenceTransformer

from news_pipeline.analytics.corpus import chunk_material, embedding_text
from news_pipeline.analytics.store import AnalyticsStore
from news_pipeline.enrichment.news import extract_entities, topics_by_embedding
from news_pipeline.index.embeddig import QdrantIndex, TextDocument
from news_pipeline.insights.numbers import numbers_in
from news_pipeline.llm.client import LLM, LLMError, LLMUnavailable, default_llm
from news_pipeline.references import TOPICS_BY_NAME, entity_key
from news_pipeline.schemas import (
    AnalyticsChunk,
    AnalyticsIn,
    EventAnalytics,
    EventAnalyticsResult,
    FeedAnalytics,
    InterestProfile,
)
from news_pipeline.settings import SETTINGS

COLLECTION = "analytics_chunks"
TS_FIELD = "published_ts"
_CONFIG = SETTINGS.analytics
_RELATION_ORDER = {"причины": 0, "прогноз": 0, "противоречит": 1, "исторический контекст": 2, "общая тема": 3}
RELATIONS = tuple(_RELATION_ORDER)


@dataclass
class EventInput:
    """Событие для подбора: текст представителя и его признаки."""

    event_id: str
    title: str
    lead: str
    published_at: datetime
    entities: list[str] = field(default_factory=list)
    topics: dict[str, float] = field(default_factory=dict)
    embedding: np.ndarray | None = None


@dataclass
class Candidate:
    chunk: AnalyticsChunk
    text: str
    vector: np.ndarray | None = None
    rrf: float = 0.0
    cand: float = 0.0
    fresh: float = 1.0
    score: float = 0.0
    rerank: float = 0.0
    merged_ids: list[str] = field(default_factory=list)


# --- BM25 ---------------------------------------------------------------------

def _stems(text: str) -> list[str]:
    """Токены BM25: основы слов (первые 6 букв) — без морфологии склеивают падежи."""
    return [word[:6] for word in re.findall(r"\w+", text.casefold().replace("ё", "е")) if len(word) > 1]


class BM25:
    """BM25 Okapi по основам слов чанков."""

    def __init__(self, documents: dict[str, str], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.ids = list(documents)
        self.terms = [Counter(_stems(documents[i])) for i in self.ids]
        self.lengths = [sum(t.values()) for t in self.terms]
        self.avg = (sum(self.lengths) / len(self.lengths)) if self.lengths else 1.0
        df = Counter(term for terms in self.terms for term in terms)
        n = len(self.ids)
        self.idf = {term: math.log(1 + (n - f + 0.5) / (f + 0.5)) for term, f in df.items()}

    def search(self, query: str, *, allowed: set[str], limit: int) -> list[tuple[str, float]]:
        query_terms = set(_stems(query))
        scores = []
        for doc_id, terms, length in zip(self.ids, self.terms, self.lengths):
            if doc_id not in allowed:
                continue
            score = 0.0
            for term in query_terms:
                tf = terms.get(term)
                if tf:
                    score += self.idf[term] * tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * length / self.avg))
            if score > 0:
                scores.append((doc_id, score))
        return sorted(scores, key=lambda item: -item[1])[:limit]


def rrf(rank_lists: list[list[str]], k: int) -> dict[str, float]:
    scores: dict[str, float] = {}
    for ranks in rank_lists:
        for rank, chunk_id in enumerate(ranks):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank + 1)
    return scores


def _topic_cosine(a: dict[str, float], b: dict[str, float]) -> float:
    dot = sum(a[t] * b[t] for t in a.keys() & b.keys())
    norm = math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values()))
    return dot / norm if norm else 0.0


def _jaccard(a: list[str], b: list[str]) -> float:
    left = {entity_key(x) for x in a} - {""}
    right = {entity_key(x) for x in b} - {""}
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _tau_days(topics: dict[str, float]) -> float:
    taus = _CONFIG["tau_days"]
    if not topics:
        return float(taus["default"])
    dominant = max(topics, key=topics.get)
    topic = TOPICS_BY_NAME.get(dominant)
    return float(taus.get(topic.id if topic else "", taus["default"]))


def _utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


# --- LLM-судья ------------------------------------------------------------------

_JUDGE_PROMPT = """Ты аналитик банка. Руководителю показывают новость. Реши, какие фрагменты \
аналитики помогают ему понять эту новость.

Для каждого фрагмента оцени:
- score: 0 — не связан; 1 — та же тема, но не помогает понять новость; 2 — даёт полезный \
контекст; 3 — прямо объясняет причины или последствия;
- relation: "причины" | "прогноз" | "исторический контекст" | "противоречит" | "общая тема";
- is_outdated: true, если выводы фрагмента уже неактуальны с учётом новости;
- reason: одно предложение, почему.

Будь строгим: если фрагмент про другую отрасль или тему и даёт лишь общий экономический фон, не объясняя причины или последствия именно этого события, — score 1.
Не придумывай факты, которых нет в тексте. Ответ — только JSON:
{"items": [{"id": 1, "score": 0, "relation": "...", "is_outdated": false, "reason": "..."}]}
/no_think"""

_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "score": {"type": "integer"},
                    "relation": {"type": "string", "enum": list(RELATIONS)},
                    "is_outdated": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "score", "relation", "is_outdated", "reason"],
            },
        }
    },
    "required": ["items"],
}


def _judge_validator(count: int) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def validate(data: dict[str, Any]) -> dict[str, Any]:
        items = {}
        for item in data.get("items") or []:
            number = int(item.get("id") or 0)
            score = int(item.get("score", -1))
            relation = item.get("relation")
            if not 1 <= number <= count or not 0 <= score <= 3 or relation not in _RELATION_ORDER:
                continue
            if relation == "противоречит" and score < 2:
                relation = "общая тема"  # «противоречит» допускается только со score ≥ 2
            items[number] = {"score": score, "relation": relation,
                             "is_outdated": bool(item.get("is_outdated")),
                             "reason": str(item.get("reason") or "").strip() or "—"}
        if not items:
            raise LLMError("нет оценок фрагментов")
        return {"items": items}

    return validate


# --- связка для пользователя ---------------------------------------------------

_BRIDGE_PROMPT = """Ты аналитик Альфа-Банка. Напиши 2–3 предложения для руководителя под новостью:
1. что говорит аналитика банка об этом событии;
2. что это значит с учётом роли и компании руководителя;
3. только если во входных данных указано «противоречит новости: да» — скажи прямо: «Аналитика \
от {date} ожидала обратного…»; иначе не пиши о расхождении.
Только факты из новости и фрагмента: без новых чисел и дат. Ответ — JSON с полем summary.
/no_think"""


class AnalyticsMatcher:
    """Подбор аналитики к событиям: индексация, поиск, LLM-проверка, связка."""

    def __init__(
        self,
        *,
        client: QdrantClient | None = None,
        model: SentenceTransformer | None = None,
        qdrant_path: Any = None,
        store: AnalyticsStore | None = None,
        llm: LLM | None = None,
        use_llm: bool = True,
    ) -> None:
        location = {"client": client} if client is not None else {"qdrant_path": qdrant_path}
        self.index = QdrantIndex(COLLECTION, **location, model=model)
        self.store = store or AnalyticsStore()
        self.llm = llm
        self.use_llm = use_llm
        self._chunks: dict[str, AnalyticsChunk] = {}
        self._bm25: BM25 | None = None
        self.reload()

    # --- индексация ---------------------------------------------------------------

    def reload(self) -> None:
        """Перечитывает чанки из SQLite и пересобирает BM25."""
        self._chunks = {chunk.chunk_id: chunk for chunk in self.store.chunks()}
        self._bm25 = BM25({cid: self._search_text(chunk) for cid, chunk in self._chunks.items()}) if self._chunks else None
        if self._chunks and self.index.count() < len(self._chunks):
            self._upsert(list(self._chunks.values()))
        self.index.ensure_payload_index(TS_FIELD, models.PayloadSchemaType.FLOAT)

    @staticmethod
    def _search_text(chunk: AnalyticsChunk) -> str:
        return embedding_text(chunk.analytics_title, chunk.section_title, chunk.text)

    def _upsert(self, chunks: list[AnalyticsChunk], vectors: np.ndarray | None = None) -> None:
        self.index.index(
            [
                TextDocument(
                    text=self._search_text(chunk), id=chunk.chunk_id,
                    payload={
                        "document_id": chunk.chunk_id, "analytics_id": chunk.analytics_id,
                        "analytics_title": chunk.analytics_title, "section_title": chunk.section_title,
                        "published_at": chunk.published_at.isoformat() if chunk.published_at else None,
                        TS_FIELD: _utc(chunk.published_at).timestamp() if chunk.published_at else None,
                        "entities": chunk.entities, "topics": chunk.topics,
                    },
                )
                for chunk in chunks
            ],
            dense_vectors=vectors,
        )

    def index_materials(self, materials: list[AnalyticsIn]) -> int:
        """Индексирует материалы: чанки, сущности, темы, эмбеддинги → SQLite, Qdrant, BM25."""
        indexed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        total = 0
        for material in materials:
            drafts = chunk_material(material)
            if not drafts:
                continue
            texts = [embedding_text(material.title, d.section_title, d.text) for d in drafts]
            vectors = self.index.encode(texts)
            chunks = [
                AnalyticsChunk(
                    chunk_id=f"{material.analytics_id}_{position}", analytics_id=material.analytics_id,
                    analytics_title=material.title, section_title=draft.section_title, position=position,
                    text=draft.text, published_at=material.published_at, type=material.type, url=material.url,
                    entities=extract_entities(draft.section_title, draft.text),
                    topics=topics_by_embedding(vector),
                )
                for position, (draft, vector) in enumerate(zip(drafts, vectors))
            ]
            self.store.put_material(material, chunks, indexed_at)
            self._upsert(chunks, vectors)
            total += len(chunks)
        self.reload()
        return total

    # --- подбор -------------------------------------------------------------------

    def candidates(self, event: EventInput) -> list[Candidate]:
        """Кандидаты: предфильтр, dense + BM25, RRF, бонусы, дедупликация, свежесть (§5–§6)."""
        if not self._chunks:
            return []
        published = _utc(event.published_at)
        oldest = published - timedelta(days=_CONFIG["max_age_days"])
        allowed = {
            cid for cid, chunk in self._chunks.items()
            if chunk.published_at is not None and oldest <= _utc(chunk.published_at) <= published
        }
        if not allowed:
            return []
        query = f"{event.title}\n{event.lead}"
        vector = event.embedding if event.embedding is not None else self.index.encode([query])[0]
        dense = self.index.find_similar(
            vector, limit=_CONFIG["dense_top"], with_vectors=True,
            query_filter=models.Filter(must=[models.FieldCondition(
                key=TS_FIELD, range=models.Range(gte=oldest.timestamp(), lte=published.timestamp()))]),
        )
        dense = [item for item in dense if str(item.id) in allowed]
        vectors = {str(item.id): item.vector for item in dense}
        bm25 = self._bm25.search(f"{query}\n{' '.join(event.entities)}", allowed=allowed, limit=_CONFIG["bm25_top"])
        fused = rrf([[str(item.id) for item in dense], [cid for cid, _ in bm25]], _CONFIG["rrf_k"])
        top = max(fused.values())

        pool = []
        for chunk_id, value in fused.items():
            chunk = self._chunks[chunk_id]
            cand = (value / top + _CONFIG["entity_bonus"] * _jaccard(chunk.entities, event.entities)
                    + _CONFIG["topic_bonus"] * _topic_cosine(chunk.topics, event.topics))
            pool.append(Candidate(chunk, chunk.text, vectors.get(chunk_id), rrf=value, cand=cand, merged_ids=[chunk_id]))
        pool.sort(key=lambda c: -c.cand)

        # Не больше N чанков из материала; соседние чанки одного раздела склеиваются.
        per_material: dict[str, list[Candidate]] = defaultdict(list)
        result: list[Candidate] = []
        for candidate in pool:
            siblings = per_material[candidate.chunk.analytics_id]
            neighbour = next((c for c in siblings if c.chunk.section_title == candidate.chunk.section_title
                              and abs(c.chunk.position - candidate.chunk.position) == 1), None)
            if neighbour is not None:
                first, second = sorted([neighbour, candidate], key=lambda c: c.chunk.position)
                neighbour.text = f"{first.text}\n\n{second.text}" if neighbour is first else f"{candidate.text}\n\n{neighbour.text}"
                neighbour.merged_ids.extend(candidate.merged_ids)
                continue
            if len(siblings) >= _CONFIG["max_chunks_per_material"]:
                continue
            siblings.append(candidate)
            result.append(candidate)

        kept = []
        for candidate in result:
            age = max((published - _utc(candidate.chunk.published_at)).total_seconds() / 86400, 0.0)
            candidate.fresh = math.exp(-age / _tau_days(candidate.chunk.topics))
            if candidate.fresh < _CONFIG["min_fresh"]:
                continue
            candidate.score = candidate.cand * candidate.fresh
            kept.append(candidate)
        if _CONFIG.get("require_topic_overlap", False) and event.topics:
            kept = [c for c in kept if set(c.chunk.topics) & set(event.topics)]
        kept.sort(key=lambda c: -c.score)
        kept = kept[: _CONFIG["rerank_input"]]

        missing = [c for c in kept if c.vector is None]
        if missing:
            for candidate, encoded in zip(missing, self.index.encode([self._search_text(c.chunk) for c in missing])):
                candidate.vector = encoded
        for candidate in kept:
            candidate.rerank = float(np.dot(vector, candidate.vector))
        kept.sort(key=lambda c: -c.rerank)
        return kept

    def _judge(self, event: EventInput, top: list[Candidate]) -> dict[int, dict[str, Any]] | None:
        """Один вызов LLM-судьи на событие; ``None`` — LLM недоступна или не справилась."""
        if not self.use_llm or not top:
            return None
        fragments = "\n\n".join(
            f"[{n}] ({c.chunk.published_at:%d.%m.%Y}, «{c.chunk.analytics_title}», раздел «{c.chunk.section_title}»)\n"
            f"{c.text[:1400]}"
            for n, c in enumerate(top, 1)
        )
        content = (f"Новость ({_utc(event.published_at):%d.%m.%Y}):\n{event.title}\n{event.lead}\n\n"
                   f"Фрагменты аналитики:\n{fragments}")
        try:
            data = (self.llm or default_llm()).json(
                "analytics_check",
                [{"role": "system", "content": _JUDGE_PROMPT}, {"role": "user", "content": content}],
                schema=_JUDGE_SCHEMA,
                cache_key=f"{event.event_id}:{','.join(c.chunk.chunk_id for c in top)}",
                validator=_judge_validator(len(top)), max_tokens=1500,
            )
        except (LLMUnavailable, LLMError):
            return None
        # Ответ из кэша прошёл через JSON: ключи-номера фрагментов стали строками.
        return {int(number): verdict for number, verdict in data["items"].items()}

    def match_event(self, event: EventInput) -> EventAnalyticsResult:
        """Полный подбор для события и запись результата в ``event_analytics`` (§5–§8)."""
        now = datetime.now(timezone.utc)
        candidates = self.candidates(event)
        top = candidates[: _CONFIG["judge_top"]]
        verdicts = self._judge(event, top)
        attached: list[tuple[EventAnalytics, float]] = []
        if not self._chunks:
            status = "no_corpus"
        elif not top:
            status = "empty"  # подходящих кандидатов нет — LLM-судья не нужен
        elif verdicts is None:
            status = "fallback"
            if top and top[0].rerank >= _CONFIG["fallback_min_rerank"]:
                best = top[0]
                attached.append((self._item(best, 2, "общая тема", "Подобрано по смысловой близости без LLM-проверки.", now), best.fresh))
        else:
            status = "matched"
            for number, candidate in enumerate(top, 1):
                verdict = verdicts.get(number)
                if not verdict or verdict["score"] < _CONFIG["min_score"] or verdict["is_outdated"]:
                    continue
                attached.append((self._item(candidate, verdict["score"], verdict["relation"], verdict["reason"], now),
                                 candidate.fresh))

        # Правила прикрепления (§8): по одному на материал, порядок, не больше трёх.
        best_per_material: dict[str, tuple[EventAnalytics, float]] = {}
        for item, fresh in attached:
            current = best_per_material.get(item.analytics_id)
            if current is None or (item.score, item.rerank or 0) > (current[0].score, current[0].rerank or 0):
                best_per_material[item.analytics_id] = (item, fresh)
        ordered = sorted(best_per_material.values(),
                         key=lambda pair: (-pair[0].score, _RELATION_ORDER[pair[0].relation], -pair[1]))
        items = [item for item, _ in ordered[: _CONFIG["max_per_event"]]]
        if status == "matched" and not items:
            status = "empty"
        self.store.put_event(event.event_id, items, status=status, published_at=_utc(event.published_at).isoformat(),
                             matched_at=now.isoformat(timespec="seconds"), pipeline_version=SETTINGS.pipeline_version)
        return EventAnalyticsResult(event_id=event.event_id, analytics=items, status=status,
                                    pipeline_version=SETTINGS.pipeline_version)

    @staticmethod
    def _item(candidate: Candidate, score: int, relation: str, reason: str, now: datetime) -> EventAnalytics:
        chunk = candidate.chunk
        return EventAnalytics(
            analytics_id=chunk.analytics_id, chunk_id=chunk.chunk_id, score=score, relation=relation,
            contradicts=relation == "противоречит", reason=reason, rerank=round(candidate.rerank, 4), matched_at=now,
            analytics_title=chunk.analytics_title, section_title=chunk.section_title,
            published_at=chunk.published_at, url=chunk.url,
        )

    def get(self, event_id: str) -> EventAnalyticsResult | None:
        status, items = self.store.event(event_id)
        if status is None:
            return None
        return EventAnalyticsResult(event_id=event_id, analytics=items, status=status,
                                    pipeline_version=SETTINGS.pipeline_version)

    def events_to_refresh(self) -> list[str]:
        """События за ``refresh_days`` без фрагментов или только со score 2 (§8, обновление)."""
        since = datetime.now(timezone.utc) - timedelta(days=_CONFIG["refresh_days"])
        return self.store.events_to_refresh(since.isoformat())

    # --- персонализация (§9) ---------------------------------------------------------

    def for_user(self, profile: InterestProfile, event_id: str, *, news_title: str, news_lead: str) -> FeedAnalytics | None:
        """Фрагмент для пользователя: ``0.5·score/3 + 0.5·max_k cos(аспект, чанк)`` и связка."""
        status, items = self.store.event(event_id)
        if not items:
            return None
        chunks = {chunk.chunk_id: chunk for chunk in self.store.chunks([item.chunk_id for item in items])}
        aspects = np.array([a.embedding for a in profile.aspects if a.embedding]) if profile.aspects else None
        vectors = self.index.encode([self._search_text(chunks[item.chunk_id]) for item in items if item.chunk_id in chunks])
        best, best_value = None, -1.0
        for item, vector in zip([i for i in items if i.chunk_id in chunks], vectors):
            similarity = float((aspects @ vector).max()) if aspects is not None and len(aspects) else 0.0
            value = 0.5 * item.score / 3 + 0.5 * similarity
            if value > best_value:
                best, best_value = item, value
        if best is None:
            return None
        chunk = chunks[best.chunk_id]
        summary = self._bridge(profile, event_id, best, chunk, news_title, news_lead)
        return FeedAnalytics(
            analytics_id=best.analytics_id, chunk_id=best.chunk_id, title=chunk.analytics_title,
            section_title=chunk.section_title, published_at=chunk.published_at, relation=best.relation,
            contradicts=best.contradicts, summary=summary, url=chunk.url,
        )

    def _bridge(self, profile: InterestProfile, event_id: str, item: EventAnalytics, chunk: AnalyticsChunk,
                title: str, lead: str) -> str:
        """Связка 2–3 предложения; без LLM или при выдуманных числах — ``reason`` судьи."""
        date = f"{chunk.published_at:%d.%m.%Y}" if chunk.published_at else "—"
        reference = f" («{chunk.analytics_title}», {date})"
        if not self.use_llm:
            return item.reason + reference
        allowed = numbers_in("\n".join([title, lead, chunk.text]))

        def validate(data: dict[str, Any]) -> dict[str, Any]:
            text = str(data.get("summary") or "").strip()
            if not text:
                raise LLMError("пустая связка")
            invented = numbers_in(text) - allowed
            if invented:
                raise LLMError(f"числа, которых нет во входных текстах: {', '.join(sorted(invented))}")
            return {"summary": text}

        aspects = "; ".join(a.title for a in profile.aspects[:5])
        content = (f"Роль: {profile.role}\nИнтересы: {aspects}\n\nНовость:\n{title}\n{lead}\n\n"
                   f"Фрагмент аналитики ({date}, «{chunk.analytics_title}», раздел «{chunk.section_title}», "
                   f"связь: {item.relation}, противоречит новости: {'да' if item.contradicts else 'нет'}):\n"
                   f"{chunk.text[:1500]}")
        try:
            data = (self.llm or default_llm()).json(
                "analytics_bridge",
                [{"role": "system", "content": _BRIDGE_PROMPT.replace("{date}", date)},
                 {"role": "user", "content": content}],
                schema={"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]},
                cache_key=f"{profile.user_id}:{event_id}:{item.chunk_id}", validator=validate,
                max_tokens=600, retries=1,
            )
        except (LLMUnavailable, LLMError):
            return item.reason + reference
        return data["summary"] + reference
