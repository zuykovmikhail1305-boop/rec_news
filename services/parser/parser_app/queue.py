"""Durable URL queue with compare-and-swap claims and fenced completion."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError

from .adapters import adapter_for
from .errors import CollectionError
from .models import NewsRaw, NewsSource, QueueSourceState
from .publications import parse_publication
from .ml_export import export_publication

MAX_ATTEMPTS = 3
LEASE = timedelta(minutes=15)
REQUEST_INTERVAL = timedelta(seconds=5)


def utcnow():
    return datetime.now(timezone.utc)


def iso(value):
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).isoformat()


def _ensure_state(db, source_id):
    if db.get(QueueSourceState, source_id) is None:
        try:
            with db.begin_nested():
                db.add(QueueSourceState(source_id=source_id))
                db.flush()
        except IntegrityError:
            pass  # Another producer/worker initialized this source.


def enqueue(db, urls, *, commit=True, new_only=False):
    # Validate the entire batch before modifying the database.
    prepared = [(adapter_for(url), url) for url in urls]
    prepared = [(adapter, adapter.normalize(url)) for adapter, url in prepared]
    items = []
    # Python sqlite3 legacy mode does not start a real transaction for SAVEPOINT.
    # Explicit BEGIN keeps the entire batch atomic even when a later source is disabled.
    if db.get_bind().dialect.name == "sqlite":
        connection = db.connection()
        if not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql("BEGIN")
    try:
        for adapter, url in prepared:
            source = db.scalar(select(NewsSource).where(NewsSource.code == adapter.code))
            if source is None:
                try:
                    with db.begin_nested():
                        source = NewsSource(code=adapter.code, name=adapter.name,
                            base_url="https://" + adapter.host, listing_url=adapter.listing_url,
                            parser_key=adapter.code + "_html")
                        db.add(source)
                        db.flush()
                except IntegrityError:
                    source = db.scalar(select(NewsSource).where(NewsSource.code == adapter.code))
            if not source.enabled:
                raise ValueError(f"Источник {source.name} отключён")
            _ensure_state(db, source.id)
            created = False
            item = db.scalar(select(NewsRaw).where(NewsRaw.source_id == source.id, NewsRaw.url == url))
            if item is None:
                try:
                    with db.begin_nested():
                        item = NewsRaw(source_id=source.id, url=url, status="pending", attempt_count=0)
                        db.add(item)
                        db.flush()
                        created = True
                except IntegrityError:
                    item = db.scalar(select(NewsRaw).where(NewsRaw.source_id == source.id, NewsRaw.url == url))
            if item.status == "ready" and not new_only:
                export_publication(db, item)
            if created or not new_only:
                items.append(item)
        if commit:
            db.commit()
        return items
    except Exception:
        db.rollback()
        raise


def eligible(now):
    return or_(NewsRaw.status == "pending",
        and_(NewsRaw.status == "retry", or_(NewsRaw.next_retry_at.is_(None), NewsRaw.next_retry_at <= now)),
        and_(NewsRaw.status == "processing", or_(NewsRaw.processing_started_at.is_(None), NewsRaw.processing_started_at <= now - LEASE)))


@dataclass(frozen=True)
class Claim:
    id: int
    source_id: int
    url: str
    attempt: int
    started_at: datetime


def claim_next(db, *, now=None, item_id=None):
    now = now or utcnow()
    # Also initialize state for publications created by older versions.
    for source_id in db.scalars(select(NewsSource.id)):
        _ensure_state(db, source_id)
    db.execute(update(NewsRaw).execution_options(synchronize_session=False).where(eligible(now), NewsRaw.attempt_count >= MAX_ATTEMPTS)
        .values(status="failed", processing_started_at=None, next_retry_at=None,
                last_error="Лимит попыток исчерпан; предыдущий воркер не завершил задачу"))
    db.commit()
    candidates = select(NewsRaw.id, NewsRaw.source_id).join(NewsSource).join(
        QueueSourceState, QueueSourceState.source_id == NewsSource.id).where(
        NewsSource.enabled.is_(True), eligible(now), NewsRaw.attempt_count < MAX_ATTEMPTS,
        or_(QueueSourceState.next_request_at.is_(None), QueueSourceState.next_request_at <= now))
    if item_id is not None:
        candidates = candidates.where(NewsRaw.id == item_id)
    candidates = list(db.execute(candidates.order_by(NewsRaw.discovered_at, NewsRaw.id).limit(100)))
    db.rollback()  # Do not hold a read transaction while competing for a claim.
    for candidate_id, source_id in candidates:
        reserved = db.execute(update(QueueSourceState).execution_options(synchronize_session=False).where(QueueSourceState.source_id == source_id,
            or_(QueueSourceState.next_request_at.is_(None), QueueSourceState.next_request_at <= now))
            .values(next_request_at=now + REQUEST_INTERVAL)).rowcount
        if not reserved:
            db.rollback()
            continue
        updated = db.execute(update(NewsRaw).execution_options(synchronize_session=False).where(NewsRaw.id == candidate_id, eligible(now),
            NewsRaw.attempt_count < MAX_ATTEMPTS,
            NewsRaw.source_id.in_(select(NewsSource.id).where(NewsSource.enabled.is_(True))))
            .values(status="processing", attempt_count=NewsRaw.attempt_count + 1,
                    processing_started_at=now, next_retry_at=None)).rowcount
        if not updated:
            db.rollback()
            continue
        row = db.get(NewsRaw, candidate_id, populate_existing=True)
        claim = Claim(row.id, row.source_id, row.url, row.attempt_count, now)
        db.commit()
        return claim
    return None


def process_claim(db, client, claim, *, now=None):
    # No database transaction is held while downloading HTML.
    try:
        article = parse_publication(client, claim.url)
        finished = now or utcnow()
        values = {key: article[key] for key in
            ("title", "text", "raw_html", "published_at", "published_date", "parser_version")}
        # Keep the queued identity URL. Redirect destinations are still validated by the adapter.
        values.update(status="ready", fetched_at=finished,
            content_hash=sha256(article["text"].encode("utf-8")).hexdigest(),
            processing_started_at=None, next_retry_at=None, last_error=None)
        cooldown = None
    except CollectionError as exc:
        finished = now or utcnow()
        retry = exc.retryable and claim.attempt < MAX_ATTEMPTS
        delay = max(60 * 2 ** (claim.attempt - 1), exc.retry_after or 0)
        values = dict(status="retry" if retry else "failed", last_error=str(exc)[:2000],
            next_retry_at=finished + timedelta(seconds=delay) if retry else None,
            processing_started_at=None)
        cooldown = finished + timedelta(seconds=max(900, exc.retry_after or 0)) if exc.status_code in (403, 429) else None
    completed = db.execute(update(NewsRaw).execution_options(synchronize_session=False).where(NewsRaw.id == claim.id,
        NewsRaw.status == "processing", NewsRaw.attempt_count == claim.attempt,
        NewsRaw.processing_started_at == claim.started_at).values(**values)).rowcount
    if completed and values["status"] == "ready":
        publication = db.get(NewsRaw, claim.id, populate_existing=True)
        export_publication(db, publication)
        db.execute(update(NewsSource).where(NewsSource.id == claim.source_id)
            .values(last_success_at=finished))
    if completed and cooldown:
        db.execute(update(QueueSourceState).execution_options(synchronize_session=False).where(QueueSourceState.source_id == claim.source_id,
            or_(QueueSourceState.next_request_at.is_(None), QueueSourceState.next_request_at < cooldown))
            .values(next_request_at=cooldown))
    db.commit()
    db.expire_all()
    return values["status"] if completed else "lease_lost"


def job_result(item):
    return {"id": item.id, "source": item.source.code, "url": item.url, "status": item.status,
            "attempt_count": item.attempt_count, "next_retry_at": iso(item.next_retry_at),
            "processing_started_at": iso(item.processing_started_at), "last_error": item.last_error,
            "title": item.title, "text": item.text, "fetched_at": iso(item.fetched_at)}
