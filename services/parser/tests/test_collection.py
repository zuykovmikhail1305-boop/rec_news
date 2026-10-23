import unittest
import os
os.environ["PARSER_DATABASE_URL"] = "sqlite:///:memory:"
from datetime import date
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from parser_app.collector import CollectionError, collect, extract, normalize_url
from parser_app.database import Base, get_db
from parser_app.main import app
from parser_app.models import NewsRaw as Publication

URL = "https://www.cbr.ru/press/event/?id=12345"
HTML = """<html><h1>Синтетическая публикация</h1><div class="news-info-line_date">20 октября 2026 года</div>
<div class="lead-text"><p>Учебное сообщение для проверки сбора информации без использования моделей машинного обучения.</p></div>
<div class="landing-text"><p>Основной текст синтетического материала для тестирования сохранения публикации.</p></div>
<footer>ПОДВАЛ_НЕ_ТЕКСТ</footer></html>"""


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)

    def tearDown(self):
        app.dependency_overrides.clear()
        self.db.close()
        self.engine.dispose()

    def test_extract_date_and_body_without_navigation(self):
        data = extract(HTML)
        self.assertEqual(data["published_date"], date(2026, 10, 20))
        self.assertNotIn("ПОДВАЛ", data["text"])
        self.assertIn("Основной текст", data["text"])
        self.assertIsNone(extract(HTML.replace("20 октября 2026 года", "неизвестно"))["published_date"])

    def test_allowed_urls_and_normalization(self):
        self.assertEqual(normalize_url(URL.replace("www.", "") + "&utm_source=test#section"), URL)
        for url in ["https://evil.test/press/event/?id=1", "https://www.cbr.ru@evil.test/press/event/?id=1",
                    "http://www.cbr.ru/press/event/?id=1", "https://www.cbr.ru/", URL + "&id=2",
                    "https://www.cbr.ru:8080/press/event/?id=1"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                normalize_url(url)

    def test_save_and_repeat_without_network(self):
        with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, headers={"content-type":"text/html"}, text=HTML))) as client:
            status, item = collect(self.db, client, URL)
        self.assertEqual(status, "ready")
        self.assertEqual(item.raw_html, HTML)
        self.assertEqual(len(item.content_hash), 64)
        with httpx.Client(transport=httpx.MockTransport(lambda req: self.fail("duplicate must not be fetched"))) as client:
            status, item = collect(self.db, client, URL)
        self.assertEqual(status, "skipped")
        self.assertEqual(len(self.db.scalars(select(Publication)).all()), 1)

    def test_refusal_bad_html_and_external_redirect_are_not_saved(self):
        for response in [httpx.Response(403), httpx.Response(429),
                         httpx.Response(302, headers={"location": "https://evil.test/"}),
                         httpx.Response(200, headers={"content-type":"text/html"}, text="<h1>Denied</h1>")]:
            with httpx.Client(transport=httpx.MockTransport(lambda req: response)) as client:
                with self.assertRaises(CollectionError):
                    collect(self.db, client, URL)
        self.assertIsNone(self.db.scalar(select(Publication)))

    def test_endpoint_and_validation(self):
        app.dependency_overrides[get_db] = lambda: self.db
        real_client = httpx.Client
        upstream = real_client(transport=httpx.MockTransport(lambda req: httpx.Response(200, headers={"content-type":"text/html"}, text=HTML)))
        with TestClient(app) as api:
            with patch("parser_app.main.httpx.Client", return_value=upstream):
                response = api.post("/collect", json={"url":URL})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "ready")
            self.assertEqual(response.json()["published_date"], "2026-10-20")
            self.assertEqual(api.post("/collect", json={"url":"https://example.test"}).status_code, 422)
            self.assertEqual(api.post("/collect", json={}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
