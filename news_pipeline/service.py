"""ML-сервис целиком: фоновый конвейер новостей, профили, лента, фидбэк.

Один процесс держит одно подключение к Qdrant (локальную базу открывает только
один клиент), поэтому конвейер новостей работает фоновым потоком внутри
сервиса: ``news.db`` → подготовка (очистка, лид, обогащение) →
``new_summary.db`` → дедупликация и события → ``unique.db`` + Qdrant.

Подбор аналитики (:class:`analytics.AnalyticsMatcher`) — офлайн-шаг того же
воркера: каждое новое событие сразу получает подбор; новая аналитика
индексируется, затем подбор пересчитывается для событий за 7 дней. Онлайн
в ленте остаются выбор фрагмента под пользователя и связка.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from news_pipeline.analytics import AnalyticsMatcher, EventInput
from news_pipeline.analytics.corpus import load_fixture_materials
from news_pipeline.config import COMPANIES_CSV, DATA_DIR, NEWS_DB, PAYMENTS_CSV, SUMMARY_DB, UNIQUE_DB
from news_pipeline.dedup.drop_dublicate import NewsDeduplicator
from news_pipeline.env import env_flag
from news_pipeline.insights.insight import AnalyticsContext, generate_insight
from news_pipeline.llm.client import default_llm
from news_pipeline.preprocessing.news_summarizer import NewsSummarizer
from news_pipeline.profile.interests import build_interest_profile
from news_pipeline.profile.store import ProfileStore
from news_pipeline.ranking.feed import FeedBuilder
from news_pipeline.schemas import (
    AnalyticsIn,
    EventAnalyticsResult,
    FeedbackEvent,
    FeedItem,
    FeedRequest,
    FeedResponse,
    InterestProfile,
    NewsIn,
    UserProfileIn,
)
from news_pipeline.settings import SETTINGS
from news_pipeline.storage.news_db import StoredNews, insert_news, read_summaries, summary_ids

log = logging.getLogger("rec_news")
FEEDBACK_DB = DATA_DIR / "feedback.db"


class MLService:
    """Все компоненты ML-части в одном объекте."""

    def __init__(self, *, use_llm: bool | None = None, worker_interval: float = 10.0) -> None:
        self.use_llm = env_flag("REC_NEWS_USE_LLM") if use_llm is None else use_llm
        self.llm = default_llm()
        self.llm.enabled = self.use_llm
        self.profiles = ProfileStore()
        self.summarizer = NewsSummarizer(
            source_database_path=NEWS_DB, summary_database_path=SUMMARY_DB, use_llm=self.use_llm, verbose=False
        )
        self.dedup = NewsDeduplicator(
            source_database_path=SUMMARY_DB, unique_database_path=UNIQUE_DB, use_llm=self.use_llm, verbose=False
        )
        self.feed = FeedBuilder(self.dedup.index, llm=self.llm, use_llm=self.use_llm)
        self.analytics = AnalyticsMatcher(
            client=self.dedup.index.client, model=self.dedup.index.model, llm=self.llm, use_llm=self.use_llm
        )
        self.analytics_bootstrapped = False
        if self.analytics.store.material_count() == 0:
            materials = load_fixture_materials()
            if materials:
                log.info("индексация аналитики из fixtures: %d материалов", len(materials))
                self.analytics.index_materials(materials)
                self.analytics_bootstrapped = True
        self.worker_interval = worker_interval
        self._pipeline_lock = threading.Lock()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._companies: Any | None = None
        self._payments: Any | None = None
        self._executor = ThreadPoolExecutor(4)
        self.profile_status: dict[str, str] = {}
        with sqlite3.connect(FEEDBACK_DB) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS feedback (user_id TEXT, news_id TEXT, type TEXT, value REAL,"
                " position INTEGER, pipeline_version TEXT, timestamp TEXT)"
            )

    # --- фоновый конвейер новостей -------------------------------------------

    def run_pipeline_once(self) -> dict[str, int]:
        """Один проход: подготовка новостей, дедупликация с событиями, подбор аналитики."""
        with self._pipeline_lock:
            prepared = self.summarizer.process_pending()
            results = self.dedup.process_pending_news()
            matched = self.match_pending_events()
        return {"prepared": len(prepared.processed), "deduplicated": len(results),
                "unique": sum(result is None for result in results), "analytics_matched": matched}

    # --- аналитика --------------------------------------------------------------

    def event_input(self, event_id: str) -> EventInput | None:
        """Событие для подбора: представитель (уникальная новость) из ``unique.db``."""
        news_id = event_id[2:] if event_id.startswith("e_") else event_id
        row = self._news_row(news_id)
        if row is None:
            return None
        enrichment = row.metadata.get("enrichment") or {}
        published = row.publication_time or row.metadata.get("deduplicated_at")
        return EventInput(
            event_id=str(row.metadata.get("event_id") or event_id), title=row.title, lead=row.lead,
            published_at=datetime.fromisoformat(str(published).replace("Z", "+00:00")),
            entities=enrichment.get("entities") or [], topics=enrichment.get("topics") or {},
        )

    def match_pending_events(self, limit: int | None = None) -> int:
        """Подбор аналитики для событий, у которых его ещё не было."""
        done = self.analytics.store.matched_events()
        count = 0
        for row in read_summaries(UNIQUE_DB):
            event_id = str(row.metadata.get("event_id") or f"e_{row.document_id}")
            if event_id in done:
                continue
            event = self.event_input(event_id)
            if event is not None:
                self.analytics.match_event(event)
                count += 1
                if limit is not None and count >= limit:
                    break
        return count

    def rematch(self, event_id: str) -> EventAnalyticsResult:
        event = self.event_input(event_id)
        if event is None:
            raise KeyError(event_id)
        return self.analytics.match_event(event)

    def index_analytics(self, materials: list[AnalyticsIn]) -> int:
        """Индексация новых материалов и повторный подбор для событий за 7 дней (§8)."""
        with self._pipeline_lock:
            chunks = self.analytics.index_materials(materials)
            for event_id in self.analytics.events_to_refresh():
                event = self.event_input(event_id)
                if event is not None:
                    self.analytics.match_event(event)
        return chunks

    def submit_analytics(self, materials: list[AnalyticsIn]) -> None:
        self._executor.submit(self.index_analytics, materials)

    def start_worker(self) -> None:
        def loop() -> None:
            while not self._stop.is_set():
                try:
                    stats = self.run_pipeline_once()
                    if stats["prepared"] or stats["deduplicated"]:
                        log.info("конвейер новостей: %s", stats)
                except Exception:  # noqa: BLE001 - воркер не должен падать
                    log.exception("ошибка конвейера новостей")
                self._stop.wait(self.worker_interval)

        self._worker = threading.Thread(target=loop, name="news-pipeline", daemon=True)
        self._worker.start()

    def close(self) -> None:
        self._stop.set()
        if self._worker:
            self._worker.join(timeout=5)
        self._executor.shutdown(wait=False)
        self.dedup.close()

    # --- новости --------------------------------------------------------------

    def accept_news(self, items: list[NewsIn]) -> int:
        """Кладёт новости в ``news.db``; дальше их обрабатывает фоновый конвейер."""
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        stored = [
            StoredNews(
                document_id=item.news_id,
                text=item.text,
                metadata={
                    "title": item.title, "url": item.url, "source": item.source, "rubric": item.rubric,
                    "publication_time": item.published_at.isoformat(), "created_at": created_at,
                },
            )
            for item in items
        ]
        return insert_news(NEWS_DB, stored, ignore_existing=True)

    # --- профили --------------------------------------------------------------

    def _dataset(self) -> tuple[Any, Any]:
        if self._companies is None:
            from news_pipeline.recommendation.recommender import _read_csv

            self._companies = _read_csv(COMPANIES_CSV) if COMPANIES_CSV.exists() else None
            self._payments = _read_csv(PAYMENTS_CSV) if PAYMENTS_CSV.exists() else None
        return self._companies, self._payments

    def build_profile(self, user: UserProfileIn) -> InterestProfile:
        """Строит и сохраняет профиль интересов (если анкета не изменилась — берёт сохранённый)."""
        if self.profiles.is_fresh(user):
            cached = self.profiles.get(user.user_id)
            if cached is not None:
                return cached
        self.profile_status[user.user_id] = "building"
        companies, payments = self._dataset() if user.company_id else (None, None)
        profile = build_interest_profile(
            user, companies=companies, payments=payments, llm=self.llm, use_llm=self.use_llm
        )
        self.profiles.put(user, profile)
        self.profile_status[user.user_id] = "ready"
        return profile

    def submit_profile(self, user: UserProfileIn) -> None:
        """Асинхронное построение профиля (``POST /profiles``)."""
        self.profile_status[user.user_id] = "queued"

        def task() -> None:
            try:
                self.build_profile(user)
            except Exception:  # noqa: BLE001
                self.profile_status[user.user_id] = "failed"
                log.exception("не удалось построить профиль %s", user.user_id)

        self._executor.submit(task)

    # --- лента ----------------------------------------------------------------

    def recommend(self, request: FeedRequest, *, now: datetime | None = None, version: int = 4) -> FeedResponse:
        profile = self.profiles.get(request.user_id)
        if profile is None:
            raise KeyError(request.user_id)
        result = self.feed.build(profile, limit=request.limit, window_days=request.window_days, now=now, version=version)
        with_insights = request.with_insights and result.llm_available
        items = list(self._executor.map(lambda item: self._enrich_item(profile, item, with_insights), result.items))
        return FeedResponse(
            user_id=profile.user_id,
            generated_at=datetime.now(timezone.utc),
            pipeline_version=SETTINGS.pipeline_version,
            llm_available=result.llm_available,
            items=items,
        )

    def _news_row(self, news_id: str):
        rows = read_summaries(UNIQUE_DB, [news_id])
        return rows[0] if rows else None

    def _enrich_item(self, profile: InterestProfile, item: FeedItem, with_insight: bool) -> FeedItem:
        """Аналитика под пользователя (§9) и инсайт для элемента ленты."""
        row = self._news_row(item.news_id)
        if row is None:
            return item
        update: dict[str, Any] = {}
        event_id = item.event_id or f"e_{item.news_id}"
        try:
            if self.analytics.get(event_id) is None:
                # Воркер ещё не дошёл до события — подбираем сразу, чтобы блок не пустовал.
                event = self.event_input(event_id)
                if event is not None:
                    self.analytics.match_event(event)
            update["analytics"] = self.analytics.for_user(
                profile, item.event_id or f"e_{item.news_id}", news_title=row.title, news_lead=row.lead
            )
        except Exception:  # noqa: BLE001 - аналитика не должна ломать ленту
            log.exception("аналитика для %s", item.news_id)
        sources = [s.get("source") for s in row.metadata.get("sources") or [] if s.get("source")]
        update["sources"] = list(dict.fromkeys(sources)) or item.sources
        if with_insight:
            contexts = []
            if update.get("analytics") is not None:
                chunks = self.analytics.store.chunks([update["analytics"].chunk_id])
                if chunks:
                    contexts.append(AnalyticsContext(update["analytics"], chunks[0].text))
            insight = generate_insight(
                profile, event_id=item.event_id or item.news_id, title=row.title, news_text=row.text,
                analytics=contexts, llm=self.llm,
            )
            if insight is not None:
                update.update(insight=insight.text, insight_sources=insight.sources)
        return item.model_copy(update=update)

    # --- фидбэк и состояние -----------------------------------------------------

    def record_feedback(self, event: FeedbackEvent) -> None:
        with sqlite3.connect(FEEDBACK_DB) as connection:
            connection.execute(
                "INSERT INTO feedback VALUES (?, ?, ?, ?, ?, ?, ?)",
                (event.user_id, event.news_id, event.type, event.value, event.position,
                 event.pipeline_version or SETTINGS.pipeline_version, event.timestamp.isoformat()),
            )

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "pipeline_version": SETTINGS.pipeline_version,
            "llm_enabled": self.use_llm,
            "llm_available": self.llm.is_available(),
            "analytics_materials": self.analytics.store.material_count(),
            "analytics_events": len(self.analytics.store.matched_events()),
            "news_pending": len(summary_ids(SUMMARY_DB)),
            "news_unique": len(summary_ids(UNIQUE_DB)),
            "indexed": self.dedup.index.count(),
            "profiles": len(self.profiles.all_ids()),
        }

    @staticmethod
    def dumps(model: Any) -> str:
        return json.dumps(model, ensure_ascii=False, default=str)
