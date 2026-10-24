"""Signed demo profile session. This is profile selection, not password authentication."""
import os
import secrets

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

COOKIE = "demo_session"
CSRF_COOKIE = "demo_csrf"
MAX_AGE = 14 * 24 * 60 * 60
# A configured secret keeps sessions valid across restarts; otherwise restart signs everyone out.
_SECRET = os.getenv("SECRET_KEY") or secrets.token_urlsafe(32)
_session = URLSafeTimedSerializer(_SECRET, salt="rec-news-demo-session-v1")
_csrf = URLSafeTimedSerializer(_SECRET, salt="rec-news-demo-csrf-v1")


def current_user_id(request):
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    try:
        data = _session.loads(token, max_age=MAX_AGE)
        user_id = data.get("user_id") if isinstance(data, dict) else None
        return user_id if isinstance(user_id, str) and 0 < len(user_id) <= 200 else None
    except (BadSignature, SignatureExpired):
        return None


def _set_cookie(response, name, value, max_age):
    response.set_cookie(name, value, max_age=max_age, httponly=True, samesite="lax",
                        secure=os.getenv("ENV", "dev").lower() == "prod", path="/")


def remember_profile(response, user_id):
    _set_cookie(response, COOKIE, _session.dumps({"user_id": user_id}), MAX_AGE)


def clear_session(response):
    response.delete_cookie(COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


def csrf_token(request):
    token = request.cookies.get(CSRF_COOKIE)
    if token:
        try:
            _csrf.loads(token, max_age=3600)
            return token
        except (BadSignature, SignatureExpired):
            pass
    return _csrf.dumps(secrets.token_urlsafe(32))


def set_csrf_cookie(response, token):
    _set_cookie(response, CSRF_COOKIE, token, 3600)


def valid_csrf(request, token):
    cookie = request.cookies.get(CSRF_COOKIE, "")
    if not token or not cookie or not secrets.compare_digest(cookie, token):
        return False
    try:
        _csrf.loads(token, max_age=3600)
        return True
    except (BadSignature, SignatureExpired):
        return False
