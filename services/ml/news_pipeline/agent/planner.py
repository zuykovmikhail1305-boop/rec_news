"""Планировщик — план ответа по схеме (docs/agent_tz.md §4.1).

LLM выбирает схему из библиотеки (``answer_schemes.json``) и адаптирует её:
цели разделов под вопрос, источники каждого раздела, лишние разделы убирает.
План проверяется программно: ID источников существуют, ``bank_view`` только
при наличии аналитики, разделам без источников назначаются лучшие по
релевантности. Без LLM или при сбое — план по правилам: схема по ключевым
словам вопроса и источники по умолчанию.
"""

from __future__ import annotations

import json
import re
from typing import Any

from news_pipeline.agent.schemas import EvidencePack, Plan, PlanSection
from news_pipeline.agent.style import KINDS, SCHEMES, TONE_PROMPT_SHORT
from news_pipeline.llm.client import LLM, LLMError, LLMUnavailable
from news_pipeline.prompts import prompt
from news_pipeline.settings import SETTINGS

_SCHEMES_TEXT = "\n".join(
    f"- {name}: {scheme['description']}. Разделы: "
    + ", ".join(f"{s['kind']}(≤{s['max_sentences']})" for s in scheme["sections"])
    for name, scheme in SCHEMES.items()
)
_KINDS_TEXT = "\n".join(f"- {kind}: {info['rule']}" for kind, info in KINDS.items())

_PROMPT = prompt("agent.planner")

_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string"},
        "answer_type": {"type": "string", "enum": list(SCHEMES)},
        "out_of_scope": {"type": "boolean"},
        "clarify": {"type": "string"},
        "headline_goal": {"type": "string"},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(KINDS)},
                    "goal": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "max_sentences": {"type": "integer"},
                },
                "required": ["kind", "goal", "evidence", "max_sentences"],
            },
        },
    },
    "required": ["intent", "answer_type", "out_of_scope", "clarify", "headline_goal", "sections"],
}

_RULES = [
    ("bank_view", r"банк\w* (дума|счита|ожида|оцени|прогноз)|аналитик|прогноз|альфа"),
    ("risk_scan", r"риск|угроз|опасн|что может пойти|на что обратить"),
    ("compare", r"сравн|чем отлича|что лучше|разниц"),
    ("action", r"что делать|как поступ|какие шаги|что предпринять|стоит ли"),
    ("digest", r"главн|что нового|что важно|за неделю|за период|дайджест|обзор"),
]


def guess_type(question: str, *, brief: bool = False, followup: bool = False) -> str:
    """Схема ответа по ключевым словам — для плана без LLM."""
    if brief:
        return "digest"
    text = question.casefold()
    for name, pattern in _RULES:
        if re.search(pattern, text):
            return name
    if followup and len(text.split()) <= 12:
        return "followup"
    return "event_impact"


def _default_evidence(kind: str, pack: EvidencePack) -> list[str]:
    news = [item.id for item in pack.of_type("news")]
    analytics = [item.id for item in pack.of_type("analytics")]
    defaults = {
        "summary": news[:2] + analytics[:1] + ["P"],
        "facts": news[:3],
        "bank_view": analytics[:2],
        "impact": news[:2] + analytics[:1] + ["P"],
        "risks": news[:3] + ["P"],
        "actions": news[:2] + ["P"],
        "compare": news[:3],
        "answer": news[:3] + analytics[:1] + ["P"],
    }
    return defaults.get(kind, news[:2])


def default_plan(answer_type: str, pack: EvidencePack, question: str) -> Plan:
    """План по схеме без LLM."""
    sections = []
    for spec in SCHEMES[answer_type]["sections"]:
        evidence = _default_evidence(spec["kind"], pack)
        if not [e for e in evidence if e != "P"]:
            continue
        sections.append(PlanSection(id=f"s{len(sections) + 1}", kind=spec["kind"], goal=spec["goal"],
                                    evidence=evidence, max_sentences=spec["max_sentences"]))
    return Plan(intent=answer_type, answer_type=answer_type, headline_goal=question, sections=sections, source="rules")


