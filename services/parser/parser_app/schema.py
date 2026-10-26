"""Shared news schema initialization; no data resets or ML table management."""
from sqlalchemy import Date, inspect, text
from sqlalchemy.schema import CreateSchema

from .database import Base
from .settings import auto_create_tables


def target_schema(bind):
    return bind.get_execution_options().get("schema_translate_map", {}).get(None)


def create_tables(bind):
    from . import models  # register parser-owned tables only
    schema = target_schema(bind)
    if bind.dialect.name == "postgresql" and schema != "public":
        with bind.begin() as connection:
            connection.execute(CreateSchema(schema, if_not_exists=True))
    Base.metadata.create_all(bind)
    # The original shared news schema predates date-only publication metadata.
    # Nullable additive change preserves ready rows and consumers of the old columns.
    inspector = inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("news_raw", schema=schema)}
    if "published_date" not in columns:
        quote = bind.dialect.identifier_preparer.quote
        table = (quote(schema) + "." if schema else "") + quote("news_raw")
        with bind.begin() as connection:
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN published_date {Date().compile(bind.dialect)}"))


def schema_problems(bind):
    from . import models
    inspector = inspect(bind)
    schema = target_schema(bind)
    problems = []
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name, schema=schema):
            problems.append(f"Missing table: {table.name}")
            continue
        columns = {column["name"] for column in inspector.get_columns(table.name, schema=schema)}
        missing = set(table.columns.keys()) - columns
        if missing:
            problems.append(f"Missing columns in {table.name}: {', '.join(sorted(missing))}")
    from .ml_export import export_schema_problems
    return problems + export_schema_problems(bind)


def initialize_database(bind):
    if auto_create_tables(bind.dialect.name):
        create_tables(bind)
    problems = schema_problems(bind)
    if problems:
        raise RuntimeError("; ".join(problems) + ". Initialize ML input tables first, then run parser scripts/init_db.py.")
