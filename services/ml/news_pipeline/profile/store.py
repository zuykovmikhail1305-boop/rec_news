"""Кэш профилей интересов в ``data/profiles.db`` (ORM-модель :class:`Profile`): профиль считается один раз
и пересчитывается только при изменении анкеты (ТЗ §4)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy import Text, func, select
from sqlalchemy.orm import Mapped, mapped_column

from news_pipeline.config import DATA_DIR
from news_pipeline.schemas import InterestProfile, UserProfileIn
from news_pipeline.storage import db

PROFILES_DB = DATA_DIR / "profiles.db"


class Profile(db.Base):
    __tablename__ = "profiles"

    user_id: Mapped[str] = mapped_column(Text, primary_key=True)
    input: Mapped[str] = mapped_column(Text)        # анкета UserProfileIn (JSON)
    input_hash: Mapped[str] = mapped_column(Text)   # SHA-1 анкеты: изменилась — профиль пересчитывается
    interests: Mapped[str] = mapped_column(Text)    # профиль интересов InterestProfile (JSON)


class ProfileStore:
    """Анкеты и построенные профили интересов по ``user_id``."""

    def __init__(self, path: str | Path = PROFILES_DB) -> None:
        self.path = Path(path)
        self.engine = db.engine(self.path)
        db.ensure_tables(self.engine, [Profile])

    @staticmethod
    def input_hash(user: UserProfileIn) -> str:
        return hashlib.sha1(user.model_dump_json().encode("utf-8")).hexdigest()

    def _value(self, column, user_id: str) -> str | None:  # noqa: ANN001
        with db.session(self.engine) as session:
            return session.scalar(select(column).where(Profile.user_id == user_id))

    def get(self, user_id: str) -> InterestProfile | None:
        value = self._value(Profile.interests, user_id)
        return InterestProfile.model_validate_json(value) if value else None

    def get_input(self, user_id: str) -> UserProfileIn | None:
        value = self._value(Profile.input, user_id)
        return UserProfileIn.model_validate_json(value) if value else None

    def is_fresh(self, user: UserProfileIn) -> bool:
        """True, если профиль уже построен по той же анкете."""
        return self._value(Profile.input_hash, user.user_id) == self.input_hash(user)

    def put(self, user: UserProfileIn, interests: InterestProfile) -> None:
        # ORM-upsert: атомарно, в отличие от session.merge (чтение, затем запись) при записи из нескольких процессов
        with db.session(self.engine) as session:
            session.execute(db.upsert(Profile), [{
                "user_id": user.user_id, "input": user.model_dump_json(), "input_hash": self.input_hash(user),
                "interests": interests.model_dump_json(),
            }])

    def inputs(self, *, limit: int | None = None, offset: int = 0) -> list[UserProfileIn]:
        """Анкеты по ``user_id`` (без профилей интересов — они тяжёлые: в аспектах векторы)."""
        query = select(Profile.input).order_by(Profile.user_id)
        if limit is not None:
            query = query.limit(limit).offset(offset)
        with db.session(self.engine) as session:
            return [UserProfileIn.model_validate_json(value) for value in session.scalars(query)]

    def count(self) -> int:
        with db.session(self.engine) as session:
            return session.scalar(select(func.count()).select_from(Profile))

    def all_ids(self) -> list[str]:
        with db.session(self.engine) as session:
            return list(session.scalars(select(Profile.user_id).order_by(Profile.user_id)))
