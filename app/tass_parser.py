"""Small, synchronous HTML collector. Run one instance at a time."""

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
import trafilatura
from selectolax.parser import HTMLParser
from sqlalchemy import select

from database import SessionLocal
from init_news_db import init_news_db
from models import NewsRaw, NewsSource

LISTING_URL = "https://tass.ru/ekonomika"
PARSER_VERSION = "tass-html-v1"
USER_AGENT = "AlfaHackNewsPrototype/0.1"


def article_url(value: str, base: str = LISTING_URL) -> str | None:
    parsed = urlsplit(urljoin(base, value))
    if parsed.scheme not in ("http", "https") or parsed.netloc.lower() not in ("tass.ru", "www.tass.ru"):
        return None
    path = parsed.path.rstrip("/")
    if not re.fullmatch(r"/[a-z0-9-]+/[0-9]+", path):
        return None
    return urlunsplit(("https", "tass.ru", path, "", ""))


def discover_articles(html: str) -> list[str]:
    urls = dict.fromkeys(
        url for node in HTMLParser(html).css("a[href]")
        if (url := article_url(node.attributes["href"]))
    )
    return list(urls)


def _json_objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _json_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _json_objects(child)


def _publication_date(value: str | None) -> datetime | None:
    if not value or "T" not in value:
        return None  # Do not invent a publication time from a calendar date.
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except ValueError:
        return None


@dataclass
class Article:
    title: str
    text: str
    published_at: datetime | None


def extract_article(html: str, url: str) -> Article:
    tree = HTMLParser(html)
    metadata = {}
    for script in tree.css('script[type="application/ld+json"]'):
        try:
            objects = _json_objects(json.loads(script.text()))
            for obj in objects:
                types = obj.get("@type", [])
                if isinstance(types, str):
                    types = [types]
                if "NewsArticle" in types or "Article" in types:
                    metadata = obj
                    break
        except (ValueError, TypeError):
            continue
        if metadata:
            break

    extracted = trafilatura.extract(
        html, url=url, output_format="json", with_metadata=True,
        include_comments=False, include_tables=False, favor_precision=True,
    )
    data = json.loads(extracted) if extracted else {}
    text = (data.get("text") or "").strip()
    title_node = tree.css_first('meta[property="og:title"]')
    h1 = tree.css_first("h1")
    title = (
        metadata.get("headline")
        or (title_node.attributes.get("content") if title_node else None)
        or (h1.text(strip=True) if h1 else None)
        or data.get("title") or ""
    ).strip()
    date_node = tree.css_first('meta[property="article:published_time"]')
    date = metadata.get("datePublished") or (
        date_node.attributes.get("content") if date_node else None
    )
    if not title or len(text) < 80 or title.lower() in ("forbidden", "access denied"):
        raise ValueError("Article title or full text was not found; HTML layout needs inspection")
    return Article(title, text, _publication_date(date))


class FetchError(RuntimeError):
    def __init__(self, message, retryable=False, blocked=False):
        super().__init__(message)
        self.retryable = retryable
        self.blocked = blocked


