"""Агент-проверщик карточки инсайта.

Один вызов LLM другой модели, чем у агентов блоков: проверщик видит
источники (публикации N*, аналитику A1, данные компании F*) и всю карточку и
по каждому блоку решает, верно ли он построен по новости и аналитике:

* ``fact`` — только факты из публикаций, без оценок; решённое и обсуждаемое не перепутаны;
* ``bank_view`` — точно передаёт аналитику A1, расхождение с новостью названо;
* ``ai_conclusion`` — вывод следует из события и данных компании, не общий;
* ``risks`` — конкретны для компании и следуют из источников;
* ``hypothesis`` — правдоподобное «если…, то…», не выдано за прогноз;
* ``actions`` — практичны для роли, без прямых советов;
* ``header``, ``headline`` — согласованы с содержанием карточки;
* ``bank_offer`` — к месту, закрывает названный риск.

Блоки с ошибкой (``ok = false`` и ``severity = error``) переписываются их
агентами по замечанию ``fix`` и проверяются снова.
"""

from __future__ import annotations

from dataclasses import dataclass

from news_pipeline.agent.style import TONE_PROMPT_SHORT
from news_pipeline.insights.blocks import SPECS, blocks_text
from news_pipeline.insights.context import CardContext
from news_pipeline.llm.client import LLM, LLMError, LLMUnavailable
from news_pipeline.settings import SETTINGS

CRITERIA = {
    "fact": "только факты из публикаций N*, без оценок и прогнозов; решённое и обсуждаемое не перепутаны; цифры и даты точные",
    "bank_view": "точно и без додумывания передаёт аналитику A1; если аналитика расходится с новостью — это сказано",
    "ai_conclusion": "вывод логично следует из события и данных компании F*, конкретен для этой компании, не приписывает ей лишнего",
    "risks": "риски и возможности конкретны для компании, следуют из источников, не противоречат факту",
    "hypothesis": "правдоподобный сценарий «если…, то…», вытекает из события, не выдан за прогноз",
    "actions": "практичные шаги под роль читателя, без прямых советов купить или продать",
    "header": "влияние, приоритет и горизонт согласованы с выводом и рисками",
    "headline": "одна фраза о том, что событие значит для этой компании (не заголовок новости): учитывает роль и "
                "особенности компании из профиля, не противоречит оценке аналитиков банка A1, согласована с выводом",
    "bank_offer": "продукт помогает именно с названным риском, предложение к месту",
}

_PROMPT = """Ты проверщик карточек инсайтов Альфа-Банка. Карточка объясняет руководителю, что новость \
значит для его компании. Её блоки написали разные агенты. Проверь, насколько верно каждый блок построен \
по публикациям, аналитике Альфа-Банка и данным о компании.

Критерии по блокам:
{criteria}

Для каждого блока из карточки верни элемент blocks: block — имя блока; ok — блок построен верно; \
severity — "error", если блок вводит в заблуждение, противоречит источникам или другим блокам, \
выдумывает факты или нарушает критерий; "warning" — мелочь, которую можно оставить; problem — что не так \
(пусто, если ok); fix — что конкретно исправить агенту блока.
Не придирайся к стилю, если смысл верный. Не требуй фактов, которых нет в источниках.
Прежде чем написать, что факта, числа или даты нет в источниках, найди их в текстах публикаций и аналитики: \
если они там есть — это не ошибка. Публикация может сама упоминать прошлые события (например, решение ЦБ) — \
их можно приводить в факте.
{tone}
comment — общий вывод о карточке, одно предложение.
Ответ — JSON.
/no_think"""

_SCHEMA = {
    "type": "object",
    "properties": {
        "blocks": {"type": "array", "items": {"type": "object", "properties": {
            "block": {"type": "string"}, "ok": {"type": "boolean"}, "severity": {"type": "string"},
            "problem": {"type": "string"}, "fix": {"type": "string"}},
            "required": ["block", "ok", "severity", "problem", "fix"]}},
        "comment": {"type": "string"},
    },
    "required": ["blocks", "comment"],
}


@dataclass(frozen=True)
class Verdict:
    block: str
    ok: bool
    severity: str
    problem: str
    fix: str

    @property
    def needs_rewrite(self) -> bool:
        return not self.ok and self.severity == "error"


@dataclass(frozen=True)
class Review:
    verdicts: dict[str, Verdict]
    comment: str


def _validator(names: set[str]):
    def validate(data: dict) -> dict:
        verdicts = {}
        for item in data.get("blocks") or []:
            if not isinstance(item, dict) or item.get("block") not in names:
                continue
            ok = bool(item.get("ok"))
            severity = str(item.get("severity") or "").lower()
            severity = severity if severity in ("error", "warning") else ("warning" if ok else "error")
            verdicts[item["block"]] = {"block": item["block"], "ok": ok, "severity": severity,
                                       "problem": str(item.get("problem") or "").strip(),
                                       "fix": str(item.get("fix") or "").strip()}
        missing = names - set(verdicts)
        if len(missing) > len(names) // 2:
            raise LLMError(f"нет оценки блоков: {', '.join(sorted(missing))}")
        return {"blocks": list(verdicts.values()), "comment": str(data.get("comment") or "")}

    return validate


class CardVerifier:
    def __init__(self, llm: LLM, config: dict | None = None) -> None:
        self.llm = llm
        self.config = config or SETTINGS.insight_card

    def review(self, ctx: CardContext, blocks: dict[str, dict], round_number: int) -> Review | None:
        """Вердикты по блокам; ``None`` — проверщик не справился (карточка остаётся непроверенной)."""
        names = [name for name in SPECS if blocks.get(name) is not None]
        criteria = "\n".join(f"- {name} («{SPECS[name].title}»): {CRITERIA[name]}" for name in names)
        parts = [
            ctx.profile_text(),
            "Данные о компании:\n" + ctx.facts_text(),
            "Публикации о событии:\n" + ctx.news_text(self.config["news_chars"], self.config["other_news_chars"]),
            "Аналитика Альфа-Банка:\n" + ctx.analytics_text(self.config["analytics_chars"]),
            "Карточка (имя блока в скобках):\n" + "\n\n".join(
                blocks_text(blocks, [name]).replace("### ", f"### ({name}) ", 1) for name in names),
        ]
        lines = []
        for name in names:
            found = {number: ids for number, ids in ctx.number_sources(blocks_text(blocks, [name])).items() if ids}
            if found:
                lines.append(f"- {name}: " + "; ".join(f"{number} → {', '.join(ids)}" for number, ids in found.items()))
        if lines:
            parts.append("Сверка чисел кодом — эти числа найдены в источниках, не считай их выдуманными "
                         "(проверяй только, верно ли они использованы):\n" + "\n".join(lines))
        try:
            data = self.llm.json(
                "insight_verifier",
                [{"role": "system", "content": _PROMPT.format(criteria=criteria, tone=TONE_PROMPT_SHORT)},
                 {"role": "user", "content": "\n\n".join(parts)}],
                schema=_SCHEMA, cache_key=f"{ctx.user_id}:{ctx.news_id}:verify:{round_number}",
                validator=_validator(set(names)), max_tokens=self.config["max_tokens"]["verifier"], retries=1,
            )
        except LLMUnavailable:
            raise
        except LLMError:
            return None
        return Review(verdicts={item["block"]: Verdict(**item) for item in data["blocks"]}, comment=data["comment"])
