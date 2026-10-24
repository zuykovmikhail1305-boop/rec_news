"""Реакции пользователей на новости — ``data/feedback.db`` (ORM-модель :class:`Feedback`).

Строка на событие (показ, клик, полезно/не полезно, скрыть) из ``POST /feedback``.
События только дописываются; ключ — порядковый ``id``.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Float, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from news_pipeline.config import DATA_DIR
from news_pipeline.schemas import FeedbackEvent
from news_pipeline.storage import db

FEEDBACK_DB = DATA_DIR / "feedback.db"


class Feedback(db.Base):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str | None] = mapped_column(Text)
    news_id: Mapped[str | None] = mapped_column(Text)
    type: Mapped[str | None] = mapped_column(Text)
    value: Mapped[float | None] = mapped_column(Float)
    position: Mapped[int | None] = mapped_column(Integer)
    pipeline_version: Mapped[str | None] = mapped_column(Text)
    timestamp: Mapped[str | None] = mapped_column(Text)


class FeedbackStore:
    def __init__(self, path: str | Path = FEEDBACK_DB) -> None:
        self.engine = db.engine(path)
        db.ensure_tables(self.engine, [Feedback])

    def add(self, event: FeedbackEvent, pipeline_version: str) -> None:
        with db.session(self.engine) as session:
            session.add(Feedback(
                user_id=event.user_id, news_id=event.news_id, type=event.type, value=event.value,
                position=event.position, pipeline_version=event.pipeline_version or pipeline_version,
                timestamp=event.timestamp.isoformat(),
            ))
