import threading
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from typing import Annotated, Literal
from sqlalchemy import select
from .models import NewsRaw
from .queue import enqueue, job_result
from sqlalchemy.orm import Session

from .collector import CollectionError, collect, normalize_url, result
from .database import Base, engine, get_db
from .publications import parse_publication, public_result

# Single process prototype: reject overlapping runs rather than hammering the source.
_lock = threading.Lock()
_last_finished = 0.0


@asynccontextmanager
async def lifespan(app):
    Base.metadata.create_all(engine)
    yield
    engine.dispose()


app = FastAPI(title="rec_news collection service", lifespan=lifespan)


class PublicationRequest(BaseModel):
    url: str = Field(max_length=2048)


class ParsedPublication(BaseModel):
    source: str
    url: str
    title: str
    text: str
    published_date: date | None = None
    published_at: datetime | None = None
    parser_version: str


def validated_url(body):
    try:
        return normalize_url(body.url)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@contextmanager
def upstream_client():
    global _last_finished
    if not _lock.acquire(blocking=False):
        raise HTTPException(409, "Сбор уже выполняется")
    try:
        time.sleep(max(0, 5 - (time.monotonic() - _last_finished)))
        with httpx.Client(timeout=30, follow_redirects=False,
                          headers={"User-Agent": "RecNewsHackathonCollector/0.1"}) as client:
            yield client
    except CollectionError as exc:
        raise HTTPException(502, str(exc)) from exc
    finally:
        _last_finished = time.monotonic()
        _lock.release()


@app.post("/parse", response_model=ParsedPublication, summary="Извлечь текст публикации по URL без сохранения в БД")
def parse_page(body: PublicationRequest):
    url = validated_url(body)
    with upstream_client() as client:
        return public_result(parse_publication(client, url))


@app.post("/collect", summary="Извлечь и сохранить публикацию")
def collect_page(body: PublicationRequest, db: Session = Depends(get_db)):
    url = validated_url(body)
    with upstream_client() as client:
        status, item = collect(db, client, url)
        return result(status, item)


class QueueRequest(BaseModel):
    urls: list[Annotated[str, Field(min_length=1, max_length=2048)]] = Field(min_length=1, max_length=100)


@app.post("/queue", status_code=202, summary="Поставить публикации в очередь без загрузки")
def enqueue_pages(body: QueueRequest, db: Session = Depends(get_db)):
    try:
        return {"items": [job_result(item) for item in enqueue(db, body.urls)]}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/queue", summary="Посмотреть очередь")
def list_jobs(status: Literal["pending", "processing", "ready", "retry", "failed"] | None = None,
              limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0),
              db: Session = Depends(get_db)):
    query = select(NewsRaw).order_by(NewsRaw.id)
    if status:
        query = query.where(NewsRaw.status == status)
    return {"items": [job_result(item) for item in db.scalars(query.limit(limit).offset(offset))]}


@app.get("/queue/{job_id}", summary="Статус и результат обработки публикации")
def get_job(job_id: int, db: Session = Depends(get_db)):
    item = db.get(NewsRaw, job_id)
    if item is None:
        raise HTTPException(404, "Задача не найдена")
    return job_result(item)
