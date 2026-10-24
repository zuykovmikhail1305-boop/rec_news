# rec_news

- `services/backend/` — веб-сервис: выбор демо-профиля без авторизации, главная страница и отображение инсайтов. Запуск: `python -m uvicorn backend.main:app --port 8000` из `services/backend`.
- `services/ml/` — аналитика, рекомендации и генерация инсайтов. [Инструкция](services/ml/README.md). Запуск: `python scripts/run_api.py --port 8002` из `services/ml`.
- `services/parser/` — независимый сбор публикаций и очередь обработки, без ML. [Инструкция](services/parser/README.md).
- `app/` — прежние модели новостей и TASS-парсер.
- `docs/` — схемы и документация.

## Парсер

PowerShell, из корня репозитория:

```powershell
cd services/parser
uv sync --locked
uv run scripts/init_db.py
uv run scripts/run_api.py
```

Во втором терминале из `services/parser`:

```powershell
uv run scripts/run_worker.py
```

API: http://127.0.0.1:8001/docs. Воркер нужен для обработки `/queue`.
Сервисы запускаются независимо, каждый со своими зависимостями и настройками.

## Общее хранилище — требование дальнейшей разработки

Сейчас парсер и ML используют отдельные базы и собственные наборы данных.
Автоматический перенос публикаций из БД парсера в ML пока не реализован.
Целевая архитектура — одна общая БД для информации всех сервисов;
сервисы остаются отдельными процессами. Требования к переходу:
[единая БД](docs/shared-database.md).
