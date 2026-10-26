import re
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from backend import routes as demo_routes
from backend.main import app
from backend.ml_client import (MLClient, MLUnavailable, Profile, ProfileDetails,
                               ProfileList, ProfileNotFound, UserFeed, get_ml_client)


PROFILES = {
    "u_01": Profile(user_id="u_01", role="собственник бизнеса", company_name="Компания Альфа", industry="Торговля",
                    company_id="U_1"),
    "u_02": Profile(user_id="u_02", role="финансовый директор", company_name="Компания Бета", industry="Строительство",
                    company_id="U_2"),
    "u_07": Profile(user_id="u_07", role="коммерческий директор", company_name="Компания Гамма", industry="Логистика",
                    company_id="U_7"),
}
DEMO_ID = "demo_cfo_build"   # финансовый директор + компания профиля u_02


class FakeML:
    unavailable = False

    def __init__(self):
        self.built, self.rebuilt = [], []

    def processing_ready(self):
        if self.unavailable: raise MLUnavailable("offline")
        return True

    def profiles(self, limit=50, offset=0):
        if self.unavailable:
            raise MLUnavailable("ML offline")
        return ProfileList(total=len(PROFILES), items=list(PROFILES.values())[offset:offset+limit])

    def profile(self, user_id):
        if self.unavailable:
            raise MLUnavailable("ML offline")
        profile = PROFILES.get(user_id)
        if profile is None and user_id.startswith("demo_"):
            profile = PROFILES["u_02"].model_copy(update={"user_id": user_id, "role": "финансовый директор"})
        if profile is None:
            raise ProfileNotFound()
        return ProfileDetails(user_id=user_id, profile=profile,
            company_facts=[{"text": "Учебный факт о компании", "origin": "анкета"}],
            activity={"feed_status": "ready", "feed_total": 7, "insight_cards": 2})

    def ensure_profile(self, user_id, *, role, company):
        if self.unavailable: raise MLUnavailable("offline")
        self.built.append((user_id, role, company.company_id))
        return self.profile(user_id)

    def rebuild_news(self, user_id):
        if self.unavailable: raise MLUnavailable("offline")
        self.rebuilt.append(user_id)

    def feed(self, user_id, limit=20, offset=0):
        if self.unavailable:
            raise MLUnavailable("offline")
        return UserFeed(user_id=user_id, status="not_computed", items=[])

    def latest_job(self, user_id):
        if self.unavailable:
            raise MLUnavailable("offline")
        raise ProfileNotFound()


class DemoLoginTests(unittest.TestCase):
    def setUp(self):
        demo_routes._companies.clear()
        self.ml = FakeML()
        app.dependency_overrides[get_ml_client] = lambda: self.ml
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        app.dependency_overrides.clear()

    def test_login_page_has_three_users_and_three_companies(self):
        for url in ("/", "/profiles", "/?user_id=u_02"):   # без демо-профиля — страница входа
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.text.count('class="card login-user"'), 3)
            self.assertEqual(response.text.count('name="company"'), 9)
            for name in ("Компания Альфа", "Компания Бета", "Компания Гамма", "финансовый директор"):
                self.assertIn(name, response.text)
        self.assertFalse(self.client.cookies)

    def test_enter_builds_profile_and_rebuilds_news_only_for_user(self):
        response = self.client.get("/enter", params={"user": "cfo", "company": "build"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/?user_id=" + DEMO_ID)
        self.assertEqual(self.ml.built, [(DEMO_ID, "финансовый директор", "U_2")])
        self.assertEqual(self.ml.rebuilt, [DEMO_ID])

    def test_enter_rejects_unknown_choice(self):
        self.assertEqual(self.client.get("/enter", params={"user": "x", "company": "build"}).status_code, 400)
        self.assertEqual(self.ml.built, [])

    def test_home_of_demo_profile_is_explicit_and_survives_refresh(self):
        for _ in range(2):
            response = self.client.get("/?user_id=" + DEMO_ID)
            self.assertEqual(response.status_code, 200)
            self.assertIn("Компания Бета", response.text)
            self.assertIn(f'data-user-id="{DEMO_ID}"', response.text)
        self.assertFalse(self.client.cookies)

    def test_ml_outage_is_visible_without_redirect_loop(self):
        self.ml.unavailable = True
        self.assertEqual(self.client.get("/profiles").status_code, 503)
        self.assertEqual(self.client.get("/?user_id=" + DEMO_ID).status_code, 503)
        self.assertEqual(self.client.get("/enter", params={"user": "cfo", "company": "build"}).status_code, 503)
        self.assertFalse(self.client.cookies)

    def test_company_names_are_escaped(self):
        bad = PROFILES["u_01"].model_copy(update={"company_name": "<script>bad</script>"})
        with patch.dict(PROFILES, {"u_01": bad}):
            self.assertNotIn("<script>bad", self.client.get("/profiles").text)

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


class DemoClientTests(unittest.TestCase):
    def test_profile_and_rebuild_contract(self):
        import json
        requests = []
        def handler(request):
            requests.append(request)
            if request.url.path.startswith("/profiles/"):
                return httpx.Response(200, json={"user_id": DEMO_ID, "profile": {**PROFILES["u_02"].model_dump(), "user_id": DEMO_ID}})
            if request.url.path.endswith("/news.rebuild"):
                return httpx.Response(202, json={"run_id": "run_1", "status": "queued"})
            return httpx.Response(202, json={"job_id": "j", "user_id": DEMO_ID, "status": "queued", "total": 10})
        with httpx.Client(base_url="http://ml", transport=httpx.MockTransport(handler)) as http:
            client = MLClient(http)
            client.ensure_profile(DEMO_ID, role="финансовый директор", company=PROFILES["u_02"])
            self.assertEqual(client.rebuild_news(DEMO_ID).run_id, "run_1")
            client.generate_insights(DEMO_ID, limit=10)
        self.assertEqual((requests[0].method, requests[0].url.path), ("PUT", "/profiles/" + DEMO_ID))
        self.assertEqual(json.loads(requests[0].content)["company_id"], "U_2")
        self.assertEqual(requests[1].url.path, f"/users/{DEMO_ID}/news.rebuild")
        self.assertEqual(json.loads(requests[2].content), {"refresh": False, "limit": 10})
