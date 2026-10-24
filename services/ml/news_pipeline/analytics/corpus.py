"""Подготовка корпуса аналитики (ТЗ «подбор аналитики», §4): очистка и чанкинг.

* :func:`clean_analytics_text` — тот же пайплайн очистки, что для новостей,
  плюс служебные блоки отчётов: колонтитулы, дисклеймеры, контакты,
  оглавления, сноски «Источник:».
* :func:`split_sections` — разделы по ``sections`` материала или по заголовкам
  в тексте (короткая строка без точки в конце).
* :func:`chunk_material` — чанки: раздел ≤ ``chunk_max_tokens`` — один чанк,
  длиннее — по абзацам с перекрытием в 1 абзац, короче ``chunk_min_tokens`` —
  склеивается со следующим; короткий материал целиком — один чанк.
* :func:`load_fixture_materials` — материалы из ``fixtures/analytics``:
  распознанные тексты PDF (``*.txt``) и их метаданные (``materials.json``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from news_pipeline.config import FIXTURES_DIR
from news_pipeline.preprocessing.cleaning import clean_text
from news_pipeline.schemas import AnalyticsIn
from news_pipeline.settings import SETTINGS

ANALYTICS_FIXTURES = FIXTURES_DIR / "analytics"
_CONFIG = SETTINGS.analytics

_PAGE_RE = re.compile(r"^=== стр\. \d+ ===$", re.MULTILINE)
_NOISE_RE = re.compile(
    r"(настоящий (материал|документ|отч[её]т)|не является (индивидуальной )?инвестиционной|"
    r"информационно-аналитическ\w+ материал|@alfabank|alfabank\.ru|www\.|e-?mail|тел\.|телефон|"
    r"\+7\s?\(?\d|все права защищены|дисклеймер|disclaimer|^источник\w*:|^примечани\w*:|"
    r"^содержание$|^оглавление$|\.{4,}\s*\d+$|центр макроэкономического анализа|"
    r"^(рис|табл|график|диаграмма)\w*[.:\s]|^исmочн|^прuмеч|права защищены|генеральная лицензия|"
    r"настоящее письмо|не является офертой|лицензия цб)",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё]+")
# OCR путает похожие латинские и кириллические буквы («Bce права») — для поиска шума приводим к кириллице.
_HOMOGLYPHS = str.maketrans("ABCEHKMOPTXaceopxy", "АВСЕНКМОРТХасеорху")
_MONTHS_RE = re.compile(r"^(янв|фев|мар|апр|май|мая|июн|июл|авг|сен|окт|ноя|дек)", re.IGNORECASE)


def _is_prose(paragraph: str) -> bool:
    """Абзац — связный текст, а не распознанные подписи осей, легенды и таблицы.

    Доля «нормальных» слов (от 3 букв, кириллица без латиницы внутри) среди
    всех токенов должна быть не меньше половины, и таких слов не меньше пяти.
    """
    tokens = paragraph.split()
    words = [w for w in _WORD_RE.findall(paragraph)
             if len(w) >= 3 and re.fullmatch(r"[А-Яа-яЁё-]+", w)]
    return len(words) >= 5 and len(words) / max(len(tokens), 1) >= 0.5
_HEADING_MAX_CHARS = 90


@lru_cache(maxsize=1)
def _tokenizer():
    from news_pipeline.index.embeddig import shared_model

    return shared_model().tokenizer


def count_tokens(text: str) -> int:
    """Число токенов модели эмбеддингов (тот же токенизатор, что у bge-m3)."""
    return len(_tokenizer()(text, add_special_tokens=False)["input_ids"])


def clean_analytics_text(text: str) -> str:
    """Очистка текста материала: разметка страниц, колонтитулы, дисклеймеры, контакты."""
    text = _PAGE_RE.sub("\n\n", text)
    paragraphs = []
    for paragraph in clean_text(text).split("\n\n"):
        paragraph = paragraph.strip()
        if len(paragraph) < 3 or _NOISE_RE.search(paragraph.translate(_HOMOGLYPHS)):
            continue
        if not _is_prose(paragraph) and not _is_heading(paragraph):  # оси, легенды, таблицы
            continue
        paragraphs.append(paragraph)
    # Колонтитулы повторяются на каждой странице — убираем повторы коротких абзацев.
    seen: dict[str, int] = {}
    for paragraph in paragraphs:
        if len(paragraph) <= _HEADING_MAX_CHARS:
            seen[paragraph] = seen.get(paragraph, 0) + 1
    return "\n\n".join(p for p in paragraphs if seen.get(p, 0) < 3)


def _is_heading(paragraph: str) -> bool:
    """Заголовок раздела: короткая строка из настоящих слов, без точки в конце."""
    words = paragraph.split()
    proper = [w for w in words if re.fullmatch(r"[А-ЯЁа-яё][а-яё-]{2,}[,]?", w) and not _MONTHS_RE.match(w)]
    return (
        len(paragraph) <= _HEADING_MAX_CHARS
        and 2 <= len(words) <= 12
        and len(proper) >= 0.8 * len(words)
        and not paragraph.rstrip().endswith((".", ",", ";", ":"))
        and paragraph[:1].isupper()
    )


@dataclass
class Section:
    title: str
    paragraphs: list[str]

    @property
    def text(self) -> str:
        return "\n\n".join(self.paragraphs)


def split_sections(material: AnalyticsIn, text: str) -> list[Section]:
    """Разделы материала: по ``sections`` (если заданы) или по заголовкам в тексте."""
    known = {title.strip().casefold() for title in material.sections}
    sections: list[Section] = [Section(material.title, [])]
    for paragraph in text.split("\n\n"):
        heading = paragraph.casefold() in known if known else _is_heading(paragraph)
        if heading:
            sections.append(Section(paragraph.strip(), []))
        else:
            sections[-1].paragraphs.append(paragraph)
    return [section for section in sections if section.paragraphs]


@dataclass
class DraftChunk:
    section_title: str
    text: str


def _paragraph_pieces(paragraph: str, max_tokens: int) -> list[str]:
    """Абзац длиннее лимита делится по предложениям."""
    if count_tokens(paragraph) <= max_tokens:
        return [paragraph]
    from news_pipeline.preprocessing.cleaning import sentences

    pieces, current = [], []
    for sentence in sentences(paragraph):
        if current and count_tokens(" ".join(current + [sentence])) > max_tokens:
            pieces.append(" ".join(current))
            current = []
        current.append(sentence)
    if current:
        pieces.append(" ".join(current))
    return pieces


def _split_long(section: Section, max_tokens: int) -> list[DraftChunk]:
    """Раздел длиннее лимита — окна по абзацам с перекрытием в один абзац."""
    chunks, window, size = [], [], 0
    paragraphs = [piece for paragraph in section.paragraphs for piece in _paragraph_pieces(paragraph, max_tokens // 2)]
    for paragraph in paragraphs:
        tokens = count_tokens(paragraph)
        if window and size + tokens > max_tokens:
            chunks.append(DraftChunk(section.title, "\n\n".join(window)))
            window, size = [window[-1]], count_tokens(window[-1])
        window.append(paragraph)
        size += tokens
    if window and (not chunks or len(window) > 1):
        chunks.append(DraftChunk(section.title, "\n\n".join(window)))
    return chunks


def chunk_material(material: AnalyticsIn) -> list[DraftChunk]:
    """Чанки материала по правилам §4.2."""
    text = clean_analytics_text(material.text)
    if not text:
        return []
    max_tokens, min_tokens = _CONFIG["chunk_max_tokens"], _CONFIG["chunk_min_tokens"]
    if count_tokens(text) <= max_tokens:
        return [DraftChunk(material.title, text)]
    sections = split_sections(material, text)
    merged: list[Section] = []
    pending: Section | None = None
    for section in sections:
        if pending is not None:
            section = Section(pending.title, pending.paragraphs + section.paragraphs)
            pending = None
        if count_tokens(section.text) < min_tokens:
            pending = section
            continue
        merged.append(section)
    if pending is not None:
        if merged:
            merged[-1].paragraphs.extend(pending.paragraphs)
        else:
            merged.append(pending)
    chunks: list[DraftChunk] = []
    for section in merged:
        if count_tokens(section.text) <= max_tokens:
            chunks.append(DraftChunk(section.title, section.text))
        else:
            chunks.extend(_split_long(section, max_tokens))
    return chunks


def embedding_text(analytics_title: str, section_title: str, text: str) -> str:
    """Текст чанка для эмбеддинга с префиксом «название материала. заголовок раздела.»."""
    prefix = analytics_title if section_title == analytics_title else f"{analytics_title}. {section_title}"
    return f"{prefix}. {text}"


def load_fixture_materials(directory: Path = ANALYTICS_FIXTURES) -> list[AnalyticsIn]:
    """Материалы из ``fixtures/analytics``: тексты ``*.txt`` + метаданные ``materials.json``."""
    meta_path = directory / "materials.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    materials = []
    for path in sorted(directory.glob("*.txt")):
        info = meta.get(path.stem, {})
        published = info.get("published_at")
        if not published:
            continue  # без даты материал нельзя честно использовать (утечка будущего)
        materials.append(
            AnalyticsIn(
                analytics_id=info.get("analytics_id") or f"a_{re.sub(r'[^0-9A-Za-z]+', '_', path.stem).strip('_').lower()}",
                title=info.get("title") or path.stem,
                text=path.read_text(encoding="utf-8"),
                published_at=datetime.fromisoformat(published),
                type=info.get("type"),
                url=info.get("url"),
                author=info.get("author"),
                sections=info.get("sections") or [],
            )
        )
    return materials
