"""Read-only ML client used by demo sign-in. No profile/account creation."""
import os
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field, ValidationError


class MLUnavailable(RuntimeError):
    pass


class ProfileNotFound(RuntimeError):
    pass


class Profile(BaseModel):
    user_id: str = Field(min_length=1, max_length=200)
    role: str
    company_name: str | None = None
    industry: str | None = None
    company_id: str | None = None
    company_description: str | None = None
    regions: list[str] = Field(default_factory=list)
    products: list[str] = Field(default_factory=list)


class ProfileList(BaseModel):
    total: int = Field(ge=0)
    items: list[Profile]


class ProfileDetails(BaseModel):
    user_id: str
    profile: Profile
    company_facts: list[dict] = Field(default_factory=list)
    activity: dict = Field(default_factory=dict)
    interests: dict = Field(default_factory=dict)


class MLClient:
    def __init__(self, client):
        self.client = client

    def _get(self, path, model, **params):
        try:
            response = self.client.get(path, params=params)
            if response.status_code == 404:
                raise ProfileNotFound("Профиль больше не доступен")
            response.raise_for_status()
            return model.model_validate(response.json())
        except (httpx.HTTPError, ValidationError, ValueError) as exc:
            raise MLUnavailable("Не удалось получить профили. Проверьте, что ML-сервис запущен.") from exc

    def profiles(self, limit=50, offset=0):
        return self._get("/profiles", ProfileList, limit=limit, offset=offset)

    def profile(self, user_id):
        result = self._get("/profiles/" + quote(user_id, safe=""), ProfileDetails)
        if result.user_id != user_id or result.profile.user_id != user_id:
            raise MLUnavailable("ML-сервис вернул профиль с другим идентификатором")
        return result


def get_ml_client():
    with httpx.Client(base_url=os.getenv("ML_API_URL", "http://127.0.0.1:8002").rstrip("/"),
                      timeout=httpx.Timeout(10, connect=3), follow_redirects=False) as client:
        yield MLClient(client)
