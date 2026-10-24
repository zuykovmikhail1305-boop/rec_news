from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates

if __package__:
    from .auth import (clear_session, csrf_token, current_user_id, remember_profile,
                       set_csrf_cookie, valid_csrf)
    from .ml_client import MLClient, MLUnavailable, MLNotReady, ProfileNotFound, get_ml_client
else:
    from auth import (clear_session, csrf_token, current_user_id, remember_profile,
                      set_csrf_cookie, valid_csrf)
    from ml_client import MLClient, MLUnavailable, MLNotReady, ProfileNotFound, get_ml_client


router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def safe_url(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        return value if parsed.scheme in ("https", "http") and parsed.hostname and not parsed.username else None
    except ValueError:
        return None


def local_date(value):
    if not value:
        return ""
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo:
            dt = dt.astimezone(timezone(timedelta(hours=3)))
        return dt.strftime("%d.%m.%Y")
    except (ValueError, TypeError, AttributeError):
        return ""


templates.env.filters.update(safe_url=safe_url, local_date=local_date,
                             path_id=lambda value: quote(str(value), safe=""))


def feed_state(ml, user_id, offset=0):
    feed, job, error = None, None, None
    try:
        feed = ml.feed(user_id, limit=20, offset=offset)
    except (ProfileNotFound, MLNotReady):
        error = "Лента пользователя ещё не подготовлена."
    except MLUnavailable:
        error = "Не удалось загрузить инсайты. Попробуйте ещё раз."
    try:
        job = ml.latest_job(user_id)
    except ProfileNotFound:
        pass
    except (MLUnavailable, MLNotReady):
        error = error or "Не удалось обновить состояние подготовки карточек."
    return feed, job, error


def feed_context(feed, job, error, offset=0):
    return {"feed": feed, "job": job, "feed_error": error, "offset": offset,
            "item_states": {item.news_id: item.status for item in job.items} if job else {}}


def json_response(data, status=200):
    return JSONResponse(data, status_code=status, headers={"Cache-Control": "no-store"})


def page(request, template, context, status=200):
    response = templates.TemplateResponse(request, template, context, status_code=status)
    response.headers["Cache-Control"] = "no-store"
    return response


def login_page(request, ml, *, offset=0, error=None, status=200):
    profiles = None
    try:
        profiles = ml.profiles(limit=50, offset=offset)
    except (MLUnavailable, ProfileNotFound):
        error = error or "Профили пока недоступны. Запустите ML-сервис и попробуйте ещё раз."
        status = 503
    token = csrf_token(request)
    response = page(request, "demo_login.html", {"profiles": profiles, "error": error,
                    "csrf": token, "offset": offset, "page_size": 50}, status)
    set_csrf_cookie(response, token)
    return response


@router.get("/login")
def login(request: Request, offset: int = Query(0, ge=0), switch: bool = False,
          ml: MLClient = Depends(get_ml_client)):
    if current_user_id(request) and not switch:
        return RedirectResponse("/", status_code=303)
    return login_page(request, ml, offset=offset)


@router.post("/login")
def select_profile(request: Request, user_id: Annotated[str, Form(min_length=1, max_length=200)],
                   csrf: Annotated[str, Form()], ml: MLClient = Depends(get_ml_client)):
    if not valid_csrf(request, csrf):
        return login_page(request, ml, error="Форма устарела. Выберите профиль ещё раз.", status=403)
    try:
        details = ml.profile(user_id)
    except ProfileNotFound:
        return login_page(request, ml, error="Этот профиль больше не доступен. Выберите другой.", status=404)
    except MLUnavailable as exc:
        return login_page(request, ml, error=str(exc), status=503)
    response = RedirectResponse("/", status_code=303)
    remember_profile(response, details.user_id)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/")
def home(request: Request, offset: int = Query(0, ge=0), ml: MLClient = Depends(get_ml_client)):
    user_id = current_user_id(request)
    if user_id is None:
        return RedirectResponse("/login", status_code=303)
    try:
        details = ml.profile(user_id)
    except ProfileNotFound:
        response = RedirectResponse("/login", status_code=303)
        clear_session(response)
        return response
    except MLUnavailable:
        details = None
    token = csrf_token(request)
    feed, job, error = feed_state(ml, user_id, offset) if details else (None, None, None)
    response = page(request, "demo_home.html", {"details": details, "user_id": user_id,
                    "csrf": token, **feed_context(feed, job, error, offset)}, status=200 if details else 503)
    set_csrf_cookie(response, token)
    return response



@router.post("/logout")
def logout(request: Request, csrf: Annotated[str, Form()]):
    if not valid_csrf(request, csrf):
        return page(request, "demo_error.html", {"message": "Форма устарела. Обновите страницу и повторите выход."}, 403)
    response = RedirectResponse("/login", status_code=303)
    clear_session(response)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/insights")
@router.get("/insights/prototype")
def insights(request: Request):
    return RedirectResponse("/" if current_user_id(request) else "/login", status_code=303)


@router.post("/api/insights/generate")
def generate_insights(request: Request, csrf: Annotated[str, Form()], ml: MLClient = Depends(get_ml_client)):
    user_id = current_user_id(request)
    if user_id is None:
        return json_response({"detail": "Нужен вход"}, 401)
    if not valid_csrf(request, csrf):
        return json_response({"detail": "Форма устарела. Обновите страницу."}, 403)
    try:
        job = ml.generate_insights(user_id)
        return json_response({"user_id": user_id, "job": job.model_dump(mode="json")}, 202)
    except MLNotReady:
        return json_response({"detail": "Для этого профиля ещё не подготовлена лента. Генерация пока недоступна."}, 409)
    except ProfileNotFound:
        return json_response({"detail": "Для этого профиля пока нет новостей для генерации."}, 404)
    except MLUnavailable:
        return json_response({"detail": "Не удалось запустить подготовку инсайтов. Попробуйте ещё раз."}, 503)


@router.get("/api/insights")
def get_insights(request: Request, offset: int = Query(0, ge=0), ml: MLClient = Depends(get_ml_client)):
    user_id = current_user_id(request)
    if user_id is None:
        return json_response({"detail": "Нужен вход"}, 401)
    feed, job, error = feed_state(ml, user_id, offset)
    context = feed_context(feed, job, error, offset)
    html = templates.env.get_template("insights_live_feed.html").render(**context)
    return json_response({"user_id": user_id, "html": html,
        "poll": bool(job and job.status in ("queued", "running")), "error": error}, 200 if feed else 503)


def card_state(ml, user_id, news_id):
    try:
        return ml.insight(user_id, news_id), "ready", None
    except ProfileNotFound:
        try:
            job = ml.latest_job(user_id)
        except (ProfileNotFound, MLUnavailable, MLNotReady):
            return None, "missing", "Карточка пока не подготовлена. Вернитесь на главную, чтобы запустить подготовку."
        item = next((item for item in job.items if item.news_id == news_id), None)
        if item and item.status in ("queued", "running"):
            return None, "pending", "Карточка готовится. Она появится здесь автоматически."
        if item and item.status in ("failed", "cancelled"):
            return None, "failed", "Карточку не удалось подготовить." if item.status == "failed" else "Подготовка карточки отменена."
        return None, "missing", "Для этой новости пока нет готовой карточки."
    except (MLUnavailable, MLNotReady):
        return None, "error", "Не удалось загрузить карточку. Попробуйте ещё раз."


@router.get("/insights/{news_id}")
def insight_page(request: Request, news_id: str, ml: MLClient = Depends(get_ml_client)):
    user_id = current_user_id(request)
    if user_id is None:
        return RedirectResponse("/login", status_code=303)
    card, state, message = card_state(ml, user_id, news_id)
    token = csrf_token(request)
    response = page(request, "insights_live_page.html", {"user_id": user_id, "card": card,
                    "card_state": state, "message": message, "news_id": news_id, "csrf": token},
                    status=503 if state == "error" else 200)
    set_csrf_cookie(response, token)
    return response


@router.get("/api/insights/{news_id}")
def get_insight(request: Request, news_id: str, ml: MLClient = Depends(get_ml_client)):
    user_id = current_user_id(request)
    if user_id is None:
        return json_response({"detail": "Нужен вход"}, 401)
    card, state, message = card_state(ml, user_id, news_id)
    html = templates.env.get_template("insights_live_card.html").render(card=card, card_state=state, message=message)
    return json_response({"user_id": user_id, "html": html, "poll": state == "pending"}, 503 if state == "error" else 200)
