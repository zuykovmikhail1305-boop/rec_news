"""Готовые рекомендации пользователям и журнал запусков обработки — ``data/recommendations.db``, ORM-модели.

* :class:`UserNewsRow` (``user_news``) — какие новости подходят пользователю: строка на пару
  (``user_id``, ``news_id``) с местом в ленте, оценками и прикреплённой
  аналитикой. Пересчитывается целиком для пользователя при каждом запуске
  обработки (``POST /processing.enqueue``), отдаётся ручкой ``GET /users/{user_id}/news``;
* :class:`PipelineRunRow` (``pipeline_runs``) — запуски обработки: статус, этап, счётчики, ошибка.

Описание колонок — ``docs/news_db.md``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import Float, Index, Integer, Text, delete, func, select, update
from sqlalchemy.orm import Mapped, Session, mapped_column

from news_pipeline.config import DATA_DIR
from news_pipeline.storage import db

RECOMMENDATIONS_DB = DATA_DIR / "recommendations.db"
ACTIVE_RUN = ("queued", "running")


class UserNewsRow(db.Base):
    __tablename__ = "user_news"
    __table_args__ = (Index("user_news_rank", "user_id", "rank"), Index("user_news_news_id", "news_id"))

    user_id: Mapped[str] = mapped_column(Text, primary_key=True)
    news_id: Mapped[str] = mapped_column(Text, primary_key=True)
    rank: Mapped[int] = mapped_column(Integer)
    event_id: Mapped[str | None] = mapped_column(Text)
    score: Mapped[float] = mapped_column(Float)
    retrieval: Mapped[float | None] = mapped_column(Float)
    sem: Mapped[float | None] = mapped_column(Float)
    ent: Mapped[float | None] = mapped_column(Float)
    topic: Mapped[float | None] = mapped_column(Float)
    imp: Mapped[float | None] = mapped_column(Float)
    fresh: Mapped[float | None] = mapped_column(Float)
    llm: Mapped[int | None] = mapped_column(Integer)
    why_relevant: Mapped[str | None] = mapped_column(Text)
    analytics_id: Mapped[str | None] = mapped_column(Text)
    analytics_chunk_id: Mapped[str | None] = mapped_column(Text)
    analytics_relation: Mapped[str | None] = mapped_column(Text)
    analytics_contradicts: Mapped[int | None] = mapped_column(Integer)
    analytics_summary: Mapped[str | None] = mapped_column(Text)
    run_id: Mapped[str] = mapped_column(Text)
    computed_at: Mapped[str] = mapped_column(Text)
    pipeline_version: Mapped[str] = mapped_column(Text)


class PipelineRunRow(db.Base):
    __tablename__ = "pipeline_runs"

    run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str] = mapped_column(Text)
    stage: Mapped[str | None] = mapped_column(Text)
    requested_at: Mapped[str] = mapped_column(Text)
    started_at: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[str | None] = mapped_column(Text)
    demo_now: Mapped[str | None] = mapped_column(Text)
    news_prepared: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    news_deduplicated: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    news_unique: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    events_matched: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    users_total: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    users_done: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    users_failed: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    insights_built: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    error: Mapped[str | None] = mapped_column(Text)


MODELS = [UserNewsRow, PipelineRunRow]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class UserNewsStore:
    def __init__(self, path: str | Path = RECOMMENDATIONS_DB) -> None:
        self.path = Path(path)
        self.engine = db.engine(self.path, wal=True)
        db.ensure_tables(self.engine, MODELS)

    # --- рекомендации ---------------------------------------------------------

    def replace_user(self, user_id: str, rows: list[UserNewsRow]) -> None:
        """Заменяет рекомендации пользователя одной транзакцией."""
        with db.session(self.engine) as session:
            session.execute(delete(UserNewsRow).where(UserNewsRow.user_id == user_id))
            session.add_all(rows)

    def for_user(self, user_id: str, *, limit: int | None = None, offset: int = 0) -> list[UserNewsRow]:
        query = select(UserNewsRow).where(UserNewsRow.user_id == user_id).order_by(UserNewsRow.rank)
        if limit is not None:
            query = query.limit(limit).offset(offset)
        with db.session(self.engine) as session:
            return list(session.scalars(query))

    def row(self, user_id: str, news_id: str) -> UserNewsRow | None:
        with db.session(self.engine) as session:
            return session.get(UserNewsRow, (user_id, news_id))

    def count(self, user_id: str) -> int:
        with db.session(self.engine) as session:
            return session.scalar(select(func.count()).select_from(UserNewsRow).where(UserNewsRow.user_id == user_id))

    # --- запуски обработки ----------------------------------------------------

    # Состояние запусков живёт только здесь, в базе: обработку выполняет воркер очереди (поток в процессе API
    # или воркер Celery), а API читает статус из той же таблицы.

    @staticmethod
    def _fail_stale(session: Session, timeout_hours: float) -> None:
        """Запуск, который идёт дольше ``timeout_hours`` (воркер упал), — ``failed``: иначе он навсегда
        «идёт», и новые запуски не создаются."""
        border = (datetime.now(timezone.utc) - timedelta(hours=timeout_hours)).isoformat(timespec="seconds")
        session.execute(
            update(PipelineRunRow)
            .where(PipelineRunRow.status.in_(ACTIVE_RUN),
                   func.coalesce(PipelineRunRow.started_at, PipelineRunRow.requested_at) < border)
            .values(status="failed", finished_at=now_iso(), error=f"запуск не завершился за {timeout_hours:g} ч")
            .execution_options(synchronize_session=False)
        )

    @staticmethod
    def _active(session: Session) -> PipelineRunRow | None:
        return session.scalar(select(PipelineRunRow).where(PipelineRunRow.status.in_(ACTIVE_RUN))
                              .order_by(PipelineRunRow.requested_at.desc()).limit(1))

    def create_run_if_idle(self, demo_now: datetime | None, timeout_hours: float) -> tuple[dict[str, Any], bool]:
        """Новый запуск (``queued``), если не идёт другой; иначе текущий. Второе значение — создан ли новый.

        Проверка и создание — одна транзакция с блокировкой записи: два одновременных запроса
        (из одного или разных процессов) не создадут два запуска.
        """
        with db.session(self.engine) as session:
            db.write_lock(session, "pipeline_runs")
            self._fail_stale(session, timeout_hours)
            run = self._active(session)
            created = run is None
            if created:
                run = PipelineRunRow(run_id=f"run_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}",
                                     status="queued", requested_at=now_iso(),
                                     demo_now=demo_now.isoformat() if demo_now else None)
                session.add(run)
        return db.as_dict(run), created

    def create_run(self, demo_now: datetime | None) -> dict[str, Any]:
        """Новый запуск (``queued``) без проверки, идёт ли другой: подбор для отдельных пользователей.
        Запуски выполняет одна очередь processing.run — по одному."""
        with db.session(self.engine) as session:
            run = PipelineRunRow(run_id=f"run_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}",
                                 status="queued", requested_at=now_iso(),
                                 demo_now=demo_now.isoformat() if demo_now else None)
            session.add(run)
        return db.as_dict(run)

    def active_run(self, timeout_hours: float) -> dict[str, Any] | None:
        """Запуск, который стоит в очереди или идёт; ``None`` — обработка не идёт."""
        with db.session(self.engine) as session:
            self._fail_stale(session, timeout_hours)
            run = self._active(session)
            return db.as_dict(run) if run else None

    def start_run(self, run_id: str) -> bool:
        """``queued → running``. ``False`` — запуск уже взят или завершён (повторная доставка задачи)."""
        with db.session(self.engine) as session:
            return session.execute(
                update(PipelineRunRow).where(PipelineRunRow.run_id == run_id, PipelineRunRow.status == "queued")
                .values(status="running", started_at=now_iso(), stage="prepare")
                .execution_options(synchronize_session=False)
            ).rowcount > 0

    def queued_runs(self) -> list[dict[str, Any]]:
        with db.session(self.engine) as session:
            return [db.as_dict(run) for run in session.scalars(
                select(PipelineRunRow).where(PipelineRunRow.status == "queued").order_by(PipelineRunRow.requested_at))]

    def update_run(self, run_id: str, **values: Any) -> None:
        if not values:
            return
        with db.session(self.engine) as session:
            run = session.get(PipelineRunRow, run_id)
            for name, value in values.items():
                setattr(run, name, value)

    def run(self, run_id: str) -> dict[str, Any] | None:
        with db.session(self.engine) as session:
            run = session.get(PipelineRunRow, run_id)
            return db.as_dict(run) if run else None

    def latest_run(self) -> dict[str, Any] | None:
        with db.session(self.engine) as session:
            run = session.scalar(select(PipelineRunRow).order_by(PipelineRunRow.requested_at.desc()).limit(1))
            return db.as_dict(run) if run else None

    def fail_running(self) -> None:
        """Запуски ``running``, оборванные остановкой процесса, — ``failed``. Только для очереди в процессе
        API: с внешним брокером запуск выполняет воркер, и перезапуск API его не обрывает."""
        with db.session(self.engine) as session:
            session.execute(
                update(PipelineRunRow).where(PipelineRunRow.status == "running")
                .values(status="failed", finished_at=now_iso(), error="сервис остановлен во время обработки")
                .execution_options(synchronize_session=False)
            )
