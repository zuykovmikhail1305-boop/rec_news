# API ML-сервиса «Новостная аналитика»

Справочник по ручкам и контрактам для бэкенда и фронтенда.

| Документ | Что в нём |
|---|---|
| `docs/architecture.md` | как устроены и работают компоненты ML-части |
| `docs/openapi.json` | машиночитаемый контракт; обновляется командой `python scripts/export_openapi.py` |
| `docs/insight_card.md` | подробно о карточке инсайта: как строится и как показывать блоки |
| `docs/insight_jobs.md` | задания на генерацию карточек и очередь задач |
| `docs/news_db.md` | базы и колонки |

---

## 1. Общие правила

**Адрес.**
- Запуск из `services/ml`: `.venv/Scripts/python scripts/run_api.py --port 8002`. Без `--port` — `8000`, но его занимает веб-сервис (`services/web`), парсер — `8001`. Хост и порт задаются `--host` и `--port` или переменными `REC_NEWS_API_HOST`, `REC_NEWS_API_PORT`.
- Интерактивная документация: `/docs` (Swagger) и `/redoc`.
- Схема OpenAPI: `/openapi.json`.

**Формат.**
- Тела запросов и ответов — JSON в UTF-8.
- Авторизации нет: сервис внутренний, его вызывает только бэкенд.

**Время.** Все поля дат — ISO 8601. Время, которое ставит сам сервис, — UTC, например `2026-10-23T15:20:11Z`.

**Идентификаторы** задаёт бэкенд: `user_id`, `news_id` (ID новости у источника), `analytics_id`. Сервис добавляет свои:

| ID | Вид | Что это |
|---|---|---|
| `event_id` | `e_<news_id представителя>` | событие: новость и все её дубли и пересказы |
| `run_id` | `run_20261023_151502_a1b2c3` | запуск обработки |
| `job_id` | `ij_20261023_160501_a1b2c3` | задание на генерацию карточек |
| `card_id` | `ic_…` | построенная карточка; меняется при каждом перестроении |

**Ошибки.** Ответ с кодом 4xx или 5xx имеет вид `{"detail": "текст на русском"}`.

| Код | Когда |
|---|---|
| `404` | нет профиля, новости, запуска или задания |
| `409` | у пользователя ещё нет подобранных новостей |
| `422` | тело или параметры не прошли проверку. Формат FastAPI: `{"detail": [{"loc": [...], "msg": "...", "type": "..."}]}` |
| `503` | сервис ещё запускается |

В `openapi.json` объявлены только успешные ответы и `422`. Коды `404`, `409` и `503` в схеме не описаны,
но возвращаются, как указано у каждой ручки.

**Долгие операции** не держат соединение: ручка отвечает `202` и возвращает объект с ID, ход работы читается отдельным `GET`. Так устроены обработка новостей (`/processing.enqueue`, готовность — `/processing.result`) и генерация карточек (`/insights/generate`).

Синхронно и долго (до минуты-полутора) работают `POST /users/{id}/news/{news_id}/insight` для новой карточки и `POST /chat/{user_id}`: таймаут на стороне бэкенда для них — не меньше 120 с.

**Совместимость.**
- В ответы могут добавляться новые поля: клиент должен игнорировать незнакомые.
- Поле, равное `null`, означает «нет данных» или «блок не построен». Это не ошибка.
- `pipeline_version` в ответах — версия ML-конвейера, которой посчитаны данные.

**Без LLM** (`REC_NEWS_USE_LLM=0` или модель недоступна) все ручки продолжают работать в упрощённом режиме. Ответы помечаются статусами: `degraded`, `fallback`, `llm_available: false`.

---

## 2. Основной сценарий бэкенда

```
1. POST /processing.enqueue                обработка новостей из БД: новости → события → аналитика → ленты
2. GET  /processing.result                 ждать result = true
3. GET  /users/{user_id}/news              лента пользователя с аналитикой банка
4. POST /users/{user_id}/insights/generate карточки инсайтов для всех новостей ленты (в фоне)
5. GET  /insights/jobs/{job_id}            ход генерации; готовые карточки сразу видны в ленте (поле insight)
6. GET  /users/{user_id}/news/{news_id}/insight   полная карточка по новости
7. POST /chat/{user_id}                    сообщение пользователя в его чат с агентом
   GET  /chat/{user_id}                    история чата (при открытии экрана чата)
```

В API только ручки обработки ML. Новости и профили пользователей попадают в базы ML-части не через API:
новости — в `data/news.db` со статусом `new` (`news_pipeline/storage/news_db.py`, `insert_news`), профили —
в `data/profiles.db` (`MLService.build_profile`, скрипты `scripts/bootstrap_demo.py`).

Обработку запускает только бэкенд. Новости, пришедшие во время обработки, попадут в следующий
запуск. Автоматический фоновый запуск каждые 10 с включается переменной `REC_NEWS_PIPELINE_WORKER=1`.

---

