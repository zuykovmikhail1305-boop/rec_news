from datetime import datetime, timezone
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .adapters import adapter_for, extract_cbr as extract, normalize_url
from .errors import CollectionError
from .publications import fetch, parse_publication
from .models import NewsRaw as Publication, NewsSource


def collect(db, client, url):
    url = normalize_url(url)
    existing = db.scalar(select(Publication).where(Publication.url == url))
    if existing:
        return ("skipped" if existing.status == "ready" else existing.status), existing
    article = parse_publication(client, url)
    html, url = article.pop("raw_html"), article.pop("url")
    article.pop("source")
    version = article.pop("parser_version")
    existing = db.scalar(select(Publication).where(Publication.url == url))
    if existing:
        return ("skipped" if existing.status == "ready" else existing.status), existing
    adapter = adapter_for(url)
    source = db.scalar(select(NewsSource).where(NewsSource.code == adapter.code))
    if source is None:
        source = NewsSource(code=adapter.code, name=adapter.name, base_url="https://" + adapter.host,
                            listing_url=adapter.listing_url, parser_key=adapter.code + "_html")
        db.add(source)
        db.flush()
    elif not source.enabled:
        raise CollectionError("Источник отключён")
    publication = Publication(source_id=source.id, status="ready", attempt_count=1,
        fetched_at=datetime.now(timezone.utc), url=url, raw_html=html,
        content_hash=sha256(article["text"].encode("utf-8")).hexdigest(),
        parser_version=version, **article)
    db.add(publication)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(Publication).where(Publication.url == url))
        if existing:
            return "skipped", existing
        raise
    db.refresh(publication)
    return "ready", publication


def result(status, item):
    return {"status": status, "id": item.id, "source": item.source.code, "url": item.url,
            "title": item.title, "text": item.text,
            "published_date": item.published_date.isoformat() if item.published_date else None,
            "fetched_at": item.fetched_at.replace(tzinfo=timezone.utc).isoformat() if item.fetched_at else None, "content_hash": item.content_hash,
            "parser_version": item.parser_version}
