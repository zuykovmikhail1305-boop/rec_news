# Генерация всех карточек инсайтов пользователя

Бэкенд одной ручкой запускает построение карточек для всех новостей пользователя. Карточки
строятся в фоне через очередь задач. Ход работы виден по заданию.

## Ручки

| Ручка | Что делает |
|---|---|
| `POST /users/{user_id}/insights/generate` | поставить задание → `202` и задание. Если у пользователя задание уже идёт, новое не создаётся: возвращается текущее с кодом `200` |
| `GET /insights/jobs/{job_id}` | статус задания и каждой карточки |
| `GET /users/{user_id}/insights/jobs/latest` | последнее задание пользователя |
| `POST /insights/jobs/{job_id}/cancel` | отменить: ещё не начатые карточки не строятся, начатые достраиваются |

Тело `POST …/generate` необязательно:

```json
{"news_ids": null, "limit": null, "refresh": false}
```

| Поле | Что значит |
|---|---|
| `news_ids` | построить только для этих новостей; по умолчанию — для всех новостей пользователя из `user_news` |
| `limit` | только для первых N новостей ленты |
| `refresh` | строить заново и готовые карточки; без него готовая карточка с неизменными входными данными берётся из базы мгновенно |

Ошибки:
- `404` — нет профиля или новости из `news_ids`;
- `409` — у пользователя ещё нет подобранных новостей, сначала нужен `POST /pipeline/run`.

Ответ — задание:

```json
{"job_id": "ij_20261023_160501_a1b2c3", "user_id": "u_01", "status": "running", "refresh": false,
 "created_at": "…", "started_at": "…", "finished_at": null,
 "total": 20, "queued": 16, "running": 2, "done": 2, "failed": 0, "cancelled": 0,
 "items": [{"news_id": "syn_stock_market_5", "position": 1, "status": "done", "card_id": "ic_…",
            "card_status": "ready", "headline": "…", "error": null, "started_at": "…", "finished_at": "…"}]}
```

**Статус задания:**
- `queued` → `running` → `done`, если построена хотя бы одна карточка;
- `failed` — не построено ни одной;
- `cancelled` — задание отменено.

**Статус карточки в `items`:**
- `queued`, `running`, `done`, `failed` (причина — в `error`), `cancelled`;
- `card_status` — статус самой карточки: `ready`, `partial` или `degraded`.

Сценарий бэкенда:
1. `POST …/generate`.
2. Опрашивать `GET /insights/jobs/{job_id}`, пока `status` не перестанет быть `queued` или `running`.
3. Каждая построенная карточка сразу доступна в `GET /users/{id}/news/{news_id}/insight`, а её заголовок и чипы — в поле `insight` ленты `GET /users/{id}/news`. Ждать конца всего задания не нужно.

Время и стоимость — как у одной карточки (`docs/insight_card.md`), умноженные на число новостей.
Одновременно строятся `REC_NEWS_INSIGHT_WORKERS` карточек (по умолчанию 2).

## Как устроено

```
POST …/generate ─► InsightJobs.start ─► jobs.db: задание + строка на новость
                                    └─► publish("insights.generate", InsightTask) × N
очередь ─► InsightJobs.handle(body) ─► карточка (InsightCardAgent) ─► jobs.db: результат строки
```

| Файл | Что внутри |
|---|---|
| `news_pipeline/tasks/messages.py` | сообщение `InsightTask` (JSON) и имя очереди `insights.generate` |
| `news_pipeline/tasks/queue.py` | интерфейс `TaskQueue` (`publish`, `subscribe`, `emit`, `close`), повтор `RetryLater`, реализация `LocalQueue` и выбор реализации `create_queue()` |
| `news_pipeline/tasks/celery_app.py` | Celery + RabbitMQ: `CeleryQueue`, задачи воркера, приём событий других сервисов |
| `news_pipeline/tasks/insight_jobs.py` | постановка задания и обработчик сообщения |
| `news_pipeline/tasks/store.py` | состояние заданий в `data/jobs.db` (в PostgreSQL — схема `ml_jobs`) |

**`data/jobs.db`:**
- `insight_jobs` — задание: `job_id`, `user_id`, `status`, `refresh`, время создания, начала и конца;
- `insight_job_items` — строка на новость: `position`, `status`, `card_id`, `card_status`, `headline`, `error`, время.

Счётчики задания не хранятся: они считаются по строкам, поэтому одновременные обработчики не затирают друг друга.

Очередь выбирается переменной `REC_NEWS_QUEUE`:
- `local` (по умолчанию) — пул потоков внутри процесса API (`LocalQueue`). Сообщения в памяти теряются
  при перезапуске, поэтому при старте API незавершённые карточки и запуски обработки снова ставятся
  в очередь (`resume_unfinished`, `resume_unfinished_runs`);
- `celery` — Celery + RabbitMQ: API только ставит задачи, выполняют их воркеры Celery, сообщения хранит
  брокер. Как запустить и как устроен обмен с другими сервисами — `docs/broker.md`.

## Очереди

| Очередь (имя задачи) | Сообщение | Обработчик | Состояние | Ручки |
|---|---|---|---|---|
| `processing.run` | `ProcessingTask`: `run_id`, `now`, `user_ids`, `insights_top` | `MLService.handle_processing` | `recommendations.db`, `pipeline_runs` | `POST /processing.enqueue`, `GET /processing.result` |
| `insights.generate` | `InsightTask`: `job_id`, `user_id`, `news_id`, `refresh` | `InsightJobs.handle` | `jobs.db` | `POST /users/{id}/insights/generate` и ручки заданий |
| `analytics.analyze` | `AnalyzeNewsTask`: `news_id` | `NewsAnalysis.handle` | статус новости в `news.db` | — (сообщения `NewsDiscovered` / `AnalyzeNews`, docs/broker.md) |
| `feeds.refresh` | `FeedsRefreshTask` | `NewsAnalysis.handle_feeds` | `pipeline_runs` | — (после обработки новостей) |

Ручки только создают запись в базе и публикуют сообщение, а статус читают из базы. Работу делает
обработчик очереди, и ему всё равно, в каком процессе он запущен. Обработка новостей — одна за раз:
новый запуск создаётся атомарно и только если другой не стоит в очереди и не идёт. Запуск, который
идёт дольше `run_timeout_hours` (`config/ml.toml`, по умолчанию 3 ч), считается упавшим.

## Celery

Переход сделан: `CeleryQueue` (`news_pipeline/tasks/celery_app.py`) реализует тот же интерфейс, обработчики
не изменились. Имя задачи Celery = имя очереди, аргумент — JSON-сообщение; подтверждение — после
обработки (`acks_late`), повторная доставка безопасна (взятый запуск или построенная карточка
пропускаются). Обработка новостей по-прежнему одна за раз: запуск создаётся атомарно, а сама обработка
идёт под блокировкой `pipeline`, общей для всех процессов (`pg_advisory_lock` в PostgreSQL).
Запуск воркеров, Qdrant-сервер и PostgreSQL — `docs/broker.md`.
