from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates

if __package__:
    from . import demo
    from .ml_client import MLClient, MLUnavailable, MLNotReady, ProfileNotFound, get_ml_client
else:
    import demo
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
        feed = ml.feed(user_id, limit=demo.INSIGHTS_LIMIT, offset=0)
    except (ProfileNotFound, MLNotReady):
        error = None   # лента ещё подбирается — это не ошибка, показываем статус подготовки
    except MLUnavailable:
        error = "Не удалось загрузить инсайты. Попробуйте ещё раз."
    try:
        job = ml.latest_job(user_id)
    except ProfileNotFound:
        pass
    except (MLUnavailable, MLNotReady):
        error = error or "Не удалось обновить состояние подготовки карточек."
    return feed, job, error


def completed_cards(ml, user_id, feed, job, offset=0):
    """Read actual cached cards when the personal feed is absent or empty."""
    if (feed and feed.items) or not job:
        return []
    ids = list(dict.fromkeys(item.news_id for item in job.items if item.status == "done"))
    cards = []
    for news_id in ids[offset:offset + 20]:
        try:
            cards.append(ml.insight(user_id, news_id))
        except (ProfileNotFound, MLNotReady, MLUnavailable):
            # A completed job is not proof that its card still exists.
            continue
    return cards


def feed_context(feed, job, error, offset=0, cards=None):
    return {"feed": feed, "job": job, "feed_error": error, "offset": offset,
            "completed_cards": cards or [],
            "item_states": {item.news_id: item.status for item in job.items} if job else {}}


def json_response(data, status=200):
    return JSONResponse(data, status_code=status, headers={"Cache-Control": "no-store"})


def page(request, template, context, status=200):
    response = templates.TemplateResponse(request, template, context, status_code=status)
    response.headers["Cache-Control"] = "no-store"
    return response


_companies: dict = {}


def demo_companies(ml):
    """Данные трёх компаний демо из готовых профилей ML (читаются один раз)."""
    if len(_companies) < len(demo.COMPANIES):
        for key, source in demo.COMPANIES.items():
            if key not in _companies:
                _companies[key] = ml.profile(source).profile
    return _companies


def profiles_page(request, ml, *, error=None, status=200):
    companies = {}
    try:
        companies = demo_companies(ml)
    except (MLUnavailable, ProfileNotFound):
        error = error or "Компании пока недоступны. Запустите ML-сервис и попробуйте ещё раз."
        status = 503
    return page(request, "demo_login.html", {"users": demo.USERS, "companies": companies, "error": error,
                                             "insights_limit": demo.INSIGHTS_LIMIT}, status)


@router.get("/profiles")
def profiles(request: Request, ml: MLClient = Depends(get_ml_client)):
    return profiles_page(request, ml)


@router.get("/login", include_in_schema=False)
def legacy_login():
    return RedirectResponse("/profiles", status_code=303)


@router.get("/enter")
def enter(request: Request, user: str = Query(...), company: str = Query(...), ml: MLClient = Depends(get_ml_client)):
    """Вход: профиль «роль + компания» в ML и подбор новостей только для него, затем — главная."""
    if user not in demo.USERS or company not in demo.COMPANIES:
        return profiles_page(request, ml, error="Выберите пользователя и компанию из списка.", status=400)
    user_id = demo.profile_id(user, company)
    try:
        ml.ensure_profile(user_id, role=demo.USERS[user]["role"], company=demo_companies(ml)[company])
        ml.rebuild_news(user_id)
    except (MLUnavailable, ProfileNotFound, MLNotReady):
        return profiles_page(request, ml, error="Не удалось подготовить профиль. Попробуйте ещё раз.", status=503)
    return RedirectResponse("/?user_id=" + quote(user_id, safe=""), status_code=303)


@router.get("/")
def home(request: Request, user_id: str | None = Query(None, min_length=1, max_length=200),
         offset: int = Query(0, ge=0), ml: MLClient = Depends(get_ml_client)):
    if user_id is None or demo.parse_profile_id(user_id) is None:
        return profiles_page(request, ml)
    try:
        details = ml.profile(user_id)
    except ProfileNotFound:
        return profiles_page(request, ml, error="Профиль не найден. Выберите другую компанию.", status=404)
    except MLUnavailable:
        details = None
    feed, job, error = feed_state(ml, user_id, offset) if details else (None, None, None)
    cards = completed_cards(ml, user_id, feed, job, offset)
    return page(request, "demo_home.html", {"details": details, "user_id": user_id,
                **feed_context(feed, job, error, offset, cards)}, status=200 if details else 503)


