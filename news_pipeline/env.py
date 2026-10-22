"""Переменные окружения из файла ``.env`` в корне проекта.

Всё, что зависит от машины и способа запуска (пути, адрес LLM-сервера, ключ,
модели, адрес API), задаётся переменными ``REC_NEWS_*``; шаблон с описанием
каждой — ``.env.example``. Параметры алгоритмов (веса, пороги, окна, версии
промптов) остаются в ``config/ml.toml``.

Переменные, уже заданные в окружении процесса (в том числе флагами скриптов
вроде ``--data-dir`` и ``--no-llm``), важнее значений из ``.env``.

Модуль не импортирует ничего из пакета, поэтому его можно подключить в скрипте
до разбора аргументов командной строки.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT_DIR / ".env"

load_dotenv(ENV_FILE, override=False)

_FALSE = ("0", "false", "no", "off")


def env_str(name: str, default: str | None = None) -> str | None:
    """Строка из окружения; пустое значение равносильно отсутствию переменной."""
    value = (os.environ.get(name) or "").strip()
    return value or default


def env_int(name: str, default: int) -> int:
    value = env_str(name)
    return default if value is None else int(value)


def env_float(name: str, default: float) -> float:
    value = env_str(name)
    return default if value is None else float(value)


def env_flag(name: str, default: bool = True) -> bool:
    """Флаг: ``0``, ``false``, ``no``, ``off`` — выключен, любое другое значение — включён."""
    value = env_str(name)
    return default if value is None else value.lower() not in _FALSE


def env_path(name: str, default: Path) -> Path:
    """Путь из окружения; относительный путь считается от корня проекта."""
    value = env_str(name)
    if value is None:
        return default
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT_DIR / path
