import re
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

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


class DemoProfilesTests(unittest.TestCase):
    def setUp(self):
        self.ml = FakeML()
        self.ml.unavailable = False
        app.dependency_overrides[get_ml_client] = lambda: self.ml
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        app.dependency_overrides.clear()

    def test_home_opens_without_cookies_and_defaults_to_first_profile(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Компания Альфа", response.text)
        self.assertNotIn("set-cookie", response.headers)
        self.assertNotIn("/logout", response.text)
        self.assertNotIn('name="csrf"', response.text)
        self.assertFalse(self.client.cookies)

    def test_profile_selection_is_explicit_and_survives_refresh(self):
        response = self.client.get("/profiles")
        self.assertIn('method="get"', response.text)
        for _ in range(2):
            response = self.client.get("/?user_id=u_02")
            self.assertEqual(response.status_code, 200)
            self.assertIn("Компания Бета", response.text)
            self.assertIn('data-user-id="u_02"', response.text)
        self.assertFalse(self.client.cookies)

    def test_old_cookies_are_ignored(self):
        self.client.cookies.set("demo_session", "invalid")
        self.client.cookies.set("access_token", "invalid")
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_empty_profiles_and_deleted_profile(self):
        with patch.object(self.ml, "profiles", return_value=ProfileList(total=0, items=[])):
            response = self.client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertIn("Пока нет доступных профилей", response.text)
        self.assertEqual(self.client.get("/?user_id=missing").status_code, 404)

    def test_ml_outage_is_visible_without_redirect_loop(self):
        self.ml.unavailable = True
        self.assertEqual(self.client.get("/").status_code, 503)
        self.assertEqual(self.client.get("/?user_id=u_01").status_code, 503)
        self.assertFalse(self.client.cookies)

    def test_profile_list_escapes_html_and_has_pagination(self):
        profile = Profile(user_id="u_x", role="demo", company_name='<script>bad</script>')
        with patch.object(self.ml, "profiles", return_value=ProfileList(total=120, items=[profile])):
            response = self.client.get("/profiles?offset=50")
            self.assertNotIn("<script>bad", response.text)
            self.assertIn("/profiles?offset=100", response.text)

    def test_no_login_or_logout_actions(self):
        self.assertEqual(self.client.get("/login", follow_redirects=False).headers["location"], "/profiles")
        self.assertEqual(self.client.post("/login").status_code, 405)
        self.assertEqual(self.client.post("/logout").status_code, 404)


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
