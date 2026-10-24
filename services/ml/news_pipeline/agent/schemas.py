"""Схемы агента бизнес-инсайтов (docs/agent_tz.md §3–§7).

* API: :class:`ChatRequest`, :class:`BriefRequest`, :class:`AgentAnswer`,
  :class:`ChatFeedback`, :class:`ChatHistory`;
* внутренние: пакет доказательств (:class:`Evidence`, :class:`EvidencePack`),
  план (:class:`Plan`), черновик раздела (:class:`SectionDraft`), замечание
  проверки (:class:`Issue`), оценка (:class:`Review`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Status = Literal["ok", "best_effort", "degraded", "fallback", "no_evidence", "clarify", "out_of_scope"]

# --- API ----------------------------------------------------------------------


class UserChatMessage(BaseModel):
    """Сообщение пользователя в его чат (``POST /chat/{user_id}``).

    Диалог у пользователя один: сообщение продолжает текущий диалог, и агент помнит последние
    реплики и источники. ``new_dialog = true`` — начать новый диалог (история прежнего сохраняется).
    """

    message: str = Field(min_length=1, max_length=2000)
    new_dialog: bool = False
    now: datetime | None = None  # «текущий момент» для демо на синтетике; по умолчанию REC_NEWS_DEMO_NOW


class ChatRequest(BaseModel):
    user_id: str
    question: str = Field(min_length=1, max_length=2000)
    session_id: str | None = None
    now: datetime | None = None  # для демо на синтетических данных


class BriefRequest(BaseModel):
    user_id: str
    session_id: str | None = None
    now: datetime | None = None


class AgentSection(BaseModel):
    id: str
    kind: str
    title: str
    origin: Literal["bank", "ai"]
    text: str
    sources: list[str] = Field(default_factory=list)


class AgentSource(BaseModel):
    id: str
    type: Literal["news", "analytics", "profile"]
    title: str
    source: str | None = None
    published_at: str | None = None
    url: str | None = None
    news_id: str | None = None
    chunk_id: str | None = None


class AgentQuality(BaseModel):
    iterations: int = 0
    restarts: int = 0
    critic: dict[str, Any] | None = None
    checks: list[dict[str, Any]] = Field(default_factory=list)


class AgentAnswer(BaseModel):
    answer_id: str
    session_id: str
    user_id: str
    status: Status
    question: str
    summary: str
    text: str
    sections: list[AgentSection] = Field(default_factory=list)
    sources: list[AgentSource] = Field(default_factory=list)
    clarify: str | None = None
    plan: dict[str, Any] | None = None
    quality: AgentQuality = Field(default_factory=AgentQuality)
    llm_available: bool = True
    pipeline_version: str = ""
    timings_ms: dict[str, int] = Field(default_factory=dict)
    created_at: datetime


class ChatFeedback(BaseModel):
    answer_id: str
    type: Literal["useful", "not_useful"]
    comment: str | None = None


class ChatTurn(BaseModel):
    turn: int
    role: Literal["user", "assistant"]
    text: str
    answer: AgentAnswer | None = None
    created_at: datetime


class ChatHistory(BaseModel):
    session_id: str | None   # None — пользователь ещё не писал в чат
    user_id: str
    turns: list[ChatTurn]


# --- пакет доказательств ------------------------------------------------------


@dataclass
class Evidence:
    """Источник в пакете: ``N*`` — новость, ``A*`` — фрагмент аналитики, ``P`` — профиль."""

    id: str
    type: Literal["news", "analytics", "profile"]
    title: str
    text: str
    relevance: float = 0.0
    source: str | None = None
    published_at: str | None = None
    url: str | None = None
    news_id: str | None = None
    event_id: str | None = None
    chunk_id: str | None = None
    origin: str = "feed"  # feed / search / event
    relation: str | None = None
    contradicts: bool = False
    why: str | None = None

    def brief(self) -> str:
        """Одна строка для планировщика."""
        if self.type == "profile":
            return f"{self.id} — профиль пользователя и компании"
        date = (self.published_at or "")[:10]
        first = self.text.split("\n")[-1][:220]
        extra = f"; связь: {self.relation}" + ("; ПРОТИВОРЕЧИТ новости" if self.contradicts else "") if self.relation else ""
        return f"{self.id} — «{self.title}» ({self.source or 'аналитика Альфа-Банка'}, {date}{extra}): {first}"

    def block(self) -> str:
        """Полный блок для писателя: текст обёрнут как данные, а не инструкции."""
        if self.type == "profile":
            head = "профиль пользователя"
        elif self.type == "news":
            head = f"новость; {self.source or '—'}; {(self.published_at or '')[:10]}"
        else:
            head = (f"аналитика Альфа-Банка; {(self.published_at or '')[:10]}; связь с новостью: {self.relation or '—'}"
                    + ("; противоречит новости" if self.contradicts else ""))
        return f'<источник id="{self.id}" тип="{head}">\n{self.title}\n{self.text}\n</источник>'

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class EvidencePack:
    items: list[Evidence]
    relevant: bool = True
    best_relevance: float = 0.0

    def get(self, evidence_id: str) -> Evidence | None:
        return next((item for item in self.items if item.id == evidence_id), None)

    @property
    def ids(self) -> set[str]:
        return {item.id for item in self.items}

    def of_type(self, kind: str) -> list[Evidence]:
        return [item for item in self.items if item.type == kind]

    def to_dict(self) -> dict[str, Any]:
        return {"items": [item.to_dict() for item in self.items], "relevant": self.relevant,
                "best_relevance": self.best_relevance}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvidencePack":
        return cls([Evidence(**item) for item in data.get("items", [])], data.get("relevant", True),
                   data.get("best_relevance", 0.0))


# --- план, черновики, проверка, оценка ----------------------------------------


@dataclass
class PlanSection:
    id: str
    kind: str
    goal: str
    evidence: list[str]
    max_sentences: int = 3


@dataclass
class Plan:
    intent: str
    answer_type: str
    headline_goal: str
    sections: list[PlanSection]
    out_of_scope: bool = False
    clarify: str | None = None
    source: str = "llm"  # llm / rules

    def section(self, section_id: str) -> PlanSection | None:
        return next((s for s in self.sections if s.id == section_id), None)

    def to_dict(self) -> dict[str, Any]:
        return {"intent": self.intent, "answer_type": self.answer_type, "headline_goal": self.headline_goal,
                "out_of_scope": self.out_of_scope, "clarify": self.clarify, "source": self.source,
                "sections": [s.__dict__ for s in self.sections]}


@dataclass
class SectionDraft:
    section_id: str
    text: str
    claims: list[dict[str, Any]] = field(default_factory=list)
    attempts: int = 1
    error: str | None = None
    auto_citations: int = 0  # ссылок, проставленных программно (attach_citations)


@dataclass
class Issue:
    section_id: str | None
    code: str
    severity: Literal["error", "warning"]
    message: str

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class Review:
    scores: dict[str, int]
    overall: float
    accepted: bool
    feedback: list[dict[str, str]]
    replan: bool
    comment: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)
