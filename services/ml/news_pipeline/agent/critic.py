"""Оценщик ответа (docs/agent_tz.md §4.5).

Один вызов LLM: оценки 1–5 по семи критериям, правки по разделам и решение,
нужно ли перепланировать. Решение «принято» — программное: все оценки
не ниже ``min_criterion`` и среднее не ниже ``accept_score``. Если LLM не
справилась, ответ принимается без оценки (``None``).
"""

from __future__ import annotations

from news_pipeline.agent.schemas import EvidencePack, Issue, Plan, Review, SectionDraft
from news_pipeline.agent.style import TONE_PROMPT, section_title
from news_pipeline.llm.client import LLM, LLMError, LLMUnavailable
from news_pipeline.settings import SETTINGS

CRITERIA = {
    "relevance": "отвечает на заданный вопрос, а не пересказывает ленту",
    "grounding": "выводы следуют из источников, нет натяжек и домыслов",
    "personalization": "учтены роль, отрасль, регионы, продукты и контрагенты из профиля",
    "usefulness": "понятно, что проверить или обсудить и с кем",
    "clarity": "кратко, без воды и повторов между разделами",
    "structure": "структура подходит под вопрос",
    "tone": "голос Альфа-Банка: ясно, спокойно, по делу, без канцелярита, драматизации и категоричности",
}

_PROMPT = """Ты оценщик ответов ассистента Альфа-Банка для руководителей крупных компаний.

Оцени ответ по шкале 1–5 (5 — отлично, 3 — приемлемо, 1 — плохо) по критериям:
{criteria}

Будь строгим, но не придирайся к мелочам. Если что-то нужно исправить — добавь в feedback \
пункт: section_id (ID раздела из ответа или пустая строка для всего ответа), problem — что не так, \
fix — что конкретно сделать. Не требуй фактов, которых нет в источниках.
replan = true, только если не подходит сама структура: не тот тип ответа, не те разделы, \
не хватает раздела; иначе false — тогда разделы перепишут по твоим правкам.

{tone}

Ответ — JSON.
/no_think"""

_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "object",
            "properties": {name: {"type": "integer"} for name in CRITERIA},
            "required": list(CRITERIA),
        },
        "feedback": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"section_id": {"type": "string"}, "problem": {"type": "string"}, "fix": {"type": "string"}},
                "required": ["section_id", "problem", "fix"],
            },
        },
        "replan": {"type": "boolean"},
        "comment": {"type": "string"},
    },
    "required": ["scores", "feedback", "replan", "comment"],
}


def _validate(data: dict) -> dict:
    scores = data.get("scores") or {}
    clean = {}
    for name in CRITERIA:
        try:
            clean[name] = max(1, min(5, int(scores.get(name))))
        except (TypeError, ValueError) as error:
            raise LLMError(f"нет оценки {name} (целое 1–5)") from error
    feedback = [
        {"section_id": str(item.get("section_id") or "").strip(), "problem": str(item.get("problem") or "").strip(),
         "fix": str(item.get("fix") or "").strip()}
        for item in data.get("feedback") or [] if isinstance(item, dict)
    ]
    return {"scores": clean, "feedback": [f for f in feedback if f["problem"] or f["fix"]],
            "replan": bool(data.get("replan")), "comment": str(data.get("comment") or "")}


class Critic:
    def __init__(self, llm: LLM, config: dict | None = None) -> None:
        self.llm = llm
        self.config = config or SETTINGS.agent

    def review(
        self,
        question: str,
        plan: Plan,
        drafts: dict[str, SectionDraft],
        pack: EvidencePack,
        warnings: list[Issue],
    ) -> Review | None:
        criteria = "\n".join(f"- {name}: {text}" for name, text in CRITERIA.items())
        system = _PROMPT.format(criteria=criteria, tone=TONE_PROMPT)
        answer = "\n".join(
            f"[{s.id}] {section_title(s.kind)} (цель: {s.goal}):\n{drafts[s.id].text}"
            for s in plan.sections if s.id in drafts
        )
        parts = [
            f"Вопрос: {question}",
            f"Профиль (P):\n{pack.get('P').text if pack.get('P') else '—'}",
            "Источники:\n" + "\n".join(item.brief() for item in pack.items if item.type != "profile"),
            f"Тип ответа: {plan.answer_type}. Цель: {plan.headline_goal}",
            f"Ответ:\n{answer}",
        ]
        if warnings:
            parts.append("Замечания автоматической проверки:\n" + "\n".join(
                f"- [{w.section_id or 'весь ответ'}] {w.message}" for w in warnings))
        try:
            data = self.llm.json(
                "agent_critic",
                [{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join(parts)}],
                schema=_SCHEMA,
                cache_key="review",
                validator=_validate,
                max_tokens=self.config["max_tokens"]["critic"],
                retries=1,
            )
        except LLMUnavailable:
            raise
        except LLMError:
            return None
        scores = data["scores"]
        overall = round(sum(scores.values()) / len(scores), 2)
        accepted = overall >= self.config["accept_score"] and min(scores.values()) >= self.config["min_criterion"]
        return Review(scores=scores, overall=overall, accepted=accepted, feedback=data["feedback"],
                      replan=data["replan"] and not accepted, comment=data["comment"])
