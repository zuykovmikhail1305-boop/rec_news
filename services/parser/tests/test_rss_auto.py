import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
from sqlalchemy import select
import test_rss as fixtures
from parser_app.models import NewsRaw, RSSPollState
from parser_app.rss import RSSConfig, claim_poll, poll_rss, poll_status, save_feed


class AutoPollTests(unittest.TestCase):
    setUp = fixtures.RSSTests.setUp
    tearDown = fixtures.RSSTests.tearDown
    client = fixtures.RSSTests.client
    now = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)

    def poll(self, handler, *, seconds=0, **kwargs):
        with self.client(handler) as client:
            return poll_rss(config=kwargs.pop('config', self.config), factory=self.factory,
                client=client, now=self.now + timedelta(seconds=seconds), **kwargs)['feeds'][0]

    def state(self):
        with self.factory() as db: return db.scalar(select(RSSPollState))

    def test_conditional_get_304_creates_no_jobs(self):
        self.poll(lambda r:httpx.Response(200,headers={'etag':'"v1"','last-modified':'Sun, 25 Oct 2026 12:00:00 GMT'},content=fixtures.rss([fixtures.ARTICLE])))
        def unchanged(request):
            self.assertEqual(request.headers['if-none-match'],'"v1"')
            self.assertIn('if-modified-since',request.headers)
            return httpx.Response(304)
        result=self.poll(unchanged,seconds=300)
        self.assertEqual((result['status'],result['new'],result['job_ids']),('not_modified',0,[]))
        state=self.state()
        self.assertEqual(state.last_http_status,304)
        self.assertEqual(state.consecutive_errors,0)
        self.assertIsNone(state.lease_token)

    def test_changed_feed_only_dispatches_new_links(self):
        old=fixtures.ARTICLE; new='https://www.interfax.ru/business/54321'
        self.poll(lambda r:httpx.Response(200,headers={'etag':'v1'},content=fixtures.rss([old])))
        with patch('parser_app.rss.queue_backend',return_value='celery'), patch('parser_app.rss.dispatch_pending',return_value={'sent':1,'deferred':0}) as dispatch:
            result=self.poll(lambda r:httpx.Response(200,headers={'etag':'v2'},content=fixtures.rss([old,new])),seconds=300)
        self.assertEqual((result['new'],result['known']),(1,1))
        self.assertEqual(len(dispatch.call_args.kwargs['item_ids']),1)
        with self.factory() as db:
            rows=list(db.scalars(select(NewsRaw)))
            self.assertEqual(len(rows),2)
            self.assertEqual(db.get(NewsRaw,result['job_ids'][0]).url,new)

    def test_retry_after_and_recovery_are_persisted(self):
        result=self.poll(lambda r:httpx.Response(429,headers={'retry-after':'1200'}))
        self.assertEqual(result['retry_in'],1200)
        self.assertEqual(self.state().consecutive_errors,1)
        result=self.poll(lambda r:self.fail('cooldown must not fetch'),seconds=300)
        self.assertEqual(result['status'],'skipped')
        self.poll(lambda r:httpx.Response(200,content=fixtures.rss([])),seconds=1200)
        state=self.state()
        self.assertEqual(state.consecutive_errors,0)
        self.assertIsNone(state.last_error)
        self.assertIsNotNone(state.last_success_at)

    def test_force_does_not_bypass_an_active_poll_lease(self):
        def upstream(request):
            nested=self.poll(lambda r:self.fail('overlapping request'),force=True)
            self.assertEqual(nested['status'],'skipped')
            return httpx.Response(200,content=fixtures.rss([fixtures.ARTICLE]))
        self.assertEqual(self.poll(upstream)['new'],1)

    def test_expired_response_cannot_overwrite_newer_poll(self):
        old,_=claim_poll(self.factory,'interfax',fixtures.FEED,self.config,self.now,False)
        fresh,_=claim_poll(self.factory,'interfax',fixtures.FEED,self.config,self.now+timedelta(seconds=121),False)
        self.assertIsNotNone(fresh)
        response={'status':200,'etag':'v1','last_modified':None}
        result=save_feed(self.factory,old,response,'interfax',[fixtures.ARTICLE],self.config,self.now)
        self.assertEqual(result['status'],'lease_lost')
        with self.factory() as db:self.assertEqual(list(db.scalars(select(NewsRaw))),[])
        result=save_feed(self.factory,fresh,response,'interfax',[fixtures.ARTICLE],self.config,self.now+timedelta(seconds=121))
        self.assertEqual(result['new'],1)

    def test_entry_limit_does_not_starve_backlog_or_cache_partial_feed(self):
        config=RSSConfig(True,{'interfax':fixtures.FEED},300,1,15)
        body=fixtures.rss([fixtures.ARTICLE,'https://www.interfax.ru/business/54321'])
        def upstream(request):
            if 'if-none-match' in request.headers:return httpx.Response(304)
            return httpx.Response(200,headers={'etag':'v1'},content=body)
        first=self.poll(upstream,config=config)
        self.assertEqual((first['new'],first['remaining']),(1,1))
        self.assertIsNone(self.state().etag)
        second=self.poll(upstream,seconds=300,config=config)
        self.assertEqual((second['new'],second['remaining']),(1,0))
        self.assertEqual(self.state().etag,'v1')
        third=self.poll(upstream,seconds=600,config=config)
        self.assertEqual(third['status'],'not_modified')

    def test_enqueue_error_does_not_commit_success_or_validator(self):
        response=lambda r:httpx.Response(200,headers={'etag':'v1'},content=fixtures.rss([fixtures.ARTICLE]))
        with patch('parser_app.rss.enqueue',side_effect=ValueError('save failed')):
            self.assertEqual(self.poll(response)['status'],'failed')
        state=self.state()
        self.assertIsNone(state.etag)
        self.assertIsNone(state.last_success_at)
        with self.factory() as db:self.assertEqual(list(db.scalars(select(NewsRaw))),[])
        self.assertEqual(self.poll(response,seconds=300)['new'],1)

    def test_backoff_increases_and_status_is_available(self):
        self.assertEqual(self.poll(lambda r:httpx.Response(503))['retry_in'],300)
        self.assertEqual(self.poll(lambda r:httpx.Response(503),seconds=300)['retry_in'],600)
        with self.factory() as db: status=poll_status(db)[0]
        self.assertEqual(status['consecutive_errors'],2)
        self.assertEqual(status['last_http_status'],503)
        self.assertTrue(status['next_poll_at'].endswith('+00:00'))
        self.assertNotIn('lease_token',status)

    def test_parallel_pollers_claim_once_and_state_survives_restart(self):
        import tempfile
        from pathlib import Path
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from sqlalchemy.orm import sessionmaker
        from parser_app.database import Base, make_engine
        with tempfile.TemporaryDirectory() as directory:
            url = "sqlite:///" + str(Path(directory) / "poll.db").replace("\\", "/")
            engine = make_engine(url)
            Base.metadata.create_all(engine)
            factory = sessionmaker(engine, expire_on_commit=False)
            barrier = Barrier(2)
            def claim():
                barrier.wait()
                return claim_poll(factory, 'interfax', fixtures.FEED, self.config, self.now, False)
            try:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(lambda _: claim(), range(2)))
                self.assertEqual(sum(lease is not None for lease, reason in results), 1)
                engine.dispose()
                engine = make_engine(url)
                factory = sessionmaker(engine, expire_on_commit=False)
                lease, reason = claim_poll(factory, 'interfax', fixtures.FEED, self.config, self.now, True)
                self.assertIsNone(lease)
                self.assertEqual(reason, 'not_due_or_busy')
            finally:
                engine.dispose()

    def test_cbr_rss_file_urls_normalize_and_queue(self):
        from parser_app.adapters import normalize_url
        url = "https://www.cbr.ru/press/PR/?file=23102026_133000key.htm"
        normalized = "https://www.cbr.ru/press/pr/?file=23102026_133000key.htm"
        config = RSSConfig(True, {'cbr':'https://www.cbr.ru/rss/RssPress'}, 300, 100, 15)
        result = self.poll(lambda r:httpx.Response(200,content=fixtures.rss([url,normalized])), config=config)
        self.assertEqual(result['new'], 1)
        self.assertEqual(normalize_url(url), normalized)
        for bad in ('../secret.htm','https://evil.test/a.htm','a.htm/b','a.htm&file=b.htm','a.htm&id=1'):
            with self.assertRaises(ValueError):
                normalize_url('https://www.cbr.ru/press/pr/?file=' + bad)
