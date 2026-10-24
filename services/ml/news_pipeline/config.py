"""Настройки окружения: пути, подключение к моделям — в одном месте.

Значения берутся из переменных окружения и файла ``.env`` (см. :mod:`env`
и ``.env.example``); если переменная не задана — значение по умолчанию ниже.

Структура:

    data/news.db            входящие новости
    data/new_summary.db     подготовленные новости (заголовок + первый абзац)
    data/unique.db          уникальные новости после дедупликации
    data/qdrant/storage/    локальная база Qdrant (коллекции news, news_audience)
    models/                 локальные модели эмбеддингов
    dataset/                ur_lica.db, payment.db, fiz_lica.db (датасет в SQLite, см. storage.dataset_db)
"""

from __future__ import annotations

from datetime import datetime, timezone

from news_pipeline.env import ROOT_DIR, env_flag, env_float, env_path, env_str

# --- пути ---------------------------------------------------------------------

# Каталог баз можно переопределить (тесты, демо на чистых данных).
DATA_DIR = env_path("REC_NEWS_DATA_DIR", ROOT_DIR / "data")
NEWS_DB = DATA_DIR / "news.db"
SUMMARY_DB = DATA_DIR / "new_summary.db"
UNIQUE_DB = DATA_DIR / "unique.db"
QDRANT_PATH = DATA_DIR / "qdrant" / "storage"
# Qdrant-сервер вместо локальной базы: нужен, когда с базой работают несколько процессов
# (API и воркеры Celery) — локальную базу открывает только один процесс.
QDRANT_URL = env_str("REC_NEWS_QDRANT_URL")
QDRANT_API_KEY = env_str("REC_NEWS_QDRANT_API_KEY")

MODELS_DIR = env_path("REC_NEWS_MODELS_DIR", ROOT_DIR / "models")

# Синтетические тестовые данные лежат в репозитории (сдаются вместе с кодом).
FIXTURES_DIR = ROOT_DIR / "fixtures"

DATASET_DIR = env_path("REC_NEWS_DATASET_DIR", ROOT_DIR / "dataset")

# --- модели -------------------------------------------------------------------

EMBEDDING_MODEL = env_str("REC_NEWS_EMBEDDING_MODEL", "deepvk/USER-bge-m3")
# OCR сканов в PDF аналитики (scripts/ocr_analytics.py, EasyOCR): языки через запятую и GPU.
OCR_LANGUAGES = [code.strip() for code in (env_str("REC_NEWS_OCR_LANGUAGES", "ru,en") or "").split(",") if code.strip()]
OCR_GPU = env_flag("REC_NEWS_OCR_GPU", False)

# OpenAI-совместимый API: при разработке LM Studio, на хакатоне — разрешённая модель.
LLM_BASE_URL = env_str("REC_NEWS_LLM_BASE_URL", "http://127.0.0.1:1234/v1")
LLM_MODEL = env_str("REC_NEWS_LLM_MODEL")  # None — первая загруженная модель сервера
LLM_API_KEY = env_str("REC_NEWS_LLM_API_KEY")

# Разрешённые модели хакатона работают по одному ключу; каждая задача идёт
# в свою модель. Здесь — имена моделей на сервере (как в GET /models) по
# классам, а какая задача к какому классу относится — [llm_tasks] в ml.toml.
# Не задано — используется REC_NEWS_LLM_MODEL (в LM Studio одна qwen3-8b на всё).
LLM_MODELS: dict[str, str | None] = {
    "light": env_str("REC_NEWS_LLM_MODEL_LIGHT"),          # Gemma 4 26B A4B
    "reasoning": env_str("REC_NEWS_LLM_MODEL_REASONING"),  # GLM-5.3-Flash
    "universal": env_str("REC_NEWS_LLM_MODEL_UNIVERSAL"),  # Qwen3.6 35B A3B
    "relations": env_str("REC_NEWS_LLM_MODEL_RELATIONS"),  # DeepSeek-V4.1-Flash
    "vision": env_str("REC_NEWS_LLM_MODEL_VISION"),        # Qwen3-VL-30B-A3B-Instruct
    "text": env_str("REC_NEWS_LLM_MODEL_TEXT"),            # Kimi-K2.6
}
LLM_TIMEOUT_S = env_float("REC_NEWS_LLM_TIMEOUT_S", 180.0)
LLM_TEMPERATURE = env_float("REC_NEWS_LLM_TEMPERATURE", 0.0)

# «Текущий момент» для демо на синтетических новостях (их период заканчивается 21.10.2026):
# окно ленты и свежесть считаются от него. Пусто — реальное время.
_DEMO_NOW = env_str("REC_NEWS_DEMO_NOW")
DEMO_NOW = datetime.fromisoformat(_DEMO_NOW).replace(tzinfo=None).replace(tzinfo=timezone.utc) if _DEMO_NOW else None

# SQLite не создаёт папки сам, поэтому каталоги для баз готовим заранее.
QDRANT_PATH.parent.mkdir(parents=True, exist_ok=True)
