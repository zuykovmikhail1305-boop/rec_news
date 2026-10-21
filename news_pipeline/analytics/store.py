"""SQLite-хранилище аналитики ``data/analytics.db`` (ТЗ «подбор аналитики», §10).

* ``analytics_materials`` — материалы;
* ``analytics_chunks`` — чанки и их метаданные (сущности, темы);
* ``event_analytics`` — результаты подбора для событий.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from news_pipeline.config import DATA_DIR
from news_pipeline.schemas import AnalyticsChunk, AnalyticsIn, EventAnalytics

ANALYTICS_DB = DATA_DIR / "analytics.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS analytics_materials (
    analytics_id TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    published_at TEXT NOT NULL,
    type         TEXT,
    url          TEXT,
    author       TEXT,
    text         TEXT NOT NULL,
    indexed_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analytics_chunks (
    chunk_id        TEXT PRIMARY KEY,
    analytics_id    TEXT NOT NULL REFERENCES analytics_materials(analytics_id),
    analytics_title TEXT NOT NULL,
    section_title   TEXT NOT NULL,
    position        INTEGER NOT NULL,
    text            TEXT NOT NULL,
    published_at    TEXT,
    type            TEXT,
    url             TEXT,
    entities        TEXT NOT NULL DEFAULT '[]',
    topics          TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_chunks_material ON analytics_chunks(analytics_id);
CREATE TABLE IF NOT EXISTS event_analytics (
    event_id         TEXT NOT NULL,
    chunk_id         TEXT NOT NULL,
    analytics_id     TEXT NOT NULL,
    score            INTEGER NOT NULL,
    relation         TEXT NOT NULL,
    contradicts      INTEGER NOT NULL DEFAULT 0,
    reason           TEXT NOT NULL,
    rerank           REAL,
    matched_at       TEXT NOT NULL,
    pipeline_version TEXT NOT NULL,
    PRIMARY KEY (event_id, chunk_id)
);
CREATE TABLE IF NOT EXISTS event_analytics_runs (
    event_id         TEXT PRIMARY KEY,
    status           TEXT NOT NULL,
    published_at     TEXT,
    matched_at       TEXT NOT NULL,
    pipeline_version TEXT NOT NULL
);
"""


class AnalyticsStore:
    def __init__(self, path: str | Path = ANALYTICS_DB) -> None:
        self.path = Path(path)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    # --- материалы и чанки ----------------------------------------------------

    def put_material(self, material: AnalyticsIn, chunks: list[AnalyticsChunk], indexed_at: str) -> None:
        """Сохраняет материал и заменяет его чанки."""
        with self._connect() as connection:
            connection.execute("DELETE FROM analytics_chunks WHERE analytics_id = ?", (material.analytics_id,))
            connection.execute(
                "INSERT OR REPLACE INTO analytics_materials VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (material.analytics_id, material.title, material.published_at.isoformat(), material.type,
                 material.url, material.author, material.text, indexed_at),
            )
            connection.executemany(
                "INSERT INTO analytics_chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (c.chunk_id, c.analytics_id, c.analytics_title, c.section_title, c.position, c.text,
                     c.published_at.isoformat() if c.published_at else None, c.type, c.url,
                     json.dumps(c.entities, ensure_ascii=False), json.dumps(c.topics, ensure_ascii=False))
                    for c in chunks
                ],
            )

    def chunks(self, chunk_ids: list[str] | None = None) -> list[AnalyticsChunk]:
        query = "SELECT * FROM analytics_chunks"
        params: tuple = ()
        if chunk_ids is not None:
            if not chunk_ids:
                return []
            query += f" WHERE chunk_id IN ({','.join('?' * len(chunk_ids))})"
            params = tuple(chunk_ids)
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(query + " ORDER BY analytics_id, position", params).fetchall()
        return [
            AnalyticsChunk(
                chunk_id=row["chunk_id"], analytics_id=row["analytics_id"], analytics_title=row["analytics_title"],
                section_title=row["section_title"], position=row["position"], text=row["text"],
                published_at=datetime.fromisoformat(row["published_at"]) if row["published_at"] else None,
                type=row["type"], url=row["url"], entities=json.loads(row["entities"]), topics=json.loads(row["topics"]),
            )
            for row in rows
        ]

    def material_count(self) -> int:
        with self._connect() as connection:
            return connection.execute("SELECT COUNT(*) FROM analytics_materials").fetchone()[0]

    # --- результаты подбора ---------------------------------------------------

    def put_event(self, event_id: str, items: list[EventAnalytics], *, status: str,
                  published_at: str | None, matched_at: str, pipeline_version: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM event_analytics WHERE event_id = ?", (event_id,))
            connection.executemany(
                "INSERT INTO event_analytics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (event_id, item.chunk_id, item.analytics_id, item.score, item.relation, int(item.contradicts),
                     item.reason, item.rerank, item.matched_at.isoformat(), pipeline_version)
                    for item in items
                ],
            )
            connection.execute(
                "INSERT OR REPLACE INTO event_analytics_runs VALUES (?, ?, ?, ?, ?)",
                (event_id, status, published_at, matched_at, pipeline_version),
            )

    def event(self, event_id: str) -> tuple[str | None, list[EventAnalytics]]:
        """Статус последнего подбора и прикреплённые фрагменты события."""
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            run = connection.execute("SELECT status FROM event_analytics_runs WHERE event_id = ?", (event_id,)).fetchone()
            rows = connection.execute(
                "SELECT e.*, c.analytics_title, c.section_title, c.published_at AS chunk_published, c.url "
                "FROM event_analytics e LEFT JOIN analytics_chunks c ON c.chunk_id = e.chunk_id "
                "WHERE e.event_id = ? ORDER BY e.score DESC, e.rerank DESC",
                (event_id,),
            ).fetchall()
        items = [
            EventAnalytics(
                analytics_id=row["analytics_id"], chunk_id=row["chunk_id"], score=row["score"],
                relation=row["relation"], contradicts=bool(row["contradicts"]), reason=row["reason"],
                rerank=row["rerank"], matched_at=datetime.fromisoformat(row["matched_at"]),
                analytics_title=row["analytics_title"], section_title=row["section_title"],
                published_at=datetime.fromisoformat(row["chunk_published"]) if row["chunk_published"] else None,
                url=row["url"],
            )
            for row in rows
        ]
        return (run["status"] if run else None), items

    def events_to_refresh(self, since: str) -> list[str]:
        """События с ``published_at`` не раньше ``since`` без фрагментов или только со score 2 (§8)."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT r.event_id FROM event_analytics_runs r "
                "LEFT JOIN event_analytics e ON e.event_id = r.event_id "
                "WHERE r.published_at >= ? GROUP BY r.event_id "
                "HAVING COALESCE(MAX(e.score), 0) <= 2",
                (since,),
            ).fetchall()
        return [row[0] for row in rows]

    def matched_events(self) -> set[str]:
        with self._connect() as connection:
            return {row[0] for row in connection.execute("SELECT event_id FROM event_analytics_runs")}
