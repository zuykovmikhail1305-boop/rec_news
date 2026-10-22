import sys
import unittest
from hashlib import sha256
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from sqlalchemy import create_engine, event, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable
from sqlalchemy.dialects import postgresql

from database import Base, _enable_sqlite_foreign_keys
from init_news_db import init_news_db
from models import NewsQuote, NewsRaw, NewsSource, User, UserProfile
from news_quotes import create_quote, quote_matches_text


class NewsSchemaTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        event.listen(self.engine, "connect", _enable_sqlite_foreign_keys)
        init_news_db(self.engine)
        self.db = Session(self.engine)
        self.source = NewsSource(
            code="fixture", name="Fixture", base_url="https://example.test",
            listing_url="https://example.test/news", parser_key="fixture_html",
        )
        self.db.add(self.source)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def raw(self, url="https://example.test/news/1", **kwargs):
        raw = NewsRaw(source=self.source, url=url, **kwargs)
        self.db.add(raw)
        self.db.flush()
        return raw

    def test_additive_initialization_preserves_existing_users(self):
        Base.metadata.create_all(
            self.engine, tables=[User.__table__, UserProfile.__table__]
        )
        user = User(email="existing@example.test", hashed_password="fixture")
        self.db.add(user)
        self.db.commit()
        init_news_db(self.engine)
        init_news_db(self.engine)
        self.assertEqual(self.db.scalar(select(User.email)), user.email)
        self.assertEqual(len(inspect(self.engine).get_table_names()), 5)

    def test_pending_defaults_allow_discovery_before_fetch(self):
        raw = self.raw()
        self.assertEqual(raw.status, "pending")
        self.assertEqual(raw.attempt_count, 0)
        self.assertIsNone(raw.text)
        self.assertIsNotNone(raw.discovered_at)
        self.assertTrue(self.source.enabled)

    def test_duplicate_url_rejected_but_identical_content_kept(self):
        digest = sha256(b"same text").hexdigest()
        self.raw(content_hash=digest)
        self.raw("https://example.test/news/2", content_hash=digest)
        self.db.commit()
        with self.assertRaises(IntegrityError):
            self.raw()
        self.db.rollback()

    def test_same_url_is_allowed_for_different_sources(self):
        first = self.raw()
        other = NewsSource(
            code="other", name="Other", base_url="https://other.test",
            listing_url="https://other.test/news", parser_key="other_html",
        )
        self.db.add(NewsRaw(source=other, url=first.url))
        self.db.commit()

    def test_invalid_status_and_negative_attempt_count_are_rejected(self):
        for values in ({"status": "typo"}, {"attempt_count": -1}):
            with self.subTest(values=values):
                with self.assertRaises(IntegrityError):
                    self.raw(**values)
                self.db.rollback()

    def test_foreign_key_rejects_missing_source(self):
        self.db.add(NewsRaw(source_id=999, url="https://example.test/missing"))
        with self.assertRaises(IntegrityError):
            self.db.flush()
        self.db.rollback()

    def test_cyrillic_quote_roundtrip_and_changed_text_detection(self):
        text = "Новость: ставка сохранена на 14%."
        raw = self.raw(text=text)
        fragment = "ставка сохранена на 14%"
        start = text.index(fragment)
        quote = create_quote(raw, fragment, start, start + len(fragment))
        self.db.add(quote)
        self.db.commit()
        self.db.expire_all()
        stored = self.db.get(NewsQuote, quote.id)
        self.assertEqual(stored.raw_id, raw.id)
        self.assertTrue(quote_matches_text(stored, raw.text))
        self.assertFalse(quote_matches_text(stored, "Изменено: " + text))
        for start, end, fragment in ((-1, 2, "Но"), (0, 999, text), (0, 2, "XX")):
            with self.assertRaises(ValueError):
                create_quote(raw, fragment, start, end)

    def test_postgresql_schema_compiles(self):
        for model in (NewsSource, NewsRaw, NewsQuote):
            sql = str(CreateTable(model.__table__).compile(dialect=postgresql.dialect()))
            self.assertIn(model.__tablename__, sql)


if __name__ == "__main__":
    unittest.main()
