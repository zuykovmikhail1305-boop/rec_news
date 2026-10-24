from datetime import datetime, timezone
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .adapters import adapter_for, extract_cbr as extract, normalize_url
from .errors import CollectionError
from .publications import fetch, parse_publication
from .models import NewsRaw as Publication, NewsSource
from .ml_export import export_publication


def collect(db, client, url):
    url = normalize_url(url)
    existing = db.scalar(select(Publication).where(Publication.url == url))
    if existing:
        if existing.status == "ready":
            export_publication(db, existing)
            db.commit()
        return ("skipped" if existing.status == "ready" else existing.status), existing
    article = parse_publication(client, url)
    html, url = article.pop("raw_html"), article.pop("url")
    article.pop("source")
    version = article.pop("parser_version")
    existing = db.scalar(select(Publication).where(Publication.url == url))
    if existing:
        if existing.status == "ready":
            export_publication(db, existing)
            db.commit()
        return ("skipped" if existing.status == "ready" else existing.status), existing
    adapter = adapter_for(url)
    source = db.scalar(select(NewsSource).where(NewsSource.code == adapter.code))
    if source is None:
        try:
            with db.begin_nested():
                source = NewsSource(code=adapter.code, name=adapter.name, base_url="https://" + adapter.host,
                                    listing_url=adapter.listing_url, parser_key=adapter.code + "_html")
                db.add(source)
                db.flush()
        except IntegrityError:
            source = db.scalar(select(NewsSource).where(NewsSource.code == adapter.code))
    if not source.enabled:
        raise CollectionError("Источник отключён")
    publication = Publication(source_id=source.id, status="ready", attempt_count=1,
        fetched_at=datetime.now(timezone.utc), url=url, raw_html=html,
        content_hash=sha256(article["text"].encode("utf-8")).hexdigest(),
        parser_version=version, **article)
    source.last_success_at = publication.fetched_at
    db.add(publication)
    try:
        db.flush()
        export_publication(db, publication)
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(Publication).where(Publication.url == url))
        if existing:
            export_publication(db, existing)
            db.commit()
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
