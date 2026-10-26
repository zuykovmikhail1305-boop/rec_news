#/bin/sh
uv run scripts/run_api.py & uv run scripts/run_celery.py worker & uv run scripts/run_celery.py beat
