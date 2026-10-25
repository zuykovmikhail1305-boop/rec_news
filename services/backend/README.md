# Backend: веб-интерфейс новостей и инсайтов

FastAPI-сервис отображает компании, персональную ленту и карточки инсайтов.
HTML формируется через Jinja2; JavaScript обновляет ленту и карточки без
перезагрузки страницы. Данные и генерацию предоставляет отдельный ML API.

Сервис работает в демо-режиме: пользователь выбирает существующий профиль
компании. Авторизации, JWT, cookie-сессий и CSRF в текущем сценарии нет.
Выбранный `user_id` передаётся в URL страниц и API-запросов.

## Зависимости и запуск

Нужны Python 3.11+, `uv` и запущенный ML API с подготовленными профилями.
По умолчанию backend слушает `8000`, ML API — `8002`.

Из PowerShell:

```powershell
cd D:/repos/rec_news/services/backend
uv sync --locked
# Если .env ещё нет: скопируйте .env.example и укажите реквизиты PostgreSQL.
uv run --env-file .env python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Откройте [главную](http://127.0.0.1:8000/) или
[список компаний](http://127.0.0.1:8000/profiles).
Swagger: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).

`ML_API_URL` задаёт адрес ML API для страниц и фонового запуска processing. По умолчанию это `http://127.0.0.1:8002`. Переменная задаётся
в окружении процесса; собственный `.env` backend автоматически не читает.
Если используете свой файл, передайте его явно:

```powershell
uv run --env-file .env python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

`SECRET_KEY` и настройка сессий для запуска не требуются.

## Подготовка данных и связь с другими сервисами

Инициализация БД, создание демо-профилей, модели и обработка новостей выполняются
в ML-сервисе. Инструкции: [README ML](../ml/README.md),
[контракт ML API](../ml/docs/api.md). Backend сам не создаёт пользователей и не
заполняет БД. Если профилей нет, `/profiles` покажет пустой список.

Сбор публикаций выполняет [parser](../parser/README.md). В общем PostgreSQL
парсер записывает исходные публикации в `public.news_raw` и передаёт готовые
тексты в `ml_news.news`. Затем ML обрабатывает новости и предоставляет ленты
и карточки через HTTP. Фоновый монитор backend читает только наличие строк `new`
в `ml_news.news` и вызывает ML API. Подключение к PostgreSQL должно вести
в одну базу у parser, ML и монитора backend.

В кодовой базе остались модули прежней веб-БД; текущие обработчики страниц
и инсайтов их не используют. Отдельная инициализация веб-БД не требуется.

## Пользовательский сценарий

1. Откройте `/profiles` и выберите компанию. Без `user_id` на главной backend
   выбирает первый доступный профиль ML.
2. Главная `/?user_id=...` получает профиль, ленту и последнее задание подготовки.
3. JavaScript при открытии главной вызывает
   `POST /api/insights/generate?user_id=...`. Backend ждёт готовности processing
   через `GET /processing.result`, затем передаёт ML запрос
   с `{"refresh":false}` — готовые карточки не требуют принудительной регенерации.
4. Затем браузер запрашивает `GET /api/insights?user_id=...` и, пока задание
   находится в `queued` или `running`, обновляет данные каждые 5 секунд.
5. При переходе на `/insights/{news_id}?user_id=...` загружается карточка
   с блоками, которые вернул ML. JavaScript вызывает
   `POST /api/insights/{news_id}/generate?user_id=...`: создаётся асинхронное
   ML-задание с `news_ids=[news_id]` и `refresh=false`. Готовая карточка берётся
   из кэша ML; браузер опрашивает состояние. Если другое задание пользователя
   ещё не охватывает эту новость, запрос повторяется после ожидания.

Чтение страниц и GET-запросы обновления сами не запускают генерацию или глобальную
обработку новостей. На главной запуск делает отдельный POST из JavaScript;
без JavaScript автоматического запуска нет. Опрос ограничен 180 обновлениями,
после чего интерфейс предлагает обновить страницу позже.

## Маршруты

Страницы:

- `GET /` — профиль и лента; необязательные `user_id`, `offset`.
- `GET /profiles?offset=0` — выбор компании, по 50 профилей на страницу.
- `GET /insights/{news_id}?user_id=...` — карточка конкретной новости.
- `GET /login` — совместимый редирект на `/profiles`.
- `GET /insights` и `/insights/prototype` — редирект на главную с сохранением `user_id`.

API для интерфейса, везде обязателен `user_id`:

- `POST /api/insights/generate` — запрос подготовки; успех HTTP 202,
  ответ содержит `user_id` и `job`.
- `GET /api/insights?offset=0` — обновление ленты, по 20 новостей;
  ответ содержит `user_id`, HTML-фрагмент `html`, признак продолжения опроса `poll`
  и возможную ошибку `error`.
- `POST /api/insights/{news_id}/generate` — подготовка одной карточки, HTTP 202.
- `GET /api/processing/status` — состояние фонового монитора.
- `GET /api/insights/{news_id}` — обновление карточки;
  ответ содержит `user_id`, `html`, `poll`.

Это API веб-интерфейса: данные ленты и карточки возвращаются как HTML внутри JSON.
Для структурированных новостей, профилей и блоков используйте ML API напрямую.
Ответы страниц и API помечаются `Cache-Control: no-store`.

Пример проверки — подставьте `user_id` существующего профиля:

```powershell
$userId = [uri]::EscapeDataString('USER_ID')
Invoke-RestMethod "http://127.0.0.1:8000/api/insights/generate?user_id=$userId" -Method Post
Invoke-RestMethod "http://127.0.0.1:8000/api/insights?user_id=$userId"
```

## Вызовы ML API

Клиент `backend/ml_client.py` использует:

- `POST /processing.enqueue`, `GET /processing.result`;
- `GET /profiles`, `GET /profiles/{user_id}`;
- `GET /users/{user_id}/news` с `with_text=false`;
- `POST /users/{user_id}/insights/generate` с `refresh=false`;
- `GET /users/{user_id}/insights/jobs/latest`;
- `GET /users/{user_id}/news/{news_id}/insight`.

Клиент проверяет формат ответа и соответствие `user_id`/`news_id` запросу.
Таймаут обращения к ML — 10 секунд, подключения — 3 секунды.
Недоступность ML отображается в интерфейсе; соответствующие API возвращают 503.
Запуск генерации может вернуть 409 с `retry=true`, если processing ещё идёт
или лента не подготовлена; браузер повторяет запрос через 5 секунд. Возможен 404,
если нет данных для генерации. Отсутствующий обязательный `user_id` даёт 422.

Если карточка не появилась, проверьте профиль, подготовленность персональной ленты,
последнее задание и worker ML. Запуск backend не запускает ML worker, LLM,
парсер, RabbitMQ или PostgreSQL.

## Docker

Из `services/backend`:

```powershell
docker build -t rec-news-backend .
docker run --rm -p 8000:8000 -e ML_API_URL=http://host.docker.internal:8002 rec-news-backend
```

Этот пример рассчитан на Docker Desktop и ML API на компьютере. Убедитесь,
что ML доступен контейнеру. При запуске обоих сервисов в одной Docker-сети
задайте имя сервиса ML, например `http://ml:8002`.
Dockerfile запускает Uvicorn на `0.0.0.0:8000` с одним worker.

