"""RSS discovers supported publication URLs; the existing queue owns article fetching."""
import json
import os
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import feedparser
import httpx

from .adapters import SOURCES, adapter_for
from .broker import dispatch_pending, queue_backend
from .database import SessionLocal
from .queue import enqueue
from .settings import env_int

DEFAULT_FEEDS = {"interfax": "https://www.interfax.ru/rss.asp",
                 "ria": "https://ria.ru/export/rss2/index.xml"}
MAX_FEED_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class RSSConfig:
    enabled: bool
    sources: dict[str, str]
    interval: int
    limit: int
    timeout: int


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
        env_int("PARSER_RSS_INTERVAL", 300, 30), limit, timeout)


def fetch_feed(client, code, url):
    for _ in range(4):
        validate_feed_url(code, url)
        with client.stream("GET", url) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location:
                    raise ValueError("RSS redirect has no location")
                url = validate_feed_url(code, urljoin(url, location))
                continue
            if response.status_code != 200:
                raise ValueError(f"RSS HTTP {response.status_code}")
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > MAX_FEED_BYTES:
                    raise ValueError("RSS feed exceeds 2 MiB")
            return bytes(data), url
    raise ValueError("Too many RSS redirects")


def discover_urls(data, feed_url, code, limit):
    # Bytes only: feedparser never performs its own network requests.
    feed = feedparser.parse(data, resolve_relative_uris=False)
    if not feed.get("version") or (feed.get("bozo") and not feed.entries):
        raise ValueError("Invalid RSS/Atom document")
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


def poll_rss(*, config=None, factory=SessionLocal, client=None, dry_run=False):
    config = config or rss_config()
    if client is None:
        with httpx.Client(timeout=config.timeout, follow_redirects=False,
                headers={"User-Agent": "RecNewsHackathonCollector/0.1"}) as owned:
            return poll_rss(config=config, factory=factory, client=owned, dry_run=dry_run)
    reports = []
    for code, feed_url in config.sources.items():
        report = {"source": code, "feed_url": feed_url, "discovered": 0, "skipped": 0}
        try:
            raw, final_url = fetch_feed(client, code, feed_url)
            urls, skipped = discover_urls(raw, final_url, code, config.limit)
            report.update(discovered=len(urls), skipped=skipped)
            if dry_run:
                report.update(status="discovered", urls=urls)
            else:
                with factory() as db:
                    items = enqueue(db, urls) if urls else []
                    ids = [item.id for item in items]
                    report.update(status="queued", job_ids=ids)
                    if queue_backend() == "celery":
                        report["dispatch"] = dispatch_pending(db, item_ids=ids)
        except (httpx.RequestError, ValueError) as error:
            report.update(status="failed", error=str(error))
        reports.append(report)
    return {"feeds": reports}
