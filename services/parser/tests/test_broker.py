import os
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
from celery.contrib.testing.worker import start_worker
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from parser_app.broker import DISPATCH_LEASE, dispatch_pending, release_dispatch
from parser_app.celery_app import celery_app
from parser_app.database import Base, get_db
from parser_app.main import app
from parser_app.models import BrokerDispatch, NewsRaw
from parser_app.queue import LEASE, enqueue, utcnow
from parser_app.tasks import collect_publication, reconcile_pending
from test_collection import HTML, URL


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.engine = create_engine("sqlite:///" + (Path(self.folder.name) / "test.db").as_posix())
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(self.engine, expire_on_commit=False)
        self.db = self.factory()
        self.job = enqueue(self.db, [URL])[0]
        self.now = utcnow()
        self.messages = []

    def tearDown(self):
        app.dependency_overrides.clear()
        self.db.close()
        self.engine.dispose()
        self.folder.cleanup()

    def publish(self, job_id, token):
        self.messages.append((job_id, token))

    def dispatch(self, **kwargs):
        return dispatch_pending(self.db, publisher=self.publish, now=self.now, **kwargs)

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, headers={"content-type": "text/html"}, text=HTML)))

    def test_dispatch_after_commit_deduplicates_until_reservation_expires(self):
        self.assertEqual(self.dispatch()["sent"], 1)
        with self.factory() as other:
            self.assertIsNotNone(other.get(NewsRaw, self.messages[0][0]))
        self.assertEqual(self.dispatch()["sent"], 0)
        result = dispatch_pending(self.db, publisher=self.publish, now=self.now + DISPATCH_LEASE)
        self.assertEqual(result["sent"], 1)
        self.assertNotEqual(self.messages[0][1], self.messages[1][1])
        release_dispatch(self.db, *self.messages[0])  # stale message cannot release a newer reservation
        self.db.expire_all()
        self.assertIsNotNone(self.db.get(BrokerDispatch, self.job.id).reserved_until)

    def test_broker_failure_keeps_pending_job_and_can_be_recovered(self):
        def unavailable(*args):
            raise ConnectionError("offline")
        result = dispatch_pending(self.db, publisher=unavailable, now=self.now)
        self.assertEqual(result, {"sent": 0, "deferred": 1})
        self.db.expire_all()
        self.assertEqual(self.db.get(NewsRaw, self.job.id).attempt_count, 0)
        self.assertEqual(self.dispatch()["sent"], 1)

    def test_api_commits_job_even_if_broker_is_down(self):
        app.dependency_overrides[get_db] = lambda: self.db
        with TestClient(app) as api, patch.dict(os.environ, PARSER_QUEUE_BACKEND="celery"), patch.object(
                celery_app, "send_task", side_effect=ConnectionError("offline")):
            response = api.post("/queue", json={"urls": [URL]})
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json()["dispatch"], {"sent": 0, "deferred": 1})
            self.assertEqual(response.json()["items"][0]["status"], "pending")
            self.assertEqual(api.get(f"/queue/{self.job.id}").status_code, 200)

    def test_task_reuses_claim_and_duplicate_delivery_does_not_fetch(self):
        self.dispatch()
        with patch("parser_app.tasks.SessionLocal", self.factory), patch(
                "parser_app.tasks.httpx.Client", return_value=self.client()) as client:
            self.assertEqual(collect_publication.run(*self.messages[0]), "ready")
            self.assertEqual(collect_publication.run(*self.messages[0]), "not_eligible")
            self.assertEqual(client.call_count, 1)
        self.db.expire_all()
        row = self.db.get(NewsRaw, self.job.id)
        self.assertEqual(row.attempt_count, 1)
        self.assertEqual(row.raw_html, HTML)
        self.assertEqual(self.dispatch()["sent"], 0)

    def test_expected_failure_uses_db_retry_not_immediate_celery_retry(self):
        self.dispatch()
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503)))
        with patch("parser_app.tasks.SessionLocal", self.factory), patch(
                "parser_app.tasks.httpx.Client", return_value=client):
            self.assertEqual(collect_publication.run(*self.messages[0]), "retry")
        self.db.expire_all()
        row = self.db.get(NewsRaw, self.job.id)
        self.assertEqual(row.attempt_count, 1)
        self.assertEqual(self.dispatch()["sent"], 0)
        self.assertEqual(dispatch_pending(self.db, publisher=self.publish,
            now=utcnow() + timedelta(seconds=61))["sent"], 1)

    def test_source_cooldown_is_preserved_in_dispatcher(self):
        self.dispatch()
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(429)))
        with patch("parser_app.tasks.SessionLocal", self.factory), patch(
                "parser_app.tasks.httpx.Client", return_value=client):
            self.assertEqual(collect_publication.run(*self.messages[0]), "retry")
        self.assertEqual(dispatch_pending(self.db, publisher=self.publish,
            now=utcnow() + timedelta(seconds=61))["sent"], 0)
        self.assertEqual(dispatch_pending(self.db, publisher=self.publish,
            now=utcnow() + timedelta(seconds=901))["sent"], 1)

    def test_unexpected_worker_error_recovers_after_db_lease_expires(self):
        self.dispatch()
        with patch("parser_app.tasks.SessionLocal", self.factory), patch(
                "parser_app.tasks.httpx.Client", return_value=self.client()), patch(
                "parser_app.tasks.process_claim", side_effect=RuntimeError("worker crashed")):
            with self.assertRaises(RuntimeError):
                collect_publication.run(*self.messages[0])
        self.db.expire_all()
        self.assertEqual(self.db.get(NewsRaw, self.job.id).status, "processing")
        self.assertEqual(self.dispatch()["sent"], 0)
        self.assertEqual(dispatch_pending(self.db, publisher=self.publish,
            now=utcnow() + LEASE + timedelta(seconds=1))["sent"], 1)

    def test_beat_task_reconciles_existing_db_jobs(self):
        with patch("parser_app.tasks.SessionLocal", self.factory), patch.object(celery_app, "send_task") as send:
            self.assertEqual(reconcile_pending.run()["sent"], 1)
            self.assertEqual(send.call_args.kwargs["args"][0], self.job.id)

    def test_tasks_use_private_queue_and_persistent_json_messages(self):
        self.assertEqual(celery_app.conf.task_serializer, "json")
        self.assertTrue(celery_app.conf.task_acks_late)
        self.assertTrue(celery_app.conf.task_ignore_result)
        self.assertEqual(celery_app.conf.worker_prefetch_multiplier, 1)
        self.assertEqual(set(celery_app.conf.task_routes), {"parser.collect", "parser.dispatch_pending", "parser.poll_rss"})
        with patch.object(celery_app, "send_task") as send:
            self.assertEqual(dispatch_pending(self.db)["sent"], 1)
            self.assertEqual(send.call_args.args[0], "parser.collect")
            self.assertEqual(send.call_args.kwargs["args"][0], self.job.id)
            self.assertEqual(send.call_args.kwargs["queue"], "parser.publications")

    def test_service_queues_are_exclusive_for_modern_rabbitmq(self):
        from celery.events.receiver import EventReceiver
        with celery_app.connection("memory://") as connection:
            receiver = EventReceiver(connection, app=celery_app)
            self.assertTrue(receiver.queue.exclusive)
        self.assertTrue(celery_app.control.mailbox.get_queue("test-parser-worker").exclusive)
        self.assertTrue(celery_app.control.mailbox.get_reply_queue().exclusive)

    def test_real_celery_worker_consumes_serialized_message_using_memory_transport(self):
        # Real Celery consumer/serialization/task execution; RabbitMQ still needs a separate smoke test.
        old_url = celery_app.conf.broker_url
        celery_app.conf.broker_url = "memory://"
        self.dispatch()
        try:
            with patch("parser_app.tasks.SessionLocal", self.factory), patch(
                    "parser_app.tasks.httpx.Client", return_value=self.client()):
                with start_worker(celery_app, pool="solo", perform_ping_check=False,
                                  queues=["parser.publications"], shutdown_timeout=10):
                    collect_publication.apply_async(args=list(self.messages[0]), queue="parser.publications")
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        self.db.expire_all()
                        if self.db.get(NewsRaw, self.job.id).status == "ready":
                            break
                        time.sleep(.1)
                    self.assertEqual(self.db.get(NewsRaw, self.job.id).status, "ready")
        finally:
            celery_app.conf.broker_url = old_url
