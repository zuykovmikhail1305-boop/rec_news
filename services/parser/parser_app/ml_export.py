"""Atomic handoff into the ML-owned PostgreSQL input table; no ML inference here."""
import json
from datetime import timezone
from urllib.parse import urlsplit

from sqlalchemy import Column, MetaData, Table, Text, inspect
from sqlalchemy.dialects.postgresql import insert

from .settings import ml_export_enabled, ml_news_schema

COLUMNS = ("document_id", "text", "title", "url", "source", "publication_time",
           "created_at", "status", "status_updated_at", "extra")


def input_table(schema=None):
    # This describes only columns written by the parser. ML owns table creation/migrations.
    return Table("news", MetaData(), *(Column(name, Text, primary_key=name == "document_id")
        for name in COLUMNS), schema=schema or ml_news_schema())


def export_schema_problems(bind):
    if not ml_export_enabled(bind.dialect.name):
        return []
    inspector = inspect(bind)
    schema = ml_news_schema()
    if not inspector.has_table("news", schema=schema):
        return [f"Missing ML input table: {schema}.news; initialize the ML schema first"]
    columns = {column["name"] for column in inspector.get_columns("news", schema=schema)}
    missing = set(COLUMNS) - columns
    return [f"Missing ML input columns: {', '.join(sorted(missing))}"] if missing else []


def as_utc(value):
    if value is None:
        return None
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)).isoformat()


def export_publication(db, publication):
    """Insert only, in the caller's transaction; never reset ML progress on redelivery."""
    if not ml_export_enabled(db.get_bind().dialect.name) or publication.status != "ready":
        return None
    db.flush()
    source_code = publication.source.code
    document_id = str(publication.id)  # числовой ID публикации (news_raw.id) — один ID во всех сервисах
    created_at = as_utc(publication.fetched_at)
    payload = dict(document_id=document_id, text=publication.text, title=publication.title,
        url=publication.url, source=urlsplit(publication.url).hostname,
        publication_time=as_utc(publication.published_at), created_at=created_at,
        status="new", status_updated_at=created_at,
        extra=json.dumps({"parser": {"raw_id": publication.id, "source_code": source_code,
            "content_hash": publication.content_hash, "parser_version": publication.parser_version,
            "published_date": publication.published_date.isoformat() if publication.published_date else None}},
            ensure_ascii=False))
    table = input_table()
    db.execute(insert(table).values(**payload).on_conflict_do_nothing(index_elements=["document_id"]))
    return document_id
