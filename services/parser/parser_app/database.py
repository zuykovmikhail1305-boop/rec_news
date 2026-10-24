import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


def make_engine(url=None):
    url = url or os.getenv("PARSER_DATABASE_URL", f"sqlite:///{(Path(__file__).parent / 'parser.db').as_posix()}")
    bind = create_engine(url, pool_pre_ping=True)
    if bind.dialect.name == "sqlite":
        @event.listens_for(bind, "connect")
        def enable_foreign_keys(connection, record):
            connection.execute("PRAGMA foreign_keys=ON")
    return bind


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db():
    with SessionLocal() as db:
        yield db
