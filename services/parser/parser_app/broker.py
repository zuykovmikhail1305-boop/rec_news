"""Publish DB-backed jobs with recoverable dispatch reservations."""
import logging
import os
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from .models import BrokerDispatch, NewsRaw, NewsSource, QueueSourceState
from .queue import eligible, utcnow

log = logging.getLogger(__name__)
DISPATCH_LEASE = timedelta(seconds=60)


def queue_backend():
    value = os.getenv("PARSER_QUEUE_BACKEND", "local").strip().lower()
    if value not in ("local", "celery"):
        raise ValueError("PARSER_QUEUE_BACKEND must be local or celery")
    return value


def release_dispatch(db, job_id, token):
    db.execute(update(BrokerDispatch).where(BrokerDispatch.raw_id == job_id,
        BrokerDispatch.token == token).values(reserved_until=None))
    db.commit()


def dispatch_pending(db, *, item_ids=None, limit=100, publisher=None, now=None):
    """DB commit precedes publication; expired reservations repair either-side crashes.

    Delivery is at least once. Existing claim/fencing rules protect publication results.
    Celery Beat must keep calling this function, including after broker recovery.
    """
    if item_ids is not None and not item_ids:
        return {"sent": 0, "deferred": 0}
    now = now or utcnow()
    query = select(NewsRaw.id).join(NewsSource).outerjoin(
        QueueSourceState, QueueSourceState.source_id == NewsSource.id).outerjoin(
        BrokerDispatch, BrokerDispatch.raw_id == NewsRaw.id).where(
        NewsSource.enabled.is_(True), eligible(now),
        or_(QueueSourceState.next_request_at.is_(None), QueueSourceState.next_request_at <= now),
        or_(BrokerDispatch.reserved_until.is_(None), BrokerDispatch.reserved_until <= now))
    if item_ids is not None:
        query = query.where(NewsRaw.id.in_(item_ids))
    ids = list(db.scalars(query.order_by(NewsRaw.discovered_at, NewsRaw.id).limit(limit)))
    db.rollback()
    if publisher is None:
        from .celery_app import celery_app
        publisher = lambda job_id, token: celery_app.send_task(
            "parser.collect", args=[job_id, token], queue="parser.publications")
    sent = deferred = 0
    for job_id in ids:
        if db.get(BrokerDispatch, job_id) is None:
            try:
                with db.begin_nested():
                    db.add(BrokerDispatch(raw_id=job_id))
                    db.flush()
            except IntegrityError:
                pass
        token = uuid4().hex
        reserved = db.execute(update(BrokerDispatch).execution_options(synchronize_session=False).where(
            BrokerDispatch.raw_id == job_id,
            or_(BrokerDispatch.reserved_until.is_(None), BrokerDispatch.reserved_until <= now))
            .values(token=token, reserved_until=now + DISPATCH_LEASE)).rowcount
        db.commit()
        if not reserved:
            continue
        try:
            publisher(job_id, token)
            sent += 1
        except Exception:
            # Avoid logging broker URLs that might contain credentials.
            release_dispatch(db, job_id, token)
            log.warning("Broker publication failed; job %s remains in the database", job_id)
            # Do not block the HTTP request for each URL while the broker is down.
            deferred = len(ids) - sent
            break
    return {"sent": sent, "deferred": deferred}
