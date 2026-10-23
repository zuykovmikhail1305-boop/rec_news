import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from parser_app.adapters import normalize_url
from parser_app.errors import CollectionError
from parser_app.main import app
from parser_app.publications import parse_publication
from test_collection import HTML, URL

TASS_URL = "https://tass.ru/ekonomika/999000001"
TASS_HTML = (Path(__file__).resolve().parents[3] / "tests/fixtures/tass_article.html").read_text(encoding="utf-8")


class ParseTests(unittest.TestCase):
    def test_same_pipeline_for_three_sources(self):
        for url, html, source in [(URL, HTML, "cbr"), (TASS_URL, TASS_HTML, "tass"),
                                  ("https://www.interfax.ru/business/12345", TASS_HTML, "interfax")]:
            with self.subTest(source=source):
                with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, headers={"content-type":"text/html"}, text=html))) as client:
                    data = parse_publication(client, url)
                self.assertEqual(data["source"], source)
                self.assertIn("text", data)
                self.assertGreater(len(data["text"]), 80)
                self.assertEqual(data["raw_html"], html)
                if source != "cbr":
                    self.assertEqual(data["published_at"], datetime(2026, 10, 22, 12, tzinfo=timezone.utc))

    def test_normalization_preserves_article_identity(self):
        self.assertEqual(normalize_url(TASS_URL + "?utm_source=test#anchor"), TASS_URL)
        self.assertEqual(normalize_url("https://www.interfax.ru/business/12345/?utm_source=test"),
                         "https://www.interfax.ru/business/12345")
        for url in ["https://127.0.0.1/a/123", "https://tass.ru/", "https://interfax.ru/business/",
                    "https://tass.ru:bad/a/123", URL + "&id="]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                normalize_url(url)

    def test_interfax_redirect_does_not_loop(self):
        requested = []
        def upstream(request):
            requested.append(str(request.url))
            if request.url.host == "interfax.ru":
                return httpx.Response(301, headers={"location": "https://www.interfax.ru/business/12345"})
            return httpx.Response(200, headers={"content-type":"text/html"}, text=TASS_HTML)
        with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
            data = parse_publication(client, "https://interfax.ru/business/12345")
        self.assertEqual(data["url"], "https://www.interfax.ru/business/12345")
        self.assertEqual(requested, ["https://www.interfax.ru/business/12345"])

    def test_vedomosti_keeps_all_paragraphs_and_rejects_subscription(self):
        url = "https://www.vedomosti.ru/economics/news/2026/10/23/123456-test"
        html = TASS_HTML.replace("</body>", '<p class="box-paragraph__text">' + "Первый учебный абзац. " * 8 + '</p><p class="box-paragraph__text">Последний абзац.</p></body>')
        with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, headers={"content-type":"text/html"}, text=html))) as client:
            data = parse_publication(client, url)
        self.assertEqual(data["source"], "vedomosti")
        self.assertIn("Первый учебный абзац", data["text"])
        self.assertTrue(data["text"].endswith("Последний абзац."))
        blocked = html.replace("</body>", '<script type="application/ld+json">{"@type":"NewsArticle","isAccessibleForFree":false}</script></body>')
        with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, headers={"content-type":"text/html"}, text=blocked))) as client:
            with self.assertRaises(CollectionError):
                parse_publication(client, url)
        with self.assertRaises(ValueError):
            normalize_url("https://www.vedomosti.ru/economics")

    def test_kommersant_keeps_paragraphs_and_list_in_order(self):
        url = "https://www.kommersant.ru/doc/12345?utm_source=rss"
        html = TASS_HTML.replace("</body>", '<div class="doc-body"><div class="doc-text-block__paragraph">' + "Начало учебной публикации. " * 5 + '</div><ul><li><p class="doc-list-block__text">Элемент списка</p></li></ul><div class="doc-text-block__paragraph">Конец публикации</div></div><footer>ПОСТОРОННИЙ ТЕКСТ</footer></body>')
        with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, headers={"content-type":"text/html"}, text=html))) as client:
            data = parse_publication(client, url)
        self.assertEqual(data["url"], "https://www.kommersant.ru/doc/12345")
        self.assertEqual(data["source"], "kommersant")
        self.assertIn("Элемент списка", data["text"])
        self.assertLess(data["text"].index("Начало"), data["text"].index("Элемент списка"))
        self.assertTrue(data["text"].endswith("Конец публикации"))
        self.assertNotIn("ПОСТОРОННИЙ", data["text"])
        with self.assertRaises(ValueError):
            normalize_url("https://www.kommersant.ru/rss/news.xml")

    def test_redirect_cannot_switch_sources(self):
        with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(302, headers={"location": URL}))) as client:
            with self.assertRaises(CollectionError):
                parse_publication(client, TASS_URL)

    def test_endpoint_returns_text_without_database_dependency(self):
        def no_database():
            raise AssertionError("/parse must not open a database session")
        from parser_app.database import get_db
        app.dependency_overrides[get_db] = no_database
        try:
            with TestClient(app) as api:
                client_type = httpx.Client
                with patch("parser_app.main.time.sleep"), patch("parser_app.main.httpx.Client",
                        side_effect=lambda **kwargs: client_type(transport=httpx.MockTransport(
                            lambda req: httpx.Response(200, headers={"content-type":"text/html"}, text=HTML)))):
                    response = api.post("/parse", json={"url": URL})
                    again = api.post("/parse", json={"url": URL})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(again.json(), response.json())
                self.assertIn("Учебное сообщение", response.json()["text"])
                self.assertNotIn("raw_html", response.json())
                self.assertNotIn("id", response.json())
                self.assertEqual(api.post("/parse", json={"url":"https://example.test"}).status_code, 422)
        finally:
            app.dependency_overrides.clear()

    def test_endpoint_reports_upstream_failure(self):
        with TestClient(app) as api:
            with patch("parser_app.main.time.sleep"), patch("parser_app.main.parse_publication",
                    side_effect=CollectionError("HTTP 403")):
                response = api.post("/parse", json={"url":TASS_URL})
            self.assertEqual(response.status_code, 502)
            self.assertEqual(response.json()["detail"], "HTTP 403")
