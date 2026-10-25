import os
import unittest
from unittest.mock import MagicMock, patch
from backend.ml_client import ProcessingJob, MLUnavailable
from backend.processing import ProcessingConfig, processing_tick


class ProcessingTests(unittest.TestCase):
    config = ProcessingConfig(True, 'postgresql://demo', 'ml_news', 15)

    def db(self, locked=True, pending=True):
        db=MagicMock()
        db.execute.side_effect=[MagicMock(fetchone=lambda:(locked,)),
                                MagicMock(fetchone=lambda:(pending,)), MagicMock()]
        connection=MagicMock()
        connection.__enter__.return_value=db
        return connection,db

    def test_no_new_news_means_no_processing_request(self):
        connection,db=self.db(pending=False)
        ml=MagicMock()
        with patch('backend.processing.psycopg.connect',return_value=connection):
            self.assertEqual(processing_tick(self.config,ml),{'status':'idle'})
        ml.enqueue_processing.assert_not_called()
        self.assertIn('pg_advisory_unlock',db.execute.call_args.args[0])

    def test_pending_news_calls_backend_ml_client_and_does_not_update_rows(self):
        connection,db=self.db()
        ml=MagicMock();ml.enqueue_processing.return_value=ProcessingJob(status='accepted',news=2)
        with patch('backend.processing.psycopg.connect',return_value=connection):
            self.assertEqual(processing_tick(self.config,ml),{'status':'accepted','news':2})
        ml.enqueue_processing.assert_called_once()
        self.assertEqual(db.execute.call_count,3)

    def test_other_backend_holds_lock(self):
        connection,db=self.db(locked=False)
        ml=MagicMock()
        with patch('backend.processing.psycopg.connect',return_value=connection):
            self.assertEqual(processing_tick(self.config,ml),{'status':'busy'})
        self.assertEqual(db.execute.call_count,1)
        ml.enqueue_processing.assert_not_called()

    def test_failed_call_releases_lock_and_retries_next_tick(self):
        ml=MagicMock();ml.enqueue_processing.side_effect=[MLUnavailable(),ProcessingJob(status='accepted',news=1)]
        for attempt in range(2):
            connection,db=self.db()
            with patch('backend.processing.psycopg.connect',return_value=connection):
                if attempt==0:
                    with self.assertRaises(MLUnavailable):processing_tick(self.config,ml)
                else:self.assertEqual(processing_tick(self.config,ml)['status'],'accepted')
            self.assertIn('pg_advisory_unlock',db.execute.call_args.args[0])

    def test_configuration_defaults_and_errors(self):
        with patch.dict(os.environ,{},clear=True):
            self.assertFalse(ProcessingConfig.from_env().enabled)
            os.environ['BACKEND_PROCESSING_ENABLED']='true'
            with self.assertRaises(ValueError):ProcessingConfig.from_env()
            os.environ['BACKEND_DATABASE_URL']='postgresql+psycopg://db/news'
            self.assertEqual(ProcessingConfig.from_env().database_url,'postgresql://db/news')
            os.environ['BACKEND_PROCESSING_INTERVAL']='0'
            with self.assertRaises(ValueError):ProcessingConfig.from_env()


@unittest.skipUnless(os.getenv('BACKEND_TEST_DATABASE_URL'), 'Set BACKEND_TEST_DATABASE_URL for PostgreSQL checks')
class ProcessingPostgresTests(unittest.TestCase):
    def test_committed_news_lock_and_retry_against_postgres(self):
        import psycopg
        from psycopg import sql
        from uuid import uuid4
        from threading import Event
        from concurrent.futures import ThreadPoolExecutor
        lock_patch=patch('backend.processing.LOCK_ID',int(uuid4().hex[:12],16))
        lock_patch.start()
        self.addCleanup(lock_patch.stop)
        schema='backend_test_'+uuid4().hex
        url=os.environ['BACKEND_TEST_DATABASE_URL'].replace('postgresql+psycopg://','postgresql://',1)
        config=ProcessingConfig(True,url,schema,15)
        started,release=Event(),Event()
        with psycopg.connect(url,autocommit=True) as db:
            db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
            try:
                db.execute(sql.SQL('CREATE TABLE {}.news (status text)').format(sql.Identifier(schema)))
                ml=MagicMock()
                self.assertEqual(processing_tick(config,ml)['status'],'idle')
                ml.enqueue_processing.assert_not_called()
                db.execute(sql.SQL("INSERT INTO {}.news VALUES ('new')").format(sql.Identifier(schema)))
                def enqueue():
                    started.set()
                    if not release.wait(5):raise TimeoutError()
                    return ProcessingJob(status='accepted',news=1)
                ml.enqueue_processing.side_effect=enqueue
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future=pool.submit(processing_tick,config,ml)
                    self.assertTrue(started.wait(5))
                    try:self.assertEqual(processing_tick(config,ml)['status'],'busy')
                    finally:release.set()
                    self.assertEqual(future.result()['status'],'accepted')
                ml.enqueue_processing.assert_called_once()
                ml.enqueue_processing.side_effect=MLUnavailable()
                with self.assertRaises(MLUnavailable):processing_tick(config,ml)
                ml.enqueue_processing.side_effect=None
                ml.enqueue_processing.return_value=ProcessingJob(status='already_running',news=1)
                self.assertEqual(processing_tick(config,ml)['status'],'already_running')
                db.execute(sql.SQL("UPDATE {}.news SET status='processing'").format(sql.Identifier(schema)))
                ml.reset_mock()
                self.assertEqual(processing_tick(config,ml)['status'],'idle')
                ml.enqueue_processing.assert_not_called()
            finally:
                db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
