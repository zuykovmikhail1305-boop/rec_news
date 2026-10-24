import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from parser_app.database import Base, get_db
from parser_app.main import app
from parser_app.models import NewsRaw, NewsSource, QueueSourceState
from parser_app.queue import LEASE, claim_next, enqueue, process_claim
from test_collection import HTML, URL

NOW = datetime(2026, 10, 23, 10, tzinfo=timezone.utc)
OTHER = "https://www.cbr.ru/press/event/?id=12346"


def client_for(status=200, headers=None):
    return httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(
        status, headers={"content-type": "text/html", **(headers or {})}, text=HTML)))


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, expire_on_commit=False)

    def tearDown(self):
        app.dependency_overrides.clear()
        self.db.close()
        self.engine.dispose()

    def test_enqueue_is_idempotent_and_validates_whole_batch(self):
        first = enqueue(self.db, [URL])[0]
        second = enqueue(self.db, [URL + "&utm_source=rss", URL])[0]
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.status, "pending")
        self.assertEqual(first.attempt_count, 0)
        with self.assertRaises(ValueError):
            enqueue(self.db, [OTHER, "https://example.test"])
        self.assertEqual(self.db.scalar(select(func.count()).select_from(NewsRaw)), 1)

    def test_batch_rolls_back_if_later_source_is_disabled(self):
        self.db.add(NewsSource(code="tass", name="ТАСС", base_url="https://tass.ru", listing_url="https://tass.ru/ekonomika", parser_key="tass_html", enabled=False))
        self.db.commit()
        with self.assertRaises(ValueError):
            enqueue(self.db, [URL, "https://tass.ru/ekonomika/123"])
        self.assertEqual(self.db.scalar(select(func.count()).select_from(NewsRaw)), 0)
        self.assertIsNone(self.db.scalar(select(NewsSource).where(NewsSource.code == "cbr")))

    def test_process_success_and_duplicate_does_not_reset(self):
        job = enqueue(self.db, [URL])[0]
        claim = claim_next(self.db, now=NOW)
        self.assertEqual(claim.id, job.id)
        self.assertIsNone(claim_next(self.db, now=NOW))
        with client_for() as client:
            self.assertEqual(process_claim(self.db, client, claim, now=NOW), "ready")
        stored = self.db.get(NewsRaw, job.id)
        self.assertEqual(stored.raw_html, HTML)
        self.assertEqual(stored.attempt_count, 1)
        self.assertIsNone(stored.last_error)
        self.assertEqual(enqueue(self.db, [URL])[0].status, "ready")
        self.assertIsNone(claim_next(self.db, now=NOW + timedelta(days=1)))

    def test_network_retry_waits_and_eventually_succeeds(self):
        job = enqueue(self.db, [URL])[0]
        claim = claim_next(self.db, now=NOW)
        with client_for(503) as client:
            self.assertEqual(process_claim(self.db, client, claim, now=NOW), "retry")
        self.assertIsNone(claim_next(self.db, now=NOW + timedelta(seconds=59)))
        second = claim_next(self.db, now=NOW + timedelta(seconds=60))
        with client_for() as client:
            self.assertEqual(process_claim(self.db, client, second, now=NOW + timedelta(seconds=60)), "ready")
        self.assertEqual(self.db.get(NewsRaw, job.id).attempt_count, 2)

    def test_timeout_retries_but_invalid_article_fails(self):
        job = enqueue(self.db, [URL])[0]
        first = claim_next(self.db, now=NOW)
        def timeout(request):
            raise httpx.ReadTimeout("fixture timeout", request=request)
        with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
            self.assertEqual(process_claim(self.db, client, first, now=NOW), "retry")
        second = claim_next(self.db, now=NOW + timedelta(minutes=1))
        with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200,
                headers={"content-type": "text/html"}, text="<h1>Empty page</h1>"))) as client:
            self.assertEqual(process_claim(self.db, client, second, now=NOW + timedelta(minutes=1)), "failed")
        self.assertIsNone(self.db.get(NewsRaw, job.id).next_retry_at)

    def test_retry_exhaustion_and_permanent_refusal(self):
        job = enqueue(self.db, [URL])[0]
        for attempt in range(3):
            now = NOW + timedelta(hours=attempt)
            claim = claim_next(self.db, now=now)
            with client_for(503) as client:
                status = process_claim(self.db, client, claim, now=now)
            self.assertEqual(status, "failed" if attempt == 2 else "retry")
        self.assertIsNone(claim_next(self.db, now=NOW + timedelta(days=1)))
        self.assertIn("503", self.db.get(NewsRaw, job.id).last_error)
        enqueue(self.db, [OTHER])
        claim = claim_next(self.db, now=NOW + timedelta(days=1))
        with client_for(403) as client:
            self.assertEqual(process_claim(self.db, client, claim, now=NOW + timedelta(days=1)), "failed")

    def test_retry_after_and_per_source_cooldown(self):
        enqueue(self.db, [URL, OTHER])
        first = claim_next(self.db, now=NOW)
        with client_for(429, {"retry-after": "1200"}) as client:
            self.assertEqual(process_claim(self.db, client, first, now=NOW), "retry")
        self.assertIsNone(claim_next(self.db, now=NOW + timedelta(seconds=1199)))
        self.assertIsNotNone(claim_next(self.db, now=NOW + timedelta(seconds=1200)))

    def test_source_throttle_and_disabled_source(self):
        enqueue(self.db, [URL, OTHER, "https://www.interfax.ru/business/12345"])
        first = claim_next(self.db, now=NOW)
        second = claim_next(self.db, now=NOW)
        self.assertNotEqual(first.source_id, second.source_id)
        self.assertIsNone(claim_next(self.db, now=NOW + timedelta(seconds=4)))
        self.db.execute(update(NewsSource).where(NewsSource.code == "cbr").values(enabled=False))
        self.db.commit()
        self.assertIsNone(claim_next(self.db, now=NOW + timedelta(seconds=6)))

    def test_expired_claim_is_recovered_and_old_worker_cannot_overwrite(self):
        job = enqueue(self.db, [URL])[0]
        old = claim_next(self.db, now=NOW)
        new = claim_next(self.db, now=NOW + LEASE + timedelta(seconds=1))
        self.assertEqual(new.attempt, 2)
        with client_for() as client:
            self.assertEqual(process_claim(self.db, client, new), "ready")
        with client_for(503) as client:
            self.assertEqual(process_claim(self.db, client, old), "lease_lost")
        self.assertEqual(self.db.get(NewsRaw, job.id).status, "ready")

    def test_crashed_last_attempt_becomes_failed(self):
        job = enqueue(self.db, [URL])[0]
        for i in range(3):
            self.assertIsNotNone(claim_next(self.db, now=NOW + (LEASE + timedelta(seconds=1)) * i))
        self.assertIsNone(claim_next(self.db, now=NOW + LEASE * 4))
        self.db.expire_all()
        self.assertEqual(self.db.get(NewsRaw, job.id).status, "failed")

    def test_queue_api_does_not_fetch_and_status_is_available(self):
        app.dependency_overrides[get_db] = lambda: self.db
        with TestClient(app) as api, patch("parser_app.queue.parse_publication", side_effect=AssertionError("enqueue must not fetch")):
            response = api.post("/queue", json={"urls": [URL]})
            self.assertEqual(response.status_code, 202)
            item = response.json()["items"][0]
            self.assertEqual(item["status"], "pending")
            self.assertEqual(api.get(f"/queue/{item['id']}").json()["attempt_count"], 0)
            self.assertEqual(len(api.get("/queue?status=pending").json()["items"]), 1)
            self.assertEqual(api.get("/queue/999999").status_code, 404)
            self.assertEqual(api.post("/queue", json={"urls": []}).status_code, 422)

    def test_parallel_workers_claim_once_and_queue_survives_restart(self):
        # File SQLite exercises independent connections and actual database locking.
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            path = (Path(folder) / "queue.db").as_posix()
            engine = create_engine("sqlite:///" + path, connect_args={"timeout": 10})
            Base.metadata.create_all(engine)
            factory = sessionmaker(engine)
            with factory() as db:
                enqueue(db, [URL])
            barrier = Barrier(2)
            def claim():
                with factory() as db:
                    barrier.wait()
                    return claim_next(db, now=NOW)
            try:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    claims = list(pool.map(lambda _: claim(), range(2)))
                self.assertEqual(sum(c is not None for c in claims), 1)
            finally:
                engine.dispose()
            reopened = create_engine("sqlite:///" + path)
            try:
                with Session(reopened) as db:
                    item = db.scalar(select(NewsRaw))
                    self.assertEqual(item.status, "processing")
                    self.assertIsNotNone(claim_next(db, now=NOW + LEASE + timedelta(seconds=1)))
            finally:
                reopened.dispose()
