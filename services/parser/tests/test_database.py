import os
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.schema import CreateTable
from sqlalchemy.dialects import postgresql

from parser_app.database import Base, make_engine
from parser_app.models import NewsRaw
from parser_app.schema import create_tables, initialize_database, schema_problems
from parser_app.settings import auto_create_tables, database_schema, database_url


class DatabaseConfigurationTests(unittest.TestCase):
    def test_shared_url_fallback_and_explicit_parser_override(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(database_url().get_backend_name(), "sqlite")
            os.environ["REC_NEWS_DATABASE_URL"] = "postgresql://ml@localhost/shared"
            self.assertEqual(database_url().drivername, "postgresql+psycopg")
            os.environ["DATABASE_URL"] = "postgres://common@localhost/shared"
            self.assertEqual(database_url().username, "common")
            os.environ["PARSER_DATABASE_URL"] = "postgresql+psycopg://parser@localhost/shared"
            self.assertEqual(database_url().username, "parser")

    def test_postgres_engine_has_configured_schema_without_connecting(self):
        with patch.dict(os.environ, PARSER_DATABASE_SCHEMA="ingest", PARSER_DB_POOL_SIZE="2"):
            bind = make_engine("postgresql://user:secret@localhost/shared")
            try:
                self.assertEqual(bind.dialect.driver, "psycopg")
                self.assertEqual(bind.get_execution_options()["schema_translate_map"], {None: "ingest"})
                self.assertEqual(bind.pool.size(), 2)
                self.assertNotIn("secret", bind.url.render_as_string(hide_password=True))
            finally:
                bind.dispose()

    def test_schema_and_auto_ddl_are_validated(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(auto_create_tables("postgresql"))
            self.assertTrue(auto_create_tables("sqlite"))
            os.environ["PARSER_DATABASE_SCHEMA"] = 'public; DROP TABLE news_raw'
            with self.assertRaises(ValueError): database_schema()
            os.environ["PARSER_AUTO_CREATE_TABLES"] = "sometimes"
            with self.assertRaises(ValueError): auto_create_tables("postgresql")

    def test_check_does_not_create_tables_and_old_schema_is_upgraded_additively(self):
        bind = create_engine("sqlite://")
        try:
            with patch.dict(os.environ, PARSER_AUTO_CREATE_TABLES="false"):
                with self.assertRaises(RuntimeError): initialize_database(bind)
            self.assertEqual(inspect(bind).get_table_names(), [])
            create_tables(bind)
            with bind.begin() as connection:
                connection.execute(text("ALTER TABLE news_raw DROP COLUMN published_date"))
                connection.execute(text("INSERT INTO news_sources (id,code,name,base_url,listing_url,parser_key) VALUES (1,'test','test','https://example.test','https://example.test','test')"))
                connection.execute(text("INSERT INTO news_raw (source_id,url,status,text) VALUES (1,'https://example.test/a','ready','preserved')"))
            self.assertTrue(schema_problems(bind))
            create_tables(bind)
            create_tables(bind)
            self.assertEqual(schema_problems(bind), [])
            with bind.connect() as connection:
                self.assertEqual(connection.scalar(text("SELECT text FROM news_raw")), "preserved")
        finally:
            bind.dispose()

    def test_shared_news_ddl_compiles_for_postgres(self):
        ddl = str(CreateTable(NewsRaw.__table__).compile(dialect=postgresql.dialect()))
        self.assertIn("TIMESTAMP WITH TIME ZONE", ddl)
        self.assertIn("uq_news_raw_source_url", ddl)
        self.assertIn("published_date DATE", ddl)
