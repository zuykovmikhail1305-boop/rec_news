"""Тексты промптов LLM — ``config/prompts.toml``.

    from news_pipeline.prompts import prompt
    _RERANK_PROMPT = prompt("feed.rerank")

Файл читается один раз при импорте. Изменили текст промпта — поднимите его версию
в ``[prompts]`` файла ``config/ml.toml``, иначе из кэша LLM придут ответы на старый текст.
"""

from __future__ import annotations

import tomllib

from news_pipeline.env import ROOT_DIR

PROMPTS_PATH = ROOT_DIR / "config" / "prompts.toml"

with PROMPTS_PATH.open("rb") as _file:
    _PROMPTS = tomllib.load(_file)


def prompt(path: str) -> str:
    """Текст промпта по пути ``раздел.ключ`` (например, ``insight_card.blocks.fact``)."""
    node = _PROMPTS
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(f"нет промпта {path} в {PROMPTS_PATH}")
        node = node[part]
    if not isinstance(node, str):
        raise KeyError(f"{path} в {PROMPTS_PATH} — раздел, а не текст промпта")
    return node
