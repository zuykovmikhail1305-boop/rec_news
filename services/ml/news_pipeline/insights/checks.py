"""Программная проверка блоков карточки — до агента-проверщика.

Ошибка (``error``) отправляет блок агенту на переписывание с текстом замечания,
предупреждение (``warning``) передаётся проверщику и остаётся в карточке.

Коды: ``invented_number``, ``bad_source``, ``no_source``, ``no_feature``,
``language``, ``tone_alarm``, ``tone_categorical``, ``tone_forbidden``,
``tone_bureaucratic``, ``bank_voice``, ``tone_emoji``, ``trade_advice``,
``bank_not_named``, ``contradiction_missed``, ``count``, ``duplicate``,
``too_long``, ``headline_is_title``, ``analytics_ignored``, ``unknown_aspect``, ``headline_is_advice``, ``hypothesis_advice``, ``forecast_words``, ``offer_off_topic``.
"""

from __future__ import annotations

import re

from news_pipeline.agent.style import TONE
from news_pipeline.insights.blocks import PRODUCTS, SPECS
from news_pipeline.insights.context import CardContext
from news_pipeline.insights.numbers import numbers_in
from news_pipeline.insights.schemas import BlockIssue
from news_pipeline.preprocessing.cleaning import sentences

_CJK_RE = re.compile(r"[぀-ヿ㐀-鿿가-힯]")
_EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF☀-➿⭐✅!]")
_TRADE_RE = re.compile(r"(?<!\w)(?:купи|прода|покупай|продавай|прикупи|скупай|распрода)(?:те|йте|ть)?(?!\w)", re.IGNORECASE)
_FORECAST_RE = re.compile(r"(?<!\w)(?:точно|наверняка|обязательно|неизбежно|гарантированно|с вероятностью)(?!\w)",
                          re.IGNORECASE)
_WORD_RE = re.compile(r"\w+")
_ADVICE_START_RE = re.compile(r"^(?:стоит|следует|нужно|надо|важно|провер|обсуд|пересчита|оцени|сократи|увеличь|зафиксир)",
                              re.IGNORECASE)
MAX_SENTENCES = {"fact": 4, "bank_view": 3, "ai_conclusion": 4}


def _stems(text: str) -> set[str]:
    return {word.casefold()[:5] for word in _WORD_RE.findall(text) if len(word) >= 4}


def _issue(code: str, message: str, severity: str = "error") -> BlockIssue:
    return BlockIssue(code=code, severity=severity, message=message)


def _texts(name: str, data: dict) -> list[str]:
    if name == "fact":
        return [data["text"], *data["decided"], *data["discussed"]]
    if name == "risks":
        return [item["text"] for item in data["risks"] + data["opportunities"]]
    if name == "hypothesis":
        return [data["text"]]
    if name == "actions":
        return [*data["steps"], data["question"]]
    if name == "header":
        return [data[key]["reason"] for key in ("impact", "priority", "horizon")]
    if name == "bank_offer":
        return [data["reason"]]
    return [data["text"]]


def _tone(text: str) -> list[BlockIssue]:
    issues = []
    for group, code in ((TONE.alarm, "tone_alarm"), (TONE.categorical, "tone_categorical"),
                        (TONE.forbidden, "tone_forbidden"), (TONE.bank_voice, "bank_voice")):
        found = [name for name, pattern in group if pattern.search(text)]
        if found:
            hint = "«мы» от имени банка — пиши «аналитики Альфа-Банка …» или безлично" if code == "bank_voice" \
                else "убери"
            issues.append(_issue(code, f"{hint}: {', '.join(found)}"))
    bureaucratic = [f"«{name}» → «{fix}»" for name, pattern, fix in TONE.bureaucratic if pattern.search(text)]
    if bureaucratic:
        issues.append(_issue("tone_bureaucratic", "канцелярит: " + "; ".join(bureaucratic), "warning"))
    if _EMOJI_RE.search(text):
        issues.append(_issue("tone_emoji", "без эмодзи и восклицательных знаков"))
    if _CJK_RE.search(text):
        issues.append(_issue("language", "текст должен быть только на русском"))
    return issues


