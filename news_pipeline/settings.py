"""Параметры ML-части из ``config/ml.toml``.

    from news_pipeline.settings import SETTINGS
    SETTINGS.ranking["weights"]["sem"]

Файл читается один раз при импорте. Для экспериментов можно загрузить
другой файл через :func:`load_settings`.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from news_pipeline.config import ROOT_DIR

CONFIG_PATH = ROOT_DIR / "config" / "ml.toml"


@dataclass(frozen=True)
class Settings:
    """Разделы ``ml.toml`` как словари."""

    pipeline_version: str
    models: dict[str, Any]
    prompts: dict[str, str]
    profile: dict[str, Any]
    news: dict[str, Any]
    dedup: dict[str, Any]
    ranking: dict[str, Any]
    analytics: dict[str, Any]


def load_settings(path: str | Path = CONFIG_PATH) -> Settings:
    """Читает TOML-файл параметров."""
    with Path(path).open("rb") as file:
        data = tomllib.load(file)
    return Settings(
        pipeline_version=data["pipeline_version"],
        models=data["models"],
        prompts=data["prompts"],
        profile=data["profile"],
        news=data["news"],
        dedup=data["dedup"],
        ranking=data["ranking"],
        analytics=data["analytics"],
    )


SETTINGS = load_settings()
