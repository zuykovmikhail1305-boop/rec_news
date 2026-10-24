from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from auth import (clear_session, csrf_token, current_user_id, remember_profile,
                   set_csrf_cookie, valid_csrf)
from ml_client import MLClient, MLUnavailable, ProfileNotFound, get_ml_client

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


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
def home(request: Request, ml: MLClient = Depends(get_ml_client)):
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
    response = page(request, "demo_home.html", {"details": details, "user_id": user_id,
                    "csrf": token}, status=200 if details else 503)
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
