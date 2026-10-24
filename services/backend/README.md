# Веб-сервис

Главная страница открывается без входа и cookie-сессии. При открытии `/` выбирается
первый доступный профиль ML. На `/profiles` можно выбрать другую компанию.
Выбранный `user_id` передаётся в URL главной, карточки и API-запросов.
Если профилей нет, отображается пустой список; при недоступности ML — сообщение об ошибке.

## Запуск (PowerShell)

```powershell
cd D:/repos/rec_news/services/backend
uv sync --locked
$env:ML_API_URL='http://127.0.0.1:8002'
uv run python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Открыть http://127.0.0.1:8000/. `SECRET_KEY`, JWT, CSRF и настройка сессий не нужны.
Сервис предназначен для локальной демонстрации: данные профилей доступны без авторизации.

## Инсайты

Главная запускает через JavaScript `POST /api/insights/generate?user_id=...` с `refresh=false`,
затем получает ленту и статус через `GET /api/insights?user_id=...`.
Карточка: `/insights/{news_id}?user_id=...`; обновление карточки —
`GET /api/insights/{news_id}?user_id=...`. Для API `user_id` обязателен.
Чтение страниц и опрос состояния сами не запускают генерацию или глобальную обработку.

## Проверки

```powershell
uv run python -m unittest discover -s tests -p "test_*.py" -v
```
