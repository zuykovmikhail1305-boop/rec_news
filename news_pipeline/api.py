"""HTTP API ML-части (ТЗ §9) на FastAPI.

    python scripts/run_api.py            # http://127.0.0.1:8000/docs

Переменные окружения: ``REC_NEWS_DATA_DIR`` — каталог баз (по умолчанию
``data/``), ``REC_NEWS_USE_LLM=0`` — работать без LLM.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from news_pipeline.schemas import (
    AnalyticsIn,
    EventAnalyticsResult,
    FeedbackEvent,
    FeedRequest,
    FeedResponse,
    NewsIn,
    UserProfileIn,
)
from news_pipeline.service import MLService

_service: MLService | None = None


def service() -> MLService:
    if _service is None:
        raise HTTPException(503, "сервис запускается")
    return _service


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _service
    _service = MLService()
    _service.start_worker()
    yield
    _service.close()
    _service = None


app = FastAPI(title="Новостная аналитика — ML", version="0.1.0", lifespan=lifespan)


class Accepted(BaseModel):
    status: str = "accepted"
    count: int = 1
    detail: str | None = None


@app.post("/profiles", status_code=202, response_model=Accepted)
def post_profile(user: UserProfileIn) -> Accepted:
    """Создать или обновить профиль: профиль интересов строится асинхронно."""
    service().submit_profile(user)
    return Accepted(detail=f"профиль {user.user_id} строится")


@app.get("/profiles/{user_id}/interests")
def get_interests(user_id: str, with_embeddings: bool = False) -> dict:
    """Аспекты, сущности и веса тем (для отладки и UI)."""
    svc = service()
    profile = svc.profiles.get(user_id)
    if profile is None:
        status = svc.profile_status.get(user_id)
        raise HTTPException(404 if status is None else 409, f"профиля нет (статус: {status or 'не создан'})")
    data = profile.model_dump()
    if not with_embeddings:
        for aspect in data["aspects"]:
            aspect.pop("embedding", None)
    return data


@app.post("/news", status_code=202, response_model=Accepted)
def post_news(news: NewsIn | list[NewsIn]) -> Accepted:
    """Принять новость или пачку новостей; обработка — в фоновом конвейере."""
    items = news if isinstance(news, list) else [news]
    added = service().accept_news(items)
    return Accepted(count=added, detail="новости поставлены в обработку")


@app.post("/analytics", status_code=202, response_model=Accepted)
def post_analytics(materials: AnalyticsIn | list[AnalyticsIn]) -> Accepted:
    """Принять материалы аналитики: индексация и повторный подбор для событий за 7 дней — асинхронно."""
    items = materials if isinstance(materials, list) else [materials]
    service().submit_analytics(items)
    return Accepted(count=len(items), detail="материалы поставлены на индексацию")


@app.get("/events/{event_id}/analytics", response_model=EventAnalyticsResult)
def get_event_analytics(event_id: str) -> EventAnalyticsResult:
    """Прикреплённые к событию фрагменты аналитики (для отладки и UI)."""
    result = service().analytics.get(event_id)
    if result is None:
        raise HTTPException(404, f"для события {event_id} подбор ещё не выполнялся")
    return result


@app.post("/events/{event_id}/analytics/rematch", response_model=EventAnalyticsResult)
def rematch_event_analytics(event_id: str) -> EventAnalyticsResult:
    """Принудительный повторный подбор аналитики для события."""
    try:
        return service().rematch(event_id)
    except KeyError:
        raise HTTPException(404, f"событие {event_id} не найдено")


@app.post("/recommend", response_model=FeedResponse)
def post_recommend(request: FeedRequest, now: datetime | None = None, version: int = 4) -> FeedResponse:
    """Персональная лента. ``now`` и ``version`` (1–4) — для демо и замеров."""
    try:
        return service().recommend(request, now=now, version=version)
    except KeyError:
        raise HTTPException(404, f"профиль {request.user_id} не найден — сначала POST /profiles")


@app.post("/feedback", status_code=202, response_model=Accepted)
def post_feedback(event: FeedbackEvent) -> Accepted:
    service().record_feedback(event)
    return Accepted()


@app.get("/health")
def health() -> dict:
    return service().health()
