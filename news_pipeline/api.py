"""HTTP API ML-части (ТЗ §9) на FastAPI.

    python scripts/run_api.py            # http://127.0.0.1:8000/docs

Переменные окружения: ``REC_NEWS_DATA_DIR`` — каталог баз (по умолчанию
``data/``), ``REC_NEWS_USE_LLM=0`` — работать без LLM.
"""

from __future__ import annotations

import json
import queue
import threading
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from news_pipeline.agent import AgentAnswer, BriefRequest, ChatFeedback, ChatHistory, ChatRequest
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


# --- агент бизнес-инсайтов (docs/agent_tz.md §7) -------------------------------


def _agent_call(fn, *args, **kwargs) -> AgentAnswer:
    try:
        return fn(*args, **kwargs)
    except KeyError as error:
        raise HTTPException(404, f"профиль {error.args[0]} не найден — сначала POST /profiles")
    except PermissionError as error:
        raise HTTPException(403, str(error))


@app.post("/chat", response_model=AgentAnswer)
def post_chat(request: ChatRequest) -> AgentAnswer:
    """Вопрос агенту: ответ по новостям и аналитике, подобранным для пользователя.
    ``session_id`` — продолжить диалог (уточнения ссылаются на те же источники)."""
    agent = service().agent
    return _agent_call(agent.ask, request.user_id, request.question, session_id=request.session_id, now=request.now)


@app.post("/chat/stream")
def post_chat_stream(request: ChatRequest) -> StreamingResponse:
    """То же, что ``/chat``, но потоком SSE: context → plan → sections → check → review → answer."""
    agent = service().agent
    events: queue.Queue = queue.Queue()

    def run() -> None:
        try:
            agent.ask(request.user_id, request.question, session_id=request.session_id, now=request.now,
                      on_event=lambda stage, data: events.put((stage, data)))
        except KeyError as error:
            events.put(("error", {"detail": f"профиль {error.args[0]} не найден"}))
        except Exception as error:  # noqa: BLE001
            events.put(("error", {"detail": str(error)}))
        finally:
            events.put(None)

    threading.Thread(target=run, daemon=True).start()

    def stream():
        while (item := events.get()) is not None:
            stage, data = item
            yield f"event: {stage}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/insights/brief", response_model=AgentAnswer)
def post_brief(request: BriefRequest) -> AgentAnswer:
    """Главное за период для пользователя — без вопроса."""
    return _agent_call(service().agent.brief, request.user_id, session_id=request.session_id, now=request.now)


@app.get("/chat/{session_id}", response_model=ChatHistory)
def get_chat(session_id: str) -> ChatHistory:
    history = service().agent.store.history(session_id)
    if history is None:
        raise HTTPException(404, f"сессия {session_id} не найдена")
    return history


@app.post("/chat/{session_id}/feedback", status_code=202, response_model=Accepted)
def post_chat_feedback(session_id: str, feedback: ChatFeedback) -> Accepted:
    store = service().agent.store
    history = store.history(session_id)
    if history is None:
        raise HTTPException(404, f"сессия {session_id} не найдена")
    store.feedback(session_id, history.user_id, feedback)
    return Accepted()
