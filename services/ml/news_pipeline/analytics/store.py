"""Хранилище аналитики ``data/analytics.db``, ORM-модели (ТЗ «подбор аналитики», §10).

* :class:`AnalyticsMaterial` (``analytics_materials``) — материалы;
* :class:`AnalyticsChunkRow` (``analytics_chunks``) — чанки и их метаданные (сущности, темы);
* :class:`EventAnalyticsRow` (``event_analytics``) — результаты подбора для событий;
* :class:`EventAnalyticsRun` (``event_analytics_runs``) — статус последнего подбора для события.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import Float, ForeignKey, Index, Integer, Text, delete, func, select
from sqlalchemy.orm import Mapped, mapped_column, relationship

from news_pipeline.config import DATA_DIR
from news_pipeline.schemas import AnalyticsChunk, AnalyticsIn, EventAnalytics
from news_pipeline.storage import db

ANALYTICS_DB = DATA_DIR / "analytics.db"


class AnalyticsMaterial(db.Base):
    __tablename__ = "analytics_materials"

    analytics_id: Mapped[str] = mapped_column(Text, primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    published_at: Mapped[str] = mapped_column(Text)
    type: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    indexed_at: Mapped[str] = mapped_column(Text)

    chunks: Mapped[list["AnalyticsChunkRow"]] = relationship(back_populates="material", order_by="AnalyticsChunkRow.position")


class AnalyticsChunkRow(db.Base):
    __tablename__ = "analytics_chunks"
    __table_args__ = (Index("idx_chunks_material", "analytics_id"),)

    chunk_id: Mapped[str] = mapped_column(Text, primary_key=True)
    analytics_id: Mapped[str] = mapped_column(Text, ForeignKey("analytics_materials.analytics_id"))
    analytics_title: Mapped[str] = mapped_column(Text)
    section_title: Mapped[str] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    published_at: Mapped[str | None] = mapped_column(Text)
    type: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    entities: Mapped[str] = mapped_column(Text, server_default="[]")
    topics: Mapped[str] = mapped_column(Text, server_default="{}")

    material: Mapped[AnalyticsMaterial] = relationship(back_populates="chunks")

    def to_schema(self) -> AnalyticsChunk:
        return AnalyticsChunk(
            chunk_id=self.chunk_id, analytics_id=self.analytics_id, analytics_title=self.analytics_title,
            section_title=self.section_title, position=self.position, text=self.text,
            published_at=_time(self.published_at), type=self.type, url=self.url,
            entities=json.loads(self.entities), topics=json.loads(self.topics),
        )


class EventAnalyticsRow(db.Base):
    __tablename__ = "event_analytics"

    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    chunk_id: Mapped[str] = mapped_column(Text, primary_key=True)
    analytics_id: Mapped[str] = mapped_column(Text)
    score: Mapped[int] = mapped_column(Integer)
    relation: Mapped[str] = mapped_column(Text)
    contradicts: Mapped[int] = mapped_column(Integer, server_default="0")
    reason: Mapped[str] = mapped_column(Text)
    rerank: Mapped[float | None] = mapped_column(Float)
    matched_at: Mapped[str] = mapped_column(Text)
    pipeline_version: Mapped[str] = mapped_column(Text)

    # Фрагмент аналитики (чанк может быть удалён при переиндексации материала — тогда None).
    chunk: Mapped[AnalyticsChunkRow | None] = relationship(
        primaryjoin="foreign(EventAnalyticsRow.chunk_id) == AnalyticsChunkRow.chunk_id", viewonly=True, lazy="joined")


class EventAnalyticsRun(db.Base):
    __tablename__ = "event_analytics_runs"

    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str] = mapped_column(Text)
    published_at: Mapped[str | None] = mapped_column(Text)
    matched_at: Mapped[str] = mapped_column(Text)
    pipeline_version: Mapped[str] = mapped_column(Text)


MODELS = [AnalyticsMaterial, AnalyticsChunkRow, EventAnalyticsRow, EventAnalyticsRun]


def _time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class AnalyticsStore:
    def __init__(self, path: str | Path = ANALYTICS_DB) -> None:
        self.path = Path(path)
        self.engine = db.engine(self.path)
        db.ensure_tables(self.engine, MODELS)

    # --- материалы и чанки ----------------------------------------------------

    def put_material(self, material: AnalyticsIn, chunks: list[AnalyticsChunk], indexed_at: str) -> None:
        """Сохраняет материал и заменяет его чанки."""
        with db.session(self.engine) as session:
            session.execute(delete(AnalyticsChunkRow).where(AnalyticsChunkRow.analytics_id == material.analytics_id))
            session.execute(db.upsert(AnalyticsMaterial), [{
                "analytics_id": material.analytics_id, "title": material.title,
                "published_at": material.published_at.isoformat(), "type": material.type, "url": material.url,
                "author": material.author, "text": material.text, "indexed_at": indexed_at,
            }])
            session.add_all(
                AnalyticsChunkRow(
                    chunk_id=c.chunk_id, analytics_id=c.analytics_id, analytics_title=c.analytics_title,
                    section_title=c.section_title, position=c.position, text=c.text,
                    published_at=c.published_at.isoformat() if c.published_at else None, type=c.type, url=c.url,
                    entities=json.dumps(c.entities, ensure_ascii=False), topics=json.dumps(c.topics, ensure_ascii=False),
                )
                for c in chunks
            )

    def chunks(self, chunk_ids: list[str] | None = None) -> list[AnalyticsChunk]:
        order = (AnalyticsChunkRow.analytics_id, AnalyticsChunkRow.position)
        if chunk_ids is None:
            queries = [select(AnalyticsChunkRow).order_by(*order)]
        elif not chunk_ids:
            return []
        else:
            queries = [select(AnalyticsChunkRow).where(AnalyticsChunkRow.chunk_id.in_(part)).order_by(*order)
                       for part in db.chunks(list(chunk_ids))]
        with db.session(self.engine) as session:
            rows = [row for query in queries for row in session.scalars(query)]
        if len(queries) > 1:
            rows.sort(key=lambda row: (row.analytics_id, row.position))
        return [row.to_schema() for row in rows]

    def material_count(self) -> int:
        with db.session(self.engine) as session:
            return session.scalar(select(func.count()).select_from(AnalyticsMaterial))

    # --- результаты подбора ---------------------------------------------------

    def put_event(self, event_id: str, items: list[EventAnalytics], *, status: str,
                  published_at: str | None, matched_at: str, pipeline_version: str) -> None:
        with db.session(self.engine) as session:
            session.execute(delete(EventAnalyticsRow).where(EventAnalyticsRow.event_id == event_id))
            session.add_all(
                EventAnalyticsRow(
                    event_id=event_id, chunk_id=item.chunk_id, analytics_id=item.analytics_id, score=item.score,
                    relation=item.relation, contradicts=int(item.contradicts), reason=item.reason,
                    rerank=item.rerank, matched_at=item.matched_at.isoformat(), pipeline_version=pipeline_version,
                )
                for item in items
            )
            session.execute(db.upsert(EventAnalyticsRun), [{
                "event_id": event_id, "status": status, "published_at": published_at, "matched_at": matched_at,
                "pipeline_version": pipeline_version,
            }])

    def event(self, event_id: str) -> tuple[str | None, list[EventAnalytics]]:
        """Статус последнего подбора и прикреплённые фрагменты события."""
        with db.session(self.engine) as session:
            run = session.get(EventAnalyticsRun, event_id)
            rows = list(session.scalars(
                select(EventAnalyticsRow).where(EventAnalyticsRow.event_id == event_id)
                .order_by(EventAnalyticsRow.score.desc(), EventAnalyticsRow.rerank.desc())).unique())
        items = [
            EventAnalytics(
                analytics_id=row.analytics_id, chunk_id=row.chunk_id, score=row.score, relation=row.relation,
                contradicts=bool(row.contradicts), reason=row.reason, rerank=row.rerank,
                matched_at=datetime.fromisoformat(row.matched_at),
                analytics_title=row.chunk.analytics_title if row.chunk else None,
                section_title=row.chunk.section_title if row.chunk else None,
                published_at=_time(row.chunk.published_at) if row.chunk else None,
                url=row.chunk.url if row.chunk else None,
            )
            for row in rows
        ]
        return (run.status if run else None), items

    def events_to_refresh(self, since: str) -> list[str]:
        """События с ``published_at`` не раньше ``since`` без фрагментов или только со score 2 (§8)."""
        query = (
            select(EventAnalyticsRun.event_id)
            .outerjoin(EventAnalyticsRow, EventAnalyticsRow.event_id == EventAnalyticsRun.event_id)
            .where(EventAnalyticsRun.published_at >= since)
            .group_by(EventAnalyticsRun.event_id)
            .having(func.coalesce(func.max(EventAnalyticsRow.score), 0) <= 2)
        )
        with db.session(self.engine) as session:
            return list(session.scalars(query))

    def matched_events(self) -> set[str]:
        with db.session(self.engine) as session:
            return set(session.scalars(select(EventAnalyticsRun.event_id)))