def check_block(name: str, data: dict, ctx: CardContext) -> list[BlockIssue]:
    """Замечания к блоку ``name``."""
    spec = SPECS[name]
    issues: list[BlockIssue] = []
    # позиция банка — только числа аналитики: число из новости в ней выглядело бы оценкой банка
    allowed_numbers = ctx.numbers(("analytics",) if name == "bank_view" else spec.sees)
    if name == "header":
        allowed_numbers |= {"1", "3", "6", "12"}   # горизонт: «1–3 месяца»
    allowed_ids = ctx.ids(spec.sees)
    for text in _texts(name, data):
        issues += _tone(text)
        invented = sorted(numbers_in(text) - allowed_numbers)
        if invented:
            issues.append(_issue("invented_number", f"чисел {', '.join(invented)} нет в источниках — убери их "
                                                    "или возьми числа из публикаций, аналитики и данных компании"))
    # ссылки на источники
    referenced: list[str] = []
    for key in ("source_ids", "feature_ids"):
        referenced += data.get(key) or []
    for item in data.get("risks", []) + data.get("opportunities", []) + data.get("key_figures", []):
        referenced += item.get("source_ids", []) + item.get("feature_ids", [])
    unknown = sorted(set(referenced) - allowed_ids)
    if unknown:
        issues.append(_issue("bad_source", f"нет таких источников: {', '.join(unknown)}; доступны: "
                                           f"{', '.join(sorted(allowed_ids))}"))

    if name == "fact":
        if not data["source_ids"]:
            issues.append(_issue("no_source", "укажи в source_ids публикации N*, на которые опирается факт"))
        if len(sentences(data["text"])) > MAX_SENTENCES["fact"]:
            issues.append(_issue("too_long", "не больше 4 предложений"))
    elif name == "bank_view":
        text = data["text"].lower()
        first = (sentences(data["text"]) or [""])[0].lower()
        if not any(pattern.search(first) for _, pattern in TONE.bank_mentions):
            issues.append(_issue("bank_not_named", "начни с «Аналитики Альфа-Банка …»: читатель должен видеть, "
                                                   "что это позиция банка"))
        if ctx.analytics_match is not None and ctx.analytics_match.contradicts and \
                not any(pattern.search(text) for _, pattern in TONE.contradiction_markers):
            issues.append(_issue("contradiction_missed", "аналитика банка расходится с новостью — скажи это прямо"))
        if len(sentences(data["text"])) > MAX_SENTENCES["bank_view"]:
            issues.append(_issue("too_long", "не больше 3 предложений"))
    elif name == "ai_conclusion":
        if ctx.facts and not data["feature_ids"]:
            issues.append(_issue("no_feature", "укажи в feature_ids, на какие данные о компании F* опирается вывод"))
        if not data["channels"]:
            issues.append(_issue("count", "укажи хотя бы один канал влияния из списка", "warning"))
        if len(sentences(data["text"])) > MAX_SENTENCES["ai_conclusion"]:
            issues.append(_issue("too_long", "не больше 4 предложений"))
    elif name == "risks":
        for key, label in (("risks", "рисков"), ("opportunities", "возможностей")):
            count = len(data[key])
            if count > 3:
                issues.append(_issue("count", f"не больше 3 {label}"))
            elif count == 0:
                issues.append(_issue("count", f"нужен хотя бы один пункт {label}", "warning"))
        texts = [item["text"] for item in data["risks"] + data["opportunities"]]
        for i, first in enumerate(texts):
            for second in texts[i + 1:]:
                a, b = _stems(first), _stems(second)
                if a and b and len(a & b) / len(a | b) >= 0.6:
                    issues.append(_issue("duplicate", f"пункты повторяют друг друга: «{first[:60]}…»"))
    elif name == "hypothesis":
        found = _FORECAST_RE.findall(data["text"])
        if found:
            issues.append(_issue("forecast_words", f"это предположение, а не прогноз — убери: {', '.join(found)}"))
        if re.search(r"(?<!\w)(?:стоит|следует|рекомендуе|провер(?:ить|ьте))(?!\w)", data["consequence"], re.IGNORECASE):
            issues.append(_issue("hypothesis_advice", "в гипотезе только сценарий, без советов — советы в блоке "
                                                      "«Что можно сделать»"))
        if len(data["condition"].split()) > 30 or len(data["consequence"].split()) > 30:
            issues.append(_issue("too_long", "condition и consequence — до 25 слов каждая"))
    elif name == "actions":
        if not 2 <= len(data["steps"]) <= 4:
            issues.append(_issue("count", "нужно 2–4 шага"))
        trade = _TRADE_RE.findall(" ".join(data["steps"] + [data["question"]]))
        if trade:
            issues.append(_issue("trade_advice", "без прямых советов купить или продать — "
                                                 "предложи, что проверить или обсудить"))
    elif name == "headline":
        words = len(data["text"].split())
        if words > 14:
            issues.append(_issue("too_long", f"до 12 слов, сейчас {words}"))
        if _ADVICE_START_RE.match(data["text"]):
            issues.append(_issue("headline_is_advice", "заголовок — вывод о том, что событие значит для компании, "
                                                       "а не совет; советы — в блоке «Что можно сделать»"))
        title, headline = _stems(ctx.title), _stems(data["text"])
        if headline and len(title & headline) / len(headline) >= 0.7:
            issues.append(_issue("headline_is_title", "это пересказ заголовка новости — напиши, что событие "
                                                      "значит для компании"))
        # главная фраза строится из новости, аналитики банка и профиля — все три должны быть учтены
        if "N1" not in data["source_ids"]:
            issues.append(_issue("no_source", "фраза опирается на новость — добавь N1 в source_ids"))
        if ctx.analytics is not None and ctx.analytics.id not in data["source_ids"]:
            issues.append(_issue("analytics_ignored", "учти оценку аналитиков Альфа-Банка A1: фраза не должна ей "
                                                      "противоречить; добавь A1 в source_ids"))
        if ctx.facts and not data["feature_ids"]:
            issues.append(_issue("no_feature", "укажи в feature_ids, через какие данные о компании F* событие "
                                               "касается читателя, и отрази это во фразе"))
        aspects = {aspect.title.casefold() for aspect in ctx.profile.aspects}
        if aspects and (data["aspect"] or "").casefold() not in aspects:
            issues.append(_issue("unknown_aspect", "aspect — название интереса читателя точно из списка «Интересы»",
                                 "warning"))
    elif name == "bank_offer" and data["product_id"] != "none":
        product = PRODUCTS[data["product_id"]]
        risk_text = data["reason"]
        if data.get("risk_texts"):
            risk_text += " " + " ".join(data["risk_texts"])
        if not any(stem in risk_text.lower() for stem in product["helps_with"]):
            issues.append(_issue("offer_off_topic", "продукт не связан с названными рисками — выбери другой или none"))
    return issues


def errors(issues: list[BlockIssue]) -> list[BlockIssue]:
    return [issue for issue in issues if issue.severity == "error"]
