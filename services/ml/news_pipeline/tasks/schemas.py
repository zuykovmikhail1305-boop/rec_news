"""Задания на генерацию карточек инсайтов — контракт API (docs/insight_jobs.md)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

JobStatus = Literal["queued", "running", "done", "failed", "cancelled"]
ItemStatus = Literal["queued", "running", "done", "failed", "cancelled"]


class InsightJobRequest(BaseModel):
    """Какие карточки построить. По умолчанию — для всех новостей пользователя из ``user_news``.

    ``news_ids`` — только эти новости (в этом порядке); ``limit`` — только первые N новостей ленты;
    ``refresh`` — строить заново даже готовые карточки (иначе готовая берётся из базы, если входные
    данные не менялись).
    """

    news_ids: list[str] | None = Field(default=None, min_length=1, max_length=100)
    limit: int | None = Field(default=None, ge=1, le=100)
    refresh: bool = False


class InsightJobItem(BaseModel):
    news_id: str
    position: int
    status: ItemStatus
    card_id: str | None = None
    card_status: str | None = None      # ready | partial | degraded
    headline: str | None = None
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class InsightJob(BaseModel):
    job_id: str
    user_id: str
    status: JobStatus
    refresh: bool
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    total: int
    queued: int = 0
    running: int = 0
    done: int = 0
    failed: int = 0
    cancelled: int = 0
    items: list[InsightJobItem] = Field(default_factory=list)
