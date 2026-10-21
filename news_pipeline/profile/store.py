"""Кэш профилей интересов в ``data/profiles.db``: профиль считается один раз
и пересчитывается только при изменении анкеты (ТЗ §4)."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from news_pipeline.config import DATA_DIR
from news_pipeline.schemas import InterestProfile, UserProfileIn

PROFILES_DB = DATA_DIR / "profiles.db"


class ProfileStore:
    """Анкеты и построенные профили интересов по ``user_id``."""

    def __init__(self, path: str | Path = PROFILES_DB) -> None:
        self.path = Path(path)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS profiles ("
                " user_id TEXT PRIMARY KEY, input TEXT NOT NULL, input_hash TEXT NOT NULL,"
                " interests TEXT NOT NULL)"
            )

    @staticmethod
    def input_hash(user: UserProfileIn) -> str:
        return hashlib.sha1(user.model_dump_json().encode("utf-8")).hexdigest()

    def get(self, user_id: str) -> InterestProfile | None:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute("SELECT interests FROM profiles WHERE user_id = ?", (user_id,)).fetchone()
        return InterestProfile.model_validate_json(row[0]) if row else None

    def get_input(self, user_id: str) -> UserProfileIn | None:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute("SELECT input FROM profiles WHERE user_id = ?", (user_id,)).fetchone()
        return UserProfileIn.model_validate_json(row[0]) if row else None

    def is_fresh(self, user: UserProfileIn) -> bool:
        """True, если профиль уже построен по той же анкете."""
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT input_hash FROM profiles WHERE user_id = ?", (user.user_id,)
            ).fetchone()
        return bool(row) and row[0] == self.input_hash(user)

    def put(self, user: UserProfileIn, interests: InterestProfile) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO profiles (user_id, input, input_hash, interests) VALUES (?, ?, ?, ?)",
                (user.user_id, user.model_dump_json(), self.input_hash(user), interests.model_dump_json()),
            )

    def all_ids(self) -> list[str]:
        with sqlite3.connect(self.path) as connection:
            return [row[0] for row in connection.execute("SELECT user_id FROM profiles ORDER BY user_id")]
