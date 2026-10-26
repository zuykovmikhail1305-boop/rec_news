# Сервис сбора новостей

Сервис на FastAPI получает публикации по URL, извлекает полный текст и сохраняет
исходный HTML. Новые ссылки обнаруживаются через RSS, обработка выполняется
отдельным worker. Классификация, рекомендации и генерация инсайтов относятся к ML.

Рабочий режим: PostgreSQL + RabbitMQ + Celery worker + Celery Beat.
API по умолчанию доступен на [localhost:8001](http://127.0.0.1:8001/docs).
Для автономной разработки поддерживаются SQLite и локальный worker.

## Подготовка

Нужны Python 3.12+, `uv`, PostgreSQL и RabbitMQ. Команды ниже — для PowerShell,
из каталога `services/parser`:

```powershell
cd D:/repos/rec_news/services/parser
uv sync --locked
# Только при первом запуске, если своего .env ещё нет:
Copy-Item .env.example .env
```

Файл `.env` сам код не загружает. Передавайте `--env-file .env` **каждому** процессу:
инициализации, API, worker, Beat и CLI. Все процессы должны использовать одну БД,
один брокер и одинаковые настройки RSS.

## PostgreSQL и передача в ML

Создайте базу `rec_news` на сервере PostgreSQL и задайте в `.env` реальные реквизиты:

```dotenv
PARSER_DATABASE_URL=postgresql+psycopg://USER:PASSWORD@127.0.0.1:5432/rec_news
PARSER_DATABASE_SCHEMA=public
PARSER_ML_EXPORT_ENABLED=true
PARSER_ML_NEWS_SCHEMA=ml_news
PARSER_AUTO_CREATE_TABLES=false
```

`USER` и `PASSWORD` — заполнители. Спецсимволы пароля в URL нужно кодировать.
Если процессы работают в контейнерах, используйте DNS-имя сервиса PostgreSQL
вместо `127.0.0.1`. При отказе входа проверьте, что порт не занят установленным
на компьютере PostgreSQL.

ML должен использовать ту же базу через `REC_NEWS_DATABASE_URL`. Сначала выполните
его штатную инициализацию из `services/ml` с настроенным подключением:

```powershell
uv run --env-file .env scripts/init_db.py --skip-vectors
```

Описание ML и подготовки его окружения — в [README ML](../ml/README.md).
После этого вернитесь в `services/parser` и создайте таблицы парсера:

```powershell
uv run --env-file .env scripts/init_db.py
uv run --env-file .env scripts/init_db.py --check
```

Парсер создаёт `news_sources`, `news_raw`, `news_quotes`, а также служебные таблицы
очереди и состояния RSS в схеме `public`. `news_quotes` сейчас не заполняется.
Таблицей `ml_news.news` управляет ML: парсер проверяет её наличие и колонки,
но не создаёт её своей инициализацией. Повторная инициализация сохраняет данные;
для старого контракта добавляется nullable-колонка `news_raw.published_date`.
Произвольные изменения существующих схем этот скрипт не мигрирует.

После успешного извлечения в одной транзакции фиксируются:

- `public.news_raw` со статусом `ready`, HTML, текстом и метаданными;
- `ml_news.news` со статусом `new`, текстом, заголовком, URL и источником.

Идентификатор ML: числовой ID публикации (`news_raw.id`), например `1542`. Повторная передача
не перезаписывает запись и не сбрасывает статус обработки ML. Происхождение
хранится в `extra.parser`. Дата без времени сохраняется в `published_date`;
время публикации не выдумывается. Отдельное сообщение в очередь ML не отправляется.

Контракт таблиц: [docs/news-schema.md](../../docs/news-schema.md).
Смена URL БД не переносит ранее собранные данные из SQLite. Для готовых записей,
уже находящихся в PostgreSQL, есть повторная передача в ML:

```powershell
uv run --env-file .env scripts/export_news.py --after-id 0 --limit 100
```

Следующую пачку запускайте с возвращённым `last_id` как `--after-id`.
Эта команда не импортирует SQLite.

## RabbitMQ, worker и Beat

Локальный брокер можно поднять комплектным Compose-файлом:

```powershell
docker compose -f compose.rabbitmq.yaml up -d --wait
```

Он использует порты `5672` и `15672`, пользователя `parser`, пароль `parser_local`
и vhost `rec_news`. Это локальные демо-реквизиты. Если брокер уже запущен другим
Compose-проектом, настройте его URL вместо запуска второго контейнера на тех же портах.
Панель: [RabbitMQ Management](http://127.0.0.1:15672).

```dotenv
PARSER_QUEUE_BACKEND=celery
PARSER_BROKER_URL=amqp://parser:parser_local@127.0.0.1:5672/rec_news
```

Запустите три процесса в отдельных терминалах, каждый из `services/parser`:

```powershell
uv run --env-file .env scripts/run_api.py
```

```powershell
uv run --env-file .env scripts/run_celery.py worker
```

```powershell
uv run --env-file .env scripts/run_celery.py beat
```

RabbitMQ хранит сообщения, worker выполняет задачи, Beat отправляет периодические
задачи. Они не запускаются вместе с API автоматически. Для RSS-сбора API
не обязателен. Запускайте один Beat; на Windows worker по умолчанию использует
`solo`. Dockerfile сервиса запускает только API, отдельные worker и Beat нужны
и при контейнерном развёртывании.

Очереди парсера: `parser.dispatch` и `parser.publications`. Источник истины — БД:
при недоступности RabbitMQ задание остаётся в ней. Beat каждые 10 секунд запускает
восстановление отправки доступных заданий.

## Автоматический сбор RSS

Для сбора Интерфакса и пресс-релизов ЦБ задайте:

```dotenv
PARSER_RSS_ENABLED=true
PARSER_RSS_INTERVAL=300
PARSER_RSS_LIMIT=100
PARSER_RSS_TIMEOUT=15
PARSER_RSS_BACKOFF_MAX=3600
PARSER_RSS_SOURCES={"interfax":"https://www.interfax.ru/rss.asp","cbr":"https://www.cbr.ru/rss/RssPress"}
```

После изменения `.env` перезапустите worker и Beat. В шаблоне `.env.example`
автосбор выключен и указан только Интерфакс; конфигурация выше включает оба источника.

Beat отправляет `parser.poll_rss`. Worker получает ленту, нормализует URL и добавляет
только новые публикации в общую очередь. Другие задачи worker загружают полный HTML.
RSS-заголовки, анонсы и даты не заменяют данные извлечённой статьи.

Состояние каждого опроса хранится в `parser_rss_state` и сохраняется при перезапуске.
Блокировка в БД защищает от одновременного опроса одной ленты. `ETag` и
`Last-Modified` используются, если доступны; ответ HTTP 304 не создаёт задач.
Если лимит оставил необработанные ссылки, валидаторы не сохраняются до разбора остатка.

Ошибки увеличивают паузу до `PARSER_RSS_BACKOFF_MAX`; `Retry-After` учитывается,
для 403/429 пауза составляет минимум 15 минут. Ошибка одного источника не отменяет
остальные. Текущий результат доступен через `GET /rss/status`.

Разовая проверка:

```powershell
# Получить список ссылок без подключения к БД и без загрузки статей:
uv run --env-file .env scripts/poll_rss.py --once --dry-run
# Добавить новые публикации в настроенную очередь:
uv run --env-file .env scripts/poll_rss.py --once
# Игнорировать срок следующего опроса и HTTP-валидаторы:
uv run --env-file .env scripts/poll_rss.py --once --force
```

Разовый CLI работает независимо от `PARSER_RSS_ENABLED`, но учитывает срок опроса.
`--force` не обходит активную блокировку и не создаёт дубли. При ошибке хотя бы
одной ленты разовая команда возвращает код 1. Без `--once` CLI запускает цикл;
для постоянной работы в Celery-режиме используйте Beat.

## HTTP API

Swagger: [http://127.0.0.1:8001/docs](http://127.0.0.1:8001/docs).

- `POST /parse` — извлечь публикацию без сохранения. Тело: `{"url":"https://..."}`.
  Возвращает источник, URL, заголовок, текст, дату/время и версию парсера.
- `POST /collect` — синхронно извлечь и сохранить публикацию. Такое же тело;
  повторный готовый URL возвращается как `skipped` без загрузки статьи.
- `POST /queue` — сохранить задания без загрузки HTML, ответ HTTP 202.
  Тело: `{"urls":["https://...","https://..."]}`, от 1 до 100 URL.
- `GET /queue?status=ready&limit=50&offset=0` — список заданий. `status` необязателен,
  `limit` от 1 до 100. Ответ содержит `items`.
- `GET /queue/{job_id}` — результат задания, включая текст, попытки и ошибку.
- `GET /rss/status` — время последнего и следующего опросов, HTTP-статус,
  количество найденных и добавленных ссылок, последняя ошибка по каждой ленте.

Пример постановки публикации:

```powershell
$body = @{urls=@('https://www.cbr.ru/press/event/?id=32923')} | ConvertTo-Json
$job = Invoke-RestMethod http://127.0.0.1:8001/queue -Method Post -ContentType 'application/json' -Body $body
$job.items
Invoke-RestMethod http://127.0.0.1:8001/rss/status
```

Состояния: `pending` → `processing` → `ready`; после временной ошибки — `retry`,
после постоянной ошибки или исчерпания попыток — `failed`. Максимум 3 попытки,
повторы через 60 и 120 секунд с учётом `Retry-After`. Lease обработки — 15 минут:
зависшее задание можно забрать повторно, устаревший worker не перезапишет результат.
Между началами загрузок одного источника выдерживается минимум 5 секунд;
HTTP 403/429 приостанавливает источник минимум на 15 минут.

Уникальность — по источнику и каноническому URL. Тексты разных URL не объединяются.
Неподдерживаемый URL даёт 422, отсутствующее задание — 404; ошибки загрузки
синхронных обработчиков — 502, пересекающиеся синхронные запросы — 409.
`/parse` и `/collect` используют отдельный процессный ограничитель частоты;
запускайте один API worker. Для регулярного сбора используйте очередь.

## Источники

Поддерживаются URL отдельных публикаций:

- ЦБ: `/press/event/?id=...`, `/press/pr/?id=...`, `/press/PR/?file=...htm`.
  Регистр пути нормализуется; имя файла сохраняется.
- Интерфакс и ТАСС: `/раздел/числовой-id`.
- Ведомости: `/раздел/news/YYYY/MM/DD/id-slug` и `/articles/`.
- Коммерсантъ: `/doc/числовой-id`.
- РИА: `/YYYYMMDD/slug-id.html`.

Наличие адаптера не гарантирует сетевую доступность сайта. На 25.10.2026 живой
сбор проверен для Интерфакса и ЦБ. RSS РИА повторно вернул HTTP 403, поэтому
РИА не включён в рабочий автосбор. Для Ведомостей собираются доступные открытые
публикации; подписка и защиты сайтов не обходятся. Главные страницы и списки
не принимаются как отдельная публикация.

## Остальные настройки

- `PARSER_API_HOST`, `PARSER_API_PORT`: в `.env.example` указаны `0.0.0.0`, `8001`.
  Без переменных скрипт использует `127.0.0.1`, `8001`.
  Аргументы `run_api.py --host ... --port ...` имеют приоритет.
- URL БД выбирается по приоритету `PARSER_DATABASE_URL` → `DATABASE_URL` →
  `REC_NEWS_DATABASE_URL` → SQLite `parser_app/parser.db`.
- `PARSER_DATABASE_SCHEMA`: `public`; `PARSER_ML_NEWS_SCHEMA`: `ml_news`
  либо схема из префикса `REC_NEWS_DB_SCHEMA_PREFIX` + `news`.
- `PARSER_ML_EXPORT_ENABLED`: по умолчанию включён на PostgreSQL и выключен на SQLite.
- `PARSER_AUTO_CREATE_TABLES`: по умолчанию включён на SQLite, выключен на PostgreSQL.
- `PARSER_DB_POOL_SIZE=5`, `PARSER_DB_MAX_OVERFLOW=10`, `PARSER_DB_POOL_TIMEOUT=30`,
  `PARSER_DB_CONNECT_TIMEOUT=10`: пул и таймауты PostgreSQL в секундах.
- `PARSER_RSS_INTERVAL`: секунды, минимум 30; `LIMIT`: новых публикаций за опрос,
  1–1000; `TIMEOUT`: секунды HTTPX, 1–60; `BACKOFF_MAX`: минимум 30 секунд.
- `PARSER_RSS_SOURCES`: JSON кодов адаптеров и HTTPS-адресов лент.

## Локальный режим без RabbitMQ и PostgreSQL

В отдельном локальном `.env` задайте:

```dotenv
PARSER_QUEUE_BACKEND=local
PARSER_DATABASE_URL=sqlite:///D:/repos/rec_news/services/parser/parser_app/parser.db
PARSER_ML_EXPORT_ENABLED=false
PARSER_AUTO_CREATE_TABLES=true
PARSER_RSS_ENABLED=false
```

Запустите API обычной командой, а вместо Celery worker —
`uv run --env-file .env scripts/run_worker.py`. При необходимости в третьем
терминале запустите `uv run --env-file .env scripts/poll_rss.py`.
Celery Beat в таком режиме не нужен. Экспорт в ML на SQLite не поддерживается.

## Проверки и код

```powershell
uv run python -m unittest discover -s tests -v
# Дополнительно: интеграция на тестовом PostgreSQL.
$env:PARSER_TEST_DATABASE_URL='postgresql+psycopg://USER:PASSWORD@127.0.0.1:5432/TEST_DB'
uv run python -m unittest discover -s tests -p test_postgres.py -v
Remove-Item Env:PARSER_TEST_DATABASE_URL
```

PostgreSQL-тесты создают и удаляют случайные тестовые схемы; нужен пользователь
с правами их создания. Без переменной эти проверки пропускаются. Обычный набор
проверяет извлечение, очередь, RSS, повторы, блокировки и передачу задач Celery
без обращения к живым источникам.

Основные модули: `adapters.py` и `publications.py` — URL и извлечение;
`queue.py` — долговечная очередь; `broker.py`, `tasks.py`, `celery_app.py` —
RabbitMQ/Celery; `rss.py` — обнаружение ссылок; `ml_export.py` — запись в ML;
`schema.py` — инициализация и проверка схемы. Все находятся в `parser_app/`.
Старые реализации в корневом `app/` не нужны для запуска этого сервиса.

Адрес прослушивания задаётся `PARSER_API_HOST=0.0.0.0`, порт —
`PARSER_API_PORT=8001`. Скрипт `run_api.py` читает их при запуске с `.env`;
CLI `--host`/`--port` имеет приоритет. `0.0.0.0` — все IPv4-интерфейсы:
локально открывайте `http://127.0.0.1:8001/docs`, с другого компьютера —
`http://IP_КОМПЬЮТЕРА:8001/docs`. Адреса подключения к БД и RabbitMQ
сохраняют реальные хосты. Dockerfile запускает тот же скрипт с настройками
из окружения; проброс порта контейнера должен соответствовать API-порту.
