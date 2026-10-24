import re
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from backend.auth import COOKIE, _session
from backend.main import app
from backend.ml_client import (MLClient, MLUnavailable, Profile, ProfileDetails,
                               ProfileList, ProfileNotFound, UserFeed, get_ml_client)


PROFILES = {
    "u_01": Profile(user_id="u_01", role="собственник бизнеса", company_name="Компания Альфа", industry="Торговля"),
    "u_02": Profile(user_id="u_02", role="финансовый директор", company_name="Компания Бета", industry="Строительство"),
}


class FakeML:
    unavailable = False

    def profiles(self, limit=50, offset=0):
        if self.unavailable:
            raise MLUnavailable("ML offline")
        return ProfileList(total=len(PROFILES), items=list(PROFILES.values())[offset:offset+limit])

    def profile(self, user_id):
        if self.unavailable:
            raise MLUnavailable("ML offline")
        if user_id not in PROFILES:
            raise ProfileNotFound()
        return ProfileDetails(user_id=user_id, profile=PROFILES[user_id],
            company_facts=[{"text": "Учебный факт о компании", "origin": "анкета"}],
            activity={"feed_status": "ready", "feed_total": 7, "insight_cards": 2})

    def feed(self, user_id, limit=20, offset=0):
        if self.unavailable:
            raise MLUnavailable("offline")
        return UserFeed(user_id=user_id, status="not_computed", items=[])

    def latest_job(self, user_id):
        if self.unavailable:
            raise MLUnavailable("offline")
        raise ProfileNotFound()


class DemoAuthTests(unittest.TestCase):
    def setUp(self):
        self.ml = FakeML()
        self.ml.unavailable = False
        app.dependency_overrides[get_ml_client] = lambda: self.ml
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        app.dependency_overrides.clear()

    def token(self, response=None):
        response = response or self.client.get("/login?switch=true")
        return re.search(r'name="csrf" value="([^"]+)"', response.text).group(1)

    def sign_in(self, user_id="u_01"):
        return self.client.post("/login", data={"user_id": user_id, "csrf": self.token()}, follow_redirects=False)

    def test_existing_profile_login_home_refresh_and_logout(self):
        self.assertEqual(self.client.get("/", follow_redirects=False).headers["location"], "/login")
        response = self.client.get("/login")
        self.assertIn("Компания Альфа", response.text)
        self.assertNotIn('type="password"', response.text)
        response = self.sign_in()
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/")
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertIn("SameSite=lax", response.headers["set-cookie"])
        for _ in range(2):
            page = self.client.get("/")
            self.assertEqual(page.status_code, 200)
            self.assertIn("Компания Альфа", page.text)
            self.assertIn("Учебный факт", page.text)
        self.assertEqual(page.headers["cache-control"], "no-store")
        response = self.client.post("/logout", data={"csrf": self.token(page)}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertNotIn(COOKIE, self.client.cookies)
        self.assertEqual(self.client.get("/", follow_redirects=False).headers["location"], "/login")

    def test_profile_can_be_switched(self):
        self.sign_in()
        page = self.client.get("/login?switch=true")
        response = self.client.post("/login", data={"user_id":"u_02", "csrf":self.token(page)})
        self.assertIn("Компания Бета", response.text)
        self.assertNotIn("Компания Альфа", response.text)

    def test_unknown_profile_cannot_sign_in(self):
        response = self.sign_in("not-existing")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(COOKIE, self.client.cookies)

    def test_missing_or_forged_form_token_is_rejected(self):
        self.client.get("/login")
        response = self.client.post("/login", data={"user_id":"u_01", "csrf":"forged"})
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(COOKIE, self.client.cookies)

    def test_forged_and_expired_sessions_cannot_open_home(self):
        self.client.cookies.set(COOKIE, _session.dumps({"user_id":"u_01"}) + "tampered")
        self.assertEqual(self.client.get("/", follow_redirects=False).headers["location"], "/login")
        from itsdangerous import SignatureExpired
        with patch("backend.auth._session.loads", side_effect=SignatureExpired("expired")):
            self.assertEqual(self.client.get("/", follow_redirects=False).headers["location"], "/login")

    def test_deleted_profile_session_is_cleared(self):
        self.sign_in()
        with patch.object(self.ml, "profile", side_effect=ProfileNotFound()):
            response = self.client.get("/", follow_redirects=False)
        self.assertEqual(response.headers["location"], "/login")
        self.assertNotIn(COOKIE, self.client.cookies)

    def test_ml_outage_is_visible_and_existing_session_survives(self):
        self.sign_in()
        self.ml.unavailable = True
        response = self.client.get("/")
        self.assertEqual(response.status_code, 503)
        self.assertIn("Данные временно недоступны", response.text)
        self.assertIn(COOKIE, self.client.cookies)
        response = self.client.get("/login?switch=true")
        self.assertEqual(response.status_code, 503)
        self.assertIn("Профили пока недоступны", response.text)
        self.assertNotIn('name="user_id"', response.text)

    def test_empty_profile_list_is_an_explicit_empty_state(self):
        with patch.object(self.ml, "profiles", return_value=ProfileList(total=0, items=[])):
            response = self.client.get("/login")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Пока нет доступных профилей", response.text)

    def test_profile_text_is_escaped_and_pagination_is_preserved(self):
        malicious = Profile(user_id="u_x", role="demo", company_name='<script>alert("x")</script>')
        with patch.object(self.ml, "profiles", return_value=ProfileList(total=120, items=[malicious])):
            response = self.client.get("/login?offset=50")
        self.assertNotIn('<script>alert', response.text)
        self.assertIn("&lt;script&gt;", response.text)
        self.assertIn("offset=100", response.text)
        self.assertIn("offset=0", response.text)

    def test_password_registration_is_not_exposed(self):
        self.assertEqual(self.client.get("/register").status_code, 404)
        self.assertEqual(self.client.get("/logout").status_code, 405)
        self.assertEqual(self.client.get("/insights/prototype", follow_redirects=False).headers["location"], "/login")


class MLClientTests(unittest.TestCase):
    def test_profile_list_uses_documented_contract(self):
        requested = []
        def transport(request):
            requested.append(str(request.url))
            return httpx.Response(200, json={"total":2,"items":[p.model_dump() for p in PROFILES.values()]})
        with httpx.Client(base_url="http://ml", transport=httpx.MockTransport(transport)) as http:
            self.assertEqual(MLClient(http).profiles(limit=50,offset=50).total, 2)
        self.assertIn("limit=50&offset=50", requested[0])

    def test_error_and_invalid_contract_are_not_used_as_profiles(self):
        for response in [httpx.Response(500), httpx.Response(200,json={"broken":True})]:
            with httpx.Client(base_url="http://ml", transport=httpx.MockTransport(lambda req:response)) as http:
                with self.assertRaises(MLUnavailable):
                    MLClient(http).profiles()
        with httpx.Client(base_url="http://ml", transport=httpx.MockTransport(lambda req:httpx.Response(404))) as http:
            with self.assertRaises(ProfileNotFound):
                MLClient(http).profile("missing")

    def test_profile_response_must_match_selected_user(self):
        with httpx.Client(base_url="http://ml", transport=httpx.MockTransport(lambda req:httpx.Response(200,
                json={"user_id":"u_02","profile":PROFILES["u_02"].model_dump()}))) as http:
            with self.assertRaises(MLUnavailable):
                MLClient(http).profile("u_01")
