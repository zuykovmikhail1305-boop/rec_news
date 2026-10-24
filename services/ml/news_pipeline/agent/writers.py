"""Агенты-писатели — по вызову LLM на раздел (docs/agent_tz.md §4.2).

Писатель видит вопрос, цель всего ответа, свой раздел, цели соседних
разделов (чтобы не повторяться) и только свои источники. Разделы пишутся
параллельно. Замечания проверки и оценщика передаются при переписывании.

Число одновременных писателей — ``REC_NEWS_AGENT_PARALLEL_WRITERS`` (по умолчанию
``parallel_writers`` из ``ml.toml``). LM Studio делит контекст модели между
одновременными запросами; если сервер ответил «контекст переполнен», такие
разделы переписываются по одному.

Для ``bank_view`` в промпт добавляется образец стиля аналитики банка (П13):
2 абзаца из материалов аналитики с замаскированными числами — модель берёт
из них манеру, но не факты.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from functools import cached_property

from news_pipeline.agent.schemas import EvidencePack, Plan, PlanSection, SectionDraft
from news_pipeline.agent.style import KINDS, TONE_PROMPT, section_title
from news_pipeline.analytics.store import AnalyticsStore
from news_pipeline.env import env_int
from news_pipeline.llm.client import LLM, LLMError, LLMUnavailable
from news_pipeline.preprocessing.cleaning import sentences
from news_pipeline.prompts import prompt
from news_pipeline.settings import SETTINGS

_PROMPT = prompt("agent.writer")

_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "sources": {"type": "array", "items": {"type": "string"}}},
                "required": ["text", "sources"],
            },
        },
    },
    "required": ["text", "claims"],
}

_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")


def _validate(data: dict) -> dict:
    text = str(data.get("text") or "").strip()
    # markdown и списки — в связный текст
    text = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE).replace("**", "")
    text = " ".join(text.split())
    if not text:
        raise LLMError("пустой текст раздела")
    claims = [c for c in data.get("claims") or [] if isinstance(c, dict) and str(c.get("text") or "").strip()]
    return {"text": text, "claims": claims}


class SectionWriter:
    def __init__(self, llm: LLM, analytics_store: AnalyticsStore | None = None) -> None:
        self.llm = llm
        self.analytics_store = analytics_store
        self.workers = env_int("REC_NEWS_AGENT_PARALLEL_WRITERS", SETTINGS.agent["parallel_writers"])

    @cached_property
    def style_sample(self) -> str:
        """Образец стиля аналитики банка: 2 абзаца прозы, числа замаскированы."""
        if self.analytics_store is None:
            return ""
        samples = []
        for chunk in self.analytics_store.chunks():
            prose = [s for s in sentences(chunk.text) if 60 <= len(s) <= 260]
            if len(prose) >= 3:
                samples.append(" ".join(prose[:3]))
            if len(samples) >= 2:
                break
        return "\n\n".join(_NUMBER_RE.sub("…", sample) for sample in samples)

    def write(
        self,
        section: PlanSection,
        plan: Plan,
        pack: EvidencePack,
        question: str,
        notes: list[str] | None = None,
        attempt: int = 1,
    ) -> SectionDraft:
        extra = ""
        if section.kind == "bank_view" and self.style_sample:
            extra = ("Образец манеры аналитиков Альфа-Банка — только стиль, факты и числа из него не бери:\n"
                     f"«{self.style_sample}»\n")
        system = _PROMPT.format(tone=TONE_PROMPT, title=section_title(section.kind), rule=KINDS[section.kind]["rule"],
                                max_sentences=section.max_sentences, extra=extra)
        neighbours = [f"- «{section_title(s.kind)}»: {s.goal}" for s in plan.sections if s.id != section.id]
        parts = [
            f"Вопрос пользователя: {question}",
            f"Цель всего ответа: {plan.headline_goal}",
            f"Твой раздел — «{section_title(section.kind)}». Цель: {section.goal}",
        ]
        if neighbours:
            parts.append("Соседние разделы (их содержание не повторяй):\n" + "\n".join(neighbours))
        blocks = [pack.get(e).block() for e in section.evidence if pack.get(e) is not None]
        parts.append("Источники раздела:\n" + "\n\n".join(blocks))
        if notes:
            parts.append("Предыдущая версия раздела не прошла проверку. Исправь:\n" + "\n".join(f"- {n}" for n in notes))
        try:
            data = self.llm.json(
                "agent_writer",
                [{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join(parts)}],
                schema=_SCHEMA,
                cache_key=section.kind,
                validator=_validate,
                max_tokens=SETTINGS.agent["max_tokens"]["writer"],
                retries=1,
            )
        except LLMUnavailable:
            raise
        except LLMError as error:
            return SectionDraft(section.id, "", attempts=attempt, error=str(error))
        return SectionDraft(section.id, data["text"], data["claims"], attempts=attempt)

    def write_many(
        self,
        sections: list[PlanSection],
        plan: Plan,
        pack: EvidencePack,
        question: str,
        notes: dict[str, list[str]] | None = None,
        attempts: dict[str, int] | None = None,
    ) -> dict[str, SectionDraft]:
        notes, attempts = notes or {}, attempts or {}

        def write(section: PlanSection) -> SectionDraft:
            return self.write(section, plan, pack, question, notes.get(section.id), attempts.get(section.id, 1))

        with ThreadPoolExecutor(max(1, min(self.workers, len(sections)))) as pool:
            drafts = {draft.section_id: draft for draft in pool.map(write, sections)}
        overflow = [s for s in sections if _context_overflow(drafts[s.id].error)]
        if overflow and self.workers > 1:
            for section in overflow:  # по одному: весь контекст сервера достаётся одному запросу
                drafts[section.id] = write(section)
        return drafts


def _context_overflow(error: str | None) -> bool:
    text = (error or "").lower()
    return "context" in text and ("exceed" in text or "length" in text or "overflow" in text)