class HtmlFetcher:
    def __init__(self, client, delay=5.0):
        self.client = client
        self.delay = delay
        self.last_request = None

    def fetch(self, url):
        for _ in range(6):
            if self.last_request is not None:
                time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                response = self.client.get(url)
            except httpx.RequestError as exc:
                raise FetchError(f"Network error: {type(exc).__name__}", retryable=True) from exc
            if response.status_code in (301, 302, 303, 307, 308):
                target = urljoin(url, response.headers.get("location", ""))
                parsed = urlsplit(target)
                if parsed.scheme != "https" or parsed.netloc not in ("tass.ru", "www.tass.ru"):
                    raise FetchError("Unexpected redirect outside TASS HTTPS pages")
                url = target
                continue
            if response.status_code >= 400:
                raise FetchError(
                    f"TASS returned HTTP {response.status_code} for {url}",
                    retryable=response.status_code == 429 or response.status_code >= 500,
                    blocked=response.status_code in (403, 429),
                )
            if response.status_code != 200 or "html" not in response.headers.get("content-type", "").lower():
                raise FetchError("Expected an HTTP 200 HTML response")
            return response.text
        raise FetchError("Too many redirects")


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def collect(db, fetcher, limit=5, html_path=None, url=None):
    source = db.scalar(select(NewsSource).where(NewsSource.code == "tass"))
    if source is None:
        source = NewsSource(
            code="tass", name="ТАСС", base_url="https://tass.ru",
            listing_url=LISTING_URL, parser_key="tass_html",
        )
        db.add(source)
        db.commit()
    elif not source.enabled:
        raise ValueError("TASS source is disabled")

    urls = [url] if html_path else discover_articles(fetcher.fetch(source.listing_url))
    if not urls:
        raise ValueError("No article links found; listing HTML layout needs inspection")
    result = {"discovered": len(urls), "ready": 0, "skipped": 0, "failed": 0, "retry": 0}
    processed = 0
    for link in urls:
        raw = db.scalar(select(NewsRaw).where(NewsRaw.source_id == source.id, NewsRaw.url == link))
        now = datetime.now(timezone.utc)
        if raw is not None and (
            raw.status in ("ready", "failed")
            or (raw.next_retry_at and _utc(raw.next_retry_at) > now)
            or (raw.status == "processing" and raw.processing_started_at
                and _utc(raw.processing_started_at) > now - timedelta(minutes=15))
        ):
            result["skipped"] += 1
            continue
        if processed >= limit:
            break
        processed += 1
        if raw is None:
            raw = NewsRaw(source_id=source.id, url=link)
            db.add(raw)
            db.flush()
        raw.status = "processing"
        raw.processing_started_at = now
        raw.attempt_count += 1
        db.commit()
        blocked = False
        try:
            html = Path(html_path).read_text(encoding="utf-8") if html_path else fetcher.fetch(link)
            raw.raw_html = html
            raw.fetched_at = datetime.now(timezone.utc)
            article = extract_article(html, link)
            raw.title, raw.text, raw.published_at = article.title, article.text, article.published_at
            raw.content_hash = sha256(article.text.encode("utf-8")).hexdigest()
            raw.parser_version = PARSER_VERSION
            raw.status, raw.last_error, raw.next_retry_at = "ready", None, None
            result["ready"] += 1
        except (FetchError, ValueError, OSError) as exc:
            retry = isinstance(exc, FetchError) and exc.retryable and raw.attempt_count < 3
            raw.status = "retry" if retry else "failed"
            raw.last_error = str(exc)[:2000]
            raw.next_retry_at = now + timedelta(minutes=15 * raw.attempt_count) if retry else None
            result[raw.status] += 1
            blocked = isinstance(exc, FetchError) and exc.blocked
            print(f"{link}: {exc}", file=sys.stderr)
        raw.processing_started_at = None
        db.commit()
        if blocked:
            break  # Stop on anti-bot refusal or rate limiting; do not hammer other URLs.
    if result["ready"] and not result["failed"] and not result["retry"]:
        source.last_success_at = datetime.now(timezone.utc)
        db.commit()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--html", type=Path, help="Import a locally saved article HTML without network")
    parser.add_argument("--url", help="Original TASS article URL; required with --html")
    args = parser.parse_args()
    if not 1 <= args.limit <= 20:
        parser.error("--limit must be between 1 and 20")
    if bool(args.html) != bool(args.url) or (args.url and not article_url(args.url)):
        parser.error("Use --html together with a valid --url https://tass.ru/section/id")
    init_news_db()
    try:
        with SessionLocal() as db, httpx.Client(
            timeout=30, follow_redirects=False, headers={"User-Agent": USER_AGENT},
        ) as client:
            result = collect(db, HtmlFetcher(client), args.limit, args.html, article_url(args.url) if args.url else None)
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result["failed"] or result["retry"] else 0
    except (FetchError, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
