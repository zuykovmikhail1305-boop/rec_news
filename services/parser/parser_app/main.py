import threading
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime

import httpx
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field
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
