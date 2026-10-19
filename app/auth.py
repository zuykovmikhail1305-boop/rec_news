import os
import secrets
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from . import models
from .database import get_db
from .security import pwd_context

SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_hex(32))
ALGORITHM = "HS256"

REMINDER_MAX_AGE = timedelta(days=14)


def create_access_token(user_id: int) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=24)
    return jwt.encode({"sub": str(user_id), "exp": expire}, SECRET_KEY, algorithm=ALGORITHM)


def _decode(token: str) -> int | None:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return int(payload.get("sub"))
    except (JWTError, TypeError, ValueError):
        return None


def get_current_user(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get("access_token")
    if token:
        user_id = _decode(token)
        if user_id:
            user = db.get(models.User, user_id)
            if user:
                return user
    return None


def login_required(user: models.User | None):
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )
    return user


def remember_user_cookie(response, db: Session, user: models.User):
    """Памятка: ставим долгий cookie, чтобы не логиниться каждый раз."""
    token = create_access_token(user.id)
    db_exp = datetime.now(timezone.utc) + REMINDER_MAX_AGE
    response.set_cookie(
        "access_token",
        token,
        max_age=int(REMINDER_MAX_AGE.total_seconds()),
        expires=db_exp,
        httponly=True,
        samesite="lax",
    )


def logout_cookie(response):
    response.delete_cookie("access_token")


def login_redirect_response(url: str = "/"):
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


from .security import verify_password  # noqa: E402
