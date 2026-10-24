from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .settings import database_schema, database_url, env_int


class Base(DeclarativeBase):
    pass


def make_engine(url=None, *, schema=None):
    from sqlalchemy.engine import make_url
    target = make_url(url) if url is not None else database_url()
    if target.drivername in ("postgres", "postgresql"):
        target = target.set(drivername="postgresql+psycopg")
    options = {"pool_pre_ping": True}
    if target.get_backend_name() == "postgresql":
        options.update(pool_size=env_int("PARSER_DB_POOL_SIZE", 5, 1),
            max_overflow=env_int("PARSER_DB_MAX_OVERFLOW", 10),
            pool_timeout=env_int("PARSER_DB_POOL_TIMEOUT", 30, 1),
            connect_args={"connect_timeout": env_int("PARSER_DB_CONNECT_TIMEOUT", 10, 1),
                          "options": "-c timezone=UTC"})
    bind = create_engine(target, **options)
    if bind.dialect.name == "postgresql":
        bind = bind.execution_options(schema_translate_map={None: schema or database_schema()})
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
