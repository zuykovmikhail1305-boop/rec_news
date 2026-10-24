# Celery, RabbitMQ и PostgreSQL

ML-сервис — это **Analytics** на схемах обмена: Parser собирает новости, Backend управляет заданиями,
ML обрабатывает новость по `news_id`. Сообщения идут через RabbitMQ, данные лежат в PostgreSQL.

Без настроек сервис работает как раньше: SQLite-файлы в `data/` и очередь из потоков внутри процесса API.
Брокер и PostgreSQL включаются переменными окружения, код при этом не меняется.

## Включение

| Переменная | Значение | Что делает |
|---|---|---|
| `REC_NEWS_DATABASE_URL` | `postgresql+psycopg://user:pass@host:5432/db` | все базы в PostgreSQL вместо `data/*.db` |
| `REC_NEWS_DB_SCHEMA_PREFIX` | `ml_` (по умолчанию) | префикс схем: `data/news.db` → `ml_news` |
| `REC_NEWS_QUEUE` | `celery` (по умолчанию `local`) | задачи через Celery + RabbitMQ |
| `REC_NEWS_BROKER_URL` | `amqp://guest:guest@localhost:5672//` | адрес RabbitMQ |
| `REC_NEWS_CONSUME_NEWS_EVENTS` | `1` (по умолчанию) | воркер слушает `NewsDiscovered` и `AnalyzeNews` |
| `REC_NEWS_QDRANT_URL` | `http://localhost:6333` | Qdrant-сервер; **обязателен** с Celery |

Qdrant-сервер нужен потому, что с векторами работают API и воркеры одновременно, а локальную базу
`data/qdrant` открывает только один процесс.

Инфраструктура для разработки — `docker-compose.yml`:

```powershell
docker compose up -d     # PostgreSQL :5432, RabbitMQ :5672 (панель http://localhost:15672, guest/guest), Qdrant :6333
```

Если порт 5432 уже занят локальным PostgreSQL, задайте другой: `REC_NEWS_PG_PORT=15432 docker compose up -d`.
Тогда и в `REC_NEWS_DATABASE_URL` укажите `:15432`. Если подключаетесь к уже работающему PostgreSQL,
контейнер не нужен: достаточно указать его адрес в `REC_NEWS_DATABASE_URL`. Базу (`CREATE DATABASE`)
создаёт её владелец, схемы и таблицы ML создаёт сама.

## Запуск

```powershell
.venv/Scripts/python scripts/init_db.py                       # схемы ml_*, таблицы, коллекции Qdrant
.venv/Scripts/python scripts/migrate_to_postgres.py --qdrant   # перенос данных из data/*.db, dataset/*.db и data/qdrant
.venv/Scripts/python scripts/run_api.py --port 8002            # API: ставит задачи, читает результаты
.venv/Scripts/celery -A news_pipeline.tasks.celery_app worker --pool threads --concurrency 4 --loglevel info
```

**Воркер.** Пул `threads` используется везде, не только на Windows, где других пулов нет: модель
эмбеддингов загружается один раз на процесс. `--concurrency` задаёт, сколько задач выполняется
одновременно. Сервис внутри воркера создаётся при первой задаче, поэтому первая задача ждёт загрузки
модели и подключения к базам. Можно запустить несколько воркеров, в том числе на разных машинах.
Обработка новостей при этом остаётся последовательной: дедупликация и запуск `processing.run` идут под
блокировкой `pipeline`, общей для всех процессов (`pg_advisory_lock`).

**Перенос данных** (`scripts/migrate_to_postgres.py`):
- копирует строки без замены, поэтому повторный запуск безопасен;
- `--replace` сначала очищает таблицы PostgreSQL;
- `--skip-dataset` пропускает датасет: в нём 1,4 млн платежей, перенос через `COPY` занимает около минуты;
- `--qdrant` копирует векторы из `data/qdrant` в Qdrant-сервер (только векторы, без PostgreSQL —
  `scripts/qdrant_to_server.py`).

## Очереди и обмен

```
Parser ──► exchange news.events (topic) ──news.discovered──► analytics.news ─┐
Backend ──► analytics.commands (AnalyzeNews) ────────────────────────────────┤
                                                                             ▼
                                              воркер ML: NewsEventsConsumer → задача analytics.analyze
                                                                             │  обработка новости
                                                                             ▼
Backend ◄── analytics.events (очередь) ◄─analysis.#─ exchange analytics.events (topic)
                                                     AnalysisCompleted / AnalysisFailed
```

| Имя | Тип | Кто пишет → кто читает |
|---|---|---|
| `news.events` | topic-exchange | Parser → подписчики (`news.discovered`) |
| `analytics.news` | durable-очередь, привязана к `news.events` по `news.discovered` | ML (своя копия события, схема 3) |
| `analytics.commands` | durable-очередь (default exchange) | Backend → ML: `AnalyzeNews` (схемы 1–2) |
| `analytics.events` | topic-exchange + durable-очередь `analytics.events` (`analysis.#`) | ML → Backend |
| `processing.run`, `insights.generate`, `analytics.analyze`, `feeds.refresh` | очереди задач Celery | API/ML → воркеры ML |
| `ml.dlx` → `<очередь>.dlq` | очереди отказов | сообщения, не обработанные после всех повторов |

