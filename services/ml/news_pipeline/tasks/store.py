"""Состояние заданий на генерацию карточек — ``data/jobs.db``, ORM-модели.

* :class:`InsightJobRow` (``insight_jobs``) — задание: пользователь, статус, время;
* :class:`InsightJobItemRow` (``insight_job_items``) — карточка задания: новость, статус, результат или ошибка.

Состояние живёт в базе, а не в памяти: обработчик задачи может работать в
другом процессе (воркер Celery), а статус задания читает API.
Счётчики задания не хранятся — считаются по строкам ``insight_job_items``,
поэтому одновременные обработчики не затирают друг друга.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import Boolean, Index, Integer, Text, func, select, update
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from news_pipeline.config import DATA_DIR
from news_pipeline.storage import db
from news_pipeline.tasks.schemas import InsightJob, InsightJobItem

JOBS_DB = DATA_DIR / "jobs.db"
ACTIVE = ("queued", "running")
ITEM_STATUSES = ("queued", "running", "done", "failed", "cancelled")


class InsightJobRow(db.Base):
    __tablename__ = "insight_jobs"
    __table_args__ = (Index("insight_jobs_user", "user_id", "created_at"),)

    job_id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)          # queued → running → done | failed; cancelled
    refresh: Mapped[bool] = mapped_column(Boolean, server_default="0")
    created_at: Mapped[str] = mapped_column(Text)
    started_at: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[str | None] = mapped_column(Text)

    items: Mapped[list["InsightJobItemRow"]] = relationship(
        primaryjoin="InsightJobRow.job_id == foreign(InsightJobItemRow.job_id)",
        order_by="InsightJobItemRow.position", viewonly=True)


class InsightJobItemRow(db.Base):
    __tablename__ = "insight_job_items"
    __table_args__ = (Index("insight_job_items_status", "job_id", "status"),)

    job_id: Mapped[str] = mapped_column(Text, primary_key=True)
    news_id: Mapped[str] = mapped_column(Text, primary_key=True)
    position: Mapped[int] = mapped_column(Integer)     # порядок в задании (как в ленте)
    status: Mapped[str] = mapped_column(Text)          # queued → running → done | failed; cancelled
    card_id: Mapped[str | None] = mapped_column(Text)
    card_status: Mapped[str | None] = mapped_column(Text)
    headline: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[str | None] = mapped_column(Text)


MODELS = [InsightJobRow, InsightJobItemRow]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobStore:
    def __init__(self, path: str | Path = JOBS_DB) -> None:
        self.engine = db.engine(path, wal=True)
        db.ensure_tables(self.engine, MODELS)

    # --- создание ---------------------------------------------------------------------

    def create(self, user_id: str, news_ids: list[str], *, refresh: bool) -> str:
        job_id = f"ij_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
        with db.session(self.engine) as session:
            session.add(InsightJobRow(job_id=job_id, user_id=user_id, status="queued", refresh=refresh,
                                      created_at=now_iso()))
            session.add_all(InsightJobItemRow(job_id=job_id, news_id=news_id, position=position, status="queued")
                            for position, news_id in enumerate(news_ids, 1))
        return job_id

    # --- чтение -----------------------------------------------------------------------

    @staticmethod
    def _counts(session: Session, job_id: str) -> dict[str, int]:
        return dict(session.execute(select(InsightJobItemRow.status, func.count())
                                    .where(InsightJobItemRow.job_id == job_id)
                                    .group_by(InsightJobItemRow.status)).all())

    def job(self, job_id: str, *, with_items: bool = True) -> InsightJob | None:
        with db.session(self.engine) as session:
            row = session.get(InsightJobRow, job_id)
            if row is None:
                return None
            counts = self._counts(session, job_id)
            items = [InsightJobItem(**{name: getattr(item, name) for name in InsightJobItem.model_fields})
                     for item in row.items] if with_items else []
        return InsightJob(job_id=row.job_id, user_id=row.user_id, status=row.status, refresh=row.refresh,
                          created_at=row.created_at, started_at=row.started_at, finished_at=row.finished_at,
                          total=sum(counts.values()), items=items,
                          **{status: counts.get(status, 0) for status in ITEM_STATUSES})

    def latest(self, user_id: str, *, active_only: bool = False) -> InsightJob | None:
        query = select(InsightJobRow.job_id).where(InsightJobRow.user_id == user_id)
        if active_only:
            query = query.where(InsightJobRow.status.in_(ACTIVE))
        with db.session(self.engine) as session:
            job_id = session.scalar(query.order_by(InsightJobRow.created_at.desc()).limit(1))
        return self.job(job_id) if job_id else None

    def item_status(self, job_id: str, news_id: str) -> tuple[str | None, str | None]:
        """Статус карточки задания и статус самого задания."""
        with db.session(self.engine) as session:
            item = session.get(InsightJobItemRow, (job_id, news_id))
            job = session.get(InsightJobRow, job_id)
        return (item.status if item else None), (job.status if job else None)

    def unfinished(self) -> list[dict]:
        """Карточки незавершённых заданий: ``job_id``, ``user_id``, ``news_id``, ``refresh``."""
        query = (select(InsightJobItemRow.job_id, InsightJobItemRow.news_id, InsightJobRow.user_id,
                        InsightJobRow.refresh)
                 .join(InsightJobRow, InsightJobRow.job_id == InsightJobItemRow.job_id)
                 .where(InsightJobRow.status.in_(ACTIVE), InsightJobItemRow.status.in_(ACTIVE))
                 .order_by(InsightJobRow.created_at, InsightJobItemRow.position))
        with db.session(self.engine) as session:
            return [dict(row._mapping) for row in session.execute(query)]

    # --- изменение ----------------------------------------------------------------------

    def start_item(self, job_id: str, news_id: str) -> bool:
        """Берёт карточку в работу. ``False`` — её уже обработали или задание отменено (повторная доставка)."""
        now = now_iso()
        with db.session(self.engine) as session:
            taken = session.execute(
                update(InsightJobItemRow)
                .where(InsightJobItemRow.job_id == job_id, InsightJobItemRow.news_id == news_id,
                       InsightJobItemRow.status.in_(ACTIVE))
                .values(status="running", started_at=now, error=None)
                .execution_options(synchronize_session=False)
            ).rowcount > 0
            if taken:
                session.execute(update(InsightJobRow)
                                .where(InsightJobRow.job_id == job_id, InsightJobRow.status == "queued")
                                .values(status="running", started_at=now)
                                .execution_options(synchronize_session=False))
        return taken

    def finish_item(self, job_id: str, news_id: str, *, status: str, card_id: str | None = None,
                    card_status: str | None = None, headline: str | None = None, error: str | None = None) -> None:
        """Результат карточки; если в задании не осталось незавершённых карточек — завершает задание."""
        with db.session(self.engine) as session:
            item = session.get(InsightJobItemRow, (job_id, news_id))
            if item is not None:
                item.status, item.card_id, item.card_status = status, card_id, card_status
                item.headline, item.error, item.finished_at = headline, error, now_iso()
            session.flush()
            self._close_if_finished(session, job_id)

    def _close_if_finished(self, session: Session, job_id: str) -> None:
        counts = self._counts(session, job_id)
        if any(counts.get(status) for status in ACTIVE):
            return
        status = "done" if counts.get("done") else "failed" if counts.get("failed") else "cancelled"
        session.execute(update(InsightJobRow)
                        .where(InsightJobRow.job_id == job_id, InsightJobRow.status.in_(ACTIVE))
                        .values(status=status, finished_at=now_iso())
                        .execution_options(synchronize_session=False))

    def cancel(self, job_id: str) -> None:
        """Отменяет задание: карточки, которые ещё не начаты, не строятся; начатые достраиваются."""
        with db.session(self.engine) as session:
            session.execute(update(InsightJobItemRow)
                            .where(InsightJobItemRow.job_id == job_id, InsightJobItemRow.status == "queued")
                            .values(status="cancelled", finished_at=now_iso())
                            .execution_options(synchronize_session=False))
            session.execute(update(InsightJobRow)
                            .where(InsightJobRow.job_id == job_id, InsightJobRow.status.in_(ACTIVE))
                            .values(status="cancelled", finished_at=now_iso())
                            .execution_options(synchronize_session=False))

    def requeue_running(self) -> int:
        """Карточки, оборванные остановкой процесса (``running``), снова ``queued``."""
        with db.session(self.engine) as session:
            return session.execute(update(InsightJobItemRow).where(InsightJobItemRow.status == "running")
                                   .values(status="queued", started_at=None)
                                   .execution_options(synchronize_session=False)).rowcount
