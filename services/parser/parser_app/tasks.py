"""Celery tasks share the existing DB claim, retry and source-throttling logic."""
import httpx

from .broker import dispatch_pending, release_dispatch
from .celery_app import celery_app
from .database import SessionLocal
from .queue import claim_next, process_claim


@celery_app.task(name="parser.collect")
def collect_publication(job_id: int, dispatch_token: str):
    if type(job_id) is not int or job_id <= 0 or not isinstance(dispatch_token, str):
        raise ValueError("Invalid publication task")
    with SessionLocal() as db:
        claim = claim_next(db, item_id=job_id)
        release_dispatch(db, job_id, dispatch_token)
    if claim is None:
        return "not_eligible"
    # Unexpected errors leave a DB lease; the reconciler recovers it after expiry.
    # Expected upstream failures use DB retries, avoiding a second retry counter in Celery.
    with httpx.Client(timeout=30, follow_redirects=False,
                      headers={"User-Agent": "RecNewsHackathonCollector/0.1"}) as client:
        with SessionLocal() as db:
            return process_claim(db, client, claim)


@celery_app.task(name="parser.dispatch_pending")
def reconcile_pending():
    with SessionLocal() as db:
        return dispatch_pending(db)


@celery_app.task(name="parser.poll_rss")
def discover_rss_publications():
    from .rss import poll_rss, rss_config
    config = rss_config()
    if not config.enabled:
        return {"status": "disabled"}
    return poll_rss(config=config)
