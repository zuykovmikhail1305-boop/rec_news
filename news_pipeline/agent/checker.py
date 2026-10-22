"""Программная проверка ответа агента (docs/agent_tz.md §4.4, §11).

Каждое замечание — :class:`Issue` с кодом и серьёзностью. Ошибки (``error``)
отправляют раздел на переписывание с текстом замечания, предупреждения
(``warning``) уходят оценщику.

Коды: ``empty``, ``length``, ``coverage``, ``no_citation``, ``bad_citation``,
``invented_number``, ``bank_without_source``, ``contradiction_missed``,
``unknown_entity``, ``language``, ``forbidden``, ``duplicate``,
``tone_filler``, ``tone_emoji``, ``tone_bureaucratic``, ``tone_alarm``,
``tone_categorical``, ``long_sentence``, ``abbr_unexplained``, ``bank_voice``,
``too_long``, ``auto_citation``.

:func:`attach_citations` до проверки ставит ссылку предложению без неё, если
источник понятен: из ``claims`` модели или по совпадению слов с источником
раздела. Предложение, которое ни на что не опирается, остаётся ошибкой.
"""

from __future__ import annotations

import math
import re

from news_pipeline.agent.schemas import EvidencePack, Issue, Plan, SectionDraft
from news_pipeline.agent.schemas import PlanSection
from news_pipeline.agent.style import TONE, citations, split_sentences, strip_citations
from news_pipeline.enrichment.news import extract_entities
from news_pipeline.insights.insight import numbers_in
from news_pipeline.references import entity_key
from news_pipeline.settings import SETTINGS

# Разделы, где у каждого утверждения должна быть своя ссылка; в остальных — хотя бы одна на раздел.
_PER_SENTENCE = {"facts", "bank_view", "impact", "risks", "compare"}
_MIN_CLAIM_WORDS = 6
_CJK_RE = re.compile(r"[぀-ヿ㐀-鿿가-힯]")
_EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF☀-➿⭐✅]")
_CAPS_RE = re.compile(r"(?<!\w)[А-ЯЁA-Z]{5,}(?!\w)")
_ABBR_RE = re.compile(r"(?<![\w(])([А-ЯЁA-Z]{2,6})(?![\w)])")
_LETTER_RE = re.compile(r"[^\W\d_]")
_CYRILLIC_RE = re.compile(r"[а-яё]", re.IGNORECASE)
_WORD_RE = re.compile(r"\w+")
_ALWAYS_KNOWN = {"альфа", "банк", "банка", "банку", "банко", "росси", "цб"}


def _dates(published_at: str | None) -> str:
    """Дата публикации в форматах, в которых её может написать модель: 2026-10-15, 15.10.2026."""
    if not published_at or len(published_at) < 10:
        return ""
    year, month, day = published_at[:10].split("-")
    return f"{published_at[:10]} {day}.{month}.{year} {int(day)}"


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text)


def _stems(text: str) -> set[str]:
    return {word.casefold()[:6] for word in _words(text) if len(word) >= 4}


def _hits(patterns, text: str) -> list[str]:
    return [name for name, pattern in patterns if pattern.search(text)]