## Проверки и структура

```powershell
uv run python -m unittest discover -s tests -p "test_*.py" -v
```

Тесты страниц и инсайтов проверяют выбор профиля, вызовы ML, состояния генерации,
карточки и обработку ошибок с подменённым ML-клиентом.

- `backend/main.py` — FastAPI и статические файлы.
- `backend/routes.py` — страницы и API для интерфейса.
- `backend/ml_client.py` — HTTP-клиент и модели ответов ML.
- `backend/templates/` — HTML-шаблоны.
- `backend/static/live-insights.js` — запуск подготовки и опрос состояния.
- `tests/` — проверки backend.

Самостоятельный frontend в `services/web` и старые HTML-прототипы не нужны
для запуска описанного FastAPI-интерфейса.

## Автоматический запуск обработки новых новостей

Скопируйте `.env.example` в `.env`, если своего файла ещё нет, и задайте:

```dotenv
ML_API_URL=http://127.0.0.1:8002
BACKEND_DATABASE_URL=postgresql+psycopg://USER:PASSWORD@127.0.0.1:5432/rec_news
BACKEND_PROCESSING_ENABLED=true
BACKEND_PROCESSING_INTERVAL=15
BACKEND_ML_NEWS_SCHEMA=ml_news
```

Запускайте backend с `uv run --env-file .env python -m uvicorn backend.main:app
--host 127.0.0.1 --port 8000`. Эти настройки включают фоновый монитор в lifespan
приложения. Он работает без посетителей сайта и каждые 15 секунд проверяет,
есть ли в таблице ML новости со статусом `new`. Только в этом случае backend
вызывает `POST /processing.enqueue`. Backend не меняет статусы новостей.

Для подключения поддерживается и `REC_NEWS_DATABASE_URL`, если
`BACKEND_DATABASE_URL` не задан. Без URL монитор по умолчанию выключен;
явное включение без URL считается ошибкой конфигурации. Минимальный интервал —
5 секунд. Перезапуск backend перечитывает конфигурацию.

Блокировка PostgreSQL защищает отправку от нескольких экземпляров backend;
ML дополнительно объединяет активные запуски. При ошибке БД или ML монитор
повторяет проверку на следующем интервале. После перезапуска он снова обнаружит
необработанные строки. Требуются права чтения таблицы; схемы он не создаёт.
В ML должен работать исполнитель задач `processing.run`; у parser, backend
и ML должна быть одна PostgreSQL-база. Встроенный периодический запуск ML
`REC_NEWS_PIPELINE_WORKER` можно оставить выключенным, когда orchestration делает backend.

Диагностика: `GET /api/processing/status` возвращает `enabled`, `status`,
`checked_at` и тип ошибки, если она произошла. Статусы `accepted`/`already_running`
означают ответ ML на последнюю проверку; `idle` — входных строк `new` не найдено.
Это состояние монитора, а не гарантия завершения ML-конвейера.

Дополнительные проверки:

```powershell
node tests/live-insights.cjs
$env:BACKEND_TEST_DATABASE_URL='postgresql://USER:PASSWORD@127.0.0.1:5432/TEST_DB'
uv run python -m unittest discover -s tests -p test_processing.py -v
Remove-Item Env:BACKEND_TEST_DATABASE_URL
```

Интеграционный тест создаёт и удаляет только свою случайную схему `backend_test_*`.

Если ML вернул пустую персональную ленту, но в последнем задании есть завершённые
элементы, backend дополнительно читает до 20 готовых карточек через
`GET /users/{user_id}/news/{news_id}/insight` и показывает доступные результаты.
Сам статус `done` не считается подтверждением наличия карточки: если чтение
неуспешно, интерфейс сообщает, что карточки пока недоступны. Это отображение
результатов последнего задания, а не восстановление всей истории ленты.
