"""Диалоги агента и последние ленты пользователей — ``data/chat.db`` на SQLAlchemy (docs/agent_tz.md §6).

* ``sessions`` — сессия чата и зафиксированный пакет доказательств;
* ``messages`` — реплики пользователя и ответы агента (``AgentAnswer`` в JSON);
* ``feeds`` — последняя лента пользователя (контекст агента без пересборки);
* ``chat_feedback`` — оценки ответов.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import Column, Integer, MetaData, Table, Text, func, select, update

from news_pipeline.agent.schemas import AgentAnswer, ChatFeedback, ChatHistory, ChatTurn, EvidencePack
from news_pipeline.config import DATA_DIR
from news_pipeline.schemas import FeedResponse
from news_pipeline.storage import db

CHAT_DB = DATA_DIR / "chat.db"

metadata = MetaData()

SESSIONS = Table(
    "sessions", metadata,
    Column("session_id", Text, primary_key=True),
    Column("user_id", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    Column("evidence", Text),
)

MESSAGES = Table(
    "messages", metadata,
    Column("session_id", Text, primary_key=True),
    Column("turn", Integer, primary_key=True),
    Column("role", Text, nullable=False),
    Column("text", Text, nullable=False),
    Column("answer", Text),
    Column("answer_id", Text),
    Column("created_at", Text, nullable=False),
)

FEEDS = Table(
    "feeds", metadata,
    Column("user_id", Text, primary_key=True),
    Column("generated_at", Text, nullable=False),
    Column("feed", Text, nullable=False),
)

CHAT_FEEDBACK = Table(
    "chat_feedback", metadata,
    Column("answer_id", Text),
    Column("session_id", Text),
    Column("user_id", Text),
    Column("type", Text),
    Column("comment", Text),
    Column("created_at", Text),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ChatStore:
    def __init__(self, path: str | Path = CHAT_DB) -> None:
        self.path = Path(path)
        self.engine = db.engine(self.path, wal=True)
        db.ensure_tables(self.engine, [SESSIONS, MESSAGES, FEEDS, CHAT_FEEDBACK])

    # --- сессии ---------------------------------------------------------------

    def _session_user(self, session_id: str) -> str | None:
        with self.engine.connect() as connection:
            return connection.scalar(select(SESSIONS.c.user_id).where(SESSIONS.c.session_id == session_id))

    def user_session(self, user_id: str) -> str | None:
        """Текущий (последний) диалог пользователя; ``None`` — пользователь ещё не писал в чат."""
        with self.engine.connect() as connection:
            return connection.scalar(select(SESSIONS.c.session_id).where(SESSIONS.c.user_id == user_id)
                                     .order_by(SESSIONS.c.created_at.desc(), SESSIONS.c.session_id.desc()).limit(1))

    def open_session(self, user_id: str, session_id: str | None) -> str:
        """Существующая сессия пользователя или новая."""
        if session_id:
            owner = self._session_user(session_id)
            if owner is not None:
                if owner != user_id:
                    raise PermissionError(f"сессия {session_id} принадлежит другому пользователю")
                return session_id
        session_id = session_id or f"c_{uuid.uuid4().hex[:12]}"
        with self.engine.begin() as connection:
            connection.execute(SESSIONS.insert().values(session_id=session_id, user_id=user_id, created_at=_now()))
        return session_id

    def evidence(self, session_id: str) -> EvidencePack | None:
        with self.engine.connect() as connection:
            value = connection.scalar(select(SESSIONS.c.evidence).where(SESSIONS.c.session_id == session_id))
        return EvidencePack.from_dict(json.loads(value)) if value else None

    def put_evidence(self, session_id: str, pack: EvidencePack) -> None:
        with self.engine.begin() as connection:
            connection.execute(update(SESSIONS).where(SESSIONS.c.session_id == session_id)
                               .values(evidence=json.dumps(pack.to_dict(), ensure_ascii=False)))

    # --- реплики --------------------------------------------------------------

    def add_turn(self, session_id: str, role: str, text: str, answer: AgentAnswer | None = None) -> int:
        with self.engine.begin() as connection:
            turn = connection.scalar(select(func.coalesce(func.max(MESSAGES.c.turn), 0) + 1)
                                     .where(MESSAGES.c.session_id == session_id))
            connection.execute(MESSAGES.insert().values(
                session_id=session_id, turn=turn, role=role, text=text,
                answer=answer.model_dump_json() if answer else None,
                answer_id=answer.answer_id if answer else None, created_at=_now(),
            ))
        return turn

    def history(self, session_id: str, limit: int | None = None) -> ChatHistory | None:
        user_id = self._session_user(session_id)
        if user_id is None:
            return None
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(MESSAGES.c.turn, MESSAGES.c.role, MESSAGES.c.text, MESSAGES.c.answer, MESSAGES.c.created_at)
                .where(MESSAGES.c.session_id == session_id).order_by(MESSAGES.c.turn)
            ).all()
        if limit is not None:
            rows = rows[-limit:]
        turns = [
            ChatTurn(turn=turn, role=role, text=text,
                     answer=AgentAnswer.model_validate_json(answer) if answer else None,
                     created_at=datetime.fromisoformat(created))
            for turn, role, text, answer, created in rows
        ]
        return ChatHistory(session_id=session_id, user_id=user_id, turns=turns)

    def recent_dialog(self, session_id: str, turns: int) -> list[tuple[str, str]]:
        """Последние ``turns`` пар «вопрос — краткий ответ» для планировщика."""
        history = self.history(session_id, limit=turns * 2)
        if history is None:
            return []
        return [(turn.role, turn.answer.summary if turn.answer else turn.text) for turn in history.turns]

    def feedback(self, session_id: str, user_id: str, item: ChatFeedback) -> None:
        with self.engine.begin() as connection:
            connection.execute(CHAT_FEEDBACK.insert().values(
                answer_id=item.answer_id, session_id=session_id, user_id=user_id, type=item.type,
                comment=item.comment, created_at=_now(),
            ))

    # --- ленты ----------------------------------------------------------------

    def put_feed(self, feed: FeedResponse) -> None:
        with self.engine.begin() as connection:
            connection.execute(db.upsert(FEEDS), {"user_id": feed.user_id, "generated_at": _now(),
                                                  "feed": feed.model_dump_json()})

    def feed(self, user_id: str, ttl_hours: float) -> FeedResponse | None:
        with self.engine.connect() as connection:
            row = connection.execute(select(FEEDS.c.generated_at, FEEDS.c.feed).where(FEEDS.c.user_id == user_id)).first()
        if row is None:
            return None
        if datetime.now(timezone.utc) - datetime.fromisoformat(row.generated_at) > timedelta(hours=ttl_hours):
            return None
        return FeedResponse.model_validate_json(row.feed)
