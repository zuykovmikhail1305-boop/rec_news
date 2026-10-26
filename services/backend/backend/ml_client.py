"""Backend client for demo profiles, user feeds and asynchronous insight generation."""
import os
from datetime import datetime
from typing import Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field, ValidationError


class MLUnavailable(RuntimeError):
    pass


class MLNotReady(RuntimeError):
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


class InsightSummary(BaseModel):
    card_id: str
    status: str
    headline: str | None = None
    impact: str
    priority: str
    horizon: str
    reliability: str


class NewsItem(BaseModel):
    news_id: str
    title: str
    lead: str = ""
    source: str | None = None
    published_at: datetime | None = None
    why_relevant: str | None = None
    insight: InsightSummary | None = None
    analytics: dict | None = None
    topics: list[str] = Field(default_factory=list)


class UserFeed(BaseModel):
    user_id: str
    status: Literal["ready", "not_computed"]
    total: int = Field(default=0, ge=0)
    items: list[NewsItem]


class JobItem(BaseModel):
    news_id: str
    status: Literal["queued", "running", "done", "failed", "cancelled"]
    error: str | None = None


class InsightJob(BaseModel):
    job_id: str
    user_id: str
    status: Literal["queued", "running", "done", "failed", "cancelled"]
    total: int = Field(ge=0)
    done: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    cancelled: int = Field(default=0, ge=0)
    items: list[JobItem] = Field(default_factory=list)


class InsightCard(BaseModel):
    card_id: str
    user_id: str
    news_id: str
    status: Literal["ready", "partial", "degraded"]
    generated_at: datetime
    header: dict
    headline: dict | None = None
    fact: dict
    bank_view: dict
    ai_conclusion: dict | None = None
    risks: dict | None = None
    hypothesis: dict | None = None
    actions: dict | None = None
    bank_offer: dict | None = None
    evidence: dict
    verification: dict


class ProcessingResult(BaseModel):
    result: bool


class ProcessingRun(BaseModel):
    run_id: str
    status: str


class ProcessingJob(BaseModel):
    status: Literal["accepted", "already_running"]
    news: int = Field(ge=0)


class MLClient:
    def __init__(self, client):
        self.client = client

    def _request(self, method, path, model, **kwargs):
        try:
            response = self.client.request(method, path, **kwargs)
            if response.status_code == 404:
                raise ProfileNotFound("Данные не найдены")
            if response.status_code == 409:
                raise MLNotReady("Лента пользователя ещё не подготовлена")
            response.raise_for_status()
            return model.model_validate(response.json())
        except (httpx.HTTPError, ValidationError, ValueError) as exc:
            raise MLUnavailable("Не удалось получить данные. Проверьте доступность ML-сервиса.") from exc

    def _get(self, path, model, **params):
        return self._request("GET", path, model, params=params)

    @staticmethod
    def _owned(result, user_id):
        if result.user_id != user_id:
            raise MLUnavailable("ML-сервис вернул данные другого профиля")
        return result

    def profiles(self, limit=50, offset=0):
        return self._get("/profiles", ProfileList, limit=limit, offset=offset)

    def profile(self, user_id):
        result = self._get("/profiles/" + quote(user_id, safe=""), ProfileDetails)
        if result.user_id != user_id or result.profile.user_id != user_id:
            raise MLUnavailable("ML-сервис вернул профиль с другим идентификатором")
        return result

    def feed(self, user_id, *, limit=20, offset=0):
        return self._owned(self._get("/users/" + quote(user_id, safe="") + "/news", UserFeed,
                                    limit=limit, offset=offset, with_text=False), user_id)

    def processing_ready(self):
        return self._get("/processing.result", ProcessingResult).result

    def enqueue_processing(self):
        return self._request("POST", "/processing.enqueue", ProcessingJob)

    def ensure_profile(self, user_id, *, role, company):
        """Профиль «роль + компания» в ML: строится при первом входе, дальше берётся готовый."""
        body = {"role": role, "company_id": company.company_id, "company_name": company.company_name,
                "industry": company.industry, "company_description": company.company_description}
        # построение профиля (аспекты через LLM) дольше обычного запроса
        return self._request("PUT", "/profiles/" + quote(user_id, safe=""), ProfileDetails, json=body, timeout=120)

    def rebuild_news(self, user_id):
        """Подбор новостей только этому пользователю (запуск processing.run в ML)."""
        return self._request("POST", "/users/" + quote(user_id, safe="") + "/news.rebuild", ProcessingRun)

    def generate_insights(self, user_id, *, news_ids=None, limit=None):
        body = {"refresh": False}
        if news_ids is not None:
            body["news_ids"] = news_ids
        if limit is not None:
            body["limit"] = limit
        return self._owned(self._request("POST", "/users/" + quote(user_id, safe="") + "/insights/generate",
                                        InsightJob, json=body), user_id)

    def latest_job(self, user_id):
        return self._owned(self._get("/users/" + quote(user_id, safe="") + "/insights/jobs/latest", InsightJob), user_id)

    def insight(self, user_id, news_id):
        result = self._owned(self._get("/users/" + quote(user_id, safe="") + "/news/" +
                          quote(news_id, safe="") + "/insight", InsightCard), user_id)
        if result.news_id != news_id:
            raise MLUnavailable("ML-сервис вернул карточку другой новости")
        return result


def get_ml_client():
    with httpx.Client(base_url=os.getenv("ML_API_URL", "http://127.0.0.1:8002").rstrip("/"),
                      timeout=httpx.Timeout(10, connect=3), follow_redirects=False) as client:
        yield MLClient(client)
