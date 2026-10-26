"""Environment-only database configuration; uv --env-file loads .env explicitly."""
import os
import re
from pathlib import Path

from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]


def database_url():
    value = next((os.environ[key].strip() for key in
        ("PARSER_DATABASE_URL", "DATABASE_URL", "REC_NEWS_DATABASE_URL")
        if os.environ.get(key, "").strip()), None)
    if value is None:
        return make_url("sqlite:///" + (ROOT / "parser_app/parser.db").as_posix())
    if value.startswith("postgres://"):
        value = "postgresql+psycopg://" + value[len("postgres://"):]
    url = make_url(value)
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+psycopg")
    if url.get_backend_name() not in ("sqlite", "postgresql"):
        raise ValueError("Parser supports SQLite and PostgreSQL database URLs")
    return url


def database_schema():
    value = os.getenv("PARSER_DATABASE_SCHEMA", "public").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", value):
        raise ValueError("PARSER_DATABASE_SCHEMA must be a PostgreSQL identifier of up to 63 characters")
    return value


def env_int(name, default, minimum=0):
    value = int(os.getenv(name, str(default)))
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def auto_create_tables(dialect):
    value = os.getenv("PARSER_AUTO_CREATE_TABLES")
    if value is None:
        return dialect == "sqlite"
    if value.lower() not in ("true", "false", "1", "0"):
        raise ValueError("PARSER_AUTO_CREATE_TABLES must be true or false")
    return value.lower() in ("true", "1")


def ml_export_enabled(dialect):
    value = os.getenv("PARSER_ML_EXPORT_ENABLED")
    if value is None:
        return dialect == "postgresql"
    if value.lower() not in ("true", "false", "1", "0"):
        raise ValueError("PARSER_ML_EXPORT_ENABLED must be true or false")
    enabled = value.lower() in ("true", "1")
    if enabled and dialect != "postgresql":
        raise ValueError("ML export requires the same PostgreSQL database for parser and ML")
    return enabled


def ml_news_schema():
    value = os.getenv("PARSER_ML_NEWS_SCHEMA", os.getenv("REC_NEWS_DB_SCHEMA_PREFIX", "ml_") + "news").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", value):
        raise ValueError("PARSER_ML_NEWS_SCHEMA must be a valid PostgreSQL schema identifier")
    return value
