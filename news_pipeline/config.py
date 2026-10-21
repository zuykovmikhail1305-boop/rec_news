"""Пути проекта: базы данных, Qdrant, модели и датасет — в одном месте.

Структура:

    data/news.db            входящие новости
    data/new_summary.db     подготовленные новости (заголовок + первый абзац)
    data/unique.db          уникальные новости после дедупликации
    data/qdrant/storage/    локальная база Qdrant (коллекции news, news_audience)
    models/                 локальные модели эмбеддингов
    dataset/                ur_lica.csv, payment.csv, fiz_lica.csv
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]

# Каталог баз можно переопределить переменной окружения (тесты, демо на чистых данных).
DATA_DIR = Path(os.environ.get("REC_NEWS_DATA_DIR") or ROOT_DIR / "data")
NEWS_DB = DATA_DIR / "news.db"
SUMMARY_DB = DATA_DIR / "new_summary.db"
UNIQUE_DB = DATA_DIR / "unique.db"
QDRANT_PATH = DATA_DIR / "qdrant" / "storage"

MODELS_DIR = ROOT_DIR / "models"

# Синтетические тестовые данные лежат в репозитории (сдаются вместе с кодом).
FIXTURES_DIR = ROOT_DIR / "fixtures"

DATASET_DIR = ROOT_DIR / "dataset"
COMPANIES_CSV = DATASET_DIR / "ur_lica.csv"
PAYMENTS_CSV = DATASET_DIR / "payment.csv"

# SQLite не создаёт папки сам, поэтому каталоги для баз готовим заранее.
QDRANT_PATH.parent.mkdir(parents=True, exist_ok=True)
