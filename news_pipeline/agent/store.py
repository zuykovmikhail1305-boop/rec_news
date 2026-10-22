"""Диалоги агента и последние ленты пользователей — ``data/chat.db`` (docs/agent_tz.md §6).

* ``sessions`` — сессия чата и зафиксированный пакет доказательств;
* ``messages`` — реплики пользователя и ответы агента (``AgentAnswer`` в JSON);
* ``feeds`` — последняя лента пользователя (контекст агента без пересборки);
* ``chat_feedback`` — оценки ответов.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from news_pipeline.agent.schemas import AgentAnswer, ChatFeedback, ChatHistory, ChatTurn, EvidencePack
from news_pipeline.config import DATA_DIR
from news_pipeline.schemas import FeedResponse

CHAT_DB = DATA_DIR / "chat.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ChatStore:
    def __init__(self, path: str | Path = CHAT_DB) -> None:
        self.path = Path(path)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                "CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,"
                " created_at TEXT NOT NULL, evidence TEXT);"
                "CREATE TABLE IF NOT EXISTS messages (session_id TEXT NOT NULL, turn INTEGER NOT NULL,"
                " role TEXT NOT NULL, text TEXT NOT NULL, answer TEXT, answer_id TEXT, created_at TEXT NOT NULL,"
                " PRIMARY KEY (session_id, turn));"
                "CREATE TABLE IF NOT EXISTS feeds (user_id TEXT PRIMARY KEY, generated_at TEXT NOT NULL,"
                " feed TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS chat_feedback (answer_id TEXT, session_id TEXT, user_id TEXT,"
                " type TEXT, comment TEXT, created_at TEXT);"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    # --- сессии ---------------------------------------------------------------

    def open_session(self, user_id: str, session_id: str | None) -> str:
        """Существующая сессия пользователя или новая."""
        if session_id:
            with self._connect() as connection:
                row = connection.execute("SELECT user_id FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
            if row is not None:
                if row[0] != user_id:
                    raise PermissionError(f"сессия {session_id} принадлежит другому пользователю")
                return session_id
        session_id = session_id or f"c_{uuid.uuid4().hex[:12]}"
        with self._connect() as connection:
            connection.execute("INSERT INTO sessions (session_id, user_id, created_at) VALUES (?, ?, ?)",
                               (session_id, user_id, _now()))
        return session_id

    def evidence(self, session_id: str) -> EvidencePack | None:
        with self._connect() as connection:
            row = connection.execute("SELECT evidence FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        return EvidencePack.from_dict(json.loads(row[0])) if row and row[0] else None

    def put_evidence(self, session_id: str, pack: EvidencePack) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE sessions SET evidence = ? WHERE session_id = ?",
                               (json.dumps(pack.to_dict(), ensure_ascii=False), session_id))

    # --- реплики --------------------------------------------------------------

    def add_turn(self, session_id: str, role: str, text: str, answer: AgentAnswer | None = None) -> int:
        with self._connect() as connection:
            turn = connection.execute("SELECT COALESCE(MAX(turn), 0) + 1 FROM messages WHERE session_id = ?",
                                      (session_id,)).fetchone()[0]
            connection.execute(
                "INSERT INTO messages (session_id, turn, role, text, answer, answer_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, turn, role, text, answer.model_dump_json() if answer else None,
                 answer.answer_id if answer else None, _now()),
            )
        return turn

    def history(self, session_id: str, limit: int | None = None) -> ChatHistory | None:
        with self._connect() as connection:
            session = connection.execute("SELECT user_id FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
            if session is None:
                return None
            rows = connection.execute(
                "SELECT turn, role, text, answer, created_at FROM messages WHERE session_id = ? ORDER BY turn",
                (session_id,),
            ).fetchall()
        if limit is not None:
            rows = rows[-limit:]
        turns = [
            ChatTurn(turn=turn, role=role, text=text,
                     answer=AgentAnswer.model_validate_json(answer) if answer else None,
                     created_at=datetime.fromisoformat(created))
            for turn, role, text, answer, created in rows
        ]
        return ChatHistory(session_id=session_id, user_id=session[0], turns=turns)

    def recent_dialog(self, session_id: str, turns: int) -> list[tuple[str, str]]:
        """Последние ``turns`` пар «вопрос — краткий ответ» для планировщика."""
        history = self.history(session_id, limit=turns * 2)
        if history is None:
            return []
        return [(turn.role, turn.answer.summary if turn.answer else turn.text) for turn in history.turns]

    def feedback(self, session_id: str, user_id: str, item: ChatFeedback) -> None:
        with self._connect() as connection:
            connection.execute("INSERT INTO chat_feedback VALUES (?, ?, ?, ?, ?, ?)",
                               (item.answer_id, session_id, user_id, item.type, item.comment, _now()))

    # --- ленты ----------------------------------------------------------------

    def put_feed(self, feed: FeedResponse) -> None:
        with self._connect() as connection:
            connection.execute("INSERT OR REPLACE INTO feeds (user_id, generated_at, feed) VALUES (?, ?, ?)",
                               (feed.user_id, _now(), feed.model_dump_json()))

    def feed(self, user_id: str, ttl_hours: float) -> FeedResponse | None:
        with self._connect() as connection:
            row = connection.execute("SELECT generated_at, feed FROM feeds WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            return None
        if datetime.now(timezone.utc) - datetime.fromisoformat(row[0]) > timedelta(hours=ttl_hours):
            return None
        return FeedResponse.model_validate_json(row[1])
