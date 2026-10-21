"""Инсайт «что это значит для вашей компании» (ТЗ §8).

Для новости ленты LLM пишет 3–5 предложений только по фактам из новости
и прикреплённой аналитики:

1. что произошло — одно предложение;
2. что говорит аналитика — если она прикреплена;
3. что это значит для компании пользователя с учётом роли;
4. что стоит проверить — опционально, без категоричных рекомендаций.

Защита от галлюцинаций: запрет на факты вне входных текстов в промпте;
список утверждений со ссылкой на источник каждого (``news`` или
``analytics:<chunk_id>``); автоматическая проверка — каждое число инсайта
должно встречаться во входных текстах, иначе повторная генерация. Если
аналитика противоречит новости, это говорится прямо. Без LLM инсайт не
строится. Ответы кэшируются по паре (``user_id``, ``event_id``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from news_pipeline.llm.client import LLM, LLMError, LLMUnavailable, default_llm
from news_pipeline.schemas import FeedAnalytics, InterestProfile

_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")


@dataclass(frozen=True)
class AnalyticsContext:
    """Прикреплённый фрагмент аналитики с текстом для промпта."""

    match: FeedAnalytics
    text: str


@dataclass(frozen=True)
class Insight:
    text: str
    claims: list[dict[str, str]] = field(default_factory=list)

    @property
    def sources(self) -> list[str]:
        return sorted({claim["source"] for claim in self.claims})


def numbers_in(text: str) -> set[str]:
    """Числа текста в едином виде: запятая → точка, без ведущих нулей."""
    result = set()
    for number in _NUMBER_RE.findall(text):
        whole, _, fraction = number.replace(",", ".").partition(".")
        whole = whole.lstrip("0") or "0"
        fraction = fraction.rstrip("0")
        result.add(f"{whole}.{fraction}" if fraction else whole)
    return result


_PROMPT = """Ты аналитик Альфа-Банка и пишешь короткий вывод для руководителя крупной компании.

Напиши инсайт из 3–5 предложений по-русски:
1. Что произошло — одно предложение.
2. Что говорит аналитика Альфа-Банка — только если фрагмент аналитики дан; если аналитика \
противоречит новости, скажи это прямо («Аналитики банка оценивают иначе: …»).
3. Что это значит для компании руководителя с учётом его роли.
4. Что стоит проверить или обсудить — без категоричных рекомендаций.

Строгие правила:
- используй только факты, числа и даты из новости и аналитики; не добавляй своих чисел;
- не выдумывай факты о компании, опирайся только на профиль;
- claims — каждое утверждение инсайта отдельно, source: "news" или "analytics:<chunk_id>" \
(для выводов о компании — источник того факта, на котором вывод основан).
Ответ — JSON с полями insight и claims.
/no_think"""

_SCHEMA = {
    "type": "object",
    "properties": {
        "insight": {"type": "string"},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "source": {"type": "string"}},
                "required": ["text", "source"],
            },
        },
    },
    "required": ["insight", "claims"],
}


def _validator(allowed_sources: set[str], source_numbers: set[str]):
    def validate(data: dict[str, Any]) -> dict[str, Any]:
        text = str(data.get("insight") or "").strip()
        if not text:
            raise LLMError("пустой инсайт")
        invented = sorted(numbers_in(text) - source_numbers)
        if invented:
            raise LLMError(
                f"в инсайте есть числа, которых нет во входных текстах: {', '.join(invented)}. "
                "Убери их или возьми числа только из новости и аналитики"
            )
        claims = []
        for claim in data.get("claims") or []:
            source = str(claim.get("source") or "").strip()
            claim_text = str(claim.get("text") or "").strip()
            if claim_text:
                claims.append({"text": claim_text, "source": source if source in allowed_sources else "news"})
        if not claims:
            raise LLMError("нет списка утверждений с источниками")
        return {"insight": text, "claims": claims}

    return validate


def generate_insight(
    profile: InterestProfile,
    *,
    event_id: str,
    title: str,
    news_text: str,
    analytics: list[AnalyticsContext] | None = None,
    llm: LLM | None = None,
    retries: int = 2,
) -> Insight | None:
    """Инсайт для пары пользователь — событие; ``None``, если LLM недоступна или не справилась."""
    analytics = analytics or []
    llm = llm or default_llm()
    aspects = "\n".join(f"- {a.title}: {a.description}" for a in profile.aspects[:6])
    parts = [
        f"Профиль руководителя:\nРоль: {profile.role}\nИнтересы:\n{aspects}",
        f"Новость:\n{title}\n{news_text[:2500]}",
    ]
    for item in analytics:
        relation = item.match.relation
        parts.append(
            f"Фрагмент аналитики (chunk_id={item.match.chunk_id}, «{item.match.title}», связь: {relation}):\n"
            f"{item.text[:1500]}"
        )
    sources_text = "\n".join([title, news_text, *(item.text for item in analytics)])
    allowed = {"news", *(f"analytics:{item.match.chunk_id}" for item in analytics)}
    try:
        data = llm.json(
            "insight",
            [{"role": "system", "content": _PROMPT}, {"role": "user", "content": "\n\n".join(parts)}],
            schema=_SCHEMA,
            cache_key=f"{profile.user_id}:{event_id}",
            validator=_validator(allowed, numbers_in(sources_text)),
            max_tokens=1200,
            retries=retries,
        )
    except (LLMUnavailable, LLMError):
        return None
    return Insight(text=data["insight"], claims=data["claims"])
