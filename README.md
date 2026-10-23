# rec_news

- `services/web/` — независимый веб-сервис: FastAPI, авторизация, интерфейс,
  шаблоны, статика и UI-демо. Запуск и конфигурация: [README](services/web/README.md).
- `app/` — существующие модели новостей, инициализация БД и TASS-парсер.
  Зависимости: `requirements-parser.txt`, проверки: `python -m unittest discover -s tests`.
- `docs/` — схема новостей и документация парсера.

Веб запускается из `services/web` командой `python -m uvicorn web_app.main:app`.
Его изменения и зависимости держите внутри `services/web/`, чтобы уменьшить
пересечения с разработкой других сервисов.
