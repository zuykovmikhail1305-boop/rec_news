"""Optional real PostgreSQL tests; only a randomly named test schema is created/dropped."""
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4
from unittest.mock import patch

import httpx
from sqlalchemy import inspect, select, text, update, Column, MetaData, Table, Text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from parser_app.broker import dispatch_pending
from parser_app.collector import collect
from parser_app.database import make_engine
from parser_app.models import NewsRaw, NewsSource
from parser_app.ml_export import input_table, export_publication
from parser_app.queue import LEASE, claim_next, enqueue, process_claim
from parser_app.schema import create_tables, schema_problems
from test_collection import HTML, URL


@unittest.skipUnless(os.getenv("PARSER_TEST_DATABASE_URL"), "Set PARSER_TEST_DATABASE_URL to run PostgreSQL integration tests")
class PostgresIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.schema = "parser_test_" + uuid4().hex
        self.engine = make_engine(os.environ["PARSER_TEST_DATABASE_URL"], schema=self.schema)
        self.ml_schema = self.schema + "_ml"
        self.config = patch.dict(os.environ, PARSER_ML_NEWS_SCHEMA=self.ml_schema, PARSER_ML_EXPORT_ENABLED="true")
        self.config.start()
        with self.engine.begin() as connection:
            connection.execute(CreateSchema(self.ml_schema))
        # Fixture of the ML-owned input contract, created only in a random test schema.
        self.ml_table = input_table(self.ml_schema)
        self.ml_table.create(self.engine)
        create_tables(self.engine)
        self.factory = sessionmaker(self.engine, expire_on_commit=False)
        self.now = datetime.now(timezone.utc)

    def tearDown(self):
        assert self.schema.startswith("parser_test_") and len(self.schema) == 44
        with self.engine.begin() as connection:
            connection.execute(DropSchema(self.schema, cascade=True))
            connection.execute(DropSchema(self.ml_schema, cascade=True))
        self.engine.dispose()
        self.config.stop()

    def client(self, status=200):
        return httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status,
            headers={"content-type": "text/html"}, text=HTML)))

    def test_direct_collection_is_readable_by_an_independent_ml_consumer(self):
        with self.factory() as db, self.client() as client:
            status, item = collect(db, client, URL)
            self.assertEqual(status, "ready")
            raw_id = item.id
        # Raw SQL through a separate connection: no parser ORM dependency for the consumer.
        with self.engine.connect() as connection:
            row = connection.execute(text(f'SELECT n.id,n.status,n.text,n.raw_html,n.content_hash,n.published_date,s.code FROM "{self.schema}".news_raw n JOIN "{self.schema}".news_sources s ON s.id=n.source_id WHERE n.id=:id'), {"id": raw_id}).one()
            self.assertEqual(row.status, "ready")
            self.assertEqual(row.raw_html, HTML)
            self.assertIn("Основной текст", row.text)
            self.assertEqual(row.code, "cbr")
            ml = connection.execute(select(self.ml_table)).one()
            self.assertEqual(ml.status, "new")
            self.assertEqual(ml.text, row.text)
            self.assertEqual(ml.title, "Синтетическая публикация")
            self.assertIsNone(ml.publication_time)  # date-only source: do not invent a timestamp
            import json
            self.assertEqual(json.loads(ml.extra)["parser"]["raw_id"], raw_id)
            self.assertEqual(ml.document_id, str(raw_id))  # ID новости в ML — числовой ID публикации
            document_id = ml.document_id
        with self.engine.begin() as connection:
            connection.execute(update(self.ml_table).values(status="processing"))
        with self.factory() as db, self.client() as client:
            status, item = collect(db, client, URL)
            self.assertEqual((status, item.id), ("skipped", raw_id))
        with self.engine.connect() as connection:
            rows = connection.execute(select(self.ml_table)).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0].document_id, rows[0].status), (document_id, "processing"))

    def test_reenqueue_ready_article_repairs_missing_handoff(self):
        with self.factory() as db, self.client() as client:
            _, raw = collect(db, client, URL)
            raw_id = raw.id
        with self.engine.begin() as connection:
            connection.execute(self.ml_table.delete())
        with self.factory() as db:
            self.assertEqual(enqueue(db, [URL])[0].id, raw_id)
            rows = db.execute(select(self.ml_table)).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].status, "new")

    def test_missing_ml_table_rolls_back_publication(self):
        with self.engine.begin() as connection:
            connection.execute(DropSchema(self.ml_schema, cascade=True))
        self.config.stop()
        self.config = patch.dict(os.environ, PARSER_ML_NEWS_SCHEMA=self.schema + "_missing", PARSER_ML_EXPORT_ENABLED="true")
        self.config.start()
        with self.factory() as db, self.client() as client:
            from sqlalchemy.exc import ProgrammingError
            with self.assertRaises(ProgrammingError): collect(db, client, URL)
            db.rollback()
            self.assertEqual(list(db.scalars(select(NewsRaw))), [])
        # Recreate only the test-owned schema for teardown.
        with self.engine.begin() as connection:
            connection.execute(CreateSchema(self.ml_schema))

    def test_queue_export_failure_does_not_mark_raw_ready(self):
        with self.factory() as db:
            raw_id = enqueue(db, [URL])[0].id
            claim = claim_next(db, now=self.now)
        with self.factory() as db, self.client() as client, patch("parser_app.queue.export_publication", side_effect=RuntimeError("handoff failure")):
            with self.assertRaises(RuntimeError): process_claim(db, client, claim)
        with self.factory() as db:
            self.assertEqual(db.get(NewsRaw, raw_id).status, "processing")
            self.assertEqual(list(db.execute(select(self.ml_table))), [])

    def test_concurrent_claim_retry_and_expired_lease(self):
        with self.factory() as db: job_id = enqueue(db, [URL])[0].id
        barrier = Barrier(2)
        def attempt():
            with self.factory() as db:
                barrier.wait()
                return claim_next(db, now=self.now)
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda _: attempt(), range(2)))
        first = next(claim for claim in claims if claim is not None)
        self.assertEqual(sum(claim is not None for claim in claims), 1)
        with self.factory() as db, self.client(503) as client:
            self.assertEqual(process_claim(db, client, first, now=self.now), "retry")
        with self.factory() as db:
            self.assertIsNone(claim_next(db, now=self.now + timedelta(seconds=59)))
            second = claim_next(db, now=self.now + timedelta(seconds=60))
        with self.factory() as db:
            third = claim_next(db, now=self.now + timedelta(seconds=60) + LEASE + timedelta(seconds=1))
        with self.factory() as db, self.client() as client:
            self.assertEqual(process_claim(db, client, third), "ready")
        with self.factory() as db, self.client(503) as client:
            self.assertEqual(process_claim(db, client, second), "lease_lost")
            self.assertEqual(db.get(NewsRaw, job_id).status, "ready")
            rows = db.execute(select(self.ml_table)).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].status, "new")

    def test_concurrent_enqueue_and_dispatch_are_idempotent(self):
        barrier = Barrier(2)
        def submit():
            with self.factory() as db:
                barrier.wait()
                return enqueue(db, [URL])[0].id
        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(lambda _: submit(), range(2)))
        self.assertEqual(ids[0], ids[1])
        messages = []
        with self.factory() as db:
            self.assertEqual(dispatch_pending(db, publisher=lambda *args: messages.append(args))["sent"], 1)
        with self.factory() as db:
            self.assertEqual(dispatch_pending(db, publisher=lambda *args: messages.append(args))["sent"], 0)
        self.assertEqual(len(messages), 1)

    def test_original_shared_schema_is_upgraded_without_losing_rows(self):
        schema = '"' + self.schema + '"'
        with self.engine.begin() as connection:
            connection.execute(text(f"ALTER TABLE {schema}.news_raw DROP COLUMN published_date"))
            connection.execute(text(f"CREATE TABLE {schema}.ml_unrelated (value TEXT)"))
            connection.execute(text(f"INSERT INTO {schema}.ml_unrelated VALUES ('keep')"))
        create_tables(self.engine)
        create_tables(self.engine)
        self.assertEqual(schema_problems(self.engine), [])
        with self.engine.connect() as connection:
            self.assertEqual(connection.scalar(text(f"SELECT value FROM {schema}.ml_unrelated")), "keep")
