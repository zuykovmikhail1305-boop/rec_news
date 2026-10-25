import json
import os
import unittest
from unittest.mock import patch

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from parser_app.database import Base
from parser_app.models import NewsRaw, NewsSource
from parser_app.rss import RSSConfig, MAX_FEED_BYTES, discover_urls, poll_rss, rss_config
from parser_app.queue import claim_next, process_claim
from parser_app.adapters import normalize_url
from parser_app.tasks import discover_rss_publications

FEED = "https://www.interfax.ru/rss.asp"
ARTICLE = "https://www.interfax.ru/business/12345"
RIA = "https://ria.ru/20261025/ekonomika-1234567890.html"


def rss(links):
    from xml.sax.saxutils import escape
    return ('<?xml version="1.0"?><rss version="2.0"><channel><title>Test</title>' + ''.join(
        '<item><title>RSS title</title><link>' + escape(link) + '</link><description>ANONCE_NOT_FULL_TEXT</description><pubDate>Sun, 25 Oct 2026 10:00:00 GMT</pubDate></item>'
        for link in links) + '</channel></rss>').encode()


class RSSTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(self.engine, expire_on_commit=False)
        self.config = RSSConfig(True, {"interfax": FEED}, 300, 100, 15)
        self.mode = patch.dict(os.environ, PARSER_QUEUE_BACKEND="local")
        self.mode.start()

    def tearDown(self):
        self.mode.stop()
        self.engine.dispose()

    def client(self, handler):
        return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)

    def test_discovers_normalizes_and_enqueues_without_reading_article_or_summary(self):
        requested = []
        def upstream(request):
            requested.append(str(request.url))
            return httpx.Response(200, content=rss([ARTICLE, ARTICLE + '?utm_source=rss', 'https://evil.test/a', '/business/54321']))
        with self.client(upstream) as client:
            result = poll_rss(config=self.config, factory=self.factory, client=client)
            repeat = poll_rss(config=self.config, factory=self.factory, client=client)
        self.assertEqual(requested, [FEED])
        self.assertEqual(repeat['feeds'][0]['status'], 'skipped')
        self.assertEqual(repeat['feeds'][0]['job_ids'], [])
        self.assertEqual(result['feeds'][0]['skipped'], 2)
        with self.factory() as db:
            rows = list(db.scalars(select(NewsRaw)))
            self.assertEqual(len(rows), 2)
            for row in rows:
                self.assertEqual(row.status, 'pending')
                self.assertIsNone(row.title)
                self.assertIsNone(row.text)
                self.assertIsNone(row.published_at)
                self.assertIsNone(row.raw_html)

    def test_existing_source_ids_are_not_overwritten(self):
        with self.factory() as db:
            db.add(NewsSource(id=1, code='cbr', name='ЦБ', base_url='https://www.cbr.ru', listing_url='https://www.cbr.ru/news/', parser_key='cbr_html'))
            db.commit()
        with self.client(lambda request: httpx.Response(200, content=rss([ARTICLE]))) as client:
            poll_rss(config=self.config, factory=self.factory, client=client)
        with self.factory() as db:
            row = db.scalar(select(NewsRaw))
            self.assertEqual(row.source.code, 'interfax')
            self.assertNotEqual(row.source_id, 1)
            self.assertEqual(db.get(NewsSource, 1).code, 'cbr')

    def test_celery_dispatch_receives_existing_queue_ids(self):
        with self.client(lambda request: httpx.Response(200, content=rss([ARTICLE]))) as client, patch.dict(
                os.environ, PARSER_QUEUE_BACKEND='celery'), patch('parser_app.rss.dispatch_pending', return_value={'sent':1,'deferred':0}) as dispatch:
            result = poll_rss(config=self.config, factory=self.factory, client=client)
        self.assertEqual(dispatch.call_args.kwargs['item_ids'], result['feeds'][0]['job_ids'])
        self.assertEqual(result['feeds'][0]['dispatch']['sent'], 1)

    def test_one_feed_failure_does_not_block_another_source(self):
        config = RSSConfig(True, {'ria':'https://ria.ru/export/rss2/index.xml','interfax':FEED}, 300, 100, 15)
        with self.client(lambda request: httpx.Response(403) if request.url.host=='ria.ru' else httpx.Response(200,content=rss([ARTICLE]))) as client:
            result=poll_rss(config=config,factory=self.factory,client=client)
        self.assertEqual([feed['status'] for feed in result['feeds']], ['failed','queued'])

    def test_disabled_source_and_malformed_feed_do_not_create_publications(self):
        with self.factory() as db:
            db.add(NewsSource(code='interfax',name='Interfax',base_url='https://www.interfax.ru',listing_url=FEED,parser_key='interfax_html',enabled=False));db.commit()
        for body in (rss([ARTICLE]), b'<html>not a feed</html>'):
            with self.client(lambda request:httpx.Response(200,content=body)) as client:
                result=poll_rss(config=self.config,factory=self.factory,client=client)
            expected='disabled' if body.startswith(b'<?xml') else 'failed'
            self.assertEqual(result['feeds'][0]['status'],expected)
            with self.factory() as db:
                source=db.scalar(select(NewsSource));source.enabled=True;db.commit()
        with self.factory() as db: self.assertEqual(list(db.scalars(select(NewsRaw))),[])

    def test_redirects_cannot_leave_source_or_use_private_host(self):
        for destination in ['https://127.0.0.1/feed','https://ria.ru/feed','http://www.interfax.ru/rss.asp']:
            with self.client(lambda request:httpx.Response(302,headers={'location':destination})) as client:
                result=poll_rss(config=self.config,factory=self.factory,client=client,force=True)
            self.assertEqual(result['feeds'][0]['status'],'failed')

    def test_feed_size_and_entry_count_are_bounded(self):
        config=RSSConfig(True,{'interfax':FEED},300,1,15)
        with self.client(lambda request:httpx.Response(200,content=rss([ARTICLE,'https://www.interfax.ru/business/54321']))) as client:
            self.assertEqual(poll_rss(config=config,factory=self.factory,client=client)['feeds'][0]['new'],1)
        with self.client(lambda request:httpx.Response(200,content=b'x'*(MAX_FEED_BYTES+1))) as client:
            self.assertEqual(poll_rss(config=config,factory=self.factory,client=client,force=True)['feeds'][0]['status'],'failed')

    def test_dry_run_does_not_open_database_or_publish(self):
        with self.client(lambda request:httpx.Response(200,content=rss([ARTICLE]))) as client:
            result=poll_rss(config=self.config,client=client,dry_run=True,factory=lambda:self.fail('dry-run opened DB'))
        self.assertEqual(result['feeds'][0]['urls'],[ARTICLE])

    def test_rss_then_existing_worker_saves_full_article(self):
        html='<html><h1>Полная статья</h1><article><p>'+'Основной текст публикации для проверки полного извлечения. '*12+'</p></article></html>'
        with self.client(lambda request:httpx.Response(200,content=rss([ARTICLE]))) as client:
            poll_rss(config=self.config,factory=self.factory,client=client)
        with self.factory() as db: claim=claim_next(db)
        with self.factory() as db, self.client(lambda request:httpx.Response(200,headers={'content-type':'text/html'},text=html)) as client:
            self.assertEqual(process_claim(db,client,claim),'ready')
        with self.factory() as db:
            row=db.get(NewsRaw,claim.id)
            self.assertIn('Основной текст',row.text)
            self.assertNotIn('ANONCE_NOT_FULL_TEXT',row.text)

    def test_ria_urls_are_supported_without_accepting_listing_pages(self):
        self.assertEqual(normalize_url(RIA+'?utm_source=rss'),RIA)
        for url in ['https://ria.ru/','https://ria.ru/export/rss2/index.xml','https://ria.ru/20261025/slug.html']:
            with self.assertRaises(ValueError): normalize_url(url)
        urls,_=discover_urls(rss([RIA]),'https://ria.ru/export/rss2/index.xml','ria',100)
        self.assertEqual(urls,[RIA])

    def test_ria_html_uses_full_article_pipeline(self):
        from parser_app.publications import parse_publication
        html='<html><h1>Учебная статья РИА</h1><article><p>'+'Полный текст учебной публикации РИА для проверки общего парсера. '*12+'</p></article></html>'
        with self.client(lambda request:httpx.Response(200,headers={'content-type':'text/html'},text=html)) as client:
            article=parse_publication(client,RIA)
        self.assertEqual(article['source'],'ria')
        self.assertIn('Полный текст',article['text'])
        self.assertEqual(article['raw_html'],html)

    def test_environment_configuration_and_disabled_periodic_task(self):
        with patch.dict(os.environ,PARSER_RSS_ENABLED='false',PARSER_RSS_SOURCES=json.dumps({'interfax':FEED})):
            self.assertFalse(rss_config().enabled)
            self.assertEqual(discover_rss_publications.run(),{'status':'disabled'})
        for value in ('[]','{"unknown":"https://example.test"}','{"interfax":"http://127.0.0.1"}'):
            with patch.dict(os.environ,PARSER_RSS_SOURCES=value):
                with self.assertRaises(ValueError):rss_config()
