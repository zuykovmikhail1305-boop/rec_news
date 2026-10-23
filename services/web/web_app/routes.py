from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models, security
from .auth import get_current_user, remember_user_cookie, logout_cookie
from .database import get_db

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

CATEGORY_OPTIONS = [c.value for c in models.BusinessCategory]
FSO_OPTIONS = [o.value for o in models.FsoExecution]
SECTOR_OPTIONS = [o.value for o in models.PreviousSector]
SERVICE_OPTIONS = [o.value for o in models.ServiceModel]


def _current(request: Request, db: Session):
    return get_current_user(request, db)


@router.get("/")
def index(request: Request, db: Session = Depends(get_db)):
    user = _current(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.profile is None:
        return RedirectResponse("/profile/setup", status_code=303)
    return templates.TemplateResponse(
        request, "dashboard.html", {"user": user, "profile": user.profile}
    )


@router.get("/register")
def register_page(request: Request):
    return templates.TemplateResponse(request, "register.html", {})


@router.post("/register")
def register(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    full_name: str = Form(""),
    db: Session = Depends(get_db),
):
    if "@" not in email or "." not in email.split("@")[1]:
        return templates.TemplateResponse(
            request,
            "register.html",
            {"error": "Введите корректный email"},
            status_code=400,
        )
    if len(password) < 6:
        return templates.TemplateResponse(
            request,
            "register.html",
            {"error": "Пароль должен содержать не менее 6 символов"},
            status_code=400,
        )
    existing = db.query(models.User).filter(models.User.email == email).first()
    if existing:
        return templates.TemplateResponse(
            request,
            "register.html",
            {"error": "Пользователь с таким email уже существует"},
            status_code=400,
        )
    user = models.User(
        email=email,
        hashed_password=security.hash_password(password),
        full_name=full_name,
    )
    try:
        db.add(user)
        db.commit()
    except IntegrityError:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "register.html",
            {"error": "Пользователь с таким email уже существует"},
            status_code=400,
        )
    db.refresh(user)
    resp = RedirectResponse("/profile/setup", status_code=303)
    remember_user_cookie(resp, db, user)
    return resp


@router.get("/login")
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html")


@router.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(models.User).filter(models.User.email == email).first()
    if user is None or not security.verify_password(password, user.hashed_password):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Неверный email или пароль"},
            status_code=400,
        )
    resp = RedirectResponse("/", status_code=303)
    remember_user_cookie(resp, db, user)
    return resp


@router.get("/profile/setup")
def profile_setup(request: Request, db: Session = Depends(get_db)):
    user = _current(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    profile = user.profile
    selected_categories = (
        (profile.categories or "").split(",") if profile and profile.categories else []
    )
    selected_service_models = (
        (profile.service_models or "").split(",")
        if profile and profile.service_models
        else []
    )
    return templates.TemplateResponse(
        request,
        "profile_setup.html",
        {
            "user": user,
            "profile": profile,
            "categories": CATEGORY_OPTIONS,
            "fso_options": FSO_OPTIONS,
            "sector_options": SECTOR_OPTIONS,
            "service_options": SERVICE_OPTIONS,
            "selected_categories": selected_categories,
            "selected_service_models": selected_service_models,
        },
    )


@router.post("/profile/setup")
def profile_setup_post(
    request: Request,
    db: Session = Depends(get_db),
    categories: list[str] = Form([]),
    activity_description: str = Form(""),
    fso_execution: str | None = Form(None),
    previous_sector: str | None = Form(None),
    holding_center_employee: str | None = Form(None),
    service_models: list[str] = Form([]),
):
    user = _current(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    if not categories or not activity_description.strip():
        profile = user.profile
        selected_categories = (
            (profile.categories or "").split(",")
            if profile and profile.categories
            else []
        )
        selected_service_models = (
            (profile.service_models or "").split(",")
            if profile and profile.service_models
            else []
        )
        return templates.TemplateResponse(
            request,
            "profile_setup.html",
            {
                "user": user,
                "profile": profile,
                "categories": CATEGORY_OPTIONS,
                "fso_options": FSO_OPTIONS,
                "sector_options": SECTOR_OPTIONS,
                "service_options": SERVICE_OPTIONS,
                "selected_categories": selected_categories,
                "selected_service_models": selected_service_models,
                "error": "Укажите хотя бы одну категорию и опишите характер деятельности",
            },
            status_code=400,
        )

    profile = user.profile or models.UserProfile(user_id=user.id)
    profile.categories = ",".join(categories)
    profile.activity_description = activity_description
    profile.fso_execution = fso_execution
    profile.previous_sector = previous_sector
    profile.holding_center_employee = holding_center_employee == "yes"
    profile.service_models = ",".join(service_models)
    user.profile = profile
    db.add(profile)
    db.commit()

    return RedirectResponse("/", status_code=303)


@router.get("/logout")
def logout():
    r = RedirectResponse("/login", status_code=303)
    logout_cookie(r)
    return r


# ---------- Прототип «Инсайты» ----------
# Декомпозированный прототип: разметка экранов лежит в services/web/web_app/templates/insights/*.html,
# стили и логика — в services/web/web_app/static/insights.{css,data,app}.js. Оригинал
# alfa-insights-prototype.html в папке services/web остаётся эталоном.
_INSIGHTS_TPL_DIR = Path(__file__).parent / "templates" / "insights"


@router.get("/insights")
def insights_redirect():
    return RedirectResponse("/insights/prototype", status_code=302)


@router.get("/insights/prototype")
def insights_prototype(request: Request):
    return templates.TemplateResponse(request, "insights/prototype.html")


@router.get("/insights/parser")
def insights_parser(request: Request):
    """Интерактивная, полностью локальная демонстрация news-parser pipeline."""
    return templates.TemplateResponse(request, "insights/parser.html")


@router.get("/insights/tpl/{name}")
def insights_tpl(name: str):
    # Отдаём HTML-фрагмент без обработки Jinja2 (только токены JS-загрузчика).
    if "/" in name or "\\" in name or ".." in name or name.startswith("."):
        raise HTTPException(status_code=404, detail="template not found")
    path = _INSIGHTS_TPL_DIR / f"{name}.html"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="template not found")
    return FileResponse(path, media_type="text/html")