Exchange и очереди объявляет воркер при старте. Объявлять их повторно безопасно. Если Backend хочет
свою копию `news.discovered`, ему нужна своя очередь, привязанная к `news.events`: например,
`backend.news`. Общую очередь с ML читать нельзя, иначе подписчики будут забирать сообщения друг у друга.

## Контракт сообщений

Все сообщения — JSON (`content_type: application/json`, UTF-8). В сообщении передаётся только ID, а не
текст новости.

**Вход — новость появилась:**

```json
{"type": "NewsDiscovered", "news_id": "12345"}
```

- В exchange `news.events` с ключом `news.discovered`.
- `news_id` — строка или число.
- Тип можно передать заголовком `type` вместо поля.
- Лишние поля (`run_id` и др.) игнорируются.
- События других типов (`ParsingFinished`) подтверждаются и пропускаются.

**Вход — команда Backend:**

```json
{"type": "AnalyzeNews", "news_id": "12345"}
```

- В очередь `analytics.commands`.
- Обрабатывается так же, как `NewsDiscovered`.
- Повторная команда на новость в статусе `failed` запускает ещё одну попытку.

**Выход — результат** (exchange `analytics.events`, ключ `analysis.completed` или `analysis.failed`):

```json
{"version": 1, "type": "AnalysisCompleted", "news_id": "12345", "status": "ready",
 "event_id": "e_12345", "unique": true, "error": null, "finished_at": "2026-10-24T19:12:25Z"}
```

```json
{"version": 1, "type": "AnalysisFailed", "news_id": "12345", "status": "failed",
 "event_id": null, "unique": null, "error": "новости 12345 нет в базе новостей ML", "finished_at": "…"}
```

- `event_id` — событие, к которому дедупликация отнесла новость.
- `unique: false` означает, что новость — дубль уже известной: её `event_id` совпадает с `event_id` оригинала.
- Нечитаемое сообщение (не JSON, нет `news_id`) сразу уходит в `analytics.news.dlq` или
  `analytics.commands.dlq`.

## Где новость и её статус

Новость с текстом должна лежать в базе новостей ML (таблица `news`, схема `ml_news`, колонки —
`docs/news_db.md`) со статусом `new` **до** отправки `NewsDiscovered`. Это COMMIT, а затем outbox,
как на схеме. Если сообщение пришло раньше записи, задача повторяется через 30, 60 … 960 с.
Всего это около 32 минут; потом новость получает `AnalysisFailed`.

Статусы новости в ML: `new` → `processing` → `ready`. Если после всех повторов обработать не удалось,
статус становится `failed`.

Обработка одной новости (`NewsAnalysis.handle`, тот же код, что у `POST /processing.enqueue`):
1. подготовка: очистка, лид, обогащение LLM. Идёт параллельно с другими новостями;
2. под блокировкой `pipeline` — дедупликация и события, затем подбор аналитики банка;
3. отправка `AnalysisCompleted`;
4. пересчёт лент пользователей. Задача `feeds.refresh` ставится с задержкой
   `feeds_refresh_delay_s`, чтобы на пачку новостей пришёлся один пересчёт; она запускает
   `processing.run`. Если идёт другой запуск, задача проверяет снова через `feeds_refresh_retry_s`.

Параметры повторов и пересчёта лент — раздел `[queue]` в `config/ml.toml`. Если `refresh_feeds = false`,
ленты пересчитываются только через `POST /processing.enqueue`.

## Гарантии

- **ACK после надёжного эффекта.** Задача подтверждается после обработки (`acks_late`). Если воркер
  упал, задачу получит другой. Входящее событие подтверждается после того, как брокер принял задачу
  `analytics.analyze` (publisher confirms).
- **Идемпотентность.** Повторная доставка новости в статусе `ready` только заново отправляет
  `AnalysisCompleted`. Повторная доставка запуска или карточки пропускается: состояние хранится в базе.
- **Повторы ограничены**, после них сообщение уходит в `<очередь>.dlq`.
- **Без своего outbox.** `AnalysisCompleted` отправляется после COMMIT. Если отправка не удалась, задача
  повторяется и событие уходит снова. Доставка — «как минимум один раз»; получатели обрабатывают её по
  `news_id` идемпотентно.

## PostgreSQL

`news_pipeline/storage/db.py`: каждая бывшая база-файл становится схемой одной базы PostgreSQL:
`ml_news`, `ml_new_summary`, `ml_unique`, `ml_analytics`, `ml_profiles`, `ml_recommendations`,
`ml_insight_cards`, `ml_jobs`, `ml_chat`, `ml_feedback`, `ml_llm_cache`, а также датасет — `ml_ur_lica`,
`ml_payment`, `ml_fiz_lica`.

- Схема подставляется через `schema_translate_map`, поэтому описания таблиц и запросы те же, что для SQLite.
- `INSERT … ON CONFLICT` работает одинаково в обеих базах.
- Взятие новостей в работу и создание запуска выполняются под `pg_advisory_xact_lock` (в SQLite —
  `BEGIN IMMEDIATE`).
- Схемы и таблицы создаются автоматически, если их нет (как раньше файлы SQLite).

Скрипты оценки (`scripts_for_tests/prepare_eval_news.py`, `eval_feed.py`) всегда работают на локальных
файлах `data_eval/`: они не трогают общий PostgreSQL, Qdrant-сервер и брокер.
