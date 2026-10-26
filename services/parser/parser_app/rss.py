"""RSS discovers supported publication URLs; the existing queue owns article fetching."""
import json
import os
from dataclasses import dataclass
from datetime import timedelta, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from urllib.parse import urljoin, urlsplit

import feedparser
import httpx

from .adapters import SOURCES, adapter_for
from .broker import dispatch_pending, queue_backend
from .database import SessionLocal
from .queue import enqueue, utcnow, iso
from .models import NewsRaw, NewsSource, RSSPollState
from .settings import env_int

DEFAULT_FEEDS = {"interfax": "https://www.interfax.ru/rss.asp"}
MAX_FEED_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class RSSConfig:
    enabled: bool
    sources: dict[str, str]
    interval: int
    limit: int
    timeout: int
    backoff_max: int = 3600


def validate_feed_url(code, url):
    adapter = next((source for source in SOURCES if source.code == code), None)
    if adapter is None or not isinstance(url, str):
        raise ValueError("RSS source must have a supported adapter code and HTTPS URL")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in {adapter.host, "www." + adapter.host}
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise ValueError("RSS URL and redirects must stay on the configured source's HTTPS host")
    return url


def rss_config():
    enabled = os.getenv("PARSER_RSS_ENABLED", "false").strip().lower()
    if enabled not in ("true", "false", "1", "0"):
        raise ValueError("PARSER_RSS_ENABLED must be true or false")
    sources = json.loads(os.getenv("PARSER_RSS_SOURCES", json.dumps(DEFAULT_FEEDS)))
    if not isinstance(sources, dict) or len(sources) > len(SOURCES):
        raise ValueError("PARSER_RSS_SOURCES must be a JSON object of source code to feed URL")
    for code, url in sources.items():
        validate_feed_url(code, url)
    limit = env_int("PARSER_RSS_LIMIT", 100, 1)
    timeout = env_int("PARSER_RSS_TIMEOUT", 15, 1)
    if limit > 1000 or timeout > 60:
        raise ValueError("PARSER_RSS_LIMIT <= 1000 and PARSER_RSS_TIMEOUT <= 60 are required")
    return RSSConfig(enabled in ("true", "1"), sources,
        env_int("PARSER_RSS_INTERVAL", 300, 30), limit, timeout, env_int("PARSER_RSS_BACKOFF_MAX", 3600, 30))


class FeedError(ValueError):
    def __init__(self, status, retry_after=None):
        super().__init__(f"RSS HTTP {status}")
        self.status, self.retry_after = status, retry_after


