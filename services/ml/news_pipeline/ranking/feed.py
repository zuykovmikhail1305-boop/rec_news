"""Ранжирование и формирование ленты (ТЗ §7).

Конвейер по запросу:

1. **Кандидаты** — для каждого вектора аспекта ближайшие новости коллекции
   ``news`` за рабочее окно; объединение до ``max_candidates``.
2. **Пять сигналов** 0–1 и retrieval-скор (§7.2):
   ``sem`` — максимум по аспектам ``w_k · clip((cos − low)/(high − low))``;
   ``ent`` — noisy-OR весов совпавших сущностей (сравнение по
   :func:`references.entity_key`); ``topic`` — средневзвешенный вес тем
   пользователя по вероятностям тем новости; ``imp`` — (важность − 1)/4;
   ``fresh`` — ``exp(−Δt/τ)``.
3. **LLM-реранк** top-``rerank_top``: оценка 0–3 и «почему это важно вам»,
   ``final = llm_mix·llm/3 + (1 − llm_mix)·retrieval``; кэш по паре
   (``user_id``, ``event_id``).
4. **Лента** — одна новость на ``event_id``, MMR (λ), не больше
   ``max_per_topic_top10`` новостей одной темы в топ-10.

Без LLM порядок задаёт retrieval-скор, а обоснование собирается из
сработавших сигналов (совпавшие сущности, аспект, тема).

``version`` включает этапы для сравнения в §11: 1 — только ``sem``;
2 — полный retrieval; 3 — + LLM-реранк; 4 — + MMR и фильтры.
Каждая выдача пишется в ``data/logs/feed.jsonl``.
"""

from __future__ import annotations

import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from qdrant_client import models

from news_pipeline.config import DATA_DIR
from news_pipeline.index.embeddig import QdrantIndex
from news_pipeline.llm.client import LLM, LLMError, LLMUnavailable, default_llm
from news_pipeline.prompts import prompt
from news_pipeline.references import entity_key, role_topic_weight, TOPICS_BY_NAME
from news_pipeline.schemas import FeedItem, InterestProfile, Signals
from news_pipeline.settings import SETTINGS

FEED_LOG_PATH = DATA_DIR / "logs" / "feed.jsonl"
PUBLICATION_TS_FIELD = "publication_ts"


@dataclass
class Candidate:
    """Новость-кандидат с сигналами и промежуточными оценками."""

    news_id: str
    payload: dict[str, Any]
    vector: np.ndarray | None
    aspect_cos: dict[str, float] = field(default_factory=dict)
    sem: float = 0.0
    ent: float = 0.0
    topic: float = 0.0
    imp: float = 0.0
    fresh: float = 0.0
    retrieval: float = 0.0
    llm: int | None = None
    final: float = 0.0
    why: str | None = None
    why_source: str = "rules"
    matched_entities: list[str] = field(default_factory=list)
    best_aspect: str | None = None

    @property
    def event_id(self) -> str:
        return str(self.payload.get("event_id") or f"e_{self.news_id}")

    @property
    def main_topic(self) -> str | None:
        topics = self.payload.get("topics") or {}
        return max(topics, key=topics.get) if topics else None

    def signals(self) -> Signals:
        return Signals(sem=round(self.sem, 4), ent=round(self.ent, 4), topic=round(self.topic, 4),
                       imp=round(self.imp, 4), fresh=round(self.fresh, 4),
                       retrieval=round(self.retrieval, 4), llm=self.llm, final=round(self.final, 4))


@dataclass
class FeedResult:
    items: list[FeedItem]
    candidates: list[Candidate]
    llm_available: bool
    seconds: float


