# Веб-сервис rec_news

Самостоятельный FastAPI-сервис: регистрация, вход, анкета и интерфейс инсайтов.
Весь код, шаблоны, статика, зависимости, UI-демо и тесты веба находятся здесь.
Импортов из корневого `app/` нет. Парсер и его модели остаются в `app/`.

## Запуск (PowerShell, Python 3.12+)

Из корня репозитория:

```powershell
cd services/web
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
$env:SECRET_KEY = "replace-with-a-random-secret"
.venv/Scripts/python -m uvicorn web_app.main:app --host 127.0.0.1 --port 8000
```

На Linux используйте `.venv/bin/python`. Виртуальное окружение сервиса независимо
от окружений других сервисов. Точка входа из корня репозитория:
`python -m uvicorn web_app.main:app --app-dir services/web`.

Открыть `/login`, `/register`, `/insights/prototype` или `/insights/parser`.

## Конфигурация и данные

- `WEB_DATABASE_URL`: по умолчанию SQLite `web_app/web.db`, независимо от рабочей папки.
- `SECRET_KEY`: ключ подписи cookie; задайте перед запуском в production.
- `ENV=prod`: включает secure-cookie (для HTTPS).

Сервис создаёт только таблицы `users` и `user_profiles`. Настройка `DATABASE_URL`
парсера на веб не влияет. Чтобы сохранить существующие аккаунты из `app/alfa.db`,
остановите старый веб и укажите `WEB_DATABASE_URL=sqlite:///D:/repos/rec_news/app/alfa.db`
либо скопируйте старую базу в `web_app/web.db` перед первым запуском.
Парсер не должен использовать отдельную веб-базу.

## Проверки

Из `services/web` с активированным окружением:

```sh
python -m unittest discover -s tests -p "test_*.py" -v
python scripts/build-parser-demo.py
node --check web_app/static/parser.js
node tests/parser-demo.cjs
```

Python-тесты проверяют обработчики, шаблоны, авторизацию и изоляцию таблиц без HTTP-сервера.
Для браузерного теста нужны Playwright и Chromium; подробнее в `docs/parser-demo.md`.

## Граница разработки

Изменения веба вносите в `services/web/`. Не добавляйте его зависимости и модели
в `app/` или `requirements-parser.txt`. Будущие интеграции с парсером оформляйте
через явный API/контракт, без импорта его внутренних модулей. Перенос уменьшает
пересечение файлов между ветками; при первом слиянии изменений старых веб-путей
Git всё ещё может потребовать разрешения конфликтов переименования.
