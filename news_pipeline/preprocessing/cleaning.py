"""Очистка текста новости (ТЗ §5.1).

* :func:`clean_text` — убирает ссылки, эмодзи, шаблонные хвосты источников
  и лишние пробелы; абзацы (пустая строка) сохраняются;
* :func:`lead` — заголовок + первые предложения для эмбеддингов.

Новости и аналитика приходят из базы уже простым текстом, поэтому HTML
здесь не разбирается.

Унификация кавычек, тире и ё→е для хешей и сравнения — в
:func:`news_pipeline.dedup.fingerprints.normalize`; читаемый текст ими
не искажается.
"""

from __future__ import annotations

import re

from razdel import sentenize

from news_pipeline.settings import SETTINGS

_URL_RE = re.compile(r"(https?://|www\.)\S+", re.IGNORECASE)
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0000FE0F\U0000200D\U00002B00-\U00002BFF]"
)
# Строки-хвосты источников: удаляются целиком, если абзац с них начинается.
_TAIL_PREFIXES = (
    "читайте также",
    "читайте нас",
    "подписывайтесь",
    "подпишитесь",
    "фото:",
    "фото ",
    "видео:",
    "источник:",
    "реклама",
    "новости по теме",
    "поделиться",
)
_SPACES_RE = re.compile(r"[ \t ]+")
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([.,;:!?»)])")


def clean_text(text: str) -> str:
    """Чистит текст новости, сохраняя деление на абзацы."""
    text = _URL_RE.sub(" ", text)
    text = _EMOJI_RE.sub(" ", text)
    paragraphs = []
    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = _SPACES_RE.sub(" ", paragraph.replace("\n", " ")).strip()
        paragraph = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", paragraph)
        if not paragraph or paragraph.casefold().startswith(_TAIL_PREFIXES):
            continue
        paragraphs.append(paragraph)
    return "\n\n".join(paragraphs)


def sentences(text: str) -> list[str]:
    """Предложения текста (razdel корректно обходит сокращения вроде «млрд руб.»)."""
    return [item.text.strip() for item in sentenize(text) if item.text.strip()]


def lead(title: str, text: str, max_sentences: int | None = None) -> str:
    """Заголовок и первые ``max_sentences`` предложений текста (по умолчанию из конфига)."""
    count = max_sentences or SETTINGS.news["lead_sentences"]
    body = " ".join(sentences(text)[:count])
    return "\n".join(part for part in (title.strip(), body) if part)
