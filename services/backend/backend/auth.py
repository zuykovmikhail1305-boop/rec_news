import os
from datetime import datetime, timedelta, timezone

from fastapi import Depends, Request
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from . import models
from .database import get_db

SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-change-me-in-production")
ALGORITHM = "HS256"

REMINDER_MAX_AGE = timedelta(days=14)


def create_access_token(user_id: int) -> str:
    """Токен живёт столько же, сколько remember-me cookie (REMINDER_MAX_AGE — 14 дней)."""
    expire = datetime.now(timezone.utc) + REMINDER_MAX_AGE
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


def remember_user_cookie(response, db: Session, user: models.User):
    """Памятка: ставим долгий cookie, чтобы не логиниться каждый раз (JWT живёт столько же)."""
    token = create_access_token(user.id)
    db_exp = datetime.now(timezone.utc) + REMINDER_MAX_AGE
    response.set_cookie(
        "access_token",
        token,
        max_age=int(REMINDER_MAX_AGE.total_seconds()),
        expires=db_exp,
        httponly=True,
        samesite="lax",
        secure=(os.getenv("ENV", "dev").lower() == "prod"),
    )


def logout_cookie(response):
    response.delete_cookie("access_token")