def retry_delay(value):
    if not value:
        return 0
    try:
        return max(0, int(value))
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
            if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
            return max(0, (dt - utcnow()).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0


def fetch_feed(client, code, url, *, headers=None):
    for _ in range(4):
        validate_feed_url(code, url)
        with client.stream("GET", url, headers=headers or {}) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location: raise ValueError("RSS redirect has no location")
                url = validate_feed_url(code, urljoin(url, location))
                continue
            result = {"url": url, "status": response.status_code,
                "etag": response.headers.get("etag"), "last_modified": response.headers.get("last-modified")}
            if response.status_code == 304:
                if not headers: raise ValueError("Unconditional RSS request unexpectedly returned 304")
                return {**result, "data": None}
            if response.status_code != 200:
                raise FeedError(response.status_code, retry_delay(response.headers.get("retry-after")))
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > MAX_FEED_BYTES: raise ValueError("RSS feed exceeds 2 MiB")
            return {**result, "data": bytes(data)}
    raise ValueError("Too many RSS redirects")


def discover_urls(data, feed_url, code, limit):
    # Bytes only: feedparser never performs its own network requests.
    feed = feedparser.parse(data, resolve_relative_uris=False)
    if not feed.get("version") or (feed.get("bozo") and not feed.entries):
        raise ValueError("Invalid RSS/Atom document")
    if len(feed.entries) > 10000: raise ValueError("RSS feed exceeds 10000 entries")
    urls, seen, skipped = [], set(), 0
    for entry in feed.entries[:limit]:
        link = entry.get("link")
        if not isinstance(link, str) or not link.strip():
            skipped += 1
            continue
        try:
            url = urljoin(feed_url, link.strip())
            adapter = adapter_for(url)
            if adapter.code != code:
                raise ValueError("Cross-source article link")
            url = adapter.normalize(url)
        except (TypeError, ValueError):
            skipped += 1
            continue
        if url in seen:
            skipped += 1
            continue
        seen.add(url)
        urls.append(url)
    return urls, skipped


def claim_poll(factory, code, url, config, now, force):
    key = sha256((code + "\n" + url).encode()).hexdigest()
    with factory() as db:
        source = db.scalar(select(NewsSource).where(NewsSource.code == code))
        if source is not None and not source.enabled: return None, "disabled"
        if db.get(RSSPollState, key) is None:
            try:
                with db.begin_nested():
                    db.add(RSSPollState(feed_key=key, source_code=code, feed_url=url))
                    db.flush()
            except IntegrityError: pass
        db.commit()
        token = uuid4().hex
        query = update(RSSPollState).where(RSSPollState.feed_key == key,
            or_(RSSPollState.lease_until.is_(None), RSSPollState.lease_until <= now))
        if not force:
            query = query.where(or_(RSSPollState.next_poll_at.is_(None), RSSPollState.next_poll_at <= now))
        claimed = db.execute(query.values(lease_token=token,
            lease_until=now + timedelta(seconds=config.timeout * 4 + 60), last_attempt_at=now)).rowcount
        db.commit()
        if not claimed: return None, "not_due_or_busy"
        row = db.get(RSSPollState, key, populate_existing=True)
        return {"key": key, "token": token, "etag": row.etag, "last_modified": row.last_modified,
                "errors": row.consecutive_errors}, None


def finish_poll(db, lease, **values):
    return db.execute(update(RSSPollState).execution_options(synchronize_session=False).where(
        RSSPollState.feed_key == lease["key"], RSSPollState.lease_token == lease["token"])
        .values(lease_token=None, lease_until=None, **values)).rowcount


def known_urls(db, code, urls):
    known = set()
    for start in range(0, len(urls), 500):
        known.update(db.scalars(select(NewsRaw.url).join(NewsSource).where(
            NewsSource.code == code, NewsRaw.url.in_(urls[start:start + 500]))))
    return known


def save_feed(factory, lease, response, code, urls, config, now):
    with factory() as db:
        known = known_urls(db, code, urls)
        unknown = [url for url in urls if url not in known]
        selected = unknown[:config.limit]
        remaining = len(unknown) - len(selected)
        not_modified = response["status"] == 304
        etag = response["etag"] or (lease["etag"] if not_modified else None)
        modified = response["last_modified"] or (lease["last_modified"] if not_modified else None)
        # This fenced UPDATE locks the state row until queue inserts commit as well.
        updated = finish_poll(db, lease, last_success_at=now, last_http_status=response["status"],
            next_poll_at=now + timedelta(seconds=config.interval), consecutive_errors=0,
            last_error=None, last_discovered=len(urls), last_queued=0,
            etag=None if remaining else etag, last_modified=None if remaining else modified)
        if not updated:
            db.rollback()
            return {"status": "lease_lost", "job_ids": []}
        items = enqueue(db, selected, commit=False, new_only=True) if selected else []
        ids = [item.id for item in items]
        db.execute(update(RSSPollState).where(RSSPollState.feed_key == lease["key"]).values(last_queued=len(ids)))
        db.commit()
        result = {"status": "not_modified" if not_modified else "queued", "job_ids": ids,
                  "new": len(ids), "known": len(known), "remaining": remaining}
        if queue_backend() == "celery" and ids:
            result["dispatch"] = dispatch_pending(db, item_ids=ids)
        return result


def fail_poll(factory, lease, error, config, now, response):
    errors = lease["errors"] + 1
    delay = min(max(config.interval, config.backoff_max), config.interval * 2 ** min(errors - 1, 10))
    delay = max(delay, getattr(error, "retry_after", 0) or 0)
    status = getattr(error, "status", response["status"] if response else None)
    if status in (403, 429): delay = max(delay, 900)
    with factory() as db:
        finish_poll(db, lease, consecutive_errors=errors, last_error=str(error)[:2000],
            last_http_status=status, next_poll_at=now + timedelta(seconds=delay))
        db.commit()
    return delay


def poll_rss(*, config=None, factory=None, client=None, dry_run=False, force=False, now=None):
    config = config or rss_config()
    factory = factory or SessionLocal
    clock = (lambda: now) if now is not None else utcnow
    if client is None:
        with httpx.Client(timeout=config.timeout, follow_redirects=False,
                headers={"User-Agent": "RecNewsHackathonCollector/0.1"}) as owned:
            return poll_rss(config=config, factory=factory, client=owned, dry_run=dry_run, force=force, now=now)
    reports = []
    for code, feed_url in config.sources.items():
        report = {"source": code, "feed_url": feed_url, "discovered": 0, "skipped": 0, "new": 0, "job_ids": []}
        lease, response = None, None
        try:
            if not dry_run:
                lease, reason = claim_poll(factory, code, feed_url, config, clock(), force)
                if lease is None:
                    report.update(status="disabled" if reason == "disabled" else "skipped", reason=reason)
                    reports.append(report)
                    continue
            headers = {}
            if lease and not force:
                if lease["etag"]: headers["If-None-Match"] = lease["etag"]
                if lease["last_modified"]: headers["If-Modified-Since"] = lease["last_modified"]
            response = fetch_feed(client, code, feed_url, headers=headers)
            if response["status"] == 304:
                urls, skipped = [], 0
            else:
                urls, skipped = discover_urls(response["data"], response["url"], code, 10000)
            report.update(discovered=len(urls), skipped=skipped)
            if dry_run:
                report.update(status="discovered", urls=urls[:config.limit])
            else:
                report.update(save_feed(factory, lease, response, code, urls, config, clock()))
        except (httpx.RequestError, ValueError) as error:
            report.update(status="failed", error=str(error))
            if lease:
                report["retry_in"] = fail_poll(factory, lease, error, config, clock(), response)
        reports.append(report)
    return {"feeds": reports}


def poll_status(db):
    return [{"source": row.source_code, "feed_url": row.feed_url,
        "last_attempt_at": iso(row.last_attempt_at), "last_success_at": iso(row.last_success_at),
        "next_poll_at": iso(row.next_poll_at), "lease_until": iso(row.lease_until),
        "consecutive_errors": row.consecutive_errors, "last_error": row.last_error,
        "last_http_status": row.last_http_status, "last_discovered": row.last_discovered,
        "last_queued": row.last_queued}
        for row in db.scalars(select(RSSPollState).order_by(RSSPollState.source_code, RSSPollState.feed_url))]
