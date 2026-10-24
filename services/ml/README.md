# ML-сервис rec_news

Обработка новостей и персонализация: подготовка и дедупликация новостей, подбор аналитики
Альфа-Банка, персональные ленты, карточки инсайтов. HTTP API на FastAPI. Весь код, конфигурация,
данные и окружение ML-части находятся здесь; импортов из других сервисов нет.

## Запуск (Python 3.12, uv)

Из корня репозитория:

```powershell
cd services/ml
uv sync
.venv/Scripts/python scripts/init_db.py          # базы, таблицы, коллекции Qdrant, аналитика + проверка окружения
.venv/Scripts/python scripts/run_api.py --port 8002
```

`scripts/init_db.py` можно запускать повторно — существующие данные не трогаются; `--check` только
проверяет и ничего не создаёт, `--import-dataset` переносит CSV датасета в `dataset/*.db`. Пока запущен
API, локальная база Qdrant занята: остановите API или добавьте `--skip-vectors`.

Swagger: http://127.0.0.1:8002/docs. На Linux используйте `.venv/bin/python`.
Сервис работает одним процессом: локальную базу векторов Qdrant открывает только один клиент.

## Конфигурация и данные

- `.env` — подключение к LLM (`REC_NEWS_LLM_*`), каталог данных и др.; шаблон — `.env.example`.
  Для реальных новостей уберите `REC_NEWS_DEMO_NOW`.
- `config/ml.toml` — веса, пороги, версии промптов и модели под задачи; `config/references/` — справочники.
- `data/` — базы ML-части (SQLite) и Qdrant, в git не хранятся. Другой каталог — `--data-dir` или
  `REC_NEWS_DATA_DIR`.
- `dataset/` — датасет хакатона (`*.db`, перенос из CSV — `scripts/import_dataset.py`).
- `models/` — модель эмбеддингов; `texts/` — PDF аналитики; `fixtures/` — синтетические и размеченные новости.

## Структура

| Папка | Что внутри |
|---|---|
| `news_pipeline/` | код: конвейер новостей, аналитика, профили, ленты, карточки инсайтов, очередь задач, хранение |
| `scripts/` | запуск API, загрузка демо-данных, оценка лент (`eval_feed.py`), остаток бюджета LLM |
| `docs/` | `api.md` и `openapi.json` — ручки и контракт; `architecture.md` — как работают компоненты; `insight_card.md`, `insight_jobs.md`, `news_db.md` |

## Ручки

`POST /processing.enqueue`, `GET /processing.result`,
`GET /users/{user_id}/news`, `POST`/`GET /users/{user_id}/news/{news_id}/insight`,
`POST /users/{user_id}/insights/generate`, `GET /insights/jobs/{job_id}`,
`GET /users/{user_id}/insights/jobs/latest`, `POST /insights/jobs/{job_id}/cancel`,
`POST /chat/{user_id}` и `GET /chat/{user_id}` — чат пользователя с агентом.
Подробно — `docs/api.md`.