def _normalize(data: dict[str, Any], pack: EvidencePack, question: str) -> Plan:
    """Программная проверка плана; ``LLMError`` — повторный запрос с текстом ошибки."""
    answer_type = data.get("answer_type")
    if answer_type not in SCHEMES:
        raise LLMError(f"answer_type должен быть одним из: {', '.join(SCHEMES)}")
    clarify = str(data.get("clarify") or "").strip() or None
    plan = Plan(intent=str(data.get("intent") or answer_type), answer_type=answer_type,
                headline_goal=str(data.get("headline_goal") or question).strip(), sections=[],
                out_of_scope=bool(data.get("out_of_scope")), clarify=clarify)
    if plan.out_of_scope or plan.clarify:
        return plan
    has_analytics = bool(pack.of_type("analytics"))
    unknown: list[str] = []
    for raw in data.get("sections") or []:
        kind = raw.get("kind")
        if kind not in KINDS or (kind == "bank_view" and not has_analytics):
            continue
        evidence = []
        for item in raw.get("evidence") or []:
            item = str(item).strip().strip("[]")
            if item in pack.ids:
                if item not in evidence:
                    evidence.append(item)
            else:
                unknown.append(item)
        if kind == "bank_view":
            evidence = [e for e in evidence if e.startswith("A")]
        if not [e for e in evidence if e != "P"]:
            evidence = _default_evidence(kind, pack) + [e for e in evidence if e == "P"]
        if not [e for e in evidence if e != "P"]:
            continue
        plan.sections.append(PlanSection(
            id=f"s{len(plan.sections) + 1}", kind=kind, goal=str(raw.get("goal") or KINDS[kind]["rule"]).strip(),
            evidence=list(dict.fromkeys(evidence))[:5],
            max_sentences=max(1, min(5, int(raw.get("max_sentences") or 3))),
        ))
    if unknown and not plan.sections:
        raise LLMError(f"источников {', '.join(sorted(set(unknown)))} нет в списке; используй только ID из списка")
    if not plan.sections:
        raise LLMError("в плане нет ни одного раздела с источниками")
    plan.sections = plan.sections[: SETTINGS.agent["max_sections"]]
    return plan


class Planner:
    def __init__(self, llm: LLM) -> None:
        self.llm = llm

    def plan(
        self,
        question: str,
        pack: EvidencePack,
        *,
        dialog: list[tuple[str, str]],
        brief: bool = False,
        previous: Plan | None = None,
        feedback: list[str] | None = None,
    ) -> Plan:
        profile = pack.get("P")
        parts = [f"Вопрос: {question}"]
        if dialog:
            parts.append("Недавний диалог:\n" + "\n".join(
                f"{'Пользователь' if role == 'user' else 'Ассистент'}: {text[:300]}" for role, text in dialog))
        parts.append(f"Профиль (P):\n{profile.text if profile else '—'}")
        parts.append("Источники:\n" + "\n".join(item.brief() for item in pack.items if item.type != "profile"))
        if previous is not None and feedback:
            parts.append("Предыдущий план:\n" + json.dumps(previous.to_dict(), ensure_ascii=False))
            parts.append("Замечания к ответу по предыдущему плану — исправь план с их учётом:\n"
                         + "\n".join(f"- {item}" for item in feedback))
        try:
            data = self.llm.json(
                "agent_planner",
                [{"role": "system", "content": _PROMPT}, {"role": "user", "content": "\n\n".join(parts)}],
                schema=_SCHEMA,
                cache_key="plan",
                validator=lambda raw: {"plan": _normalize(raw, pack, question).to_dict()},
                max_tokens=SETTINGS.agent["max_tokens"]["planner"],
                retries=2,
            )
        except LLMUnavailable:
            raise
        except LLMError:
            return default_plan(guess_type(question, brief=brief, followup=bool(dialog)), pack, question)
        raw = data["plan"]
        return Plan(
            intent=raw["intent"], answer_type=raw["answer_type"], headline_goal=raw["headline_goal"],
            sections=[PlanSection(**section) for section in raw["sections"]],
            out_of_scope=raw["out_of_scope"], clarify=raw["clarify"], source=raw["source"],
        )