class AnswerChecker:
    def __init__(self, config: dict | None = None, *, check_entities: bool = True) -> None:
        self.config = config or SETTINGS.agent
        self.check_entities = check_entities

    def check(self, plan: Plan, drafts: dict[str, SectionDraft], pack: EvidencePack, question: str) -> list[Issue]:
        issues: list[Issue] = []
        question_numbers = numbers_in(question)
        evidence_numbers = {item.id: numbers_in(f"{item.title}\n{item.text}\n{_dates(item.published_at)}")
                            for item in pack.items}
        seen_sentences: list[tuple[str, set[str]]] = []
        for section in plan.sections:
            draft = drafts.get(section.id)
            if draft is None:
                issues.append(Issue(section.id, "coverage", "error", "раздел плана не написан"))
                continue
            text = draft.text.strip()
            if not text:
                issues.append(Issue(section.id, "empty", "error", "раздел пустой — напиши текст по источникам"))
                continue
            issues += self._section(section, text, pack, evidence_numbers, question_numbers, seen_sentences)
            if draft.auto_citations:
                issues.append(Issue(section.id, "auto_citation", "warning",
                                    f"{draft.auto_citations} ссылок проставлено по совпадению слов с источниками"))
        full = "\n".join(drafts[s.id].text for s in plan.sections if s.id in drafts)
        if len(strip_citations(full)) > self.config["answer_max_chars"] * 1.3:
            issues.append(Issue(None, "too_long", "warning",
                                f"ответ длиннее {self.config['answer_max_chars']} знаков — сократите"))
        issues += self._abbreviations(plan, drafts)
        return issues

    # --- раздел -----------------------------------------------------------------

    def _section(self, section, text, pack, evidence_numbers, question_numbers, seen_sentences) -> list[Issue]:
        sid, kind = section.id, section.kind
        issues: list[Issue] = []
        add = lambda code, severity, message: issues.append(Issue(sid, code, severity, message))  # noqa: E731
        sentences = split_sentences(text)
        limit = math.ceil(section.max_sentences * 1.5)
        if len(sentences) > limit:
            add("length", "error", f"{len(sentences)} предложений при лимите {section.max_sentences} — сократи")

        # ссылки
        cited = citations(text)
        allowed = set(section.evidence) | {"P"}
        unknown = [c for c in cited if c not in pack.ids]
        foreign = [c for c in cited if c in pack.ids and c not in allowed]
        if unknown:
            add("bad_citation", "error", f"ссылки на несуществующие источники: {', '.join(unknown)}")
        if foreign:
            add("bad_citation", "error",
                f"ссылки {', '.join(foreign)} не из источников раздела; можно: {', '.join(sorted(allowed))}")
        if not cited:
            add("no_citation", "error", "в разделе нет ни одной ссылки на источник вида [N1]")
        elif kind in _PER_SENTENCE:
            bare = [s for s in sentences if not citations(s) and len(_words(s)) >= _MIN_CLAIM_WORDS]
            if bare:
                add("no_citation", "error", "утверждения без ссылки на источник: " + " | ".join(s[:80] for s in bare[:2]))

        # числа
        allowed_numbers = set(question_numbers)
        for evidence_id in allowed:
            allowed_numbers |= evidence_numbers.get(evidence_id, set())
        invented = sorted(numbers_in(strip_citations(text)) - allowed_numbers)
        if invented:
            add("invented_number", "error",
                f"числа {', '.join(invented)} нет в источниках раздела — убери их или возьми числа из источников")

        # голос банка
        for sentence in sentences:
            has_analytics = any(c.startswith("A") for c in citations(sentence))
            if kind == "bank_view":
                if not has_analytics and len(_words(sentence)) >= _MIN_CLAIM_WORDS:
                    add("bank_without_source", "error",
                        f"в позиции банка утверждение без ссылки на аналитику A*: «{sentence[:80]}»")
            elif _hits(TONE.bank_mentions, sentence) and not has_analytics:
                add("bank_without_source", "error",
                    f"позиция банка упомянута без ссылки на фрагмент аналитики A*: «{sentence[:80]}»")
        if kind == "bank_view":
            contradicting = [e for e in section.evidence if (item := pack.get(e)) and item.contradicts and e in cited]
            if contradicting and not _hits(TONE.contradiction_markers, text):
                add("contradiction_missed", "error",
                    f"аналитика {', '.join(contradicting)} противоречит новости — скажи это прямо")

        # язык
        plain = strip_citations(text)
        letters = _LETTER_RE.findall(plain)
        if _CJK_RE.search(plain) or "<think" in plain or "</think" in plain:
            add("language", "error", "в тексте иероглифы или служебные теги — пиши только по-русски")
        elif letters and len(_CYRILLIC_RE.findall(plain)) / len(letters) < 0.75:
            add("language", "error", "слишком много латиницы — пиши по-русски")

        # тон
        for name in _hits(TONE.fillers, plain):
            add("tone_filler", "error", f"пустая фраза «{name}» — начни сразу с сути")
        if _EMOJI_RE.search(plain) or "!" in plain:
            add("tone_emoji", "error", "без эмодзи и восклицательных знаков")
        caps = [w for w in _CAPS_RE.findall(plain) if w not in TONE.abbreviations]
        if caps:
            add("tone_emoji", "error", f"слова капсом: {', '.join(caps[:3])}")
        bureaucratic = [(name, fix) for name, pattern, fix in TONE.bureaucratic if pattern.search(plain)]
        if bureaucratic:
            message = "канцелярит: " + "; ".join(f"«{pattern.split('(')[0]}…» → {fix}" for pattern, fix in bureaucratic)
            add("tone_bureaucratic", "error" if len(bureaucratic) >= 2 else "warning", message)
        sources = " ".join(f"{pack.get(e).title} {pack.get(e).text}" for e in allowed if pack.get(e)).casefold()
        alarm = [name for name, pattern in TONE.alarm if pattern.search(plain) and not pattern.search(sources)]
        if alarm:
            add("tone_alarm", "warning", f"драматизация: {', '.join(a.rstrip(chr(92) + 'b') for a in alarm)}")
        categorical = _hits(TONE.categorical, plain)
        if categorical:
            add("tone_categorical", "error", f"категоричность: {', '.join(c.replace(chr(92) + 'b', '') for c in categorical)} — "
                                             "пиши «стоит проверить», «имеет смысл обсудить»")
        forbidden = _hits(TONE.forbidden, plain)
        if forbidden:
            add("forbidden", "error", f"недопустимо: {', '.join(forbidden)} — без инвестиционных советов и запросов данных")
        voice = _hits(TONE.bank_voice, plain)
        if voice:
            add("bank_voice", "error", f"«{voice[0]}» — выводы пиши безлично, позицию банка — «Аналитики Альфа-Банка…»")
        long = [s for s in sentences if len(_words(strip_citations(s))) > self.config["max_sentence_words"]]
        if long:
            add("long_sentence", "warning", f"длинное предложение ({len(_words(long[0]))} слов): «{long[0][:60]}…»")

        # сущности
        if self.check_entities:
            corpus = _stems(sources + " " + (pack.get("P").text if pack.get("P") else ""))
            corpus |= {stem[:5] for stem in corpus}
            for name in extract_entities("", plain):
                words = entity_key(name).split()
                if words and not all(word in corpus or word in _ALWAYS_KNOWN for word in words):
                    add("unknown_entity", "warning", f"«{name}» нет в источниках раздела")

        # повторы между разделами
        for sentence in sentences:
            stems = _stems(strip_citations(sentence))
            if len(stems) < 4:
                continue
            for other_sid, other in seen_sentences:
                if other_sid != sid and len(stems & other) / len(stems | other) >= 0.7:
                    add("duplicate", "warning", f"повторяет раздел {other_sid}: «{sentence[:60]}…»")
                    break
            seen_sentences.append((sid, stems))
        return issues

    def _abbreviations(self, plan: Plan, drafts: dict[str, SectionDraft]) -> list[Issue]:
        issues, seen = [], set()
        for section in plan.sections:
            draft = drafts.get(section.id)
            if draft is None:
                continue
            plain = strip_citations(draft.text)
            for match in _ABBR_RE.finditer(plain):
                abbr = match.group(1)
                if abbr in TONE.abbreviations or abbr in seen:
                    continue
                seen.add(abbr)
                if not plain[match.end(): match.end() + 3].lstrip().startswith("("):
                    issues.append(Issue(section.id, "abbr_unexplained", "warning",
                                        f"аббревиатура {abbr} без расшифровки"))
        return issues


