"""Голос Альфа-Банка и схемы ответов (docs/agent_tz.md §4.1, §11).

* :data:`SCHEMES`, :data:`KINDS` — библиотека схем ответа
  (``config/references/answer_schemes.json``);
* :data:`TONE` — словарь тона (``config/references/tone.json``) и
  скомпилированные регулярки для программной проверки;
* :data:`TONE_PROMPT`, :data:`TONE_PROMPT_SHORT` — общий блок промпта;
* :func:`split_sentences`, :func:`citations`, :func:`strip_citations` — разбор
  текста со ссылками ``[N1]``, ``[A2]``, ``[P]``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from news_pipeline.preprocessing.cleaning import sentences
from news_pipeline.prompts import prompt
from news_pipeline.references import REFERENCES_DIR


def _load(name: str) -> dict:
    with (REFERENCES_DIR / name).open(encoding="utf-8") as file:
        return json.load(file)


_SCHEMES = _load("answer_schemes.json")
SCHEMES: dict[str, dict] = _SCHEMES["schemes"]
KINDS: dict[str, dict] = _SCHEMES["kinds"]

_TONE = _load("tone.json")


def _patterns(items) -> list[tuple[str, re.Pattern]]:
    return [(item, re.compile(r"(?<!\w)" + item, re.IGNORECASE)) for item in items]


@dataclass(frozen=True)
class Tone:
    version: str
    bureaucratic: list[tuple[str, re.Pattern, str]]
    fillers: list[tuple[str, re.Pattern]]
    alarm: list[tuple[str, re.Pattern]]
    categorical: list[tuple[str, re.Pattern]]
    forbidden: list[tuple[str, re.Pattern]]
    bank_voice: list[tuple[str, re.Pattern]]
    bank_mentions: list[tuple[str, re.Pattern]]
    contradiction_markers: list[tuple[str, re.Pattern]]
    abbreviations: frozenset[str]


TONE = Tone(
    version=_TONE["version"],
    bureaucratic=[(key, re.compile(r"(?<!\w)" + key, re.IGNORECASE), value)
                  for key, value in _TONE["bureaucratic"].items()],
    fillers=_patterns(_TONE["fillers"]),
    alarm=_patterns(_TONE["alarm"]),
    categorical=_patterns(_TONE["categorical"]),
    forbidden=_patterns(_TONE["forbidden"]),
    bank_voice=_patterns(_TONE["bank_voice"]),
    bank_mentions=_patterns(_TONE["bank_mentions"]),
    contradiction_markers=_patterns(_TONE["contradiction_markers"]),
    abbreviations=frozenset(_TONE["abbreviations"]),
)

TONE_PROMPT = prompt("tone.full")

TONE_PROMPT_SHORT = (
    prompt("tone.short")
)

_CITATION_RE = re.compile(r"\[\s*((?:[NA]\d+|P)(?:\s*[,;]\s*(?:[NA]\d+|P))*)\s*\]")
_ONLY_CITATIONS_RE = re.compile(r"^(?:\s*\[[^\]]*\]\s*[.,;]?)+$")


def citations(text: str) -> list[str]:
    """ID источников, процитированных в тексте, в порядке появления."""
    found: list[str] = []
    for match in _CITATION_RE.finditer(text):
        for item in re.split(r"[,;]", match.group(1)):
            item = item.strip()
            if item and item not in found:
                found.append(item)
    return found


def strip_citations(text: str) -> str:
    return re.sub(r"\s+([.,;:])", r"\1", _CITATION_RE.sub("", text)).strip()


def split_sentences(text: str) -> list[str]:
    """Предложения; ссылки, оторванные точкой («…14%. [N1]»), приклеиваются к предыдущему."""
    result: list[str] = []
    for sentence in sentences(text):
        if result and _ONLY_CITATIONS_RE.match(sentence):
            result[-1] = f"{result[-1]} {sentence}"
        else:
            result.append(sentence)
    return result


def section_title(kind: str) -> str:
    return KINDS.get(kind, {}).get("title", kind)


def section_origin(kind: str) -> str:
    return KINDS.get(kind, {}).get("origin", "ai")
