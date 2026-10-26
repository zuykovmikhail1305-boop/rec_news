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
.venv/Scripts/python scripts/run_api.py --port 8002
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
.venv/Scripts/python scripts/run_api.py --port 8002
.venv/Scripts/celery -A news_pipeline.tasks.celery_app worker --pool threads --concurrency 4 --loglevel info
```

Обмен с другими сервисами (`NewsDiscovered` → обработка → `AnalysisCompleted`) — `docs/broker.md`.

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

| Папка | Что внутри |
|---|---|
| `news_pipeline/` | код: конвейер новостей, аналитика, профили, ленты, карточки инсайтов, очередь задач, хранение |
| `scripts/` | запуск API, инициализация баз, перенос в PostgreSQL и векторов в Qdrant-сервер, загрузка демо-данных, остаток бюджета LLM |
| `scripts_for_tests/` | проверка качества на размеченном наборе: пересборка новостей (`rebuild_eval_news.py`), лент (`rebuild_eval_feeds.py`), оценка лент (`eval_feed.py`); создание набора (`make_eval_set*.py`), агент (`eval_agent.py`) |
| `docs/` | `api.md` и `openapi.json` — ручки и контракт; `architecture.md` — как работают компоненты; `broker.md` — Celery, RabbitMQ, PostgreSQL и обмен с другими сервисами; `insight_card.md`, `insight_jobs.md`, `news_db.md` |

## Ручки

`POST /processing.enqueue`, `GET /processing.result`, `GET /profiles`, `GET /profiles/{user_id}`,
`GET /users/{user_id}/news`, `POST`/`GET /users/{user_id}/news/{news_id}/insight`,
`POST /users/{user_id}/insights/generate`, `GET /insights/jobs/{job_id}`,
`GET /users/{user_id}/insights/jobs/latest`, `POST /insights/jobs/{job_id}/cancel`,
`POST /chat/{user_id}` и `GET /chat/{user_id}` — чат пользователя с агентом.
Подробно — `docs/api.md`.
