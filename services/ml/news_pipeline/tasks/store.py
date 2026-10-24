"""Состояние заданий на генерацию карточек — ``data/jobs.db`` (SQLAlchemy).

* ``insight_jobs`` — задание: пользователь, статус, время;
* ``insight_job_items`` — карточка задания: новость, статус, результат или ошибка.

Состояние живёт в базе, а не в памяти: обработчик задачи может работать в
другом процессе (потребитель очереди), а статус задания читает API.
Счётчики задания не хранятся — считаются по строкам ``insight_job_items``,
поэтому одновременные обработчики не затирают друг друга.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import Boolean, Column, Index, Integer, MetaData, Table, Text, func, select, update

from news_pipeline.config import DATA_DIR
from news_pipeline.storage import db
from news_pipeline.tasks.schemas import InsightJob, InsightJobItem

JOBS_DB = DATA_DIR / "jobs.db"
ACTIVE = ("queued", "running")

metadata = MetaData()

JOBS = Table(
    "insight_jobs", metadata,
    Column("job_id", Text, primary_key=True),
    Column("user_id", Text, nullable=False),
    Column("status", Text, nullable=False),          # queued → running → done | failed; cancelled
    Column("refresh", Boolean, nullable=False, server_default="0"),
    Column("created_at", Text, nullable=False),
    Column("started_at", Text),
    Column("finished_at", Text),
    Index("insight_jobs_user", "user_id", "created_at"),
)

ITEMS = Table(
    "insight_job_items", metadata,
    Column("job_id", Text, primary_key=True),
    Column("news_id", Text, primary_key=True),
    Column("position", Integer, nullable=False),     # порядок в задании (как в ленте)
    Column("status", Text, nullable=False),          # queued → running → done | failed; cancelled
    Column("card_id", Text),
    Column("card_status", Text),
    Column("headline", Text),
    Column("error", Text),
    Column("started_at", Text),
    Column("finished_at", Text),
    Index("insight_job_items_status", "job_id", "status"),
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobStore:
    def __init__(self, path: str | Path = JOBS_DB) -> None:
        self.engine = db.engine(path, wal=True)
        db.ensure_tables(self.engine, [JOBS, ITEMS])

    # --- создание ---------------------------------------------------------------------

    def create(self, user_id: str, news_ids: list[str], *, refresh: bool) -> str:
        job_id = f"ij_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
        with self.engine.begin() as connection:
            connection.execute(JOBS.insert().values(job_id=job_id, user_id=user_id, status="queued", refresh=refresh,
                                                    created_at=now_iso()))
            connection.execute(ITEMS.insert(), [{"job_id": job_id, "news_id": news_id, "position": position,
                                                 "status": "queued"} for position, news_id in enumerate(news_ids, 1)])
        return job_id

    # --- чтение -----------------------------------------------------------------------

    def job(self, job_id: str, *, with_items: bool = True) -> InsightJob | None:
        with self.engine.connect() as connection:
            row = connection.execute(select(JOBS).where(JOBS.c.job_id == job_id)).first()
            if row is None:
                return None
            counts = dict(connection.execute(select(ITEMS.c.status, func.count()).where(ITEMS.c.job_id == job_id)
                                             .group_by(ITEMS.c.status)).all())
            items = [InsightJobItem(**db.row_dict(item)) for item in connection.execute(
                select(*(ITEMS.c[name] for name in InsightJobItem.model_fields))
                .where(ITEMS.c.job_id == job_id).order_by(ITEMS.c.position))] if with_items else []
        return InsightJob(**db.row_dict(row), total=sum(counts.values()), items=items,
                          **{status: counts.get(status, 0) for status in ("queued", "running", "done", "failed",
                                                                          "cancelled")})

    def latest(self, user_id: str, *, active_only: bool = False) -> InsightJob | None:
        query = select(JOBS.c.job_id).where(JOBS.c.user_id == user_id)
        if active_only:
            query = query.where(JOBS.c.status.in_(ACTIVE))
        with self.engine.connect() as connection:
            job_id = connection.scalar(query.order_by(JOBS.c.created_at.desc()).limit(1))
        return self.job(job_id) if job_id else None

    def item_status(self, job_id: str, news_id: str) -> tuple[str | None, str | None]:
        """Статус карточки задания и статус самого задания."""
        with self.engine.connect() as connection:
            item = connection.scalar(select(ITEMS.c.status).where(ITEMS.c.job_id == job_id, ITEMS.c.news_id == news_id))
            job = connection.scalar(select(JOBS.c.status).where(JOBS.c.job_id == job_id))
        return item, job

    def unfinished(self) -> list[dict[str, Any]]:
        """Карточки незавершённых заданий: ``job_id``, ``user_id``, ``news_id``, ``refresh``."""
        query = (select(ITEMS.c.job_id, ITEMS.c.news_id, JOBS.c.user_id, JOBS.c.refresh)
                 .select_from(ITEMS.join(JOBS, JOBS.c.job_id == ITEMS.c.job_id))
                 .where(JOBS.c.status.in_(ACTIVE), ITEMS.c.status.in_(ACTIVE))
                 .order_by(JOBS.c.created_at, ITEMS.c.position))
        with self.engine.connect() as connection:
            return [db.row_dict(row) for row in connection.execute(query)]

    # --- изменение ----------------------------------------------------------------------

    def start_item(self, job_id: str, news_id: str) -> bool:
        """Берёт карточку в работу. ``False`` — её уже обработали или задание отменено (повторная доставка)."""
        now = now_iso()
        with self.engine.begin() as connection:
            before = db.changes(connection)
            connection.execute(
                update(ITEMS).where(ITEMS.c.job_id == job_id, ITEMS.c.news_id == news_id, ITEMS.c.status.in_(ACTIVE))
                .values(status="running", started_at=now, error=None)
            )
            taken = db.changes(connection) > before
            if taken:
                connection.execute(update(JOBS).where(JOBS.c.job_id == job_id, JOBS.c.status == "queued")
                                   .values(status="running", started_at=now))
        return taken

    def finish_item(self, job_id: str, news_id: str, *, status: str, card_id: str | None = None,
                    card_status: str | None = None, headline: str | None = None, error: str | None = None) -> None:
        """Результат карточки; если в задании не осталось незавершённых карточек — завершает задание."""
        with self.engine.begin() as connection:
            connection.execute(
                update(ITEMS).where(ITEMS.c.job_id == job_id, ITEMS.c.news_id == news_id)
                .values(status=status, card_id=card_id, card_status=card_status, headline=headline, error=error,
                        finished_at=now_iso())
            )
            self._close_if_finished(connection, job_id)

    def _close_if_finished(self, connection, job_id: str) -> None:
        counts = dict(connection.execute(select(ITEMS.c.status, func.count()).where(ITEMS.c.job_id == job_id)
                                         .group_by(ITEMS.c.status)).all())
        if any(counts.get(status) for status in ACTIVE):
            return
        status = "done" if counts.get("done") else "failed" if counts.get("failed") else "cancelled"
        connection.execute(update(JOBS).where(JOBS.c.job_id == job_id, JOBS.c.status.in_(ACTIVE))
                           .values(status=status, finished_at=now_iso()))

    def cancel(self, job_id: str) -> None:
        """Отменяет задание: карточки, которые ещё не начаты, не строятся; начатые достраиваются."""
        with self.engine.begin() as connection:
            connection.execute(update(ITEMS).where(ITEMS.c.job_id == job_id, ITEMS.c.status == "queued")
                               .values(status="cancelled", finished_at=now_iso()))
            connection.execute(update(JOBS).where(JOBS.c.job_id == job_id, JOBS.c.status.in_(ACTIVE))
                               .values(status="cancelled", finished_at=now_iso()))

    def requeue_running(self) -> int:
        """Карточки, оборванные остановкой процесса (``running``), снова ``queued``."""
        with self.engine.begin() as connection:
            before = db.changes(connection)
            connection.execute(update(ITEMS).where(ITEMS.c.status == "running").values(status="queued", started_at=None))
            return db.changes(connection) - before
