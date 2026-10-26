"""Backend schedules ML processing when committed input news are available."""
import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
import psycopg
from psycopg import sql

from .ml_client import MLClient

logger = logging.getLogger(__name__)
LOCK_ID = 7265636_2026


@dataclass(frozen=True)
class ProcessingConfig:
    enabled: bool
    database_url: str | None
    schema: str
    interval: int

    @classmethod
    def from_env(cls):
        url = os.getenv("BACKEND_DATABASE_URL") or os.getenv("REC_NEWS_DATABASE_URL")
        enabled = os.getenv("BACKEND_PROCESSING_ENABLED", "true" if url else "false").lower()
        if enabled not in ("true", "false", "1", "0"):
            raise ValueError("BACKEND_PROCESSING_ENABLED must be true or false")
        interval = int(os.getenv("BACKEND_PROCESSING_INTERVAL", "15"))
        if interval < 5:
            raise ValueError("BACKEND_PROCESSING_INTERVAL must be >= 5")
        if enabled in ("true", "1") and not url:
            raise ValueError("Configure BACKEND_DATABASE_URL for automatic processing")
        if url:
            url = url.replace("postgresql+psycopg://", "postgresql://", 1)
            if not url.startswith(("postgresql://", "postgres://")):
                raise ValueError("Automatic processing requires the shared PostgreSQL database")
        return cls(enabled in ("true", "1"), url,
                   os.getenv("BACKEND_ML_NEWS_SCHEMA", "ml_news"), interval)


def processing_tick(config, client):
    # A session lock prevents simultaneous backend instances from sending the same trigger.
    # ML also deduplicates active runs; input statuses remain owned by ML.
    with psycopg.connect(config.database_url, connect_timeout=3, autocommit=True) as db:
        if not db.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_ID,)).fetchone()[0]:
            return {"status": "busy"}
        try:
            pending = db.execute(sql.SQL("SELECT EXISTS (SELECT 1 FROM {}.news WHERE status='new')")
                                 .format(sql.Identifier(config.schema))).fetchone()[0]
            if not pending:
                return {"status": "idle"}
            result = client.enqueue_processing()
            return {"status": result.status, "news": result.news}
        finally:
            db.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))


async def processing_loop(config, stop, state):
    while not stop.is_set():
        try:
            def tick():
                with httpx.Client(base_url=os.getenv("ML_API_URL", "http://127.0.0.1:8002").rstrip("/"),
                                  timeout=httpx.Timeout(10, connect=3), follow_redirects=False) as http:
                    return processing_tick(config, MLClient(http))
            result = await asyncio.to_thread(tick)
            state.update(result, error=None)
        except Exception as error:
            logger.warning("Automatic ML processing failed: %s", type(error).__name__)
            # Connection exceptions can contain credentials: do not expose their text.
            state.update(status="error", error=type(error).__name__)
        state["checked_at"] = datetime.now(timezone.utc).isoformat()
        try:
            await asyncio.wait_for(stop.wait(), timeout=config.interval)
        except asyncio.TimeoutError:
            pass
