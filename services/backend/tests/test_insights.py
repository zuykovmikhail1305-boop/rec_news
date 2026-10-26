import re
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

import test_web as web_tests
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


class FakeInsightsML(web_tests.FakeML):
    def __init__(self):
        super().__init__()
        self.generated = []
        self.limits = []
        self.targeted = []
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

    def generate_insights(self, user_id, *, news_ids=None, limit=None):
        if news_ids is not None: self.targeted.append((user_id,news_ids))
        self.limits.append(limit)
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

    def tearDown(self):
        self.client.close(); app.dependency_overrides.clear()

    def generate(self):
        return self.client.post('/api/insights/generate',params={"user_id":"u_01"})

    def test_home_shows_only_ready_insights_not_news_text(self):
        home='/?user_id='+web_tests.DEMO_ID
        response=self.client.get(home)
        self.assertEqual(response.status_code,200)
        self.assertNotIn('Синтетическая новость',response.text)   # пока карточки нет — ни текста новости, ни ссылки
        self.assertNotIn('Краткий текст новости',response.text)
        self.assertIn('live-insights.js',response.text)
        self.ml.ready=True
        response=self.client.get(home)
        self.assertIn('Стоимость финансирования останется высокой',response.text)
        self.assertIn('/insights/news_1?user_id='+web_tests.DEMO_ID, response.text)
        self.assertNotIn('Краткий текст новости',response.text)
        self.assertEqual(self.ml.generated,[])  # GET and polling do not create jobs.

    def test_generation_and_polling_use_explicit_profile(self):
        response=self.client.post('/api/insights/generate?user_id=u_02')
        self.assertEqual(response.status_code,202)
        self.assertEqual(self.ml.generated,['u_02'])
        self.assertEqual(self.ml.limits,[10])   # 10 инсайтов на пользователя
        data=self.client.get('/api/insights?user_id=u_02').json()
        self.assertEqual(data['user_id'],'u_02');self.assertTrue(data['poll'])
        self.ml.ready=True
        data=self.client.get('/api/insights?user_id=u_02').json()
        self.assertFalse(data['poll'])
        self.assertIn('Стоимость финансирования',data['html'])
        self.assertEqual(self.ml.generated,['u_02'])

    def test_full_card_all_blocks_sources_and_safe_links(self):
        self.ml.ready=True
        response=self.client.get('/insights/news_1?user_id=u_01')
        self.assertEqual(response.status_code,200)
        for text in ['Синтетический факт','Учебная позиция банка','Учебный вывод','Процентные расходы',
                     'Доход по депозитам','Это предположение','Проверьте структуру долга',
                     'Управление ликвидностью','Исходная публикация','Как получен вывод']:
            self.assertIn(text,response.text)
        self.assertIn('rel="noopener noreferrer"',response.text)
        self.assertIn('source-N1',response.text)

    def test_card_can_wait_then_become_ready_without_generation(self):
        self.ml.has_job=True
        response=self.client.get('/api/insights/news_1?user_id=u_01')
        self.assertTrue(response.json()['poll'])
        self.ml.ready=True
        response=self.client.get('/api/insights/news_1?user_id=u_01')
        self.assertFalse(response.json()['poll'])
        self.assertIn('что произошло',response.json()['html'])
        self.assertEqual(self.ml.generated,[])

    def test_failed_job_or_unknown_news_does_not_poll_forever(self):
        self.ml.has_job=True;self.ml.job_status='failed'
        self.assertFalse(self.client.get('/api/insights/news_1?user_id=u_01').json()['poll'])
        self.assertFalse(self.client.get('/api/insights/unknown?user_id=u_01').json()['poll'])

    def test_api_requires_profile_but_no_authentication_or_form_token(self):
        self.assertEqual(self.client.post('/api/insights/generate').status_code,422)
        self.assertEqual(self.client.get('/api/insights').status_code,422)
        self.assertEqual(self.client.get('/api/insights/news_1').status_code,422)
        self.assertEqual(self.generate().status_code,202)
        self.assertFalse(self.client.cookies)

    def test_uncomputed_feed_is_visible_and_does_not_start_global_processing(self):
        self.ml.computed=False
        self.assertEqual(self.generate().status_code,409)
        self.assertIn('Подбираем новости',self.client.get('/?user_id='+web_tests.DEMO_ID).text)

    def test_outage_has_retry_message_without_session(self):
        self.ml.unavailable=True
        self.assertEqual(self.client.get('/api/insights?user_id=u_01').status_code,503)
        self.assertEqual(self.client.get('/insights/news_1?user_id=u_01').status_code,503)
        self.assertFalse(self.client.cookies)

    def test_partial_card_omits_null_blocks_and_never_links_javascript(self):
        data={**CARD,'status':'partial','headline':None,'ai_conclusion':None,'bank_offer':None,
              'bank_view':{'available':False,'warning':'Аналитика банка отсутствует'},
              'evidence':{'sources':[{'id':'N1','title':'<script>unsafe</script>','url':'javascript:alert(1)','reliability_label':'Источник'}]}}
        with patch.object(self.ml,'insight',return_value=InsightCard.model_validate(data)):
            response=self.client.get('/insights/news_1?user_id=u_01')
        self.assertEqual(response.status_code,200)
        self.assertIn('Аналитика банка отсутствует',response.text)
        self.assertNotIn('href="javascript:',response.text)
        self.assertNotIn('<script>unsafe',response.text)
        self.assertNotIn('Учебный вывод',response.text)

    def test_opened_card_starts_only_requested_news(self):
        response = self.client.post('/api/insights/news_1/generate?user_id=u_02')
        self.assertEqual(response.status_code,202)
        self.assertEqual(self.ml.targeted,[('u_02',['news_1'])])
        self.assertFalse(response.json()['retry'])
        page = self.client.get('/insights/news_1?user_id=u_02')
        self.assertIn('data-retry-insights',page.text)

    def test_processing_wait_retries_without_starting_generation(self):
        with patch.object(self.ml,'processing_ready',return_value=False):
            for url in ('/api/insights/generate','/api/insights/news_1/generate'):
                response = self.client.post(url+'?user_id=u_01')
                self.assertEqual(response.status_code,409)
                self.assertTrue(response.json()['retry'])
        self.assertEqual(self.ml.generated,[])
        self.assertEqual(self.generate().status_code,202)

    def test_card_defers_if_other_job_does_not_cover_this_news(self):
        response = self.client.post('/api/insights/news_2/generate?user_id=u_01')
        self.assertTrue(response.json()['retry'])

    def test_missing_news_and_outage_do_not_retry_forever(self):
        for error,code in ((ProfileNotFound(),404),(MLUnavailable(),503)):
            with patch.object(self.ml,'generate_insights',side_effect=error):
                response=self.client.post('/api/insights/news_1/generate?user_id=u_01')
                self.assertEqual(response.status_code,code)
                self.assertFalse(response.json().get('retry',False))

    def test_done_job_card_is_displayed_when_personal_feed_is_not_computed(self):
        self.ml.ready=True;self.ml.has_job=True;self.ml.computed=False
        with patch.object(self.ml,'insight',wraps=self.ml.insight) as read:
            page=self.client.get('/?user_id='+web_tests.DEMO_ID)
            self.assertIn('Стоимость финансирования',page.text)
            self.assertIn('/insights/news_1?user_id='+web_tests.DEMO_ID,page.text)
            read.assert_called_with(web_tests.DEMO_ID,'news_1')
        data=self.client.get('/api/insights?user_id=u_02').json()
        self.assertTrue(data['recovered_cards'])
        self.assertIn('Синтетический факт',data['html'])
        self.assertEqual(self.ml.generated,[])

    def test_done_job_does_not_claim_card_exists_when_card_endpoint_returns_404(self):
        self.ml.ready=True;self.ml.has_job=True;self.ml.computed=False
        with patch.object(self.ml,'insight',side_effect=ProfileNotFound()):
            data=self.client.get('/api/insights?user_id=u_01').json()
        self.assertFalse(data['recovered_cards'])
        self.assertNotIn('Открыть инсайт',data['html'])
        self.assertNotIn('Инсайты готовы',data['html'])

    def test_empty_ready_feed_also_reads_actual_completed_card(self):
        self.ml.ready=True;self.ml.has_job=True
        with patch.object(self.ml,'feed',return_value=UserFeed(user_id='u_01',status='ready',items=[])):
            data=self.client.get('/api/insights?user_id=u_01').json()
        self.assertTrue(data['recovered_cards'])
        self.assertIn('Стоимость финансирования',data['html'])


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


class ProcessingClientTests(unittest.TestCase):
    def test_processing_and_targeted_generation_contract(self):
        import json
        requests=[]
        def handler(request):
            requests.append(request)
            if request.url.path == '/processing.enqueue':
                return httpx.Response(202,json={'status':'accepted','news':2})
            if request.url.path == '/processing.result':
                return httpx.Response(200,json={'result':True})
            return httpx.Response(202,json={'job_id':'j','user_id':'u_01','status':'queued','total':1})
        with httpx.Client(base_url='http://ml',transport=httpx.MockTransport(handler)) as http:
            client=MLClient(http)
            self.assertEqual(client.enqueue_processing().news,2)
            self.assertTrue(client.processing_ready())
            client.generate_insights('u_01',news_ids=['news_1'])
        self.assertEqual(json.loads(requests[-1].content),{'refresh':False,'news_ids':['news_1']})