class FeedBuilder:
    """Собирает персональную ленту по профилю интересов."""

    def __init__(
        self,
        index: QdrantIndex,
        *,
        llm: LLM | None = None,
        use_llm: bool = True,
        log_path: str | Path = FEED_LOG_PATH,
        workers: int = 4,
    ) -> None:
        self.index = index
        self.llm = llm
        self.use_llm = use_llm
        self.config = SETTINGS.ranking
        self.log_path = Path(log_path)
        self.workers = workers

    # --- кандидаты и сигналы ------------------------------------------------

    def candidates(self, profile: InterestProfile, *, now: datetime, window_days: int) -> list[Candidate]:
        """Объединение ближайших новостей по векторам аспектов в рабочем окне (§7.1)."""
        start = now.timestamp() - window_days * 86400
        query_filter = models.Filter(must=[
            models.FieldCondition(key=PUBLICATION_TS_FIELD, range=models.Range(gte=start, lte=now.timestamp())),
        ])
        pool: dict[str, Candidate] = {}
        for aspect in profile.aspects:
            if not aspect.embedding:
                continue
            found = self.index.find_similar(
                np.asarray(aspect.embedding), limit=self.config["candidates_per_aspect"],
                query_filter=query_filter, with_vectors=True,
            )
            for item in found:
                if item.payload.get("is_representative") is False:
                    continue
                news_id = str(item.id)
                candidate = pool.setdefault(news_id, Candidate(news_id, item.payload, item.vector))
                candidate.aspect_cos[aspect.aspect_id] = max(candidate.aspect_cos.get(aspect.aspect_id, -1.0), item.score)
        ranked = sorted(pool.values(), key=lambda c: -max(c.aspect_cos.values()))
        return ranked[: self.config["max_candidates"]]

    def score(self, candidate: Candidate, profile: InterestProfile, *, now: datetime) -> None:
        """Пять сигналов и retrieval-скор (§7.2)."""
        low, high = self.config["cos_low"], self.config["cos_high"]
        best = (0.0, None)
        for aspect in profile.aspects:
            cos = candidate.aspect_cos.get(aspect.aspect_id)
            if cos is None:
                continue
            value = aspect.weight * min(max((cos - low) / (high - low), 0.0), 1.0)
            if value > best[0]:
                best = (value, aspect.title)
        candidate.sem, candidate.best_aspect = best

        profile_keys = {entity_key(name): (name, weight) for name, weight in profile.entities.items()}
        product = 1.0
        matched = []
        for name in candidate.payload.get("entities") or []:
            hit = profile_keys.get(entity_key(name))
            if hit and hit[0] not in matched:
                matched.append(hit[0])
                product *= 1.0 - hit[1]
        candidate.ent = 1.0 - product
        candidate.matched_entities = matched

        topics = candidate.payload.get("topics") or {}
        total = sum(topics.values())
        if total > 0:
            candidate.topic = sum(
                profile.topic_weights.get(name, role_topic_weight(profile.role, TOPICS_BY_NAME[name].id)
                                          if name in TOPICS_BY_NAME else 0.0) * p
                for name, p in topics.items()
            ) / total

        candidate.imp = (int(candidate.payload.get("importance") or 3) - 1) / 4
        timestamp = candidate.payload.get(PUBLICATION_TS_FIELD)
        if timestamp is not None:
            hours = max(now.timestamp() - float(timestamp), 0.0) / 3600
            candidate.fresh = math.exp(-hours / self.config["fresh_tau_hours"])
        weights = self.config["weights"]
        candidate.retrieval = (
            weights["sem"] * candidate.sem + weights["ent"] * candidate.ent + weights["topic"] * candidate.topic
            + weights["imp"] * candidate.imp + weights["fresh"] * candidate.fresh
        )

    # --- LLM-реранк -----------------------------------------------------------

    def _rule_why(self, candidate: Candidate) -> str:
        parts = []
        if candidate.matched_entities:
            parts.append("в новости есть " + ", ".join(candidate.matched_entities[:3]))
        if candidate.best_aspect and candidate.sem > 0:
            parts.append(f"она относится к вашему интересу «{candidate.best_aspect}»")
        if not parts and candidate.main_topic:
            parts.append(f"тема «{candidate.main_topic}» важна для вашей роли")
        text = "; ".join(parts) or "событие значимо для крупного бизнеса"
        return text[0].upper() + text[1:] + "."

    def rerank(self, candidates: list[Candidate], profile: InterestProfile) -> bool:
        """LLM-оценки 0–3 и обоснования для кандидатов; False — LLM недоступна."""
        llm = self.llm or default_llm()
        if not self.use_llm or not llm.is_available():
            return False
        profile_text = _profile_text(profile)
        mix = self.config["llm_mix"]
        available = True

        def judge(candidate: Candidate) -> None:
            nonlocal available
            try:
                data = llm.json(
                    "rerank",
                    [{"role": "system", "content": _RERANK_PROMPT},
                     {"role": "user", "content": f"Профиль:\n{profile_text}\n\nНовость:\n{_news_text(candidate.payload)}"}],
                    schema=_RERANK_SCHEMA, cache_key=f"{profile.user_id}:{candidate.event_id}",
                    validator=_validate_rerank, max_tokens=700,
                )
            except LLMUnavailable:
                available = False
                return
            except LLMError:
                return
            candidate.llm = data["score"]
            candidate.why, candidate.why_source = data["why_relevant"], "llm"
            candidate.final = mix * candidate.llm / 3 + (1 - mix) * candidate.retrieval

        with ThreadPoolExecutor(self.workers) as pool:
            list(pool.map(judge, candidates))
        return available

    # --- формирование ленты ---------------------------------------------------

    def _diversify(self, candidates: list[Candidate], limit: int, use_mmr: bool) -> list[Candidate]:
        """Одна новость на событие; MMR и лимит на тему в топ-10 (§7.4)."""
        by_event: dict[str, Candidate] = {}
        for candidate in sorted(candidates, key=lambda c: -c.final):
            by_event.setdefault(candidate.event_id, candidate)
        pool = list(by_event.values())
        if not use_mmr:
            return pool[:limit]
        lam, cap = self.config["mmr_lambda"], self.config["max_per_topic_top10"]
        chosen: list[Candidate] = []
        topic_counts: dict[str, int] = {}
        while pool and len(chosen) < limit:
            best, best_value = None, -1e9
            for candidate in pool:
                topic = candidate.main_topic
                if len(chosen) < 10 and topic and topic_counts.get(topic, 0) >= cap:
                    continue
                redundancy = max(
                    (float(candidate.vector @ other.vector) for other in chosen
                     if candidate.vector is not None and other.vector is not None),
                    default=0.0,
                )
                value = lam * candidate.final - (1 - lam) * redundancy
                if value > best_value:
                    best, best_value = candidate, value
            if best is None:  # оставшиеся упёрлись в лимит тем — снимаем его
                cap = limit
                continue
            chosen.append(best)
            pool.remove(best)
            if best.main_topic:
                topic_counts[best.main_topic] = topic_counts.get(best.main_topic, 0) + 1
        return chosen

    def build(
        self,
        profile: InterestProfile,
        *,
        limit: int = 15,
        window_days: int = 7,
        now: datetime | None = None,
        version: int = 4,
    ) -> FeedResult:
        """Персональная лента: ``version`` 1–4 — этапы конвейера для сравнения (§11)."""
        started = time.monotonic()
        now = now or datetime.now(timezone.utc)
        limit = max(1, min(limit, self.config["feed_max"]))
        candidates = self.candidates(profile, now=now, window_days=window_days)
        for candidate in candidates:
            self.score(candidate, profile, now=now)
            candidate.final = candidate.sem if version == 1 else candidate.retrieval
        candidates.sort(key=lambda c: -c.final)

        llm_available = False
        if version >= 3:
            llm_available = self.rerank(candidates[: self.config["rerank_top"]], profile)
            candidates.sort(key=lambda c: -c.final)
        chosen = self._diversify(candidates, limit, use_mmr=version >= 4)
        for candidate in chosen:
            if candidate.why is None:
                candidate.why = self._rule_why(candidate)

        items = [
            FeedItem(
                news_id=c.news_id, event_id=c.event_id, title=str(c.payload.get("title") or ""),
                score=round(c.final, 4), why_relevant=c.why, topics=list((c.payload.get("topics") or {})),
                sources=[s for s in [c.payload.get("source")] if s], signals=c.signals(),
            )
            for c in chosen
        ]
        result = FeedResult(items, candidates, llm_available, time.monotonic() - started)
        self._log(profile, result, version, now)
        return result

    def _log(self, profile: InterestProfile, result: FeedResult, version: int, now: datetime) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "now": now.isoformat(),
            "pipeline_version": SETTINGS.pipeline_version,
            "feed_version": version,
            "user_id": profile.user_id,
            "llm_available": result.llm_available,
            "seconds": round(result.seconds, 2),
            "candidates": [
                {"news_id": c.news_id, "event_id": c.event_id, **c.signals().model_dump(), "why_source": c.why_source}
                for c in result.candidates
            ],
            "order": [item.news_id for item in result.items],
        }
        with self.log_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")


# --- промпт реранка -------------------------------------------------------------

_RERANK_PROMPT = prompt("feed.rerank")

_RERANK_SCHEMA = {
    "type": "object",
    "properties": {"score": {"type": "integer"}, "why_relevant": {"type": "string"}},
    "required": ["score", "why_relevant"],
}


def _validate_rerank(data: dict[str, Any]) -> dict[str, Any]:
    score = int(data.get("score", -1))
    why = str(data.get("why_relevant") or "").strip()
    if not 0 <= score <= 3:
        raise LLMError("score должен быть от 0 до 3")
    if not why:
        raise LLMError("пустое why_relevant")
    return {"score": score, "why_relevant": why}


def _profile_text(profile: InterestProfile) -> str:
    aspects = "\n".join(f"- {a.title}: {a.description}" for a in profile.aspects[:8])
    entities = ", ".join(list(profile.entities)[:12])
    return f"Роль: {profile.role}\nИнтересы:\n{aspects}\nВажные сущности: {entities}"


def _news_text(payload: dict[str, Any]) -> str:
    entities = ", ".join((payload.get("entities") or [])[:10])
    return f"{payload.get('text') or payload.get('title')}\nСущности: {entities}"
