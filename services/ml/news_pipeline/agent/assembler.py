"""Сборка ответа без LLM (docs/agent_tz.md §4.3, П4, П10).

Разделы — в порядке плана, у каждого ``origin``: ``bank`` — позиция банка по
аналитике, ``ai`` — вывод ассистента (визуальное разделение по ТЗ).
Источники — только реально процитированные, в порядке первого упоминания.
Здесь же — шаблонные ответы: без LLM (экстрактивный), без данных, вне темы.
"""

from __future__ import annotations

from news_pipeline.agent.schemas import AgentSection, AgentSource, EvidencePack, Plan, SectionDraft
from news_pipeline.agent.style import citations, section_origin, section_title, split_sentences, strip_citations

_ORIGIN_LABEL = {"bank": "аналитика Альфа-Банка", "ai": "вывод ассистента"}

NO_EVIDENCE = ("В подобранных для вас новостях и аналитике нет данных по этому вопросу. "
               "Могу рассказать, что главное произошло для вашей компании за последние дни.")
OUT_OF_SCOPE = ("Я отвечаю на вопросы о новостях, рынках и их значении для вашей компании. "
                "Спросите, например, что значит для компании последнее решение ЦБ по ставке.")
FALLBACK_NOTE = "Сейчас выводы недоступны, поэтому ниже — подборка источников по вашему вопросу без интерпретации."


def build_sections(plan: Plan, drafts: dict[str, SectionDraft]) -> list[AgentSection]:
    return [
        AgentSection(id=s.id, kind=s.kind, title=section_title(s.kind), origin=section_origin(s.kind),
                     text=drafts[s.id].text, sources=citations(drafts[s.id].text))
        for s in plan.sections if s.id in drafts and drafts[s.id].text.strip()
    ]


def build_sources(sections: list[AgentSection], pack: EvidencePack) -> list[AgentSource]:
    order: list[str] = []
    for section in sections:
        order += [item for item in section.sources if item not in order]
    order.sort(key=lambda e: ({"N": 0, "A": 1}.get(e[:1], 2), int(e[1:] or 0)))
    sources = []
    for evidence_id in order:
        item = pack.get(evidence_id)
        if item is None:
            continue
        sources.append(AgentSource(
            id=item.id, type=item.type, title="Ваш профиль и компания" if item.type == "profile" else item.title,
            source=item.source, published_at=(item.published_at or "")[:10] or None, url=item.url,
            news_id=item.news_id, chunk_id=item.chunk_id,
        ))
    return sources


def summary_of(sections: list[AgentSection]) -> str:
    if not sections:
        return ""
    first = sections[0]
    if first.kind in ("summary", "answer"):
        return strip_citations(first.text)
    sentences = split_sentences(first.text)
    return strip_citations(sentences[0]) if sentences else ""


def render(sections: list[AgentSection], sources: list[AgentSource], note: str | None = None) -> str:
    """Ответ одним текстом (markdown) для чата."""
    parts = [note] if note else []
    for section in sections:
        parts.append(f"**{section.title}** · _{_ORIGIN_LABEL[section.origin]}_\n{section.text}")
    if sources:
        lines = []
        for source in sources:
            meta = ", ".join(x for x in (source.source, source.published_at) if x)
            lines.append(f"[{source.id}] {source.title}" + (f" — {meta}" if meta else "")
                         + (f" ({source.url})" if source.url else ""))
        parts.append("**Источники**\n" + "\n".join(lines))
    return "\n\n".join(parts)


def fallback_sections(pack: EvidencePack, limit: int = 3) -> list[AgentSection]:
    """Экстрактивный ответ без LLM: лиды лучших новостей и фрагмент аналитики."""
    sections = []
    news = pack.of_type("news")[:limit]
    if news:
        lines = []
        for item in news:
            first = split_sentences(item.text)
            lead = first[0] if first else ""
            lines.append(f"{item.title}. {lead} [{item.id}]".replace("..", "."))
        sections.append(AgentSection(id="s1", kind="facts", title=section_title("facts"), origin="ai",
                                     text=" ".join(lines), sources=[item.id for item in news]))
    analytics = pack.of_type("analytics")[:1]
    if analytics:
        item = analytics[0]
        prose = [x for x in split_sentences(item.text) if len(x.split()) >= 8 and "@" not in x]
        text = " ".join(prose[:2]) or " ".join(split_sentences(item.text)[:2])
        sections.append(AgentSection(id="s2", kind="bank_view", title=section_title("bank_view"), origin="bank",
                                     text=f"{text} [{item.id}]", sources=[item.id]))
    return sections
