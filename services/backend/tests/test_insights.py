import re
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

import test_web as auth_tests
from backend.main import app
from backend.ml_client import (InsightCard, InsightJob, MLClient, MLUnavailable, MLNotReady,
    ProfileNotFound, UserFeed, get_ml_client)


CARD = {
    "card_id":"ic_1", "user_id":"u_01", "news_id":"news_1", "status":"ready",
    "generated_at":"2026-10-24T12:00:00Z",
    "header":{key:{"value":value,"label":label} for key,value,label in [
        ("impact","risk","Риск"),("priority","high","Высокий приоритет"),
        ("horizon","1-3m","1–3 месяца"),("reliability","confirmed","Подтверждено")]},
    "headline":{"text":"Стоимость финансирования останется высокой","company":"Компания Альфа","role":"собственник бизнеса","source_ids":["N1"]},
    "fact":{"text":"Синтетический факт о ставке.","decided":["Ставка сохранена"],"discussed":["Будущая динамика"],"key_figures":[{"value":"14%","label":"Ключевая ставка","source_ids":["N1"]}],"source_ids":["N1"]},
    "bank_view":{"available":True,"text":"Учебная позиция банка.","material_title":"Обзор ставок","url":"https://example.test/bank","source_ids":["A1"]},
    "ai_conclusion":{"text":"Учебный вывод для вашей компании.","company_features":[{"id":"F1","text":"Есть кредиты"}],"channels":["кредиты"],"source_ids":["N1","A1"]},
    "risks":{"risks":[{"text":"Процентные расходы","source_ids":["N1"]}],"opportunities":[{"text":"Доход по депозитам"}]},
    "hypothesis":{"text":"Если ставка сохранится, расходы останутся высокими.","disclaimer":"Это предположение, а не прогноз."},
    "actions":{"steps":["Проверьте структуру долга"],"question":"Нужна ли фиксация ставки?"},
    "bank_offer":{"name":"Управление ликвидностью","description":"Учебный продукт","reason":"Для свободных средств","cta":{"label":"Обсудить с менеджером","action":"contact_manager"}},
    "evidence":{"sources":[{"id":"N1","title":"Исходная публикация","url":"https://example.test/news","outlet":"Учебный источник","reliability_label":"Высокая надёжность","quote":"Ставка сохранена"}],"analytics_material":{"id":"A1","title":"Материал банка","url":"https://example.test/bank","reliability_label":"Аналитика"},"company_data":[{"id":"F1","text":"Компания использует кредиты"}],"trace":[{"step":1,"title":"Сопоставление","detail":"Учтены признаки компании"}]},
    "verification":{"verdict":"passed","blocks":{}},
}
CARD["header"]["sources_count"] = 2


class FakeInsightsML(auth_tests.FakeML):
    def __init__(self):
        self.generated = []
        self.ready = False
        self.has_job = False
        self.computed = True
        self.unavailable = False
        self.job_status = "running"

    def job(self, user_id):
        return InsightJob(job_id="ij_test",user_id=user_id,status="done" if self.ready else self.job_status,
            total=1,done=1 if self.ready else 0,items=[{"news_id":"news_1","status":"done" if self.ready else ("failed" if self.job_status=="failed" else "running")}])

    def feed(self, user_id, limit=20, offset=0):
        if self.unavailable: raise MLUnavailable("offline")
        item={"news_id":"news_1","title":"Синтетическая новость","lead":"Краткий текст новости","source":"Учебный источник","published_at":"2026-10-24T12:00:00Z","why_relevant":"Важно из-за кредитов"}
        if self.ready:
            item["insight"]={"card_id":"ic_1","status":"ready","headline":"Стоимость финансирования останется высокой","impact":"risk","priority":"high","horizon":"1-3m","reliability":"confirmed"}
        return UserFeed(user_id=user_id,status="ready" if self.computed else "not_computed",total=1 if self.computed else 0,items=[item] if self.computed else [])

    def generate_insights(self, user_id):
        self.generated.append(user_id)
        if not self.computed: raise MLNotReady()
        self.has_job = True
        return self.job(user_id)

    def latest_job(self, user_id):
        if self.unavailable: raise MLUnavailable("offline")
        if not self.has_job: raise ProfileNotFound()
        return self.job(user_id)

    def insight(self, user_id, news_id):
        if self.unavailable: raise MLUnavailable("offline")
        if not self.ready or news_id != "news_1": raise ProfileNotFound()
        return InsightCard.model_validate({**CARD,"user_id":user_id})


