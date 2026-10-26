#!/bin/sh
# API, воркер и планировщик Celery в одном контейнере: падение любого — остановка контейнера.
uv run scripts/run_api.py &
uv run scripts/run_celery.py worker &
uv run scripts/run_celery.py beat &
wait -n 2>/dev/null || wait
exit 1
