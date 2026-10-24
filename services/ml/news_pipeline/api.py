"""HTTP API ML-части на FastAPI — только ручки обработки ML.

    python scripts/run_api.py            # http://127.0.0.1:8000/docs

* обработка новостей из базы: ``POST /processing.enqueue``, ``GET /processing.result``;
* профили пользователей: ``GET /profiles``, ``GET /profiles/{user_id}``;
* новости пользователя с аналитикой: ``GET /users/{user_id}/news``;
* карточка инсайта: ``POST`` / ``GET /users/{user_id}/news/{news_id}/insight``;
* все карточки пользователя через очередь задач: ``POST /users/{user_id}/insights/generate``,
  ``GET /insights/jobs/{job_id}``, ``GET /users/{user_id}/insights/jobs/latest``,
  ``POST /insights/jobs/{job_id}/cancel``;
* чат пользователя с агентом: ``POST /chat/{user_id}``, ``GET /chat/{user_id}``.

Переменные окружения: ``REC_NEWS_DATA_DIR`` — каталог баз (по умолчанию
``data/``), ``REC_NEWS_USE_LLM=0`` — работать без LLM.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Response
from pydantic import BaseModel

from news_pipeline.agent import AgentAnswer, ChatHistory, UserChatMessage
from news_pipeline.config import DEMO_NOW
from news_pipeline.env import env_flag
from news_pipeline.insights.schemas import InsightCard
from news_pipeline.schemas import ProfileDetails, ProfileList, UserNewsResponse
from news_pipeline.service import MLService
from news_pipeline.tasks.insight_jobs import NothingToGenerate
from news_pipeline.tasks.schemas import InsightJob, InsightJobRequest

_service: MLService | None = None


def service() -> MLService:
    if _service is None:
        raise HTTPException(503, "сервис запускается")
    return _service


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _service
    _service = MLService()
    if env_flag("REC_NEWS_PIPELINE_WORKER", False):  # иначе обработку запускает бэкенд: POST /processing.enqueue
        _service.start_worker()
    _service.resume_unfinished_runs()           # очередь в памяти: незавершённые запуски и карточки — заново
    _service.submit(_service.warm_up)            # датасет и факты о компаниях профилей — заранее, в фоне
    _service.insight_jobs.resume_unfinished()
    yield
    _service.close()
    _service = None


app = FastAPI(title="Новостная аналитика — ML", version="0.1.0", lifespan=lifespan)


# --- обработка ---------------------------------------------------------------------


class Enqueued(BaseModel):
    status: str        # accepted — запуск поставлен в очередь; already_running — обработка уже стоит в очереди или идёт
    news: int          # сколько новостей со статусом new ждало обработки


class ProcessingResult(BaseModel):
    result: bool       # true — обработка завершена и есть новости со статусом ready


@app.post("/processing.enqueue", status_code=202, response_model=Enqueued)
def processing_enqueue() -> Enqueued:
    """Обработать новости из ``news.db``: новости со статусом ``new`` проходят подготовку, дедупликацию
    и подбор аналитики (статус ``new → processing → ready``), затем пересчитываются ленты пользователей.
    Ставит задачу в очередь ``processing.run`` и сразу отвечает; готовность — ``GET /processing.result``.
    Если обработка уже стоит в очереди или идёт, новая не запускается (``status = already_running``)."""
    waiting, created = service().enqueue_processing()
    return Enqueued(status="accepted" if created else "already_running", news=waiting)


@app.get("/processing.result", response_model=ProcessingResult)
def processing_result() -> ProcessingResult:
    """``true`` — есть новости со статусом ``ready`` и обработка завершена (ленты пересчитаны);
    ``false`` — готовых новостей нет или обработка ещё идёт."""
    return ProcessingResult(result=service().processing_result())


# --- профили пользователей -------------------------------------------------------------


@app.get("/profiles", response_model=ProfileList)
def get_profiles(limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)) -> ProfileList:
    """Профили пользователей, доступные ML-части (по ним строятся ленты, карточки и чат): ``user_id``,
    роль, компания. По порядку ``user_id``; ``total`` — сколько профилей всего."""
    return service().profile_list(limit=limit, offset=offset)


@app.get("/profiles/{user_id}", response_model=ProfileDetails)
def get_profile(user_id: str) -> ProfileDetails:
    """Данные пользователя: анкета, профиль интересов (аспекты, важные сущности, веса тем), данные
    компании из анкеты и датасета (``F1…`` — на них опираются карточки инсайтов) и что уже посчитано:
    лента, карточки, чат."""
    try:
        return service().profile_details(user_id)
    except KeyError:
        raise HTTPException(404, f"профиль {user_id} не найден")


# --- новости пользователя ------------------------------------------------------------


@app.get("/users/{user_id}/news", response_model=UserNewsResponse)
def get_user_news(user_id: str, limit: int = 20, offset: int = 0, with_text: bool = False) -> UserNewsResponse:
    """Новости, подобранные пользователю при последней обработке, с аналитикой Альфа-Банка.
    ``status = not_computed`` — для пользователя ещё не было обработки (``POST /processing.enqueue``).
    ``with_text=true`` — полный текст новости и фрагмента аналитики."""
    try:
        return service().user_news(user_id, limit=limit, offset=offset, with_text=with_text)
    except KeyError:
        raise HTTPException(404, f"профиль {user_id} не найден")


# --- карточки инсайтов -----------------------------------------------------------------


@app.post("/users/{user_id}/news/{news_id}/insight", response_model=InsightCard)
def post_insight_card(user_id: str, news_id: str, refresh: bool = False) -> InsightCard:
    """Карточка инсайта по новости для пользователя: агент на каждый блок → программная проверка →
    агент-проверщик. Готовая карточка отдаётся сразу, если входные данные не менялись; новая строится
    30–80 с. ``refresh=true`` — построить заново. Контракт блоков — ``docs/insight_card.md``."""
    try:
        return service().insight_card(user_id, news_id, refresh=refresh)
    except KeyError:
        raise HTTPException(404, f"профиль {user_id} не найден")
    except LookupError:
        raise HTTPException(404, f"новость {news_id} не найдена среди обработанных")


@app.get("/users/{user_id}/news/{news_id}/insight", response_model=InsightCard)
def get_insight_card(user_id: str, news_id: str) -> InsightCard:
    """Готовая карточка инсайта без построения; ``404`` — её ещё нет (``POST`` той же ручки)."""
    card = service().cached_insight_card(user_id, news_id)
    if card is None:
        raise HTTPException(404, f"карточки для {user_id}/{news_id} ещё нет")
    return card


@app.post("/users/{user_id}/insights/generate", status_code=202, response_model=InsightJob)
def post_generate_insights(user_id: str, response: Response, request: InsightJobRequest | None = None) -> InsightJob:
    """Запустить генерацию карточек инсайтов для пользователя — по умолчанию для всех его новостей
    из ``user_news``. Работает в фоне через очередь задач и сразу возвращает задание (``202``);
    ход — ``GET /insights/jobs/{job_id}``. Если у пользователя уже идёт задание, новое не создаётся:
    возвращается текущее со статусом ответа ``200``. Готовые карточки — ``GET /users/{id}/news/{news_id}/insight``."""
    request = request or InsightJobRequest()
    try:
        job, created = service().insight_jobs.start(user_id, news_ids=request.news_ids, limit=request.limit,
                                                    refresh=request.refresh)
    except KeyError:
        raise HTTPException(404, f"профиль {user_id} не найден")
    except NothingToGenerate:
        raise HTTPException(409, f"у {user_id} ещё нет подобранных новостей — сначала POST /processing.enqueue")
    except LookupError as error:
        raise HTTPException(404, f"новости не найдены среди обработанных: {error}")
    if not created:
        response.status_code = 200
    return job


@app.get("/insights/jobs/{job_id}", response_model=InsightJob)
def get_insight_job(job_id: str) -> InsightJob:
    """Статус задания: ``queued`` → ``running`` → ``done`` (хотя бы одна карточка построена),
    ``failed`` (ни одной) или ``cancelled``. ``items`` — статус и заголовок каждой карточки."""
    job = service().insight_jobs.job(job_id)
    if job is None:
        raise HTTPException(404, f"задание {job_id} не найдено")
    return job


@app.get("/users/{user_id}/insights/jobs/latest", response_model=InsightJob)
def get_latest_insight_job(user_id: str) -> InsightJob:
    """Последнее задание пользователя."""
    job = service().insight_jobs.latest(user_id)
    if job is None:
        raise HTTPException(404, f"у {user_id} ещё не было заданий")
    return job


@app.post("/insights/jobs/{job_id}/cancel", response_model=InsightJob)
def cancel_insight_job(job_id: str) -> InsightJob:
    """Отменить задание: ещё не начатые карточки не строятся, начатые достраиваются."""
    job = service().insight_jobs.cancel(job_id)
    if job is None:
        raise HTTPException(404, f"задание {job_id} не найдено")
    return job


# --- чат пользователя с агентом ------------------------------------------------------------

# Сообщения одного пользователя обрабатываются по очереди: реплики диалога не перемешиваются.
_chat_locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)
_chat_locks_guard = threading.Lock()


def _chat_lock(user_id: str) -> threading.Lock:
    with _chat_locks_guard:
        return _chat_locks[user_id]


@app.post("/chat/{user_id}", response_model=AgentAnswer)
def post_user_chat(user_id: str, request: UserChatMessage) -> AgentAnswer:
    """Сообщение пользователя в его чат с агентом. У каждого пользователя свой диалог: бэкенду не нужно
    хранить ``session_id`` — сообщение продолжает текущий диалог пользователя, агент помнит последние
    реплики и источники. ``new_dialog: true`` — начать новый диалог.

    Агент отвечает по новостям ленты пользователя и аналитике Альфа-Банка: ``text`` — готовый текст
    ответа, ``sections`` — разделы с пометкой ``origin`` (``bank`` — позиция банка, ``ai`` — вывод
    ассистента), ``sources`` — источники. Ответ строится до минуты: таймаут клиента — не меньше 120 с."""
    svc = service()
    with _chat_lock(user_id):
        session_id = None if request.new_dialog else svc.agent.store.user_session(user_id)
        try:
            return svc.agent.ask(user_id, request.message, session_id=session_id, now=request.now or DEMO_NOW)
        except KeyError:
            raise HTTPException(404, f"профиль {user_id} не найден")


@app.get("/chat/{user_id}", response_model=ChatHistory)
def get_user_chat(user_id: str, limit: int = 50) -> ChatHistory:
    """Текущий диалог пользователя: последние ``limit`` реплик по порядку (у ответов агента — полный
    ``answer``). ``session_id = null`` и пустой ``turns`` — пользователь ещё не писал в чат."""
    svc = service()
    if svc.profiles.get(user_id) is None:
        raise HTTPException(404, f"профиль {user_id} не найден")
    session_id = svc.agent.store.user_session(user_id)
    history = svc.agent.store.history(session_id, limit=limit) if session_id else None
    return history or ChatHistory(session_id=None, user_id=user_id, turns=[])
