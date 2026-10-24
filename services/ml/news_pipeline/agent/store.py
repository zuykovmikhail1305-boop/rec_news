"""Диалоги агента и последние ленты пользователей — ``data/chat.db``, ORM-модели (docs/agent_tz.md §6).

* :class:`ChatSession` (``sessions``) — сессия чата и зафиксированный пакет доказательств;
* :class:`ChatMessage` (``messages``) — реплики пользователя и ответы агента (``AgentAnswer`` в JSON);
* :class:`UserFeed` (``feeds``) — последняя лента пользователя (контекст агента без пересборки);
* :class:`ChatFeedbackRow` (``chat_feedback``) — оценки ответов.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import Integer, Text, func, select
from sqlalchemy.orm import Mapped, mapped_column

from news_pipeline.agent.schemas import AgentAnswer, ChatFeedback, ChatHistory, ChatTurn, EvidencePack
from news_pipeline.config import DATA_DIR
from news_pipeline.schemas import FeedResponse
from news_pipeline.storage import db

CHAT_DB = DATA_DIR / "chat.db"


class ChatSession(db.Base):
    __tablename__ = "sessions"

    session_id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)
    evidence: Mapped[str | None] = mapped_column(Text)


class ChatMessage(db.Base):
    __tablename__ = "messages"

    session_id: Mapped[str] = mapped_column(Text, primary_key=True)
    turn: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    answer: Mapped[str | None] = mapped_column(Text)
    answer_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)


class UserFeed(db.Base):
    __tablename__ = "feeds"

    user_id: Mapped[str] = mapped_column(Text, primary_key=True)
    generated_at: Mapped[str] = mapped_column(Text)
    feed: Mapped[str] = mapped_column(Text)


class ChatFeedbackRow(db.Base):
    __tablename__ = "chat_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    answer_id: Mapped[str | None] = mapped_column(Text)
    session_id: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[str | None] = mapped_column(Text)
    type: Mapped[str | None] = mapped_column(Text)
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str | None] = mapped_column(Text)


MODELS = [ChatSession, ChatMessage, UserFeed, ChatFeedbackRow]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ChatStore:
    def __init__(self, path: str | Path = CHAT_DB) -> None:
        self.path = Path(path)
        self.engine = db.engine(self.path, wal=True)
        db.ensure_tables(self.engine, MODELS)

    # --- сессии ---------------------------------------------------------------

    def _session_user(self, session_id: str) -> str | None:
        with db.session(self.engine) as session:
            chat = session.get(ChatSession, session_id)
            return chat.user_id if chat else None

    def user_session(self, user_id: str) -> str | None:
        """Текущий (последний) диалог пользователя; ``None`` — пользователь ещё не писал в чат."""
        with db.session(self.engine) as session:
            return session.scalar(select(ChatSession.session_id).where(ChatSession.user_id == user_id)
                                  .order_by(ChatSession.created_at.desc(), ChatSession.session_id.desc()).limit(1))

    def open_session(self, user_id: str, session_id: str | None) -> str:
        """Существующая сессия пользователя или новая."""
        if session_id:
            owner = self._session_user(session_id)
            if owner is not None:
                if owner != user_id:
                    raise PermissionError(f"сессия {session_id} принадлежит другому пользователю")
                return session_id
        session_id = session_id or f"c_{uuid.uuid4().hex[:12]}"
        with db.session(self.engine) as session:
            session.add(ChatSession(session_id=session_id, user_id=user_id, created_at=_now()))
        return session_id

    def evidence(self, session_id: str) -> EvidencePack | None:
        with db.session(self.engine) as session:
            chat = session.get(ChatSession, session_id)
        return EvidencePack.from_dict(json.loads(chat.evidence)) if chat and chat.evidence else None

    def put_evidence(self, session_id: str, pack: EvidencePack) -> None:
        with db.session(self.engine) as session:
            chat = session.get(ChatSession, session_id)
            if chat is not None:
                chat.evidence = json.dumps(pack.to_dict(), ensure_ascii=False)

    # --- реплики --------------------------------------------------------------

    def add_turn(self, session_id: str, role: str, text: str, answer: AgentAnswer | None = None) -> int:
        with db.session(self.engine) as session:
            turn = session.scalar(select(func.coalesce(func.max(ChatMessage.turn), 0) + 1)
                                  .where(ChatMessage.session_id == session_id))
            session.add(ChatMessage(
                session_id=session_id, turn=turn, role=role, text=text,
                answer=answer.model_dump_json() if answer else None,
                answer_id=answer.answer_id if answer else None, created_at=_now(),
            ))
        return turn

    def history(self, session_id: str, limit: int | None = None) -> ChatHistory | None:
        user_id = self._session_user(session_id)
        if user_id is None:
            return None
        with db.session(self.engine) as session:
            messages = list(session.scalars(select(ChatMessage).where(ChatMessage.session_id == session_id)
                                            .order_by(ChatMessage.turn)))
        if limit is not None:
            messages = messages[-limit:]
        turns = [
            ChatTurn(turn=message.turn, role=message.role, text=message.text,
                     answer=AgentAnswer.model_validate_json(message.answer) if message.answer else None,
                     created_at=datetime.fromisoformat(message.created_at))
            for message in messages
        ]
        return ChatHistory(session_id=session_id, user_id=user_id, turns=turns)

    def recent_dialog(self, session_id: str, turns: int) -> list[tuple[str, str]]:
        """Последние ``turns`` пар «вопрос — краткий ответ» для планировщика."""
        history = self.history(session_id, limit=turns * 2)
        if history is None:
            return []
        return [(turn.role, turn.answer.summary if turn.answer else turn.text) for turn in history.turns]

    def feedback(self, session_id: str, user_id: str, item: ChatFeedback) -> None:
        with db.session(self.engine) as session:
            session.add(ChatFeedbackRow(answer_id=item.answer_id, session_id=session_id, user_id=user_id,
                                        type=item.type, comment=item.comment, created_at=_now()))

    # --- ленты ----------------------------------------------------------------

    def put_feed(self, feed: FeedResponse) -> None:
        with db.session(self.engine) as session:
            session.execute(db.upsert(UserFeed), [{"user_id": feed.user_id, "generated_at": _now(),
                                                   "feed": feed.model_dump_json()}])

    def feed(self, user_id: str, ttl_hours: float) -> FeedResponse | None:
        with db.session(self.engine) as session:
            row = session.get(UserFeed, user_id)
        if row is None:
            return None
        if datetime.now(timezone.utc) - datetime.fromisoformat(row.generated_at) > timedelta(hours=ttl_hours):
            return None
        return FeedResponse.model_validate_json(row.feed)