_MIN_OVERLAP = 0.35


def _cite(sentence: str, ids: list[str]) -> str:
    """Ссылка перед финальным знаком препинания: «…14% [N1].»"""
    mark = f" [{', '.join(ids)}]"
    stripped = sentence.rstrip()
    if stripped and stripped[-1] in ".!?…":
        return stripped[:-1] + mark + stripped[-1]
    return stripped + mark


def attach_citations(section: PlanSection, draft: SectionDraft, pack: EvidencePack) -> SectionDraft:
    """Ставит ссылки предложениям без них, если источник однозначен; возвращает новый черновик."""
    if not draft.text.strip():
        return draft
    allowed = [e for e in section.evidence if pack.get(e) is not None]
    if section.kind == "bank_view":
        allowed = [e for e in allowed if e.startswith("A")]
    evidence_stems = {e: _stems(f"{pack.get(e).title} {pack.get(e).text}") for e in allowed}
    claims = [(_stems(str(c.get("text") or "")), [str(x).strip("[] ") for x in c.get("sources") or []])
              for c in draft.claims]
    result, added = [], 0
    for sentence in split_sentences(draft.text):
        stems = _stems(strip_citations(sentence))
        if citations(sentence) or len(_words(sentence)) < _MIN_CLAIM_WORDS or not stems:
            result.append(sentence)
            continue
        ids: list[str] = []
        for claim_stems, sources in claims:  # 1) источник из claims модели
            if claim_stems and len(stems & claim_stems) / len(stems | claim_stems) >= 0.5:
                ids = [x for x in sources if x in allowed]
                if ids:
                    break
        if not ids:  # 2) источник раздела с наибольшим совпадением слов
            scored = sorted(((len(stems & e_stems) / len(stems), e) for e, e_stems in evidence_stems.items()
                             if e != "P"), reverse=True)
            if scored and scored[0][0] >= _MIN_OVERLAP:
                ids = [scored[0][1]]
        if ids:
            result.append(_cite(sentence, ids[:2]))
            added += 1
        else:
            result.append(sentence)
    if not added:
        return draft
    return SectionDraft(draft.section_id, " ".join(result), draft.claims, draft.attempts, draft.error,
                        draft.auto_citations + added)


def errors(issues: list[Issue]) -> list[Issue]:
    return [issue for issue in issues if issue.severity == "error"]


def notes_for(issues: list[Issue], section_id: str) -> list[str]:
    return [issue.message for issue in issues if issue.section_id == section_id and issue.severity == "error"]
