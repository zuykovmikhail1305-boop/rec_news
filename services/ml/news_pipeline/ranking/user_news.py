"""Готовые рекомендации пользователям и журнал запусков обработки — ``data/recommendations.db`` (SQLAlchemy).

* ``user_news`` — какие новости подходят пользователю: строка на пару
  (``user_id``, ``news_id``) с местом в ленте, оценками и прикреплённой
  аналитикой. Пересчитывается целиком для пользователя при каждом запуске
  обработки (``POST /pipeline/run``), отдаётся ручкой ``GET /users/{user_id}/news``;
* ``pipeline_runs`` — запуски обработки: статус, этап, счётчики, ошибка.

Описание колонок — ``docs/news_db.md``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import Column, Float, Index, Integer, MetaData, Table, Text, delete, func, select, update

from news_pipeline.config import DATA_DIR
from news_pipeline.storage import db

RECOMMENDATIONS_DB = DATA_DIR / "recommendations.db"
ACTIVE_RUN = ("queued", "running")

metadata = MetaData()

USER_NEWS = Table(
    "user_news", metadata,
    Column("user_id", Text, primary_key=True),
    Column("news_id", Text, primary_key=True),
    Column("rank", Integer, nullable=False),
    Column("event_id", Text),
    Column("score", Float, nullable=False),
    Column("retrieval", Float),
    Column("sem", Float),
    Column("ent", Float),
    Column("topic", Float),
    Column("imp", Float),
    Column("fresh", Float),
    Column("llm", Integer),
    Column("why_relevant", Text),
    Column("analytics_id", Text),
    Column("analytics_chunk_id", Text),
    Column("analytics_relation", Text),
    Column("analytics_contradicts", Integer),
    Column("analytics_summary", Text),
    Column("run_id", Text, nullable=False),
    Column("computed_at", Text, nullable=False),
    Column("pipeline_version", Text, nullable=False),
    Index("user_news_rank", "user_id", "rank"),
    Index("user_news_news_id", "news_id"),
)

PIPELINE_RUNS = Table(
    "pipeline_runs", metadata,
    Column("run_id", Text, primary_key=True),
    Column("status", Text, nullable=False),
    Column("stage", Text),
    Column("requested_at", Text, nullable=False),
    Column("started_at", Text),
    Column("finished_at", Text),
    Column("demo_now", Text),
    *(Column(name, Integer, nullable=False, server_default="0")
      for name in ("news_prepared", "news_deduplicated", "news_unique", "events_matched", "users_total",
                   "users_done", "users_failed", "insights_built")),
    Column("error", Text),
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class UserNewsRow:
    user_id: str
    news_id: str
    rank: int
    event_id: str | None
    score: float
    retrieval: float | None
    sem: float | None
    ent: float | None
    topic: float | None
    imp: float | None
    fresh: float | None
    llm: int | None
    why_relevant: str | None
    analytics_id: str | None
    analytics_chunk_id: str | None
    analytics_relation: str | None
    analytics_contradicts: int | None
    analytics_summary: str | None
    run_id: str
    computed_at: str
    pipeline_version: str


USER_NEWS_COLUMNS = tuple(item.name for item in fields(UserNewsRow))
_SELECT_ROWS = select(*(USER_NEWS.c[name] for name in USER_NEWS_COLUMNS))


class UserNewsStore:
    def __init__(self, path: str | Path = RECOMMENDATIONS_DB) -> None:
        self.path = Path(path)
        self.engine = db.engine(self.path, wal=True)
        db.ensure_tables(self.engine, [USER_NEWS, PIPELINE_RUNS])

    # --- рекомендации ---------------------------------------------------------

    def replace_user(self, user_id: str, rows: list[UserNewsRow]) -> None:
        """Заменяет рекомендации пользователя одной транзакцией."""
        with self.engine.begin() as connection:
            connection.execute(delete(USER_NEWS).where(USER_NEWS.c.user_id == user_id))
            if rows:
                connection.execute(USER_NEWS.insert(), [row.__dict__ for row in rows])

    def for_user(self, user_id: str, *, limit: int | None = None, offset: int = 0) -> list[UserNewsRow]:
        query = _SELECT_ROWS.where(USER_NEWS.c.user_id == user_id).order_by(USER_NEWS.c.rank)
        if limit is not None:
            query = query.limit(limit).offset(offset)
        with self.engine.connect() as connection:
            return [UserNewsRow(*row) for row in connection.execute(query)]

    def row(self, user_id: str, news_id: str) -> UserNewsRow | None:
        query = _SELECT_ROWS.where(USER_NEWS.c.user_id == user_id, USER_NEWS.c.news_id == news_id)
        with self.engine.connect() as connection:
            row = connection.execute(query).first()
        return UserNewsRow(*row) if row else None

    def count(self, user_id: str) -> int:
        with self.engine.connect() as connection:
            return connection.scalar(select(func.count()).select_from(USER_NEWS).where(USER_NEWS.c.user_id == user_id))

    # --- запуски обработки ----------------------------------------------------

    # Состояние запусков живёт только здесь, в базе: обработку выполняет воркер очереди (сейчас — поток
    # в процессе API, с Celery — отдельный процесс), а API читает статус из той же таблицы.

    @staticmethod
    def _fail_stale(connection, timeout_hours: float) -> None:
        """Запуск, который идёт дольше ``timeout_hours`` (воркер упал), — ``failed``: иначе он навсегда
        «идёт», и новые запуски не создаются."""
        border = (datetime.now(timezone.utc) - timedelta(hours=timeout_hours)).isoformat(timespec="seconds")
        connection.execute(
            update(PIPELINE_RUNS)
            .where(PIPELINE_RUNS.c.status.in_(ACTIVE_RUN), func.coalesce(PIPELINE_RUNS.c.started_at,
                                                                         PIPELINE_RUNS.c.requested_at) < border)
            .values(status="failed", finished_at=now_iso(), error=f"запуск не завершился за {timeout_hours:g} ч")
        )

    def create_run_if_idle(self, demo_now: datetime | None, timeout_hours: float) -> tuple[dict[str, Any], bool]:
        """Новый запуск (``queued``), если не идёт другой; иначе текущий. Второе значение — создан ли новый.

        Проверка и создание — одна транзакция с блокировкой записи: два одновременных запроса
        (из одного или разных процессов) не создадут два запуска.
        """
        run_id = f"run_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
        with self.engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            self._fail_stale(connection, timeout_hours)
            active = connection.scalar(select(PIPELINE_RUNS.c.run_id).where(PIPELINE_RUNS.c.status.in_(ACTIVE_RUN))
                                       .order_by(PIPELINE_RUNS.c.requested_at.desc()).limit(1))
            if active is None:
                connection.execute(PIPELINE_RUNS.insert().values(
                    run_id=run_id, status="queued", requested_at=now_iso(),
                    demo_now=demo_now.isoformat() if demo_now else None,
                ))
            connection.commit()
        return self.run(active or run_id), active is None

    def active_run(self, timeout_hours: float) -> dict[str, Any] | None:
        """Запуск, который стоит в очереди или идёт; ``None`` — обработка не идёт."""
        with self.engine.begin() as connection:
            self._fail_stale(connection, timeout_hours)
        return self._run(select(PIPELINE_RUNS).where(PIPELINE_RUNS.c.status.in_(ACTIVE_RUN))
                         .order_by(PIPELINE_RUNS.c.requested_at.desc()).limit(1))

    def start_run(self, run_id: str) -> bool:
        """``queued → running``. ``False`` — запуск уже взят или завершён (повторная доставка задачи)."""
        with self.engine.begin() as connection:
            before = db.changes(connection)
            connection.execute(update(PIPELINE_RUNS).where(PIPELINE_RUNS.c.run_id == run_id,
                                                           PIPELINE_RUNS.c.status == "queued")
                               .values(status="running", started_at=now_iso(), stage="prepare"))
            return db.changes(connection) > before

    def queued_runs(self) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            return [db.row_dict(row) for row in connection.execute(
                select(PIPELINE_RUNS).where(PIPELINE_RUNS.c.status == "queued").order_by(PIPELINE_RUNS.c.requested_at))]

    def update_run(self, run_id: str, **values: Any) -> None:
        if not values:
            return
        with self.engine.begin() as connection:
            connection.execute(update(PIPELINE_RUNS).where(PIPELINE_RUNS.c.run_id == run_id).values(**values))

    def _run(self, query) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            row = connection.execute(query).first()
        return db.row_dict(row) if row else None

    def run(self, run_id: str) -> dict[str, Any] | None:
        return self._run(select(PIPELINE_RUNS).where(PIPELINE_RUNS.c.run_id == run_id))

    def latest_run(self) -> dict[str, Any] | None:
        return self._run(select(PIPELINE_RUNS).order_by(PIPELINE_RUNS.c.requested_at.desc()).limit(1))

    def fail_running(self) -> None:
        """Запуски ``running``, оборванные остановкой процесса, — ``failed``. Только для очереди в процессе
        API: с внешним брокером запуск выполняет воркер, и перезапуск API его не обрывает."""
        with self.engine.begin() as connection:
            connection.execute(
                update(PIPELINE_RUNS).where(PIPELINE_RUNS.c.status == "running")
                .values(status="failed", finished_at=now_iso(), error="сервис остановлен во время обработки")
            )
