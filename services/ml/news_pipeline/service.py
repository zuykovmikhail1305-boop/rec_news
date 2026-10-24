"""ML-сервис целиком: фоновый конвейер новостей, профили, лента, фидбэк.

Один процесс держит одно подключение к Qdrant (локальную базу открывает только
один клиент), поэтому конвейер новостей работает фоновым потоком внутри
сервиса: ``news.db`` → подготовка (очистка, лид, обогащение) →
``new_summary.db`` → дедупликация и события → ``unique.db`` + Qdrant.

Подбор аналитики (:class:`analytics.AnalyticsMatcher`) — офлайн-шаг того же
воркера: каждое новое событие сразу получает подбор; новая аналитика
индексируется, затем подбор пересчитывается для событий за 7 дней. Онлайн
в ленте остаются выбор фрагмента под пользователя и связка.

Обработку запускает бэкенд (``POST /processing.enqueue`` → :meth:`MLService.start_run`):
запуск создаётся в ``recommendations.db`` и уходит задачей в очередь ``processing.run``;
воркер очереди (:meth:`MLService.handle_processing`, сейчас — поток в процессе API, с Celery —
отдельный процесс) выполняет подготовку → дедупликацию и события → подбор аналитики → для
каждого пользователя ленту (retrieval, LLM-реранк, MMR) с фрагментом аналитики под него →
таблица ``user_news``. Состояние запуска — только в базе, его читает ``GET /processing.result``. Готовые новости
пользователя отдаёт :meth:`MLService.user_news` (``GET /users/{id}/news``).
Фоновый воркер, который обрабатывает новости сам каждые 10 с, включается
переменной ``REC_NEWS_PIPELINE_WORKER=1``.
"""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from news_pipeline.agent import BusinessInsightAgent
from news_pipeline.analytics import AnalyticsMatcher, EventInput
from news_pipeline.analytics.corpus import load_fixture_materials
from news_pipeline.config import DEMO_NOW, NEWS_DB, SUMMARY_DB, UNIQUE_DB
from news_pipeline.dedup.drop_dublicate import NewsDeduplicator
from news_pipeline.env import env_flag
from news_pipeline.insights.card import InsightCardAgent
from news_pipeline.insights.facts import CompanyFact, company_facts
from news_pipeline.insights.schemas import InsightCard
from news_pipeline.llm.client import default_llm
from news_pipeline.preprocessing.news_summarizer import NewsSummarizer
from news_pipeline.profile.interests import build_interest_profile
from news_pipeline.profile.store import ProfileStore
from news_pipeline.ranking.feed import FeedBuilder
from news_pipeline.ranking.user_news import UserNewsRow, UserNewsStore, now_iso
from news_pipeline.schemas import (
    AnalyticsIn,
    AspectInfo,
    CompanyFactInfo,
    EventAnalyticsResult,
    FeedAnalytics,
    FeedbackEvent,
    FeedItem,
    FeedRequest,
    FeedResponse,
    InterestProfile,
    NewsIn,
    PipelineRun,
    ProfileActivity,
    ProfileDetails,
    ProfileInterests,
    ProfileList,
    ProfileListItem,
    Signals,
    UserNewsAnalytics,
    UserNewsItem,
    UserNewsResponse,
    UserProfileIn,
)
from news_pipeline.settings import SETTINGS
from news_pipeline.storage.feedback_db import FeedbackStore
from news_pipeline.tasks.insight_jobs import InsightJobs
from news_pipeline.tasks.messages import PROCESSING_QUEUE, ProcessingTask
from news_pipeline.tasks.queue import create_queue
from news_pipeline.storage.news_db import StoredNews, insert_news, read_summaries, status_counts, summary_ids

