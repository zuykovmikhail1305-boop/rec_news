import os
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["WEB_DATABASE_URL"] = "sqlite:///:memory:"
os.environ["DATABASE_URL"] = "invalid-parser-url://must-not-be-used"

from sqlalchemy import inspect
from starlette.requests import Request
from starlette.responses import Response
from backend.main import app
from backend import models, routes
from backend.auth import create_access_token
from backend.database import engine, SessionLocal


def request(token=None):
    headers = [(b"cookie", ("access_token=" + token).encode())] if token else []
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers,
                    "query_string": b"", "scheme": "http", "server": ("test", 80)})


class WebServiceTests(unittest.TestCase):
    def test_database_contains_only_web_tables(self):
        self.assertEqual(set(inspect(engine).get_table_names()), {"users", "user_profiles"})

    def test_pages_and_assets_are_resolved_from_service(self):
        for handler in (routes.login_page, routes.register_page, routes.insights_prototype,
                        routes.insights_parser):
            response = handler(request())
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"html", response.body.lower())
        self.assertTrue(Path(routes.insights_tpl("feed").path).is_file())
        static = next(route.app for route in app.routes if route.path == "/static")
        self.assertTrue((Path(static.directory) / "insights-app.js").is_file())
        with SessionLocal() as db:
            self.assertEqual(routes.index(request(), db).headers["location"], "/login")

    def test_login_and_profile_flow(self):
        with SessionLocal() as db:
            with patch("web_app.security.hash_password", return_value="fixture"):
                response = routes.register(request(), "web@example.test", "secret", "Test", db)
            self.assertEqual(response.status_code, 303)
            self.assertIn("access_token=", response.headers["set-cookie"])
            with patch("web_app.security.verify_password", return_value=True):
                response = routes.login(request(), "web@example.test", "secret", db)
            self.assertEqual(response.status_code, 303)
            user = db.query(models.User).filter_by(email="web@example.test").one()
            req = request(create_access_token(user.id))
            self.assertEqual(routes.profile_setup(req, db).status_code, 200)
            response = routes.profile_setup_post(req, db, ["ИТ"], "Разработка", None, None, None, [])
            self.assertEqual(response.status_code, 303)
            self.assertEqual(routes.index(req, db).status_code, 200)
            self.assertIn("Max-Age=0", routes.logout().headers["set-cookie"])
