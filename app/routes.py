from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from . import models, security
from .auth import get_current_user, remember_user_cookie, logout_cookie
from .database import get_db

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

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
        "dashboard.html",
        {"request": request, "user": user, "profile": user.profile},
    )


@router.get("/register")
def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})


@router.post("/register")
def register(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    full_name: str = Form(""),
    db: Session = Depends(get_db),
):
    existing = db.query(models.User).filter(models.User.email == email).first()
    if existing:
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "error": "Пользователь с таким email уже существует"},
            status_code=400,
        )
    user = models.User(
        email=email,
        hashed_password=security.hash_password(password),
        full_name=full_name,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    resp = RedirectResponse("/profile/setup", status_code=303)
    remember_user_cookie(resp, db, user)
    return resp


@router.get("/login")
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


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
            "login.html",
            {"request": request, "error": "Неверный email или пароль"},
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
    return templates.TemplateResponse(
        "profile_setup.html",
        {
            "request": request,
            "user": user,
            "profile": user.profile,
            "categories": CATEGORY_OPTIONS,
            "fso_options": FSO_OPTIONS,
            "sector_options": SECTOR_OPTIONS,
            "service_options": SERVICE_OPTIONS,
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
