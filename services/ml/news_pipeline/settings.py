"""Параметры ML-части из ``config/ml.toml``.

    from news_pipeline.settings import SETTINGS
    SETTINGS.ranking["weights"]["sem"]

Файл читается один раз при импорте. Для экспериментов можно загрузить
другой файл через :func:`load_settings`. Подключение к моделям (адрес LLM,
модель, ключ) — не здесь, а в переменных окружения: см. :mod:`config`.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from news_pipeline.config import LLM_MODEL, LLM_MODELS, ROOT_DIR

CONFIG_PATH = ROOT_DIR / "config" / "ml.toml"


@dataclass(frozen=True)
class Settings:
    """Разделы ``ml.toml`` как словари."""

    pipeline_version: str
    prompts: dict[str, str]
    profile: dict[str, Any]
    news: dict[str, Any]
    dedup: dict[str, Any]
    ranking: dict[str, Any]
    analytics: dict[str, Any]
    llm_tasks: dict[str, str]
    agent: dict[str, Any]
    llm_reasoning: dict[str, str]
    recommendations: dict[str, Any]
    insight_card: dict[str, Any]


def load_settings(path: str | Path = CONFIG_PATH) -> Settings:
    """Читает TOML-файл параметров."""
    with Path(path).open("rb") as file:
        data = tomllib.load(file)
    return Settings(
        pipeline_version=data["pipeline_version"],
        prompts=data["prompts"],
        profile=data["profile"],
        news=data["news"],
        dedup=data["dedup"],
        ranking=data["ranking"],
        analytics=data["analytics"],
        llm_tasks=data.get("llm_tasks", {}),
        agent=data.get("agent", {}),
        llm_reasoning=data.get("llm_reasoning", {}),
        recommendations=data.get("recommendations", {"per_user": 20, "window_days": 7, "feed_version": 4}),
        insight_card=data.get("insight_card", {}),
    )


SETTINGS = load_settings()


def llm_model_for(task: str) -> str | None:
    """Имя модели для задачи LLM: класс из ``[llm_tasks]`` → модель из ``.env``.

    ``None`` — модель не задана, сервер отдаст первую загруженную.
    """
    model_class = SETTINGS.llm_tasks.get(task) or SETTINGS.llm_tasks.get("default", "universal")
    return LLM_MODELS.get(model_class) or LLM_MODEL


def llm_reasoning_for(task: str) -> str | None:
    """``reasoning_effort`` задачи из ``[llm_reasoning]``; ``None`` — параметр не передаётся."""
    value = SETTINGS.llm_reasoning.get(task, SETTINGS.llm_reasoning.get("default", ""))
    return value or None
