import sys
import unittest
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import httpx
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from database import _enable_sqlite_foreign_keys
from init_news_db import init_news_db
from models import NewsRaw
from tass_parser import FetchError, HtmlFetcher, article_url, collect, discover_articles, extract_article

FIXTURES = Path(__file__).parent / "fixtures"
ARTICLE_URL = "https://tass.ru/ekonomika/999000001"
HTML = (FIXTURES / "tass_article.html").read_text()
LISTING = (FIXTURES / "tass_listing.html").read_text()


class TassParserTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        event.listen(self.engine, "connect", _enable_sqlite_foreign_keys)
        init_news_db(self.engine)
        self.db = Session(self.engine)
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.db.close()
        self.engine.dispose()

    def fetcher(self, handler):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.clients.append(client)
        return HtmlFetcher(client, delay=0)

    def test_links_normalized_deduplicated_and_external_hosts_excluded(self):
        self.assertEqual(discover_articles(LISTING), [ARTICLE_URL, "https://tass.ru/ekonomika/999000002"])
        self.assertIsNone(article_url("https://tass.ru@evil.test/ekonomika/123"))

    def test_extracts_text_title_and_utc_date(self):
        article = extract_article(HTML, ARTICLE_URL)
        self.assertEqual(article.title, "Синтетическая новость для проверки парсера")
        self.assertIn("учебный склад", article.text)
        self.assertNotIn("МЕНЮ_НЕ_ТЕКСТ", article.text)
        self.assertNotIn("ПОДВАЛ_НЕ_ТЕКСТ", article.text)
        self.assertEqual(article.published_at, datetime(2026, 10, 22, 12, tzinfo=timezone.utc))

    def test_rejects_forbidden_html_even_with_http_200(self):
        with self.assertRaises(ValueError):
            extract_article("<html><h1>Forbidden</h1><p>Access denied</p></html>", ARTICLE_URL)

    def test_save_and_repeat_skips_ready_articles(self):
        requested = []
        def handler(request):
            requested.append(str(request.url))
            return httpx.Response(200, headers={"Content-Type": "text/html"},
                                  text=LISTING if request.url.path == "/ekonomika" else HTML)
        fetcher = self.fetcher(handler)
        first = collect(self.db, fetcher, limit=1)
        self.assertEqual(first["ready"], 1)
        raw = self.db.scalar(select(NewsRaw))
        self.assertEqual(raw.raw_html, HTML)
        self.assertEqual(raw.content_hash, sha256(raw.text.encode()).hexdigest())
        second = collect(self.db, fetcher, limit=1)
        self.assertEqual(second["skipped"], 1)
        self.assertEqual(second["ready"], 1)  # Collect the second, previously unprocessed URL.
        self.assertEqual(requested.count(ARTICLE_URL), 1)

    def test_listing_refusal_is_not_success(self):
        fetcher = self.fetcher(lambda request: httpx.Response(403))
        with self.assertRaisesRegex(FetchError, "HTTP 403"):
            collect(self.db, fetcher)
        self.assertIsNone(self.db.scalar(select(NewsRaw)))

    def test_article_refusal_stops_before_next_article(self):
        requested = []
        def handler(request):
            requested.append(str(request.url))
            if request.url.path == "/ekonomika":
                return httpx.Response(200, headers={"Content-Type": "text/html"}, text=LISTING)
            return httpx.Response(403)
        result = collect(self.db, self.fetcher(handler))
        self.assertEqual(result["failed"], 1)
        self.assertEqual(len(requested), 2)
        self.assertEqual(self.db.scalar(select(NewsRaw)).status, "failed")

    def test_transient_error_deferred_then_retry_succeeds(self):
        def handler(request):
            if request.url.path == "/ekonomika":
                return httpx.Response(200, headers={"Content-Type": "text/html"}, text=LISTING)
            return httpx.Response(503)
        result = collect(self.db, self.fetcher(handler), limit=1)
        self.assertEqual(result["retry"], 1)
        raw = self.db.scalar(select(NewsRaw))
        self.assertIsNotNone(raw.next_retry_at)
        raw.next_retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.db.commit()
        success = self.fetcher(lambda request: httpx.Response(
            200, headers={"Content-Type": "text/html"}, text=LISTING if request.url.path == "/ekonomika" else HTML))
        self.assertEqual(collect(self.db, success, limit=1)["ready"], 1)
        self.db.refresh(raw)
        self.assertEqual(raw.attempt_count, 2)
        self.assertIsNone(raw.last_error)

    def test_local_html_import_never_requests_network(self):
        def forbidden(request):
            self.fail("Local import must not request the network")
        result = collect(self.db, self.fetcher(forbidden), html_path=FIXTURES / "tass_article.html", url=ARTICLE_URL)
        self.assertEqual(result["ready"], 1)

    def test_redirect_to_external_site_is_rejected(self):
        fetcher = self.fetcher(lambda request: httpx.Response(302, headers={"location": "https://example.test"}))
        with self.assertRaisesRegex(FetchError, "outside TASS"):
            fetcher.fetch(ARTICLE_URL)


if __name__ == "__main__":
    unittest.main()
