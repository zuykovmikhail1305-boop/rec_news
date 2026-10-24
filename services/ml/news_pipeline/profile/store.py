"""Кэш профилей интересов в ``data/profiles.db`` (SQLAlchemy): профиль считается один раз
и пересчитывается только при изменении анкеты (ТЗ §4)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy import Column, MetaData, Table, Text, func, select

from news_pipeline.config import DATA_DIR
from news_pipeline.schemas import InterestProfile, UserProfileIn
from news_pipeline.storage import db

PROFILES_DB = DATA_DIR / "profiles.db"

metadata = MetaData()

PROFILES = Table(
    "profiles", metadata,
    Column("user_id", Text, primary_key=True),
    Column("input", Text, nullable=False),        # анкета UserProfileIn (JSON)
    Column("input_hash", Text, nullable=False),   # SHA-1 анкеты: изменилась — профиль пересчитывается
    Column("interests", Text, nullable=False),    # профиль интересов InterestProfile (JSON)
)


class ProfileStore:
    """Анкеты и построенные профили интересов по ``user_id``."""

    def __init__(self, path: str | Path = PROFILES_DB) -> None:
        self.path = Path(path)
        self.engine = db.engine(self.path)
        db.ensure_tables(self.engine, [PROFILES])

    @staticmethod
    def input_hash(user: UserProfileIn) -> str:
        return hashlib.sha1(user.model_dump_json().encode("utf-8")).hexdigest()

    def _value(self, column: str, user_id: str) -> str | None:
        with self.engine.connect() as connection:
            return connection.scalar(select(PROFILES.c[column]).where(PROFILES.c.user_id == user_id))

    def get(self, user_id: str) -> InterestProfile | None:
        value = self._value("interests", user_id)
        return InterestProfile.model_validate_json(value) if value else None

    def get_input(self, user_id: str) -> UserProfileIn | None:
        value = self._value("input", user_id)
        return UserProfileIn.model_validate_json(value) if value else None

    def is_fresh(self, user: UserProfileIn) -> bool:
        """True, если профиль уже построен по той же анкете."""
        return self._value("input_hash", user.user_id) == self.input_hash(user)

    def put(self, user: UserProfileIn, interests: InterestProfile) -> None:
        with self.engine.begin() as connection:
            connection.execute(db.upsert(PROFILES), {
                "user_id": user.user_id, "input": user.model_dump_json(), "input_hash": self.input_hash(user),
                "interests": interests.model_dump_json(),
            })

    def inputs(self, *, limit: int | None = None, offset: int = 0) -> list[UserProfileIn]:
        """Анкеты по ``user_id`` (без профилей интересов — они тяжёлые: в аспектах векторы)."""
        query = select(PROFILES.c.input).order_by(PROFILES.c.user_id)
        if limit is not None:
            query = query.limit(limit).offset(offset)
        with self.engine.connect() as connection:
            return [UserProfileIn.model_validate_json(value) for value in connection.scalars(query)]

    def count(self) -> int:
        with self.engine.connect() as connection:
            return connection.scalar(select(func.count()).select_from(PROFILES))

    def all_ids(self) -> list[str]:
        with self.engine.connect() as connection:
            return list(connection.scalars(select(PROFILES.c.user_id).order_by(PROFILES.c.user_id)))
