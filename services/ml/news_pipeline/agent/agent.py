"""Агент бизнес-инсайтов и диалога (docs/agent_tz.md).

    agent = BusinessInsightAgent(service)
    answer = agent.ask("u_01", "Что значит для нас решение ЦБ по ставке?")
    answer = agent.brief("u_01")                       # главное за период

Шаги одного ответа:

1. **контекст** — лента пользователя (кэш или быстрая сборка) → пакет
   доказательств N*/A*/P (:mod:`context`); нет релевантных источников —
   честный ответ «данных нет» без вызова LLM;
2. **планировщик** — схема ответа и источники разделов (:mod:`planner`);
3. **писатели** — разделы параллельно (:mod:`writers`);
4. **программная проверка** (:mod:`checker`) — раздел с ошибкой переписывается
   с текстом замечания (до ``section_retries`` раз); не помогло — полный
   перезапуск с планировщика (``max_restarts``), затем раздел выкидывается
   (статус ``degraded``);
5. **оценщик** (:mod:`critic`) — принято → ответ; правки по разделам →
   переписываются только они; ``replan`` → планировщик с правками. Раундов
   не больше ``max_iterations``; отдаётся версия с лучшей оценкой
   (``best_effort``, если ни одна не принята).

Без LLM — экстрактивный ответ (``fallback``). Каждый ответ пишется в
``data/chat.db`` и журнал ``data/logs/agent.jsonl``. ``on_event`` получает
этапы для потоковой выдачи (SSE).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from news_pipeline.agent.assembler import (
    FALLBACK_NOTE,
    NO_EVIDENCE,
    OUT_OF_SCOPE,
    build_sections,
    build_sources,
    fallback_sections,
    render,
    summary_of,
)
from news_pipeline.agent.checker import AnswerChecker, attach_citations, errors, notes_for
from news_pipeline.agent.context import EvidenceBuilder
from news_pipeline.agent.critic import Critic
from news_pipeline.agent.planner import Planner
from news_pipeline.agent.schemas import (
    AgentAnswer,
    AgentQuality,
    EvidencePack,
    Issue,
    Plan,
    Review,
    SectionDraft,
)
from news_pipeline.agent.store import ChatStore
from news_pipeline.agent.writers import SectionWriter
from news_pipeline.config import DATA_DIR, UNIQUE_DB
from news_pipeline.llm.client import LLM, LLMUnavailable
from news_pipeline.schemas import FeedItem, InterestProfile
from news_pipeline.settings import SETTINGS
from news_pipeline.storage.news_db import read_summaries

if TYPE_CHECKING:
    from news_pipeline.service import MLService

log = logging.getLogger("rec_news.agent")
LOG_PATH = DATA_DIR / "logs" / "agent.jsonl"
BRIEF_QUESTION = "Что главное произошло для моей компании за последние дни?"

EventHandler = Callable[[str, dict[str, Any]], None]


class BusinessInsightAgent:
    def __init__(self, service: "MLService", *, llm: LLM | None = None, store: ChatStore | None = None,
                 config: dict[str, Any] | None = None) -> None:
        self.service = service
        self.llm = llm or service.llm
        self.config = config or SETTINGS.agent
        self.store = store or ChatStore()
        self.context = EvidenceBuilder(service.dedup.index, service.analytics, config=self.config)
        self.planner = Planner(self.llm)
        self.writer = SectionWriter(self.llm, service.analytics.store)
        self.checker = AnswerChecker(self.config)
        self.critic = Critic(self.llm, self.config)
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    # --- публичные методы -----------------------------------------------------

    def brief(self, user_id: str, *, session_id: str | None = None, now: datetime | None = None,
              on_event: EventHandler | None = None) -> AgentAnswer:
        """Главное за период — тот же конвейер со схемой «дайджест»."""
        return self.ask(user_id, BRIEF_QUESTION, session_id=session_id, now=now, on_event=on_event, brief=True)

    def ask(self, user_id: str, question: str, *, session_id: str | None = None, now: datetime | None = None,
            on_event: EventHandler | None = None, brief: bool = False) -> AgentAnswer:
        """Ответ на вопрос пользователя. ``KeyError`` — профиля нет."""
        run = _Run(user_id=user_id, question=question.strip(), emit=on_event or (lambda stage, data: None))
        profile = self.service.profiles.get(user_id)
        if profile is None:
            raise KeyError(user_id)
        user = self.service.profiles.get_input(user_id)
        run.session_id = self.store.open_session(user_id, session_id)
        dialog = self.store.recent_dialog(run.session_id, self.config["history_turns"])
        self.store.add_turn(run.session_id, "user", run.question)

        with run.timer("context"):
            feed = self._feed(profile, now)
            previous = self.store.evidence(run.session_id) if dialog else None
            pack = self.context.build(run.question, profile=profile, user=user, feed=feed, now=now,
                                      brief=brief, previous=previous)
            self.store.put_evidence(run.session_id, pack)
        run.emit("context", {"evidence": [{"id": i.id, "title": i.title, "relevance": i.relevance} for i in pack.items],
                             "relevant": pack.relevant})

        if not self.llm.is_available():
            answer = self._fallback(run, pack)
        elif not pack.relevant and not dialog:
            answer = self._template(run, "no_evidence", NO_EVIDENCE, llm_available=True)
        else:
            try:
                answer = self._generate(run, pack, dialog, brief)
            except LLMUnavailable:
                log.warning("LLM пропала во время ответа — запасной режим")
                answer = self._fallback(run, pack)
        self.store.add_turn(run.session_id, "assistant", answer.text, answer)
        run.emit("answer", answer.model_dump(mode="json"))
        self._log(run, answer, pack)
        return answer

    # --- генерация -------------------------------------------------------------

    def _generate(self, run: "_Run", pack: EvidencePack, dialog: list[tuple[str, str]], brief: bool) -> AgentAnswer:
        cfg = self.config
        plan: Plan | None = None
        drafts: dict[str, SectionDraft] = {}
        review: Review | None = None
        need_plan, planner_feedback = True, None
        best: dict[str, Any] | None = None
        while True:
            if need_plan:
                with run.timer("planner"):
                    plan = self.planner.plan(run.question, pack, dialog=dialog, brief=brief,
                                             previous=plan, feedback=planner_feedback)
                run.emit("plan", plan.to_dict())
                if plan.out_of_scope:
                    return self._template(run, "out_of_scope", OUT_OF_SCOPE, plan=plan)
                if plan.clarify:
                    return self._template(run, "clarify", plan.clarify, plan=plan, clarify=plan.clarify)
                with run.timer("writers"):
                    drafts = self.writer.write_many(plan.sections, plan, pack, run.question)
                need_plan = False
            else:
                notes = self._critic_notes(review, plan, drafts)
                targets = [s for s in plan.sections if s.id in notes]
                with run.timer("writers"):
                    drafts.update(self.writer.write_many(
                        targets, plan, pack, run.question, notes,
                        {s.id: drafts[s.id].attempts + 1 for s in targets if s.id in drafts}))
            run.emit("sections", {sid: d.text for sid, d in drafts.items()})

            with run.timer("checks"):
                issues = self._repair(run, plan, drafts, pack)
            failing = {issue.section_id for issue in errors(issues)}
            if failing and run.restarts < cfg["max_restarts"]:
                run.restarts += 1
                need_plan = True
                planner_feedback = [f"раздел {i.section_id}: {i.message}" for i in errors(issues)]
                run.emit("restart", {"reason": planner_feedback})
                continue
            used = self._without(plan, failing)

            run.iterations += 1
            with run.timer("critic"):
                review = self.critic.review(run.question, used, drafts, pack,
                                            [i for i in issues if i.severity == "warning"])
            run.emit("review", review.to_dict() if review else {"accepted": True, "note": "оценщик не ответил"})
            score = review.overall if review else 0.0
            if best is None or score > best["score"]:
                best = {"score": score, "plan": used, "drafts": dict(drafts), "review": review, "issues": issues,
                        "degraded": bool(failing)}
            if review is None or review.accepted or run.iterations >= cfg["max_iterations"]:
                break
            if review.replan or not self._critic_notes(review, used, drafts):
                need_plan = True
                planner_feedback = [f"{f['problem']} → {f['fix']}" for f in review.feedback] or [review.comment]
            plan = used

        final_review = best["review"]
        if best["degraded"]:
            status = "degraded"
        elif final_review is None or final_review.accepted:
            status = "ok"
        else:
            status = "best_effort"
        return self._answer(run, status, best["plan"], best["drafts"], pack, final_review, best["issues"])

    def _repair(self, run: "_Run", plan: Plan, drafts: dict[str, SectionDraft], pack: EvidencePack) -> list[Issue]:
        """Проверка и точечное переписывание разделов с ошибками."""
        retries = self.config["section_retries"]
        for attempt in range(retries + 1):
            for section in plan.sections:
                if section.id in drafts:
                    drafts[section.id] = attach_citations(section, drafts[section.id], pack)
            issues = self.checker.check(plan, drafts, pack, run.question)
            run.emit("check", {"attempt": attempt, "issues": [i.to_dict() for i in issues]})
            targets = [s for s in plan.sections if notes_for(issues, s.id)]
            if not targets or attempt == retries:
                return issues
            notes = {s.id: self._previous(drafts.get(s.id)) + notes_for(issues, s.id) for s in targets}
            drafts.update(self.writer.write_many(
                targets, plan, pack, run.question, notes,
                {s.id: drafts[s.id].attempts + 1 for s in targets if s.id in drafts}))
        return issues

    @staticmethod
    def _previous(draft: SectionDraft | None) -> list[str]:
        return [f"твоя предыдущая версия: «{draft.text}»"] if draft and draft.text else []

    def _critic_notes(self, review: Review | None, plan: Plan, drafts: dict[str, SectionDraft]) -> dict[str, list[str]]:
        """Правки оценщика по разделам; общие правки — всем разделам."""
        if review is None:
            return {}
        notes: dict[str, list[str]] = {}
        ids = {s.id for s in plan.sections}
        for item in review.feedback:
            text = f"{item['problem']} → {item['fix']}".strip(" →")
            targets = [item["section_id"]] if item["section_id"] in ids else sorted(ids)
            for sid in targets:
                notes.setdefault(sid, self._previous(drafts.get(sid))).append(text)
        return notes

    @staticmethod
    def _without(plan: Plan, failing: set[str | None]) -> Plan:
        """План без разделов, которые так и не прошли проверку (если остаётся хоть один)."""
        keep = [s for s in plan.sections if s.id not in failing]
        if not keep or len(keep) == len(plan.sections):
            return plan
        return Plan(intent=plan.intent, answer_type=plan.answer_type, headline_goal=plan.headline_goal,
                    sections=keep, out_of_scope=plan.out_of_scope, clarify=plan.clarify, source=plan.source)

    # --- контекст ---------------------------------------------------------------

    def _feed(self, profile: InterestProfile, now: datetime | None) -> list[FeedItem]:
        """Лента, которую видит пользователь (``user_news`` последней обработки, ``GET /users/{id}/news``);
        её нет — последняя сохранённая лента; нет и её — быстрая лента без LLM-реранка."""
        rows = self.service.user_news_store.for_user(profile.user_id, limit=self.config["feed_limit"])
        if rows:
            titles = {item.document_id: item.title for item in read_summaries(UNIQUE_DB, [row.news_id for row in rows])}
            return [FeedItem(news_id=row.news_id, event_id=row.event_id, title=titles.get(row.news_id, ""),
                             score=row.score, why_relevant=row.why_relevant) for row in rows]
        if now is None:
            cached = self.store.feed(profile.user_id, self.config["feed_ttl_hours"])
            if cached is not None:
                return cached.items
        result = self.service.feed.build(profile, limit=self.config["feed_limit"], window_days=7, now=now,
                                         version=self.config["feed_version"])
        return result.items

    # --- ответы -----------------------------------------------------------------

    def _answer(self, run: "_Run", status: str, plan: Plan, drafts: dict[str, SectionDraft], pack: EvidencePack,
                review: Review | None, issues: list[Issue]) -> AgentAnswer:
        sections = build_sections(plan, drafts)
        sources = build_sources(sections, pack)
        return self._make(run, status, summary=summary_of(sections), text=render(sections, sources),
                          sections=sections, sources=sources, plan=plan.to_dict(),
                          quality=AgentQuality(iterations=run.iterations, restarts=run.restarts,
                                               critic=review.to_dict() if review else None,
                                               checks=[i.to_dict() for i in issues]))

    def _template(self, run: "_Run", status: str, text: str, *, plan: Plan | None = None,
                  clarify: str | None = None, llm_available: bool = True) -> AgentAnswer:
        return self._make(run, status, summary=text, text=text, plan=plan.to_dict() if plan else None,
                          clarify=clarify, llm_available=llm_available)

    def _fallback(self, run: "_Run", pack: EvidencePack) -> AgentAnswer:
        sections = fallback_sections(pack)
        if not sections or not pack.relevant:
            return self._template(run, "no_evidence", NO_EVIDENCE, llm_available=False)
        sources = build_sources(sections, pack)
        return self._make(run, "fallback", summary=FALLBACK_NOTE, text=render(sections, sources, FALLBACK_NOTE),
                          sections=sections, sources=sources, llm_available=False)

    def _make(self, run: "_Run", status: str, **fields: Any) -> AgentAnswer:
        fields.setdefault("quality", AgentQuality(iterations=run.iterations, restarts=run.restarts))
        return AgentAnswer(
            answer_id=f"a_{uuid.uuid4().hex[:12]}", session_id=run.session_id, user_id=run.user_id,
            status=status, question=run.question, pipeline_version=SETTINGS.pipeline_version,
            timings_ms=run.timings_ms(), created_at=datetime.now(timezone.utc), **fields,
        )

    def _log(self, run: "_Run", answer: AgentAnswer, pack: EvidencePack) -> None:
        record = {
            "time": answer.created_at.isoformat(timespec="seconds"), "user_id": run.user_id,
            "session_id": run.session_id, "answer_id": answer.answer_id, "question": run.question,
            "status": answer.status, "evidence": [i.id + ":" + (i.news_id or i.chunk_id or "") for i in pack.items],
            "best_relevance": pack.best_relevance, "plan": answer.plan, "quality": answer.quality.model_dump(),
            "timings_ms": answer.timings_ms, "models": {t: self.llm.model_for(t) for t in
                                                        ("agent_planner", "agent_writer", "agent_critic")},
        }
        with LOG_PATH.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


class _Run:
    """Состояние одного ответа: счётчики, время этапов, отправка событий."""

    def __init__(self, user_id: str, question: str, emit: EventHandler) -> None:
        self.user_id, self.question, self._emit = user_id, question, emit
        self.session_id = ""
        self.iterations = self.restarts = 0
        self._started = time.monotonic()
        self._timings: dict[str, float] = {}

    @contextmanager
    def timer(self, stage: str):
        started = time.monotonic()
        try:
            yield
        finally:
            self._timings[stage] = self._timings.get(stage, 0.0) + time.monotonic() - started

    def timings_ms(self) -> dict[str, int]:
        result = {stage: int(seconds * 1000) for stage, seconds in self._timings.items()}
        result["total"] = int((time.monotonic() - self._started) * 1000)
        return result

    def emit(self, stage: str, data: dict[str, Any]) -> None:
        try:
            self._emit(stage, data)
        except Exception:  # noqa: BLE001 - сбой потребителя событий не ломает ответ
            log.exception("обработчик событий агента")
