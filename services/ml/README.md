# ML-сервис rec_news

Обработка новостей и персонализация: подготовка и дедупликация новостей, подбор аналитики
Альфа-Банка, персональные ленты, карточки инсайтов. HTTP API на FastAPI. Весь код, конфигурация,
данные и окружение ML-части находятся здесь; импортов из других сервисов нет.

## Запуск (Python 3.12, uv)

Из корня репозитория:

```powershell
cd services/ml
uv sync
docker compose up -d qdrant                       # Qdrant в Docker :6333 (в .env: REC_NEWS_QDRANT_URL=http://localhost:6333)
.venv/Scripts/python scripts/init_db.py          # базы, таблицы, коллекции Qdrant, аналитика + проверка окружения
uv run --env-file .env scripts/run_api.py
```

`scripts/init_db.py` можно запускать повторно — существующие данные не трогаются; `--check` только
проверяет и ничего не создаёт, `--import-dataset` переносит CSV датасета в `dataset/*.db`. Пока запущен
API на локальной базе Qdrant, она занята: остановите API или добавьте `--skip-vectors` (с Qdrant в Docker этого
ограничения нет).

**Qdrant в Docker.** Векторы новостей и аналитики хранит сервер Qdrant из `docker-compose.yml` (`qdrant/qdrant:v1.19.2`,
порт 6333, панель http://localhost:6333/dashboard, данные — в томе `qdrant`). Включается строкой
`REC_NEWS_QDRANT_URL=http://localhost:6333` в `.env`; без неё используется локальная база `data/qdrant`.
Перенести векторы из локальной базы в сервер — `scripts/qdrant_to_server.py`. Подключение для всего кода —
`news_pipeline/index/qdrant.py`.

Swagger: http://127.0.0.1:8002/docs. На Linux используйте `.venv/bin/python`.
Так сервис работает одним процессом (SQLite в `data/`, очередь — потоки в процессе API). С Qdrant в Docker
к векторам одновременно подключаются API, воркеры и скрипты; локальную базу открывает только один процесс.

### С брокером и PostgreSQL

Celery + RabbitMQ, PostgreSQL и Qdrant-сервер — переменные `REC_NEWS_DATABASE_URL`, `REC_NEWS_QUEUE=celery`,
`REC_NEWS_BROKER_URL`, `REC_NEWS_QDRANT_URL` в `.env` (шаблон — `.env.example`); инфраструктура для
разработки — `docker compose up -d`:

```powershell
.venv/Scripts/python scripts/init_db.py                     # схемы ml_* в PostgreSQL, коллекции Qdrant
.venv/Scripts/python scripts/migrate_to_postgres.py --qdrant # перенести данные из data/*.db и data/qdrant
uv run --env-file .env scripts/run_api.py
.venv/Scripts/celery -A news_pipeline.tasks.celery_app worker --pool threads --concurrency 4 --loglevel info
```

Обмен с другими сервисами (`NewsDiscovered` → обработка → `AnalysisCompleted`) — `docs/broker.md`.

## Адрес и порт API

ML уже поддерживает эти переменные в `services/ml/.env`:

```dotenv
REC_NEWS_API_HOST=0.0.0.0
REC_NEWS_API_PORT=8002
```

Запуск из `services/ml`:

```powershell
uv run --env-file .env scripts/run_api.py
```

`scripts/run_api.py` читает настройки через `news_pipeline.env`; ML также сам
загружает свой `.env`. Переменные процесса имеют приоритет над файлом, аргументы
`--host` и `--port` — над переменными. Без настроек используются `127.0.0.1:8002`.
У parser переменные называются `PARSER_API_HOST`/`PARSER_API_PORT`, у backend —
`BACKEND_API_HOST`/`BACKEND_API_PORT`; их стандартные порты — `8001` и `8000`.

`0.0.0.0` — адрес прослушивания всех IPv4-интерфейсов. В браузере открывайте
[Swagger ML](http://127.0.0.1:8002/docs) или `http://IP_КОМПЬЮТЕРА:8002/docs`.
Backend должен подключаться к реальному адресу ML через `ML_API_URL`, например
`http://127.0.0.1:8002` для одной машины или `http://IP_МАШИНЫ_ML:8002` для разных.
Адрес `0.0.0.0` не подставляется в URL подключения к ML, PostgreSQL или RabbitMQ.

## Совместный запуск с parser и backend

Инструкции соседних сервисов: [parser](../parser/README.md),
[backend](../backend/README.md). Все три процесса должны использовать одну
PostgreSQL-базу `rec_news`. В `.env` ML задайте:

```dotenv
REC_NEWS_DATABASE_URL=postgresql+psycopg://USER:PASSWORD@127.0.0.1:5432/rec_news
REC_NEWS_DB_SCHEMA_PREFIX=ml_
REC_NEWS_PIPELINE_WORKER=false
# Если используете отдельный Celery worker ML:
REC_NEWS_QUEUE=celery
REC_NEWS_BROKER_URL=amqp://BROKER_USER:BROKER_PASSWORD@127.0.0.1:5672/rec_news
REC_NEWS_QDRANT_URL=http://127.0.0.1:6333
```

Замените заполнители на реквизиты существующих БД и брокера. Совпадать должны
сервер и имя БД у `REC_NEWS_DATABASE_URL` (ML), `PARSER_DATABASE_URL` (parser)
и `BACKEND_DATABASE_URL` (backend). В ML используется `ml_news.news`, у parser
должен быть `PARSER_ML_NEWS_SCHEMA=ml_news`, у backend —
`BACKEND_ML_NEWS_SCHEMA=ml_news`. В контейнерах укажите имя сервиса БД вместо
`127.0.0.1`. Изменение URL не переносит данные из SQLite автоматически.

Существующие PostgreSQL/RabbitMQ можно использовать без запуска вторых
контейнеров из ML Compose на занятых портах. Для векторов нескольких процессов
нужен Qdrant-сервер. Порядок запуска:

1. Подготовьте PostgreSQL, RabbitMQ и Qdrant, создайте базу `rec_news`.
2. В `services/ml` выполните `uv sync` и
   `uv run --env-file .env scripts/init_db.py`. ML создаст свои схемы и таблицы.
   `--skip-vectors` позволяет отдельно создать/проверить таблицы, но не заменяет
   подготовку векторов и аналитики для рабочего ML-конвейера.
3. Инициализируйте parser его `scripts/init_db.py`. Он ожидает, что ML уже
   создал `ml_news.news`.
4. Запустите ML API. При `REC_NEWS_QUEUE=celery` в отдельном терминале ML запустите:

```powershell
uv run --env-file .env celery -A news_pipeline.tasks.celery_app worker --pool threads --concurrency 4 --loglevel info
```

5. Запустите API/worker/Beat парсера и backend по их README.

Worker парсера обслуживает `parser.dispatch`/`parser.publications`, worker ML —
свои задачи, включая `processing.run` и `insights.generate`. Работа одного
worker не заменяет второй. Если используется локальная очередь ML вместо Celery,
исполнитель находится в процессе ML API.

### Как запускается обработка новостей

Parser сохраняет готовую публикацию в `public.news_raw` со статусом `ready`
и в `ml_news.news` со статусом `new` одной транзакцией. Контракт —
[news-schema.md](../../docs/news-schema.md). В текущей интеграции parser не
отправляет `NewsDiscovered` в ML: наличие поддержки этих событий в ML не
означает, что parser их публикует.

Backend без посетителей сайта проверяет наличие строк `new` каждые 15 секунд
и вызывает `POST /processing.enqueue`. Для этого у него включён
`BACKEND_PROCESSING_ENABLED=true` и задан URL общей БД. ML возвращает
`accepted` или `already_running` и выполняет обработку через свою очередь.
Статусы новостей меняет ML. Периодический запуск самого ML
`REC_NEWS_PIPELINE_WORKER` при такой схеме оставьте выключенным.
Состояние монитора backend: `GET /api/processing/status`; завершение ML:
`GET /processing.result`. Эти ответы описывают разные этапы.

### Генерация и отображение инсайтов

При открытии главной backend сначала проверяет `GET /processing.result`,
затем вызывает `POST /users/{user_id}/insights/generate` с `refresh=false`.
HTTP 202 означает приём задания, а не готовые карточки. Браузер обновляет
состояние через backend каждые 5 секунд; backend читает персональную ленту
`GET /users/{user_id}/news` и последнее задание
`GET /users/{user_id}/insights/jobs/latest`.

При открытии карточки backend использует ту же асинхронную генерацию с
`news_ids=[news_id]`, затем читает готовый результат через
`GET /users/{user_id}/news/{news_id}/insight`. Синхронный `POST` отдельной
карточки также существует в ML API, но веб-сценарий использует очередь заданий.
Готовые карточки повторно не пересоздаются принудительно.

Лента и задание генерации — разные данные. Ответ `not_computed`/`items=[]`
на запрос ленты может сосуществовать с `done` у ранее выполненного задания.
Backend в этом случае дополнительно запрашивает готовые карточки завершённых
элементов последнего задания по их `news_id` и отображает доступные результаты.
Это не восстанавливает всю историю ленты; статус задания сам по себе не
подтверждает, что карточка всё ещё доступна.

Для диагностики проверьте ответы одного ML API и одного `user_id`:

```http
GET /processing.result
GET /users/{user_id}/news
GET /users/{user_id}/insights/jobs/latest
GET /users/{user_id}/news/{news_id}/insight
```

`news_id` возьмите из `items` задания, а не из `card_id`. Если последнее чтение
вернуло 200, карточка доступна независимо от наполненности ленты. Если вернуло
404 при завершённом задании, проверьте актуальность задания и согласованность
БД/схем у ML API и worker. Если лента пуста, отдельно проверьте завершение
processing, наличие профиля и рассчитанных рекомендаций. Одного `done` у
генерации карточек недостаточно для подтверждения расчёта персональной ленты.

## Конфигурация и данные

- `.env` — подключение к LLM (`REC_NEWS_LLM_*`), каталог данных и др.; шаблон — `.env.example`.
  Для реальных новостей уберите `REC_NEWS_DEMO_NOW`.
- `config/ml.toml` — веса, пороги, версии промптов и модели под задачи; `config/prompts.toml` — тексты всех
  промптов LLM с пояснениями; `config/references/` — справочники.
- `data/` — базы ML-части (SQLite) и Qdrant, в git не хранятся. Другой каталог — `--data-dir` или
  `REC_NEWS_DATA_DIR`. С `REC_NEWS_DATABASE_URL` базы — схемы `ml_*` в PostgreSQL.
- `dataset/` — датасет хакатона (`*.db`, перенос из CSV — `scripts/import_dataset.py`).
- `models/` — модель эмбеддингов; `texts/` — PDF аналитики; `fixtures/` — синтетические и размеченные новости.

## Структура

- `news_pipeline/` — конвейер новостей, аналитика, профили, ленты, карточки, очередь и хранение.
- `scripts/` — запуск, инициализация, перенос данных, демо и проверка бюджета LLM.
- `scripts_for_tests/` — подготовка размеченных наборов и оценка качества.
- `docs/` — [API](docs/api.md), [архитектура](docs/architecture.md),
  [брокер](docs/broker.md), [карточки](docs/insight_card.md),
  [задания генерации](docs/insight_jobs.md), [хранение новостей](docs/news_db.md).

## Ручки

`POST /processing.enqueue`, `GET /processing.result`, `GET /profiles`, `GET /profiles/{user_id}`,
`GET /users/{user_id}/news`, `POST`/`GET /users/{user_id}/news/{news_id}/insight`,
`POST /users/{user_id}/insights/generate`, `GET /insights/jobs/{job_id}`,
`GET /users/{user_id}/insights/jobs/latest`, `POST /insights/jobs/{job_id}/cancel`,
`POST /chat/{user_id}` и `GET /chat/{user_id}` — чат пользователя с агентом.
Подробно — `docs/api.md`.