class InsightRoutesTests(unittest.TestCase):
    def setUp(self):
        self.ml=FakeInsightsML()
        app.dependency_overrides[get_ml_client]=lambda:self.ml
        self.client=TestClient(app)
        page=self.client.get('/login')
        self.csrf=re.search(r'name="csrf" value="([^"]+)"',page.text).group(1)
        self.client.post('/login',data={"user_id":"u_01","csrf":self.csrf},follow_redirects=False)

    def tearDown(self):
        self.client.close(); app.dependency_overrides.clear()

    def generate(self):
        return self.client.post('/api/insights/generate',data={"csrf":self.csrf})

    def test_home_shows_feed_and_auto_generation_script(self):
        response=self.client.get('/')
        self.assertEqual(response.status_code,200)
        self.assertIn('Синтетическая новость',response.text)
        self.assertIn('live-insights.js',response.text)
        self.assertEqual(self.ml.generated,[])  # GET and polling do not create jobs.

    def test_generation_and_polling_use_only_signed_session_user(self):
        response=self.client.post('/api/insights/generate?user_id=u_02',data={"csrf":self.csrf})
        self.assertEqual(response.status_code,202)
        self.assertEqual(self.ml.generated,['u_01'])
        data=self.client.get('/api/insights?user_id=u_02').json()
        self.assertEqual(data['user_id'],'u_01');self.assertTrue(data['poll'])
        self.ml.ready=True
        data=self.client.get('/api/insights').json()
        self.assertFalse(data['poll'])
        self.assertIn('Стоимость финансирования',data['html'])
        self.assertEqual(self.ml.generated,['u_01'])

    def test_full_card_all_blocks_sources_and_safe_links(self):
        self.ml.ready=True
        response=self.client.get('/insights/news_1')
        self.assertEqual(response.status_code,200)
        for text in ['Синтетический факт','Учебная позиция банка','Учебный вывод','Процентные расходы',
                     'Доход по депозитам','Это предположение','Проверьте структуру долга',
                     'Управление ликвидностью','Исходная публикация','Как получен вывод']:
            self.assertIn(text,response.text)
        self.assertIn('rel="noopener noreferrer"',response.text)
        self.assertIn('source-N1',response.text)

    def test_card_can_wait_then_become_ready_without_generation(self):
        self.ml.has_job=True
        response=self.client.get('/api/insights/news_1')
        self.assertTrue(response.json()['poll'])
        self.ml.ready=True
        response=self.client.get('/api/insights/news_1')
        self.assertFalse(response.json()['poll'])
        self.assertIn('Что произошло',response.json()['html'])
        self.assertEqual(self.ml.generated,[])

    def test_failed_job_or_unknown_news_does_not_poll_forever(self):
        self.ml.has_job=True;self.ml.job_status='failed'
        self.assertFalse(self.client.get('/api/insights/news_1').json()['poll'])
        self.assertFalse(self.client.get('/api/insights/unknown').json()['poll'])

    def test_generation_requires_csrf_and_authentication(self):
        self.assertEqual(self.client.post('/api/insights/generate',data={"csrf":"forged"}).status_code,403)
        self.client.cookies.clear()
        self.assertEqual(self.generate().status_code,401)
        self.assertEqual(self.client.get('/api/insights').status_code,401)
        self.assertEqual(self.client.get('/api/insights/news_1').status_code,401)
        self.assertEqual(self.client.get('/insights/news_1',follow_redirects=False).headers['location'],'/login')

    def test_uncomputed_feed_is_visible_and_does_not_start_global_processing(self):
        self.ml.computed=False
        self.assertEqual(self.generate().status_code,409)
        self.assertIn('Лента пока не подготовлена',self.client.get('/').text)

    def test_outage_keeps_session_and_has_retry_message(self):
        self.ml.unavailable=True
        self.assertEqual(self.client.get('/api/insights').status_code,503)
        self.assertEqual(self.client.get('/insights/news_1').status_code,503)
        from backend.auth import COOKIE
        self.assertIn(COOKIE,self.client.cookies)

    def test_partial_card_omits_null_blocks_and_never_links_javascript(self):
        data={**CARD,'status':'partial','headline':None,'ai_conclusion':None,'bank_offer':None,
              'bank_view':{'available':False,'warning':'Аналитика банка отсутствует'},
              'evidence':{'sources':[{'id':'N1','title':'<script>unsafe</script>','url':'javascript:alert(1)','reliability_label':'Источник'}]}}
        with patch.object(self.ml,'insight',return_value=InsightCard.model_validate(data)):
            response=self.client.get('/insights/news_1')
        self.assertEqual(response.status_code,200)
        self.assertIn('Аналитика банка отсутствует',response.text)
        self.assertNotIn('href="javascript:',response.text)
        self.assertNotIn('<script>unsafe',response.text)
        self.assertNotIn('Учебный вывод',response.text)


class InsightClientTests(unittest.TestCase):
    def test_generation_contract_is_async_and_does_not_force_refresh(self):
        import json
        requests=[]
        def handler(request):
            requests.append(request)
            return httpx.Response(202,json={"job_id":"ij_1","user_id":"u_01","status":"queued","total":1})
        with httpx.Client(base_url='http://ml',transport=httpx.MockTransport(handler)) as http:
            job=MLClient(http).generate_insights('u_01')
        self.assertEqual(job.job_id,'ij_1')
        self.assertEqual(requests[0].url.path,'/users/u_01/insights/generate')
        self.assertEqual(json.loads(requests[0].content),{'refresh':False})

    def test_foreign_feed_job_and_card_are_rejected(self):
        for method,payload in [('feed',{'user_id':'u_02','status':'ready','items':[]}),
                               ('latest_job',{'job_id':'ij','user_id':'u_02','status':'done','total':0}),
                               ('insight',{**CARD,'user_id':'u_02'})]:
            with httpx.Client(base_url='http://ml',transport=httpx.MockTransport(lambda req:httpx.Response(200,json=payload))) as http:
                with self.assertRaises(MLUnavailable):
                    getattr(MLClient(http),method)('u_01',*(['news_1'] if method=='insight' else []))