@router.get("/insights")
@router.get("/insights/prototype")
def insights(user_id: str | None = Query(None, min_length=1, max_length=200)):
    return RedirectResponse("/?user_id=" + quote(user_id, safe="") if user_id else "/", status_code=303)


@router.post("/api/insights/generate")
def generate_insights(user_id: str = Query(..., min_length=1, max_length=200), ml: MLClient = Depends(get_ml_client)):
    try:
        if not ml.processing_ready():
            raise MLNotReady()
        job = ml.generate_insights(user_id, limit=demo.INSIGHTS_LIMIT)
        return json_response({"user_id": user_id, "job": job.model_dump(mode="json")}, 202)
    except MLNotReady:
        return json_response({"detail": "Лента готовится. Подготовка инсайтов начнётся автоматически.", "retry": True}, 409)
    except ProfileNotFound:
        return json_response({"detail": "Для этого профиля пока нет новостей для генерации."}, 404)
    except MLUnavailable:
        return json_response({"detail": "Не удалось запустить подготовку инсайтов. Попробуйте ещё раз."}, 503)


@router.get("/api/insights")
def get_insights(request: Request, user_id: str = Query(..., min_length=1, max_length=200),
                 offset: int = Query(0, ge=0), ml: MLClient = Depends(get_ml_client)):
    feed, job, error = feed_state(ml, user_id, offset)
    cards = completed_cards(ml, user_id, feed, job, offset)
    context = {"user_id": user_id, **feed_context(feed, job, error, offset, cards)}
    html = templates.env.get_template("insights_live_feed.html").render(**context)
    return json_response({"user_id": user_id, "html": html,
        "poll": bool(job and job.status in ("queued", "running")) or not feed or feed.status == "not_computed",
        "recovered_cards": bool(cards), "error": error}, 200 if feed or cards else 503)


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
def insight_page(request: Request, news_id: str, user_id: str = Query(..., min_length=1, max_length=200),
                 ml: MLClient = Depends(get_ml_client)):
    card, state, message = card_state(ml, user_id, news_id)
    return page(request, "insights_live_page.html", {"user_id": user_id, "card": card,
                    "card_state": state, "message": message, "news_id": news_id},
                    status=503 if state == "error" else 200)


@router.get("/api/insights/{news_id}")
def get_insight(request: Request, news_id: str, user_id: str = Query(..., min_length=1, max_length=200),
                ml: MLClient = Depends(get_ml_client)):
    card, state, message = card_state(ml, user_id, news_id)
    html = templates.env.get_template("insights_live_card.html").render(card=card, card_state=state, message=message)
    return json_response({"user_id": user_id, "html": html, "poll": state == "pending"}, 503 if state == "error" else 200)


@router.post("/api/insights/{news_id}/generate")
def generate_card(news_id: str, user_id: str = Query(..., min_length=1, max_length=200),
                  ml: MLClient = Depends(get_ml_client)):
    try:
        if not ml.processing_ready():
            raise MLNotReady()
        job = ml.generate_insights(user_id, news_ids=[news_id])
        deferred = (job.status in ("queued", "running") and
                    not any(item.news_id == news_id for item in job.items))
        return json_response({"user_id": user_id, "job": job.model_dump(mode="json"),
                              "retry": deferred}, 202)
    except MLNotReady:
        return json_response({"detail": "Лента готовится. Карточка будет запрошена автоматически.", "retry": True}, 409)
    except ProfileNotFound:
        return json_response({"detail": "Профиль или новость не найдены."}, 404)
    except MLUnavailable:
        return json_response({"detail": "Не удалось запустить подготовку карточки. Попробуйте ещё раз."}, 503)


@router.get("/api/processing/status")
def processing_status(request: Request):
    return json_response(getattr(request.app.state, "processing", {"enabled": False, "status": "disabled"}))
