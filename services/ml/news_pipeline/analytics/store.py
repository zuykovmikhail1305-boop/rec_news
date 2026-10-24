"""Хранилище аналитики ``data/analytics.db`` на SQLAlchemy (ТЗ «подбор аналитики», §10).

* ``analytics_materials`` — материалы;
* ``analytics_chunks`` — чанки и их метаданные (сущности, темы);
* ``event_analytics`` — результаты подбора для событий;
* ``event_analytics_runs`` — статус последнего подбора для события.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import Column, Float, ForeignKey, Index, Integer, MetaData, Table, Text, delete, func, select

from news_pipeline.config import DATA_DIR
from news_pipeline.schemas import AnalyticsChunk, AnalyticsIn, EventAnalytics
from news_pipeline.storage import db

ANALYTICS_DB = DATA_DIR / "analytics.db"

metadata = MetaData()

MATERIALS = Table(
    "analytics_materials", metadata,
    Column("analytics_id", Text, primary_key=True),
    Column("title", Text, nullable=False),
    Column("published_at", Text, nullable=False),
    Column("type", Text),
    Column("url", Text),
    Column("author", Text),
    Column("text", Text, nullable=False),
    Column("indexed_at", Text, nullable=False),
)

CHUNKS = Table(
    "analytics_chunks", metadata,
    Column("chunk_id", Text, primary_key=True),
    Column("analytics_id", Text, ForeignKey("analytics_materials.analytics_id"), nullable=False),
    Column("analytics_title", Text, nullable=False),
    Column("section_title", Text, nullable=False),
    Column("position", Integer, nullable=False),
    Column("text", Text, nullable=False),
    Column("published_at", Text),
    Column("type", Text),
    Column("url", Text),
    Column("entities", Text, nullable=False, server_default="[]"),
    Column("topics", Text, nullable=False, server_default="{}"),
    Index("idx_chunks_material", "analytics_id"),
)

EVENT_ANALYTICS = Table(
    "event_analytics", metadata,
    Column("event_id", Text, primary_key=True),
    Column("chunk_id", Text, primary_key=True),
    Column("analytics_id", Text, nullable=False),
    Column("score", Integer, nullable=False),
    Column("relation", Text, nullable=False),
    Column("contradicts", Integer, nullable=False, server_default="0"),
    Column("reason", Text, nullable=False),
    Column("rerank", Float),
    Column("matched_at", Text, nullable=False),
    Column("pipeline_version", Text, nullable=False),
)

EVENT_RUNS = Table(
    "event_analytics_runs", metadata,
    Column("event_id", Text, primary_key=True),
    Column("status", Text, nullable=False),
    Column("published_at", Text),
    Column("matched_at", Text, nullable=False),
    Column("pipeline_version", Text, nullable=False),
)


def _time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class AnalyticsStore:
    def __init__(self, path: str | Path = ANALYTICS_DB) -> None:
        self.path = Path(path)
        self.engine = db.engine(self.path)
        db.ensure_tables(self.engine, [MATERIALS, CHUNKS, EVENT_ANALYTICS, EVENT_RUNS])

    # --- материалы и чанки ----------------------------------------------------

    def put_material(self, material: AnalyticsIn, chunks: list[AnalyticsChunk], indexed_at: str) -> None:
        """Сохраняет материал и заменяет его чанки."""
        with self.engine.begin() as connection:
            connection.execute(delete(CHUNKS).where(CHUNKS.c.analytics_id == material.analytics_id))
            connection.execute(db.upsert(MATERIALS), {
                "analytics_id": material.analytics_id, "title": material.title,
                "published_at": material.published_at.isoformat(), "type": material.type, "url": material.url,
                "author": material.author, "text": material.text, "indexed_at": indexed_at,
            })
            if chunks:
                connection.execute(CHUNKS.insert(), [
                    {"chunk_id": c.chunk_id, "analytics_id": c.analytics_id, "analytics_title": c.analytics_title,
                     "section_title": c.section_title, "position": c.position, "text": c.text,
                     "published_at": c.published_at.isoformat() if c.published_at else None, "type": c.type,
                     "url": c.url, "entities": json.dumps(c.entities, ensure_ascii=False),
                     "topics": json.dumps(c.topics, ensure_ascii=False)}
                    for c in chunks
                ])

    def chunks(self, chunk_ids: list[str] | None = None) -> list[AnalyticsChunk]:
        order = (CHUNKS.c.analytics_id, CHUNKS.c.position)
        if chunk_ids is None:
            queries = [select(CHUNKS).order_by(*order)]
        elif not chunk_ids:
            return []
        else:
            queries = [select(CHUNKS).where(CHUNKS.c.chunk_id.in_(part)).order_by(*order)
                       for part in db.chunks(list(chunk_ids))]
        with self.engine.connect() as connection:
            rows = [row for query in queries for row in connection.execute(query)]
        if len(queries) > 1:
            rows.sort(key=lambda row: (row.analytics_id, row.position))
        return [
            AnalyticsChunk(
                chunk_id=row.chunk_id, analytics_id=row.analytics_id, analytics_title=row.analytics_title,
                section_title=row.section_title, position=row.position, text=row.text,
                published_at=_time(row.published_at), type=row.type, url=row.url,
                entities=json.loads(row.entities), topics=json.loads(row.topics),
            )
            for row in rows
        ]

    def material_count(self) -> int:
        with self.engine.connect() as connection:
            return connection.scalar(select(func.count()).select_from(MATERIALS))

    # --- результаты подбора ---------------------------------------------------

    def put_event(self, event_id: str, items: list[EventAnalytics], *, status: str,
                  published_at: str | None, matched_at: str, pipeline_version: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(delete(EVENT_ANALYTICS).where(EVENT_ANALYTICS.c.event_id == event_id))
            if items:
                connection.execute(EVENT_ANALYTICS.insert(), [
                    {"event_id": event_id, "chunk_id": item.chunk_id, "analytics_id": item.analytics_id,
                     "score": item.score, "relation": item.relation, "contradicts": int(item.contradicts),
                     "reason": item.reason, "rerank": item.rerank, "matched_at": item.matched_at.isoformat(),
                     "pipeline_version": pipeline_version}
                    for item in items
                ])
            connection.execute(db.upsert(EVENT_RUNS), {
                "event_id": event_id, "status": status, "published_at": published_at, "matched_at": matched_at,
                "pipeline_version": pipeline_version,
            })

    def event(self, event_id: str) -> tuple[str | None, list[EventAnalytics]]:
        """Статус последнего подбора и прикреплённые фрагменты события."""
        e, c = EVENT_ANALYTICS, CHUNKS
        query = (
            select(e, c.c.analytics_title, c.c.section_title, c.c.published_at.label("chunk_published"), c.c.url)
            .select_from(e.outerjoin(c, c.c.chunk_id == e.c.chunk_id))
            .where(e.c.event_id == event_id)
            .order_by(e.c.score.desc(), e.c.rerank.desc())
        )
        with self.engine.connect() as connection:
            status = connection.scalar(select(EVENT_RUNS.c.status).where(EVENT_RUNS.c.event_id == event_id))
            rows = connection.execute(query).all()
        items = [
            EventAnalytics(
                analytics_id=row.analytics_id, chunk_id=row.chunk_id, score=row.score, relation=row.relation,
                contradicts=bool(row.contradicts), reason=row.reason, rerank=row.rerank,
                matched_at=datetime.fromisoformat(row.matched_at), analytics_title=row.analytics_title,
                section_title=row.section_title, published_at=_time(row.chunk_published), url=row.url,
            )
            for row in rows
        ]
        return status, items

    def events_to_refresh(self, since: str) -> list[str]:
        """События с ``published_at`` не раньше ``since`` без фрагментов или только со score 2 (§8)."""
        r, e = EVENT_RUNS, EVENT_ANALYTICS
        query = (
            select(r.c.event_id)
            .select_from(r.outerjoin(e, e.c.event_id == r.c.event_id))
            .where(r.c.published_at >= since)
            .group_by(r.c.event_id)
            .having(func.coalesce(func.max(e.c.score), 0) <= 2)
        )
        with self.engine.connect() as connection:
            return list(connection.scalars(query))

    def matched_events(self) -> set[str]:
        with self.engine.connect() as connection:
            return set(connection.scalars(select(EVENT_RUNS.c.event_id)))
