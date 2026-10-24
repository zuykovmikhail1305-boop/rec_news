"""Агенты блоков карточки инсайта — по агенту (вызову LLM) на блок.

Блоки строятся в три этапа; внутри этапа — параллельно:

1. ``fact``, ``bank_view``, ``ai_conclusion`` — читают источники;
2. ``risks``, ``hypothesis``, ``actions`` — плюс готовые блоки этапа 1;
3. ``header``, ``headline``, ``bank_offer`` — плюс всё написанное.

Каждый агент видит только нужные ему группы источников (:attr:`BlockSpec.sees`),
голос Альфа-Банка и, при переписывании, замечания программной проверки и
проверщика. Ответ — JSON по схеме блока; :func:`normalize` чистит его
(списки, markdown, ссылки в тексте) и проверяет структуру, ошибка структуры —
повторный запрос внутри вызова. Задачи LLM — ``insight_<блок>`` в ``ml.toml``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from news_pipeline.agent.style import TONE_PROMPT
from news_pipeline.insights.context import CardContext
from news_pipeline.llm.client import LLM, LLMError
from news_pipeline.prompts import prompt
from news_pipeline.references import REFERENCES_DIR
from news_pipeline.settings import SETTINGS

with (REFERENCES_DIR / "bank_products.json").open(encoding="utf-8") as _file:
    PRODUCTS: dict[str, dict] = {item["id"]: item for item in json.load(_file)["products"]}

CHANNELS = ["кредиты и долг", "отрасль", "регион", "поставщики", "покупатели", "ВЭД и валюта",
            "налоги и платежи в бюджет", "спрос", "регулирование", "персонал", "инвестиции", "ликвидность"]
IMPACT = {"risk": "Риск", "opportunity": "Возможность", "neutral": "Нейтрально"}
PRIORITY = {"high": "Высокий приоритет", "medium": "Средний приоритет", "low": "Низкий приоритет"}
HORIZON = {"1-3m": "1–3 месяца", "3-6m": "3–6 месяцев", "6-12m": "6–12 месяцев"}

_IDS = {"type": "array", "items": {"type": "string"}}
_ITEM = {"type": "object", "properties": {"text": {"type": "string"}, "source_ids": _IDS, "feature_ids": _IDS},
         "required": ["text", "source_ids"]}
_CHIP = {"type": "object", "properties": {"value": {"type": "string"}, "reason": {"type": "string"}},
         "required": ["value", "reason"]}


@dataclass(frozen=True)
class BlockSpec:
    name: str
    title: str
    stage: int
    sees: tuple[str, ...]          # profile, event (заголовок и лид N1), news, analytics, facts, blocks
    rule: str
    schema: dict
    max_tokens: int = 900

    @property
    def task(self) -> str:
        return f"insight_{self.name}"


SPECS: dict[str, BlockSpec] = {spec.name: spec for spec in [
    BlockSpec(
        "fact", "Факт · что произошло", 1, ("news",),
        prompt("insight_card.blocks.fact"),
        {"type": "object", "properties": {
            "text": {"type": "string"}, "decided": {"type": "array", "items": {"type": "string"}},
            "discussed": {"type": "array", "items": {"type": "string"}},
            "key_figures": {"type": "array", "items": {"type": "object", "properties": {
                "value": {"type": "string"}, "label": {"type": "string"}, "source_ids": _IDS},
                "required": ["value", "label"]}},
            "source_ids": _IDS}, "required": ["text", "decided", "discussed", "key_figures", "source_ids"]},
    ),
    BlockSpec(
        "bank_view", "Позиция Альфа-Банка", 1, ("event", "analytics"),
        prompt("insight_card.blocks.bank_view"),
        {"type": "object", "properties": {"text": {"type": "string"}, "source_ids": _IDS},
         "required": ["text", "source_ids"]},
    ),
    BlockSpec(
        "ai_conclusion", "Вывод AI · почему это важно для компании", 1, ("profile", "news", "analytics", "facts"),
        prompt("insight_card.blocks.ai_conclusion"),
        {"type": "object", "properties": {"text": {"type": "string"}, "channels": {"type": "array", "items": {"type": "string"}},
                                          "feature_ids": _IDS, "source_ids": _IDS},
         "required": ["text", "channels", "feature_ids", "source_ids"]},
    ),
    BlockSpec(
        "risks", "Риски / Возможности", 2, ("profile", "news", "analytics", "facts", "blocks"),
        prompt("insight_card.blocks.risks"),
        {"type": "object", "properties": {"risks": {"type": "array", "items": _ITEM},
                                          "opportunities": {"type": "array", "items": _ITEM}},
         "required": ["risks", "opportunities"]},
    ),
    BlockSpec(
        "hypothesis", "Гипотеза · как может развиваться ситуация", 2, ("profile", "news", "analytics", "facts", "blocks"),
        prompt("insight_card.blocks.hypothesis"),
        {"type": "object", "properties": {"condition": {"type": "string"}, "consequence": {"type": "string"}},
         "required": ["condition", "consequence"]},
    ),
    BlockSpec(
        "actions", "Что можно сделать", 2, ("profile", "news", "analytics", "facts", "blocks"),
        prompt("insight_card.blocks.actions"),
        {"type": "object", "properties": {"steps": {"type": "array", "items": {"type": "string"}},
                                          "question": {"type": "string"}}, "required": ["steps", "question"]},
    ),
    BlockSpec(
        "header", "Шапка", 3, ("profile", "event", "facts", "blocks"),
        prompt("insight_card.blocks.header"),
        {"type": "object", "properties": {"impact": _CHIP, "priority": _CHIP, "horizon": _CHIP},
         "required": ["impact", "priority", "horizon"]},
        max_tokens=500,
    ),
    BlockSpec(
        "headline", "Заголовок", 3, ("profile", "event", "analytics", "facts", "blocks"),
        prompt("insight_card.blocks.headline"),
        {"type": "object", "properties": {"text": {"type": "string"}, "aspect": {"type": "string"},
                                          "feature_ids": _IDS, "source_ids": _IDS},
         "required": ["text", "aspect", "feature_ids", "source_ids"]},
        max_tokens=400,
    ),
    BlockSpec(
        "bank_offer", "Предложение банка", 3, ("profile", "facts", "blocks"),
        prompt("insight_card.blocks.bank_offer"),
        {"type": "object", "properties": {"product_id": {"type": "string"}, "risk_index": {"type": "integer"},
                                          "reason": {"type": "string"}}, "required": ["product_id", "reason"]},
        max_tokens=400,
    ),
]}

_PROMPT = prompt("insight_card.block")

_BRACKETS_RE = re.compile(r"\s*\[(?:\s*[NAF]\d+\s*[,;]?)+\]")


def _decap(text: str) -> str:
    """Первая буква — строчная, если это не аббревиатура («ЦБ», «НДС»)."""
    return text if len(text) > 1 and text[1].isupper() else text[:1].lower() + text[1:]


def clean_text(value: Any) -> str:
    text = str(value or "")
    text = _BRACKETS_RE.sub("", text)
    text = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE).replace("**", "")
    return " ".join(text.split())


def _ids(value: Any, prefix: str) -> list[str]:
    items = value if isinstance(value, list) else [value] if value else []
    return list(dict.fromkeys(str(item).strip().strip("[]") for item in items
                              if str(item).strip().strip("[]").startswith(tuple(prefix))))


def _items(value: Any) -> list[dict]:
    result = []
    for item in value or []:
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            continue
        text = clean_text(item.get("text"))
        if text:
            result.append({"text": text, "source_ids": _ids(item.get("source_ids"), "NA"),
                           "feature_ids": _ids(item.get("feature_ids"), "F")})
    return result


def _chip(value: Any, allowed: dict[str, str], name: str) -> dict:
    value = value if isinstance(value, dict) else {"value": value}
    raw = str(value.get("value") or "").strip().lower().replace("–", "-").replace(" ", "")
    raw = {"1-3": "1-3m", "3-6": "3-6m", "6-12": "6-12m"}.get(raw, raw)
    if raw not in allowed:
        raise LLMError(f"{name}.value должно быть одним из: {', '.join(allowed)}")
    return {"value": raw, "reason": clean_text(value.get("reason"))}


def normalize(name: str, data: dict) -> dict:
    """Чистит ответ агента блока; ``LLMError`` — ответ не по схеме (повторный запрос)."""
    if name == "fact":
        text = clean_text(data.get("text"))
        if not text:
            raise LLMError("пустой text")
        figures = [{"value": clean_text(f.get("value")), "label": clean_text(f.get("label")),
                    "source_ids": _ids(f.get("source_ids"), "N")}
                   for f in data.get("key_figures") or [] if isinstance(f, dict) and f.get("value")]
        return {"text": text, "decided": [clean_text(x) for x in data.get("decided") or [] if clean_text(x)],
                "discussed": [clean_text(x) for x in data.get("discussed") or [] if clean_text(x)],
                "key_figures": figures[:4], "source_ids": _ids(data.get("source_ids"), "N")}
    if name == "bank_view":
        text = clean_text(data.get("text"))
        if not text:
            raise LLMError("пустой text")
        return {"text": text, "source_ids": _ids(data.get("source_ids"), "A")}
    if name == "headline":
        text = clean_text(data.get("text")).rstrip(".")
        if not text:
            raise LLMError("пустой text")
        return {"text": text, "aspect": clean_text(data.get("aspect")) or None,
                "feature_ids": _ids(data.get("feature_ids"), "F"), "source_ids": _ids(data.get("source_ids"), "NA")}
    if name == "ai_conclusion":
        text = clean_text(data.get("text"))
        if not text:
            raise LLMError("пустой text")
        channels = [c for c in (str(x).strip() for x in data.get("channels") or []) if c in CHANNELS]
        return {"text": text, "channels": list(dict.fromkeys(channels)),
                "feature_ids": _ids(data.get("feature_ids"), "F"), "source_ids": _ids(data.get("source_ids"), "NA")}
    if name == "risks":
        risks, opportunities = _items(data.get("risks")), _items(data.get("opportunities"))
        if not risks and not opportunities:
            raise LLMError("нет ни рисков, ни возможностей")
        return {"risks": risks, "opportunities": opportunities}
    if name == "hypothesis":
        condition = re.sub(r"^если\s+", "", clean_text(data.get("condition")), flags=re.IGNORECASE).rstrip(",. ")
        consequence = re.sub(r"^(?:то|тогда)\s+", "", clean_text(data.get("consequence")), flags=re.IGNORECASE)
        if not condition or not consequence:
            raise LLMError("нужны condition и consequence")
        consequence = consequence.rstrip(".") + "."
        return {"condition": condition, "consequence": consequence,
                "text": f"Если {_decap(condition)}, то {_decap(consequence)}"}
    if name == "actions":
        steps = [clean_text(x) for x in data.get("steps") or [] if clean_text(x)]
        question = clean_text(data.get("question"))
        if not steps or not question:
            raise LLMError("нужны steps и question")
        return {"steps": steps, "question": question if question.endswith("?") else question.rstrip(".") + "?"}
    if name == "header":
        return {"impact": _chip(data.get("impact"), IMPACT, "impact"),
                "priority": _chip(data.get("priority"), PRIORITY, "priority"),
                "horizon": _chip(data.get("horizon"), HORIZON, "horizon")}
    if name == "bank_offer":
        product = str(data.get("product_id") or "none").strip()
        if product != "none" and product not in PRODUCTS:
            raise LLMError(f"product_id должен быть одним из: {', '.join(PRODUCTS)} или none")
        try:
            risk_index = int(data.get("risk_index")) if data.get("risk_index") is not None else None
        except (TypeError, ValueError):
            risk_index = None
        return {"product_id": product, "risk_index": risk_index, "reason": clean_text(data.get("reason"))}
    raise KeyError(name)


def blocks_text(blocks: dict[str, dict], names: list[str] | None = None) -> str:
    """Готовые блоки для промпта следующих этапов и проверщика."""
    lines = []
    for name in names or list(SPECS):
        data = blocks.get(name)
        if data is None:
            continue
        title = SPECS[name].title
        if name == "fact":
            body = data["text"]
            if data["discussed"]:
                body += "\nПока обсуждается: " + "; ".join(data["discussed"])
        elif name == "risks":
            body = "\n".join([f"Риск {i}: {item['text']}" for i, item in enumerate(data["risks"], 1)]
                             + [f"Возможность {i}: {item['text']}" for i, item in enumerate(data["opportunities"], 1)])
        elif name == "hypothesis":
            body = data["text"]
        elif name == "actions":
            body = "\n".join(f"- {step}" for step in data["steps"]) + f"\nВопрос: {data['question']}"
        elif name == "header":
            body = "; ".join(f"{key}: {data[key]['value']} ({data[key]['reason']})" for key in ("impact", "priority", "horizon"))
        elif name == "bank_offer":
            product = PRODUCTS.get(data["product_id"])
            body = f"{product['name']}: {data['reason']}" if product else "не предлагается"
        elif name == "ai_conclusion":
            body = data["text"] + (f"\nОпирается на: {', '.join(data['feature_ids'])}" if data["feature_ids"] else "")
        else:
            body = data["text"]
        lines.append(f"### {title}\n{body}")
    return "\n\n".join(lines)


class BlockAgent:
    """Агент одного блока: промпт, вызов LLM, нормализация."""

    def __init__(self, llm: LLM, config: dict | None = None) -> None:
        self.llm = llm
        self.config = config or SETTINGS.insight_card

    def messages(self, spec: BlockSpec, ctx: CardContext, blocks: dict[str, dict], notes: list[str]) -> list[dict]:
        system = _PROMPT.format(tone=TONE_PROMPT, title=spec.title, rule=spec.rule)
        parts = [ctx.profile_text()] if "profile" in spec.sees else []
        if "facts" in spec.sees:
            parts.append("Данные о компании:\n" + ctx.facts_text())
        if "event" in spec.sees and "news" not in spec.sees:
            parts.append("Событие:\n" + ctx.event_text())
        if "news" in spec.sees:
            parts.append("Публикации о событии:\n" + ctx.news_text(self.config["news_chars"], self.config["other_news_chars"]))
        if "analytics" in spec.sees:
            parts.append("Аналитика Альфа-Банка:\n" + ctx.analytics_text(self.config["analytics_chars"]))
        if "blocks" in spec.sees:
            written = blocks_text(blocks, [name for name in SPECS if SPECS[name].stage < spec.stage])
            if written:
                parts.append("Уже написанные блоки карточки:\n" + written)
        if spec.name == "bank_offer":
            parts.append("Каталог продуктов:\n" + "\n".join(
                f"- {item['id']}: {item['name']} — {item['description']}" for item in PRODUCTS.values()))
        if notes:
            parts.append("Прошлая версия блока не прошла проверку. Исправь:\n" + "\n".join(f"- {note}" for note in notes))
        return [{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join(parts)}]

    def write(self, spec: BlockSpec, ctx: CardContext, blocks: dict[str, dict], notes: list[str] | None = None,
              attempt: int = 1) -> dict:
        """JSON блока. ``LLMUnavailable`` пробрасывается, ``LLMError`` — блок не написан."""
        validator: Callable[[dict], dict] = lambda data: normalize(spec.name, data)  # noqa: E731
        return self.llm.json(
            spec.task, self.messages(spec, ctx, blocks, notes or []), schema=spec.schema,
            cache_key=f"{ctx.user_id}:{ctx.news_id}:{spec.name}:{attempt}", validator=validator,
            max_tokens=self.config["max_tokens"].get(spec.name, spec.max_tokens), retries=1,
        )