## 3. Ручки

### Сводка: что принимает и что возвращает каждая ручка

| Ручка | Вход | Успешный ответ | Ошибки |
|---|---|---|---|
| `POST /processing.enqueue` | — | `202` [`Enqueued`](#enqueued) `{"status", "news"}` | — |
| `GET /processing.result` | — | `200` [`ProcessingResult`](#processingresult) `{"result": bool}` | — |
| `GET /profiles` | query: `limit`=100 (1–1000), `offset`=0 | `200` [`ProfileList`](#profilelist) | — |
| `GET /profiles/{user_id}` | — | `200` [`ProfileDetails`](#profiledetails) | `404` нет профиля |
| `GET /users/{user_id}/news` | query: `limit`=20, `offset`=0, `with_text`=false | `200` [`UserNewsResponse`](#usernewsresponse) | `404` нет профиля |
| `POST /users/{user_id}/news/{news_id}/insight` | query: `refresh`=false | `200` [`InsightCard`](#insightcard) | `404` нет профиля или новости |
| `GET /users/{user_id}/news/{news_id}/insight` | — | `200` [`InsightCard`](#insightcard) | `404` карточки ещё нет |
| `POST /users/{user_id}/insights/generate` | тело [`InsightJobRequest`](#insightjobrequest), необязательно | `202` [`InsightJob`](#insightjob); `200` — задание уже идёт | `404` нет профиля или новостей; `409` нет ленты |
| `GET /insights/jobs/{job_id}` | — | `200` [`InsightJob`](#insightjob) | `404` |
| `GET /users/{user_id}/insights/jobs/latest` | — | `200` [`InsightJob`](#insightjob) | `404` заданий не было |
| `POST /insights/jobs/{job_id}/cancel` | — | `200` [`InsightJob`](#insightjob) | `404` |
| `POST /chat/{user_id}` | тело [`UserChatMessage`](#userchatmessage) | `200` [`AgentAnswer`](#agentanswer) | `404` нет профиля; `422` пустое или длинное сообщение |
| `GET /chat/{user_id}` | query: `limit`=50 | `200` [`ChatHistory`](#chathistory) | `404` нет профиля |

### 3.1. Обработка

#### `POST /processing.enqueue` — обработать новости из базы
- Берёт из `data/news.db` новости со статусом `new` и обрабатывает в фоне: подготовка → дедупликация
  и события → подбор аналитики (статус новости `new → processing → ready`) → пересчёт лент всех
  пользователей.
- Тела нет. Ответ: `202` [`Enqueued`](#enqueued) — `{"status": "accepted", "news": 2}`, где `news` — сколько новостей ждало обработки.
- Если обработка уже стоит в очереди или идёт, новая не запускается: `status = "already_running"`.
- Ручка ставит задачу в очередь `processing.run` и сразу отвечает; обработку выполняет воркер очереди
  (сейчас — внутри процесса ML-сервиса, позже — Celery, см. `docs/insight_jobs.md`).
- Время: этапы новостей — секунды-минуты, пересчёт лент — около 30 с на пользователя.

#### `GET /processing.result` — готова ли обработка
- Ответ: `200` [`ProcessingResult`](#processingresult) — `{"result": true}` или `{"result": false}`.
- `true` — в базе есть новости со статусом `ready` и обработка не стоит в очереди и не идёт (ленты пересчитаны). Состояние читается из базы, поэтому ответ верный и при воркерах в других процессах.
- `false` — готовых новостей нет или обработка ещё идёт. Опрашивать раз в 5–10 с после `processing.enqueue`.

### 3.2. Профили пользователей

Профили, по которым ML-часть строит ленты, карточки и чат. Создаются не через API (`MLService.build_profile`,
скрипты); ручки только читают их.

#### `GET /profiles` — список профилей
- Параметры: `limit` (1–1000, по умолчанию 100), `offset` (0). Порядок — по `user_id`.
- Ответ: `200` [`ProfileList`](#profilelist).

```json
{"total": 25,
 "items": [{"user_id": "u_01", "role": "собственник бизнеса", "company_name": "Компания U_57706",
            "industry": "оптовая торговля топливом, металлами, химией и стройматериалами", "company_id": "U_57706"}]}
```

#### `GET /profiles/{user_id}` — данные пользователя
- Ответ: `200` [`ProfileDetails`](#profiledetails): анкета, профиль интересов, данные компании, что уже посчитано.
- Ошибка: `404` — профиля нет.
- Отвечает за сотые доли секунды: данные компании считаются по датасету при старте сервиса заранее и
  кэшируются; для профиля, добавленного позже, первый запрос — около 2 с.

```json
{
  "user_id": "u_01",
  "profile": {"user_id": "u_01", "role": "собственник бизнеса", "company_name": "Компания U_57706",
              "industry": "…", "company_description": "…", "company_id": "U_57706", "regions": [],
              "products": [], "competitors": [], "counterparties": [], "company_size": null},
  "interests": {
    "source": "llm", "pipeline_version": "0.1.0",
    "aspects": [{"aspect_id": "a1", "title": "Цены на топливо и нефтепродукты", "description": "…",
                 "examples": ["…"], "keywords": ["…"], "entities": ["…"], "topic": "нефть и газ", "weight": 0.9}],
    "entities": {"Компания U_57706": 1.0, "Центральная Азия": 0.6, "СПбМТСБ": 0.5},
    "topic_weights": {"нефть и газ": 1.0, "металлургия": 1.0}
  },
  "company_facts": [
    {"id": "F1", "text": "отрасль — оптовая торговля топливом, металлами, химией и стройматериалами", "kind": "industry", "origin": "анкета"},
    {"id": "F12", "text": "75% поступлений — от одного покупателя", "kind": "concentration", "origin": "платежи"}
  ],
  "activity": {"feed_status": "ready", "feed_total": 20, "feed_computed_at": "2026-10-23T12:26:44Z",
               "insight_cards": 2, "chat_session_id": "c_8ee28dd2f9db"}
}
```

### 3.3. Лента пользователя

#### `GET /users/{user_id}/news` — новости пользователя с аналитикой
Готовая таблица последней обработки. Отвечает сразу.

| Параметр | Тип | По умолчанию | Что значит |
|---|---|---|---|
| `limit` | int | 20 | сколько новостей |
| `offset` | int | 0 | сдвиг |
| `with_text` | bool | false | полный текст новости и фрагмента аналитики |

- Ответ: `200` [`UserNewsResponse`](#usernewsresponse). Если `status = not_computed`, для пользователя ещё не было обработки.
- Ошибка: `404` — профиля нет.

```json
{
  "user_id": "u_03", "status": "ready", "run_id": "run_20261024_113404_2219a4",
  "computed_at": "2026-10-24T11:47:10Z", "pipeline_version": "0.1.0", "total": 20,
  "items": [{
    "rank": 1, "news_id": "syn_bonds_0", "event_id": "e_syn_bonds_0",
    "title": "ЦБ сохранил ставку 14%: доходность ОФЗ пробилась выше 15,8%…", "lead": "…", "text": null,
    "source": "rbc.example", "url": "https://rbc.example/news/bonds_0", "published_at": "2026-10-19T09:10:00Z",
    "sources": ["rbc.example", "tass.example"], "topics": ["денежно-кредитная политика", "облигации"],
    "importance": 5, "score": 0.83, "why_relevant": "Для компании это значит, что кредиты не подешевеют…",
    "analytics": {"analytics_id": "ALFA-Macro_Insights-Policy_Rate_11Sep", "chunk_id": "…",
                  "title": "Ключевая ставка ЦБ РФ: ставка без изменения на 14,0%…", "section_title": "…",
                  "published_at": "2026-09-11T18:00:00+03:00", "relation": "прогноз", "contradicts": false,
                  "summary": "Аналитики Альфа-Банка ожидают…", "text": null, "url": "…"},
    "insight": {"card_id": "ic_36f2fe79e8f4", "status": "ready",
                "headline": "Стоимость вашего долга и закупок не снизится до конца года",
                "impact": "risk", "priority": "high", "horizon": "1-3m", "reliability": "confirmed"},
    "signals": {"sem": 0.71, "ent": 0.5, "topic": 0.9, "imp": 1.0, "fresh": 0.4, "retrieval": 0.68, "llm": 3, "final": 0.83}
  }]
}
```

### 3.4. Карточки инсайтов

Что новость значит для компании пользователя, разбитое на блоки: шапка с чипами, главная фраза, факт,
позиция Альфа-Банка, вывод AI, риски и возможности, гипотеза, что можно сделать, предложение банка,
источники. Подробно — `docs/insight_card.md`.

#### `POST /users/{user_id}/news/{news_id}/insight` — построить или получить карточку
- Готовая карточка возвращается сразу, если не менялись её входные данные: анкета, публикации, аналитика, версии промптов. Новая строится 20–80 с.
- Параметр `refresh` (bool) — построить заново.
- Ответ: `200` [`InsightCard`](#insightcard).
- Ошибка: `404` — нет профиля или новости среди обработанных.

#### `GET /users/{user_id}/news/{news_id}/insight` — только готовая карточка
- Ответ: `200` [`InsightCard`](#insightcard).
- Ошибка: `404` — карточки ещё нет.

Структура ответа обеих ручек (поле с `?` может быть `null` — блок не построен или не нужен):

```
card_id, user_id, news_id, event_id?, status (ready|partial|degraded), generated_at, pipeline_version
header         impact / priority / horizon / reliability: {value, label, reason?}, sources_count
headline?      text, company?, role, event_date?, aspect?, company_features[{id, text}], source_ids[], based_on_analytics
fact           text, decided[], discussed[], key_figures[{value, label, source_ids[]}], source_ids[]
bank_view      available, text?, material_title?, material_date?, section_title?, url?, relation?, contradicts,
               source_ids[], warning?            ← available=false: вместо блока показать warning
ai_conclusion? text, channels[], company_features[{id, text}], source_ids[]
risks?         risks[{text, source_ids[], feature_ids[]}], opportunities[{…}]
hypothesis?    condition, consequence, text («Если …, то …»), disclaimer
actions?       steps[], question
bank_offer?    product_id, name, description, reason, risk_index?, cta{label, action}
evidence       sources[{id, type, title, outlet?, url?, published_at?, reliability, reliability_label, quote?}],
               analytics_material?, company_data[{id, text}], trace[{step, title, detail}]
verification   verdict, rounds, comment?, blocks{имя блока: {status, attempts, model?, issues[{code, severity, message, by}]}}
timings_ms     {этап: мс}
```

Пример карточки целиком — в `docs/insight_card.md`.

### 3.5. Задания на генерацию карточек

#### `POST /users/{user_id}/insights/generate` — карточки для всех новостей пользователя
- Работает в фоне через очередь задач.
- Тело необязательно: [`InsightJobRequest`](#insightjobrequest).
- Ответ: `202` [`InsightJob`](#insightjob). Если у пользователя задание уже идёт, ответ `200` с текущим заданием.
- Ошибки:
  - `404` — нет профиля или новости из `news_ids`;
  - `409` — у пользователя ещё нет подобранных новостей, сначала нужен `/processing.enqueue`.

#### `GET /insights/jobs/{job_id}` — статус задания и каждой карточки
- Ответ: `200` [`InsightJob`](#insightjob).
- Ошибка: `404`.

```json
{"job_id": "ij_20261024_160501_a1b2c3", "user_id": "u_01", "status": "running", "refresh": false,
 "created_at": "2026-10-24T16:05:01Z", "started_at": "2026-10-24T16:05:02Z", "finished_at": null,
 "total": 20, "queued": 16, "running": 2, "done": 2, "failed": 0, "cancelled": 0,
 "items": [{"news_id": "syn_stock_market_5", "position": 1, "status": "done", "card_id": "ic_…",
            "card_status": "ready", "headline": "…", "error": null,
            "started_at": "2026-10-24T16:05:02Z", "finished_at": "2026-10-24T16:05:41Z"}]}
```

Тот же объект возвращают `POST …/insights/generate`, `GET …/insights/jobs/latest` и `POST …/cancel`.

#### `GET /users/{user_id}/insights/jobs/latest` — последнее задание пользователя
- Ответ: `200` [`InsightJob`](#insightjob).
- Ошибка: `404`.

#### `POST /insights/jobs/{job_id}/cancel` — отменить задание
- Ещё не начатые карточки не строятся, начатые достраиваются.
- Ответ: `200` [`InsightJob`](#insightjob).
- Ошибка: `404`.

### 3.6. Чат пользователя с агентом

У каждого пользователя свой чат. Бэкенду не нужно хранить ID диалога: ML-сервис сам ведёт текущий
диалог пользователя, агент помнит последние реплики и источники, на которые ссылался. Агент отвечает
по новостям ленты пользователя (той же, что в `GET /users/{id}/news`), поиску по базе новостей и
аналитике Альфа-Банка. Позиция банка и выводы ассистента в ответе разделены. Устройство агента —
`docs/agent_tz.md`.

#### `POST /chat/{user_id}` — сообщение в чат
- Тело: [`UserChatMessage`](#userchatmessage) — `{"message": "Как решение ЦБ скажется на нашем долге?"}`.
  `new_dialog: true` — начать новый диалог (прежний сохраняется в базе).
- Ответ: `200` [`AgentAnswer`](#agentanswer). Для показа в чате достаточно `text`; для своей вёрстки —
  `sections` (раздел с `origin = bank` — позиция Альфа-Банка, его стоит выделять) и `sources`.
- Работает синхронно, 30–90 с: таймаут клиента — не меньше 120 с. Сообщения одного пользователя
  обрабатываются по очереди.
- Ошибки: `404` — профиля нет; `422` — пустое сообщение или длиннее 2000 символов.

```json
{
  "answer_id": "a_3f1c2b9d8e7a", "session_id": "c_ff6818c015cf", "user_id": "u_03", "status": "ok",
  "question": "А как это повлияет на стоимость нашего долга?",
  "summary": "Стоимость вашего долга вырастет: ЦБ сохранил ключевую ставку на уровне 14%.",
  "text": "**Ответ** · _вывод ассистента_\nСтоимость вашего долга вырастет… [N1]\n\n**Позиция аналитиков Альфа-Банка** · _аналитика Альфа-Банка_\n…",
  "sections": [
    {"id": "s1", "kind": "answer", "title": "Ответ", "origin": "ai", "text": "…", "sources": ["N1", "A3"]},
    {"id": "s2", "kind": "bank_view", "title": "Позиция аналитиков Альфа-Банка", "origin": "bank", "text": "…", "sources": ["A1", "A2"]}
  ],
  "sources": [
    {"id": "N1", "type": "news", "title": "…", "source": "rbc.example", "published_at": "2026-10-19T09:10:00Z",
     "url": "…", "news_id": "syn_bonds_0", "chunk_id": null},
    {"id": "A1", "type": "analytics", "title": "…", "source": null, "published_at": "2026-09-11",
     "url": "…", "news_id": null, "chunk_id": "…"}
  ],
  "clarify": null, "llm_available": true, "pipeline_version": "0.1.0",
  "plan": {"…": "…"}, "quality": {"iterations": 1, "restarts": 0, "critic": {"…": "…"}, "checks": []},
  "timings_ms": {"context": 21468, "planner": 6906, "writers": 14796, "checks": 15391, "critic": 13764, "total": 72421},
  "created_at": "2026-10-24T12:01:15Z"
}
```

#### `GET /chat/{user_id}` — история текущего диалога
- Параметр: `limit` (int, по умолчанию 50) — сколько последних реплик.
- Ответ: `200` [`ChatHistory`](#chathistory). Пользователь ещё не писал — `session_id = null`, `turns = []`.
- Ошибка: `404` — профиля нет.

```json
{"session_id": "c_ff6818c015cf", "user_id": "u_03",
 "turns": [
   {"turn": 1, "role": "user", "text": "Какие новости из моей ленты важнее всего?", "answer": null,
    "created_at": "2026-10-24T12:00:01Z"},
   {"turn": 2, "role": "assistant", "text": "…", "answer": {"…": "полный AgentAnswer"},
    "created_at": "2026-10-24T12:01:12Z"}
 ]}
```

## 4. Модели (контракты)

Обозначения в таблицах: `*` — обязательное поле; `?` — может быть `null`; `[]` — список, по умолчанию пустой.

### Входные

#### UserChatMessage
| Поле | Тип | По умолчанию | Что значит |
|---|---|---|---|
| `message`* | string 1–2000 | | сообщение пользователя |
| `new_dialog` | bool | `false` | начать новый диалог вместо продолжения текущего |
| `now`? | datetime | `REC_NEWS_DEMO_NOW` или текущее время | «текущий момент» для демо на синтетике |

#### InsightJobRequest
| Поле | Тип | По умолчанию | Что значит |
|---|---|---|---|
| `news_ids`? | string[] 1–100 | все новости ленты | только эти новости, в этом порядке |
| `limit`? | int 1–100 | — | только первые N новостей ленты |
| `refresh` | bool | `false` | строить заново и готовые карточки |

### Обработка

#### Enqueued
| Поле | Тип | Что значит |
|---|---|---|
| `status`* | `accepted` \| `already_running` | `accepted` — запуск поставлен в очередь; `already_running` — обработка уже стоит в очереди или идёт, новая не создана |
| `news`* | int | сколько новостей со статусом `new` ждало обработки в момент вызова |

#### ProcessingResult
| Поле | Тип | Что значит |
|---|---|---|
| `result`* | bool | `true` — в базе есть новости `ready` и обработка не стоит в очереди и не идёт; `false` — иначе |

### Профили

#### ProfileList
`total`* — сколько профилей всего; `items`* — [ProfileListItem](#profilelistitem)[].

#### ProfileListItem
`user_id`*, `role`*, `company_name`?, `industry`?, `company_id`? (ID компании в датасете, `SYNTHETIC_U`).

#### ProfileDetails
| Поле | Тип | Что значит |
|---|---|---|
| `user_id`* | string | |
| `profile`* | анкета `UserProfileIn` | `user_id`, `role`, `company_name`?, `industry`?, `company_description`?, `company_id`?, `regions[]`, `products[]`, `competitors[]`, `counterparties[]`, `company_size`? |
| `interests`* | [ProfileInterests](#profileinterests) | профиль интересов, на нём держится подбор ленты |
| `company_facts` | [CompanyFactInfo](#companyfactinfo)[] | данные о компании — на них опираются карточки инсайтов |
| `activity`* | [ProfileActivity](#profileactivity) | что уже посчитано |

#### ProfileInterests
| Поле | Тип | Что значит |
|---|---|---|
| `source`* | `llm` \| `rules` | аспекты написала LLM или построены по правилам (без LLM) |
| `aspects`* | AspectInfo[] | 8–15 интересов: `aspect_id`, `title`, `description`, `examples[]`, `keywords[]`, `entities[]`, `topic`, `weight` 0–1 |
| `entities`* | {string: number} | важные пользователю сущности и их вес: своя компания 1.0, конкуренты 0.7, контрагенты и страны ВЭД 0.6, сырьё 0.5, регионы 0.3 |
| `topic_weights`* | {string: number} | вес тем 0–1 по роли и отрасли |
| `pipeline_version` | string | версия, которой построен профиль |

#### CompanyFactInfo
| Поле | Тип | Что значит |
|---|---|---|
| `id`* | string | `F1…` — те же ID, что в карточке инсайта |
| `text`* | string | «есть кредиты», «75% поступлений — от одного покупателя» |
| `kind`* | string | `industry`, `region`, `size`, `product`, `trade`, `suppliers`, `customers`, `concentration`, `profile` |
| `origin`* | string | откуда: `анкета`, `dataset` (данные компании), `платежи` (расчёт по платежам) |

#### ProfileActivity
| Поле | Тип | Что значит |
|---|---|---|
| `feed_status`* | `ready` \| `not_computed` | есть ли лента (`GET /users/{id}/news`) |
| `feed_total` | int | новостей в ленте |
| `feed_computed_at`? | datetime | когда посчитана лента |
| `insight_cards` | int | сколько карточек инсайтов построено |
| `chat_session_id`? | string | текущий диалог в чате; `null` — пользователь ещё не писал |

### Лента

#### UserNewsResponse
| Поле | Тип | Что значит |
|---|---|---|
| `user_id`* | string | |
| `status`* | `ready` \| `not_computed` | `not_computed` — обработки для пользователя ещё не было |
| `run_id`?, `computed_at`?, `pipeline_version`? | | какой запуск посчитал ленту и когда |
| `total` | int | всего новостей у пользователя |
| `items`* | [UserNewsItem](#usernewsitem)[] | |

#### UserNewsItem
| Поле | Тип | Что значит |
|---|---|---|
| `rank`* | int | место в ленте, 1 — самое важное |
| `news_id`*, `event_id`? | string | |
| `title`*, `lead`* | string | заголовок; лид (заголовок и первые предложения) |
| `text`? | string | полный текст (при `with_text=true`) |
| `source`?, `url`?, `published_at`? | | издание, ссылка, время |
| `sources` | string[] | все издания, опубликовавшие событие |
| `topics` | string[] | темы по убыванию веса |
| `importance`? | int 1–5 | важность события |
| `score`* | number 0–1 | итоговая оценка |
| `why_relevant`? | string | почему новость важна пользователю |
| `analytics`? | [UserNewsAnalytics](#usernewsanalytics) | аналитика банка под пользователя; `null` — подходящей нет |
| `insight`? | [InsightCardSummary](#insightcardsummary) | карточка инсайта, если уже построена |
| `signals`? | [Signals](#signals) | составляющие оценки (отладка) |

#### UserNewsAnalytics
| Поле | Тип | Что значит |
|---|---|---|
| `analytics_id`*, `chunk_id`* | string | материал и его фрагмент |
| `title`?, `section_title`?, `published_at`?, `url`? | | материал Альфа Ресерч |
| `relation`? | [Relation](#relation) | как аналитика связана с новостью |
| `contradicts` | bool | аналитика банка расходится с новостью — показать явно |
| `summary`? | string | 2–3 предложения: что аналитика говорит в контексте новости |
| `text`? | string | текст фрагмента (при `with_text=true`) |

Это **позиция банка**: в интерфейсе её нужно визуально отделять от выводов AI.

#### Signals
`sem`, `ent`, `topic`, `imp`, `fresh`, `retrieval`, `final` — числа 0–1; `llm`? — оценка LLM 0–3.
- `sem` — близость к интересам;
- `ent` — общие сущности;
- `topic` — темы роли;
- `imp` — важность;
- `fresh` — свежесть;
- `retrieval` — `0.35·sem + 0.25·ent + 0.20·topic + 0.10·imp + 0.10·fresh`;
- `final` — `0.6·llm/3 + 0.4·retrieval`.

### Карточка инсайта

#### InsightCard
| Поле | Тип | Что значит |
|---|---|---|
| `card_id`*, `user_id`*, `news_id`*, `event_id`? | string | |
| `status`* | `ready` \| `partial` \| `degraded` | готова и проверена; часть блоков с замечаниями или не построена; без LLM |
| `generated_at`*, `pipeline_version`* | | |
| `header`* | [Header](#header) | чипы |
| `headline`? | [Headline](#headline) | главная фраза |
| `fact`* | [Fact](#fact) | что произошло |
| `bank_view`* | [BankView](#bankview) | позиция Альфа-Банка |
| `ai_conclusion`? | [AIConclusion](#aiconclusion) | почему это важно для компании |
| `risks`? | [RisksOpportunities](#risksopportunities) | риски и возможности |
| `hypothesis`? | [Hypothesis](#hypothesis) | как может развиваться ситуация |
| `actions`? | [Actions](#actions) | что можно сделать |
| `bank_offer`? | [BankOffer](#bankoffer) | предложение банка; `null` — не к месту, это нормально |
| `evidence`* | [Evidence](#evidence) | на чём основан вывод |
| `verification`* | [Verification](#verification) | результат проверок по блокам |
| `timings_ms` | {string: int} | время этапов построения |

Блок, равный `null`, не показывается. Причина — в `verification.blocks.<блок>`.

ID источников внутри карточки: `N1…` — публикации о событии, `A1` — фрагмент аналитики, `F1…` — данные о компании.

#### Header
`impact`*, `priority`*, `horizon`*, `reliability`* — [Chip](#chip); `sources_count`* — сколько изданий сообщили о событии.

| Чип | `value` | `label` |
|---|---|---|
| `impact` | `risk`, `opportunity`, `neutral`; `unknown` — без LLM | Риск / Возможность / Нейтрально / Не оценено |
| `priority` | `high`, `medium`, `low` | Высокий / Средний / Низкий приоритет |
| `horizon` | `1-3m`, `3-6m`, `6-12m`; `unknown` | 1–3 месяца / 3–6 месяцев / 6–12 месяцев |
| `reliability` | `confirmed` (≥ 2 изданий), `single_source` | «Подтверждено N источниками» / «Пока один источник» |

#### Chip
`value`* — значение для логики; `label`* — готовая подпись; `reason`? — обоснование (для подсказки).

#### Headline
| Поле | Тип | Что значит |
|---|---|---|
| `text`* | string | главная фраза: что событие значит для компании читателя. Пишет LLM по новости, аналитике банка и профилю |
| `company`?, `role`*, `event_date`? | | подпись «для какой компании и роли, дата события» |
| `aspect`? | string | интерес из профиля, который задевает фраза |
| `company_features` | [CompanyFeature](#companyfeature)[] | особенности компании, через которые событие её касается |
| `source_ids` | string[] | `N1`, и `A1`, если аналитика есть |
| `based_on_analytics` | bool | фраза учитывает оценку аналитиков банка |

#### Fact
| Поле | Тип | Что значит |
|---|---|---|
| `text`* | string | сухое описание события, 2–4 предложения |
| `decided` | string[] | что уже решено |
| `discussed` | string[] | что пока обсуждается; пустой список — не показывать |
| `key_figures` | KeyFigure[] | до 4 ключевых чисел: `value`, `label`, `source_ids` |
| `source_ids` | string[] | публикации `N*` |

#### BankView
| Поле | Тип | Что значит |
|---|---|---|
| `available`* | bool | `false` — аналитики по теме нет: вместо блока показывается `warning` |
| `text`? | string | позиция аналитиков банка своими словами |
| `material_title`?, `material_date`?, `section_title`?, `url`? | | материал Альфа Ресерч |
| `relation`? | [Relation](#relation) | связь с новостью |
| `contradicts` | bool | аналитика расходится с новостью |
| `source_ids` | string[] | `["A1"]` |
| `warning`? | string | «Аналитики Альфа-Банка по этой теме нет. Выводы ниже — не позиция банка…» |

#### AIConclusion
- `text`* — как событие затронет компанию.
- `channels[]` — каналы влияния: `кредиты и долг`, `отрасль`, `регион`, `поставщики`, `покупатели`, `ВЭД и валюта`, `налоги и платежи в бюджет`, `спрос`, `регулирование`, `персонал`, `инвестиции`, `ликвидность`.
- `company_features[]` ([CompanyFeature](#companyfeature)) — на что опирается вывод.
- `source_ids[]`.

#### CompanyFeature
`id`* (`F1…`), `text`* — например, «есть кредиты», «75% поступлений — от одного покупателя».

#### RisksOpportunities
`risks[]` и `opportunities[]` — по 1–3 пункта. Каждый пункт — `RiskItem`: `text`*, `source_ids[]`, `feature_ids[]`.

#### Hypothesis
`condition`*, `consequence`*, `text`* («Если …, то …»), `disclaimer` («Это предположение, а не прогноз.»).

#### Actions
`steps[]` — 2–4 шага под роль; `question`* — вопрос для обсуждения внутри компании.

#### BankOffer
| Поле | Тип | Что значит |
|---|---|---|
| `product_id`* | string | ID из каталога `config/references/bank_products.json` |
| `name`*, `description`* | string | продукт |
| `reason`* | string | чем помогает с названным риском |
| `risk_index`? | int | номер риска в `risks.risks` (с 0) |
| `cta` | `CallToAction` `{label, action}` | кнопка: «Обсудить с менеджером», `contact_manager` |

#### Evidence
| Поле | Тип | Что значит |
|---|---|---|
| `sources` | EvidenceSource[] | публикации о событии |
| `analytics_material`? | EvidenceSource | материал Альфа Ресерч |
| `company_data` | [CompanyFeature](#companyfeature)[] | учтённые данные о компании |
| `trace` | TraceStep[] | шаги для кнопки «Как получен вывод»: `step`, `title`, `detail` |

`EvidenceSource`:
- `id`, `type` (`news` \| `analytics`), `title`, `outlet`?, `url`?, `published_at`?;
- `reliability` 1–5 и `reliability_label` (`высокая` / `средняя` / `низкая` / `аналитика Альфа-Банка`);
- `quote`? — дословная цитата.

#### Verification
| Поле | Тип | Что значит |
|---|---|---|
| `verdict`* | `passed` \| `passed_with_fixes` \| `partial` \| `not_verified` | принято сразу; после исправлений; остались замечания; проверщик не отработал |
| `rounds` | int | проверок агентом-проверщиком |
| `comment`? | string | общий вывод проверщика |
| `blocks` | {блок: BlockVerification} | по каждому блоку |

`BlockVerification`:
- `status` — один из:
  - `ok` — блок прошёл проверку;
  - `fixed` — исправлен по замечанию;
  - `unverified` — замечания остались, блок показан;
  - `failed` — блок не показан;
  - `skipped` — не нужен;
  - `fallback` — запасной вариант без LLM.
- `attempts` — сколько раз писался блок.
- `model` — модель, которая его писала.
- `issues[]` — замечания `BlockIssue`: `code`, `severity` (`error` \| `warning`), `message`, `by` (`code` \| `verifier`).

#### InsightCardSummary
`card_id`, `status`, `headline`?, `impact`, `priority`, `horizon`, `reliability` — значения чипов. Краткая карточка для ленты.

### Задания на карточки

#### InsightJob
| Поле | Тип | Что значит |
|---|---|---|
| `job_id`*, `user_id`* | string | |
| `status`* | `queued` \| `running` \| `done` \| `failed` \| `cancelled` | `done` — построена хотя бы одна карточка; `failed` — ни одной |
| `refresh`* | bool | |
| `created_at`*, `started_at`?, `finished_at`? | datetime | |
| `total`*, `queued`, `running`, `done`, `failed`, `cancelled` | int | счётчики карточек |
| `items` | InsightJobItem[] | по карточке на новость |

`InsightJobItem`:
- `news_id`, `position`;
- `status` (`queued` \| `running` \| `done` \| `failed` \| `cancelled`);
- `card_id`?, `card_status`? (`ready` \| `partial` \| `degraded`), `headline`?;
- `error`? — причина при `failed`;
- `started_at`?, `finished_at`?.

### Чат

#### AgentAnswer
| Поле | Тип | Что значит |
|---|---|---|
| `answer_id`*, `session_id`*, `user_id`* | string | ID ответа и диалога (диалог ведёт ML-сервис) |
| `status`* | `ok` \| `best_effort` \| `degraded` \| `fallback` \| `no_evidence` \| `clarify` \| `out_of_scope` | `ok` — ответ принят оценщиком; `best_effort` — лучшая версия после доработок; `degraded` — часть ответа не прошла проверку; `fallback` — без LLM, подборка источников; `no_evidence` — в новостях и аналитике нет данных; `clarify` — агент задаёт уточняющий вопрос; `out_of_scope` — вопрос не по теме |
| `question`* | string | вопрос пользователя |
| `summary`* | string | ответ в одну-две фразы (для превью) |
| `text`* | string | готовый текст ответа: разделы с заголовками и пометкой «аналитика Альфа-Банка» / «вывод ассистента», ссылки вида `[N1]`, список источников; разметка — markdown |
| `sections` | AgentSection[] | разделы: `id`, `kind` (`summary`, `answer`, `facts`, `bank_view`, `impact`, `risks`, `actions`, `compare`), `title`, `origin` (`bank` \| `ai`), `text`, `sources[]` |
| `sources` | AgentSource[] | источники: `id` (`N1`, `A1`, `P`), `type` (`news` \| `analytics` \| `profile`), `title`, `source`?, `published_at`?, `url`?, `news_id`?, `chunk_id`? |
| `clarify`? | string | уточняющий вопрос при `status = clarify` |
| `llm_available` | bool | была ли доступна LLM |
| `plan`?, `quality`, `timings_ms` | | план ответа, оценки проверки и время этапов — для отладки |
| `created_at`* | datetime | |

#### ChatHistory
| Поле | Тип | Что значит |
|---|---|---|
| `session_id`? | string | текущий диалог; `null` — пользователь ещё не писал |
| `user_id`* | string | |
| `turns` | ChatTurn[] | реплики по порядку: `turn`, `role` (`user` \| `assistant`), `text`, `answer`? (полный `AgentAnswer` у ответов агента), `created_at` |

### Справочники

#### Relation
Связь аналитики с новостью: `причины`, `прогноз`, `исторический контекст`, `противоречит`, `общая тема`.

#### Роли
`собственник бизнеса`, `генеральный директор`, `финансовый директор`, `главный бухгалтер`,
`руководитель казначейства`, `другой C-level-руководитель`, `коммерческий директор`, `директор по закупкам`,
`директор по ВЭД`, `директор по рискам`, `юридический директор`, `директор по производству`,
`иной сотрудник, влияющий на принятие решений и имеющий доступ к интернет-банку`.

Регистр, дефисы и синонимы приводятся к этим названиям (`config/references/role_topics.json`).