log = logging.getLogger("rec_news")


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
        self._dataset_lock = threading.Lock()
        self._facts_cache: dict[tuple[str, str], list[CompanyFact]] = {}
        self._executor = ThreadPoolExecutor(4)
        self.profile_status: dict[str, str] = {}
        self.feedback = FeedbackStore()
        self.agent = BusinessInsightAgent(self)
        self.user_news_store = UserNewsStore()
        self.insight_cards = InsightCardAgent(self)
        # Очередь задач: сейчас потоки в процессе, позже — Celery (tasks/queue.py, create_queue).
        self.queue = create_queue()
        self.queue.subscribe(PROCESSING_QUEUE, self.handle_processing, workers=1)  # одна обработка за раз
        self.insight_jobs = InsightJobs(self, self.queue)

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

    # --- обработка по запросу бэкенда ----------------------------------------------

    def start_run(self, *, now: datetime | None = None, user_ids: list[str] | None = None, insights: bool = False,
                  insights_top: int | None = None) -> tuple[PipelineRun, bool]:
        """Создаёт запуск и ставит его задачей в очередь ``processing.run``; сразу возвращает запуск
        и признак, создан ли он. Если обработка уже стоит в очереди или идёт, второй запуск не
        создаётся — возвращается текущий и ``False``.
        """
        now = now or DEMO_NOW
        run, created = self.user_news_store.create_run_if_idle(now, self._run_timeout_hours())
        if created:
            top = (insights_top or SETTINGS.insight_card["pipeline_top"]) if insights else 0
            self.queue.publish(PROCESSING_QUEUE, ProcessingTask(
                run_id=run["run_id"], now=now, user_ids=user_ids, insights_top=top).dumps())
        return PipelineRun(**run), created

    @staticmethod
    def _run_timeout_hours() -> float:
        return float(SETTINGS.recommendations.get("run_timeout_hours", 3))

    def handle_processing(self, body: bytes) -> None:
        """Обработчик задачи ``processing.run``. Повторная доставка (запуск уже взят или завершён) пропускается."""
        task = ProcessingTask.loads(body)
        if not self.user_news_store.start_run(task.run_id):
            return
        self._execute_run(task.run_id, task.now, task.user_ids, task.insights_top)

    def resume_unfinished_runs(self) -> int:
        """После старта API: при очереди в процессе запуски ``running`` оборваны остановкой — ``failed``,
        запуски ``queued`` ставятся в очередь заново. С брокером задачи хранит он сам — ничего не делаем."""
        if not self.queue.in_process:
            return 0
        self.user_news_store.fail_running()
        runs = self.user_news_store.queued_runs()
        for run in runs:
            demo_now = datetime.fromisoformat(run["demo_now"]) if run.get("demo_now") else None
            self.queue.publish(PROCESSING_QUEUE, ProcessingTask(run_id=run["run_id"], now=demo_now).dumps())
        return len(runs)

    def enqueue_processing(self) -> tuple[int, bool]:
        """Ставит в обработку новости ``news.db`` со статусом ``new`` (``POST /processing.enqueue``).

        Обработка идёт в воркере очереди (:meth:`start_run`): статус новостей ``new → processing →
        ready``, затем пересчитываются ленты пользователей. Возвращает, сколько новостей ждало
        обработки, и создан ли новый запуск (``False`` — обработка уже стоит в очереди или идёт).
        """
        waiting = status_counts(NEWS_DB, "news")["new"]
        _, created = self.start_run()
        return waiting, created

    def processing_result(self) -> bool:
        """``True`` — обработка не стоит в очереди и не идёт, и в ``news.db`` есть новости со статусом
        ``ready`` (``GET /processing.result``). Состояние берётся из базы: его видно из любого процесса."""
        if self.user_news_store.active_run(self._run_timeout_hours()) is not None:
            return False
        return status_counts(NEWS_DB, "news")["ready"] > 0

    def run_status(self, run_id: str | None = None) -> PipelineRun | None:
        """Запуск по ID или последний."""
        run = self.user_news_store.run(run_id) if run_id else self.user_news_store.latest_run()
        return PipelineRun(**run) if run else None

    def _execute_run(self, run_id: str, now: datetime | None, user_ids: list[str] | None,
                     insights_top: int = 0) -> None:
        store = self.user_news_store
        try:
            with self._pipeline_lock:
                prepared = self.summarizer.process_pending()
                store.update_run(run_id, stage="dedup", news_prepared=len(prepared.processed))
                results = self.dedup.process_pending_news()
                store.update_run(run_id, stage="analytics", news_deduplicated=len(results),
                                 news_unique=sum(result is None for result in results))
                matched = self.match_pending_events()
                store.update_run(run_id, stage="recommend", events_matched=matched)
            users = user_ids or self.profiles.all_ids()
            store.update_run(run_id, users_total=len(users))
            done = failed = 0
            for user_id in users:
                try:
                    self.recompute_user(user_id, run_id=run_id, now=now)
                    done += 1
                except Exception:  # noqa: BLE001 - один пользователь не останавливает запуск
                    failed += 1
                    log.exception("рекомендации для %s", user_id)
                store.update_run(run_id, users_done=done, users_failed=failed)
            if insights_top:
                store.update_run(run_id, stage="insights")
                built = 0
                for user_id in users:
                    for row in store.for_user(user_id, limit=insights_top):
                        try:
                            self.insight_cards.build(user_id, row.news_id)
                            built += 1
                        except Exception:  # noqa: BLE001 - одна карточка не останавливает запуск
                            log.exception("карточка инсайта %s/%s", user_id, row.news_id)
                        store.update_run(run_id, insights_built=built)
            store.update_run(run_id, status="done", stage=None, finished_at=now_iso())
            log.info("обработка %s завершена: %s", run_id, store.run(run_id))
        except Exception as error:  # noqa: BLE001
            log.exception("обработка %s", run_id)
            store.update_run(run_id, status="failed", finished_at=now_iso(), error=f"{type(error).__name__}: {error}")

    def recompute_user(self, user_id: str, *, run_id: str, now: datetime | None = None) -> int:
        """Лента пользователя с аналитикой под него → ``user_news`` (заменяет прежние рекомендации)."""
        profile = self.profiles.get(user_id)
        if profile is None:
            raise KeyError(user_id)
        config = SETTINGS.recommendations
        result = self.feed.build(profile, limit=config["per_user"], window_days=config["window_days"],
                                 now=now or DEMO_NOW, version=config["feed_version"])
        items = list(self._executor.map(lambda item: self._enrich_item(profile, item, False), result.items))
        computed_at = now_iso()
        rows = []
        for rank, item in enumerate(items, 1):
            signals, analytics = item.signals, item.analytics
            rows.append(UserNewsRow(
                user_id=user_id, news_id=item.news_id, rank=rank, event_id=item.event_id, score=item.score,
                retrieval=signals.retrieval if signals else None, sem=signals.sem if signals else None,
                ent=signals.ent if signals else None, topic=signals.topic if signals else None,
                imp=signals.imp if signals else None, fresh=signals.fresh if signals else None,
                llm=signals.llm if signals else None, why_relevant=item.why_relevant,
                analytics_id=analytics.analytics_id if analytics else None,
                analytics_chunk_id=analytics.chunk_id if analytics else None,
                analytics_relation=analytics.relation if analytics else None,
                analytics_contradicts=int(analytics.contradicts) if analytics else None,
                analytics_summary=analytics.summary if analytics else None,
                run_id=run_id, computed_at=computed_at, pipeline_version=SETTINGS.pipeline_version,
            ))
        self.user_news_store.replace_user(user_id, rows)
        return len(rows)

    def user_news(self, user_id: str, *, limit: int = 20, offset: int = 0, with_text: bool = False) -> UserNewsResponse:
        """Готовые новости пользователя с аналитикой из ``user_news``. ``KeyError`` — профиля нет."""
        if self.profiles.get(user_id) is None:
            raise KeyError(user_id)
        rows = self.user_news_store.for_user(user_id, limit=limit, offset=offset)
        total = self.user_news_store.count(user_id)
        if not rows:
            return UserNewsResponse(user_id=user_id, status="not_computed" if total == 0 else "ready", total=total,
                                    items=[])
        news = {item.document_id: item for item in read_summaries(UNIQUE_DB, [row.news_id for row in rows])}
        chunks = {chunk.chunk_id: chunk for chunk in self.analytics.store.chunks(
            [row.analytics_chunk_id for row in rows if row.analytics_chunk_id])}
        cards = self.insight_cards.store.summaries(user_id, [row.news_id for row in rows])
        items = []
        for row in rows:
            item = news.get(row.news_id)
            if item is None:
                continue
            enrichment = item.metadata.get("enrichment") or {}
            topics = enrichment.get("topics") or {}
            analytics = None
            if row.analytics_chunk_id:
                chunk = chunks.get(row.analytics_chunk_id)
                analytics = UserNewsAnalytics(
                    analytics_id=row.analytics_id, chunk_id=row.analytics_chunk_id,
                    title=chunk.analytics_title if chunk else None, section_title=chunk.section_title if chunk else None,
                    published_at=chunk.published_at if chunk else None, relation=row.analytics_relation,
                    contradicts=bool(row.analytics_contradicts), summary=row.analytics_summary,
                    text=chunk.text if chunk and with_text else None, url=chunk.url if chunk else None,
                )
            sources = [entry.get("source") for entry in item.metadata.get("sources") or [] if entry.get("source")]
            items.append(UserNewsItem(
                rank=row.rank, news_id=row.news_id, event_id=row.event_id, title=item.title, lead=item.lead,
                text=item.text if with_text else None, source=item.metadata.get("source"), url=item.url,
                published_at=item.publication_time, sources=list(dict.fromkeys(sources)),
                topics=sorted(topics, key=topics.get, reverse=True), importance=enrichment.get("importance"),
                score=row.score, why_relevant=row.why_relevant, analytics=analytics, insight=cards.get(row.news_id),
                signals=Signals(sem=row.sem or 0, ent=row.ent or 0, topic=row.topic or 0, imp=row.imp or 0,
                                fresh=row.fresh or 0, retrieval=row.retrieval or 0, llm=row.llm, final=row.score),
            ))
        return UserNewsResponse(user_id=user_id, status="ready", run_id=rows[0].run_id,
                                computed_at=datetime.fromisoformat(rows[0].computed_at),
                                pipeline_version=rows[0].pipeline_version, total=total, items=items)

    def submit(self, fn, *args):
        """Фоновая задача в пуле сервиса (подготовка, не требующая очереди)."""
        return self._executor.submit(fn, *args)

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
        self.queue.close()
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

    def dataset(self) -> tuple[Any, Any]:
        """Компании и платежи из ``dataset/*.db`` (читаются один раз; платежей 1,4 млн — несколько секунд)."""
        with self._dataset_lock:
            if self._companies is None:
                from news_pipeline.storage.dataset_db import COMPANIES, PAYMENTS, exists, read_table

                self._companies = read_table(COMPANIES) if exists(COMPANIES) else None
                self._payments = read_table(PAYMENTS) if exists(PAYMENTS) else None
        return self._companies, self._payments

    def warm_up(self) -> None:
        """Заранее читает датасет и считает факты о компаниях всех профилей: иначе первый запрос
        профиля или карточки ждёт загрузки платежей (секунды)."""
        try:
            self.dataset()
            for user in self.profiles.inputs():
                self.company_facts(user)
        except Exception:  # noqa: BLE001 - прогрев не должен ронять сервис
            log.exception("прогрев датасета")

    def company_facts(self, user: UserProfileIn) -> list[CompanyFact]:
        """Факты о компании пользователя (``F1…``). Счёт по платежам датасета долгий, поэтому результат
        кэшируется по анкете: изменилась анкета — факты пересчитываются."""
        key = (user.user_id, self.profiles.input_hash(user))
        cached = self._facts_cache.get(key)
        if cached is None:
            companies, payments = self.dataset() if user.company_id else (None, None)
            cached = self._facts_cache[key] = company_facts(user, companies=companies, payments=payments)
        return cached

    def build_profile(self, user: UserProfileIn) -> InterestProfile:
        """Строит и сохраняет профиль интересов (если анкета не изменилась — берёт сохранённый)."""
        if self.profiles.is_fresh(user):
            cached = self.profiles.get(user.user_id)
            if cached is not None:
                return cached
        self.profile_status[user.user_id] = "building"
        companies, payments = self.dataset() if user.company_id else (None, None)
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

    def profile_list(self, *, limit: int = 100, offset: int = 0) -> ProfileList:
        """Профили пользователей, по которым ML-часть может строить ленты, карточки и чат."""
        items = [ProfileListItem(user_id=user.user_id, role=user.role, company_name=user.company_name,
                                 industry=user.industry, company_id=user.company_id)
                 for user in self.profiles.inputs(limit=limit, offset=offset)]
        return ProfileList(total=self.profiles.count(), items=items)

    def profile_details(self, user_id: str) -> ProfileDetails:
        """Анкета, профиль интересов, данные компании и что уже посчитано. ``KeyError`` — профиля нет."""
        interests, user = self.profiles.get(user_id), self.profiles.get_input(user_id)
        if interests is None or user is None:
            raise KeyError(user_id)
        facts = self.company_facts(user)
        feed = self.user_news_store.for_user(user_id, limit=1)
        return ProfileDetails(
            user_id=user_id, profile=user,
            interests=ProfileInterests(
                source=interests.source, entities=interests.entities, topic_weights=interests.topic_weights,
                pipeline_version=interests.pipeline_version,
                aspects=[AspectInfo(**aspect.model_dump(exclude={"embedding"})) for aspect in interests.aspects],
            ),
            company_facts=[CompanyFactInfo(id=f.id, text=f.text, kind=f.kind, origin=f.origin) for f in facts],
            activity=ProfileActivity(
                feed_status="ready" if feed else "not_computed", feed_total=self.user_news_store.count(user_id),
                feed_computed_at=datetime.fromisoformat(feed[0].computed_at) if feed else None,
                insight_cards=self.insight_cards.store.count(user_id),
                chat_session_id=self.agent.store.user_session(user_id),
            ),
        )

    # --- лента ----------------------------------------------------------------

    def recommend(self, request: FeedRequest, *, now: datetime | None = None, version: int = 4) -> FeedResponse:
        profile = self.profiles.get(request.user_id)
        if profile is None:
            raise KeyError(request.user_id)
        result = self.feed.build(profile, limit=request.limit, window_days=request.window_days, now=now, version=version)
        with_insights = request.with_insights and result.llm_available
        items = list(self._executor.map(lambda item: self._enrich_item(profile, item, with_insights), result.items))
        response = FeedResponse(
            user_id=profile.user_id,
            generated_at=datetime.now(timezone.utc),
            pipeline_version=SETTINGS.pipeline_version,
            llm_available=result.llm_available,
            items=items,
        )
        if now is None:
            self.agent.store.put_feed(response)  # контекст агента — то, что пользователь видит в ленте
        return response

    def _news_row(self, news_id: str):
        rows = read_summaries(UNIQUE_DB, [news_id])
        return rows[0] if rows else None

    def user_analytics(self, profile: InterestProfile, event_id: str, row) -> FeedAnalytics | None:
        """Фрагмент аналитики под пользователя и связка (§9); ``row`` — новость из ``unique.db``."""
        if self.analytics.get(event_id) is None:
            # Воркер ещё не дошёл до события — подбираем сразу, чтобы блок не пустовал.
            event = self.event_input(event_id)
            if event is not None:
                self.analytics.match_event(event)
        return self.analytics.for_user(profile, event_id, news_title=row.title, news_lead=row.lead)

    def _enrich_item(self, profile: InterestProfile, item: FeedItem, with_insight: bool) -> FeedItem:
        """Аналитика под пользователя (§9) и карточка инсайта для элемента ленты."""
        row = self._news_row(item.news_id)
        if row is None:
            return item
        update: dict[str, Any] = {}
        try:
            update["analytics"] = self.user_analytics(profile, item.event_id or f"e_{item.news_id}", row)
        except Exception:  # noqa: BLE001 - аналитика не должна ломать ленту
            log.exception("аналитика для %s", item.news_id)
        sources = [s.get("source") for s in row.metadata.get("sources") or [] if s.get("source")]
        update["sources"] = list(dict.fromkeys(sources)) or item.sources
        if with_insight:
            try:
                card = self.insight_cards.build(profile.user_id, item.news_id)
                if card.headline is not None:
                    update.update(insight=card.headline.text,
                                  insight_sources=[source.id for source in card.evidence.sources])
            except Exception:  # noqa: BLE001
                log.exception("карточка инсайта для %s", item.news_id)
        return item.model_copy(update=update)

    # --- карточки инсайтов ------------------------------------------------------------

    def insight_card(self, user_id: str, news_id: str, *, refresh: bool = False) -> InsightCard:
        """Карточка инсайта (строится или берётся готовая). ``KeyError`` — нет профиля, ``LookupError`` — новости."""
        return self.insight_cards.build(user_id, news_id, refresh=refresh)

    def cached_insight_card(self, user_id: str, news_id: str) -> InsightCard | None:
        return self.insight_cards.get(user_id, news_id)

    # --- фидбэк и состояние -----------------------------------------------------

    def record_feedback(self, event: FeedbackEvent) -> None:
        self.feedback.add(event, SETTINGS.pipeline_version)

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "pipeline_version": SETTINGS.pipeline_version,
            "llm_enabled": self.use_llm,
            "llm_available": self.llm.is_available(),
            "llm_models": {task: self.llm.model_for(task) for task in SETTINGS.llm_tasks if task != "default"},
            "analytics_materials": self.analytics.store.material_count(),
            "analytics_events": len(self.analytics.store.matched_events()),
            "news_status": {"news.db": status_counts(NEWS_DB, "news"),
                            "new_summary.db": status_counts(SUMMARY_DB, "news_summary")},
            "news_unique": len(summary_ids(UNIQUE_DB)),
            "indexed": self.dedup.index.count(),
            "profiles": len(self.profiles.all_ids()),
        }

    @staticmethod
    def dumps(model: Any) -> str:
        return json.dumps(model, ensure_ascii=False, default=str)
