"""Агент карточки инсайта: агенты блоков → программная проверка → агент-проверщик (docs/insight_card.md).

1. **Контекст** (:mod:`insights.context`): публикации о событии ``N*``,
   аналитика ``A1``, данные компании ``F*``, профиль.
2. **Агенты блоков** (:mod:`insights.blocks`) в три этапа, внутри этапа —
   параллельно. После каждого блока — программная проверка
   (:mod:`insights.checks`); блок с ошибкой переписывается его агентом
   с текстом замечания (до ``block_retries`` раз).
3. **Агент-проверщик** (:mod:`insights.verifier`) смотрит всю карточку против
   источников. Блоки, где он нашёл ошибку, переписываются по его замечанию
   и проверяются снова — до ``verify_rounds`` проверок.
4. **Сборка** — блоки в контракт :class:`InsightCard`; блок «На чём основан
   вывод» и шаги «Как получен вывод» строятся кодом.

Без LLM карточка строится в упрощённом виде (``status = degraded``): шапка по
правилам, факт — лид новости, позиция банка — связка из рекомендаций.
"""

from __future__ import annotations

import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from news_pipeline.env import env_int
from news_pipeline.insights.blocks import HORIZON, IMPACT, PRIORITY, PRODUCTS, SPECS, BlockAgent
from news_pipeline.insights.checks import MAX_SENTENCES, check_block, errors
from news_pipeline.insights.context import CardContext, SourceDoc, build_context
from news_pipeline.insights.numbers import numbers_in
from news_pipeline.insights.schemas import (
    Actions, AIConclusion, BankOffer, BankView, BlockIssue, BlockVerification, Chip, CompanyFeature, Evidence,
    EvidenceSource, Fact, Header, Headline, Hypothesis, InsightCard, KeyFigure, RiskItem, RisksOpportunities,
    TraceStep, Verification,
)
from news_pipeline.insights.store import InsightCardStore
from news_pipeline.insights.verifier import CardVerifier
from news_pipeline.llm.client import LLMError, LLMUnavailable
from news_pipeline.preprocessing.cleaning import sentences
from news_pipeline.settings import SETTINGS

if TYPE_CHECKING:
    from news_pipeline.service import MLService

log = logging.getLogger("rec_news")

# Ошибки, с которыми блок нельзя показывать: он вводит в заблуждение. С остальными блок остаётся
# со статусом ``unverified``, а замечания видны в ``verification``.
HARD_CODES = {"invented_number", "bad_source", "trade_advice", "tone_forbidden", "bank_voice", "language",
              "headline_is_title", "offer_off_topic", "bank_not_named", "contradiction_missed", "llm_error"}
# Без этих блоков карточка не показывается целиком — у них есть запасной вариант или они обязательны.
OPTIONAL = {"hypothesis", "bank_offer"}
NO_ANALYTICS_WARNING = ("Аналитики Альфа-Банка по этой теме нет. Выводы ниже — не позиция банка, "
                        "а вывод AI по публикациям и данным компании.")
_RELIABILITY = {5: "высокая", 4: "высокая", 3: "средняя", 2: "низкая", 1: "низкая"}


def _stems(text: str) -> set[str]:
    return {word.casefold()[:5] for word in text.split() if len(word) >= 4}


def _quote(doc: SourceDoc, about: str) -> str | None:
    """Предложение источника, ближе всего к тексту карточки (цитата дословно)."""
    target = _stems(about)
    best, best_score = None, 0.0
    for sentence in sentences(doc.text)[:40]:
        letters = [char for char in sentence if char.isalpha()]
        if not 30 <= len(sentence) <= 320 or not letters:
            continue
        if sum(char.isupper() for char in letters) / len(letters) > 0.15 or sentence.count(":") + sentence.count(";") > 2:
            continue
        stems = _stems(sentence)
        score = len(stems & target) / (len(stems) ** 0.5 or 1)
        if score > best_score:
            best, best_score = sentence, score
    return best


class InsightCardAgent:
    def __init__(self, service: "MLService", store: InsightCardStore | None = None) -> None:
        self.service = service
        self.llm = service.llm
        self.config = SETTINGS.insight_card
        self.agent = BlockAgent(self.llm, self.config)
        self.verifier = CardVerifier(self.llm, self.config)
        self.store = store or InsightCardStore()
        self.workers = env_int("REC_NEWS_INSIGHT_PARALLEL_BLOCKS", self.config["parallel_blocks"])

    # --- публичный интерфейс -----------------------------------------------------------

    def get(self, user_id: str, news_id: str) -> InsightCard | None:
        cached = self.store.get(user_id, news_id)
        return cached[0] if cached else None

    def build(self, user_id: str, news_id: str, *, refresh: bool = False) -> InsightCard:
        """Карточка для пары пользователь — новость; готовая берётся из базы, если входные данные не менялись.

        ``KeyError`` — нет профиля, ``LookupError`` — нет новости в ``unique.db``.
        """
        ctx = build_context(self.service, user_id, news_id)
        fingerprint = ctx.fingerprint()
        cached = self.store.get(user_id, news_id)
        if cached is not None and not refresh and cached[1] == fingerprint and cached[0].status != "degraded":
            return cached[0]
        card = self._build(ctx)
        self.store.put(card, fingerprint)
        return card

    # --- построение --------------------------------------------------------------------

    def _build(self, ctx: CardContext) -> InsightCard:
        timings: dict[str, int] = {}
        blocks: dict[str, dict | None] = {}
        states: dict[str, BlockVerification] = {}
        rounds, comment, verified, used_llm = 0, None, False, False
        if self.llm.is_available():
            used_llm = True
            try:
                for stage in (1, 2, 3):
                    started = time.monotonic()
                    self._stage(stage, ctx, blocks, states)
                    timings[f"stage_{stage}"] = int((time.monotonic() - started) * 1000)
                started = time.monotonic()
                rounds, comment, verified = self._verify(ctx, blocks, states)
                timings["verify"] = int((time.monotonic() - started) * 1000)
            except LLMUnavailable:
                log.warning("LLM недоступна — карточка %s/%s в упрощённом виде", ctx.user_id, ctx.news_id)
                blocks, states, used_llm = {}, {}, False
        return self._assemble(ctx, blocks, states, rounds, comment, verified, timings, used_llm)

    def _applicable(self, name: str, ctx: CardContext, blocks: dict[str, dict | None]) -> str | None:
        """Причина пропустить блок или ``None``."""
        if name == "bank_view" and ctx.analytics is None:
            return "аналитики Альфа-Банка по теме нет"
        if name == "bank_offer" and not (blocks.get("risks") or {}).get("risks"):
            return "нет рисков, с которыми мог бы помочь продукт"
        return None

    def _stage(self, stage: int, ctx: CardContext, blocks: dict, states: dict) -> None:
        names = []
        for name, spec in SPECS.items():
            if spec.stage != stage:
                continue
            reason = self._applicable(name, ctx, blocks)
            if reason:
                blocks[name] = None
                states[name] = BlockVerification(status="skipped", issues=[BlockIssue(
                    code="not_applicable", severity="warning", message=reason)])
            else:
                names.append(name)
        snapshot = dict(blocks)
        with ThreadPoolExecutor(max(1, min(self.workers, len(names) or 1))) as pool:
            results = list(pool.map(lambda name: self._write_checked(name, ctx, snapshot), names))
        for name, (data, state) in zip(names, results):
            blocks[name], states[name] = data, state

    def _write_checked(self, name: str, ctx: CardContext, blocks: dict, notes: list[str] | None = None,
                       first_attempt: int = 1) -> tuple[dict | None, BlockVerification]:
        """Агент блока + программная проверка, до ``block_retries`` переписываний."""
        spec = SPECS[name]
        base_notes = list(notes or [])
        current_notes = list(base_notes)
        data, issues, attempts = None, [], 0
        for attempt in range(first_attempt, first_attempt + self.config["block_retries"] + 1):
            attempts += 1
            try:
                data = self.agent.write(spec, ctx, blocks, current_notes, attempt)
            except LLMUnavailable:
                raise
            except LLMError as error:
                data, issues = None, [BlockIssue(code="llm_error", severity="error", message=str(error)[:300])]
                continue
            if name == "bank_offer":
                data["risk_texts"] = self._offer_risks(data, blocks)
            issues = check_block(name, data, ctx)
            if not errors(issues):
                break
            current_notes = base_notes + [issue.message for issue in errors(issues)]
        model = self.llm.model_for(spec.task)
        if data is not None and name in MAX_SENTENCES and {issue.code for issue in errors(issues)} == {"too_long"}:
            data = dict(data, text=" ".join(sentences(data["text"])[:MAX_SENTENCES[name]]))
            issues = check_block(name, data, ctx) + [BlockIssue(
                code="trimmed", severity="warning", message="текст сокращён до допустимого числа предложений")]
        if data is None:
            return None, BlockVerification(status="failed", attempts=attempts, model=model, issues=issues)
        if name == "bank_offer" and data["product_id"] == "none":
            return None, BlockVerification(status="skipped", attempts=attempts, model=model, issues=[BlockIssue(
                code="not_applicable", severity="warning", message="подходящего продукта нет")])
        hard = [issue for issue in errors(issues) if issue.code in HARD_CODES]
        if hard:
            return None, BlockVerification(status="failed", attempts=attempts, model=model, issues=issues)
        status = "unverified" if errors(issues) else "ok"
        return data, BlockVerification(status=status, attempts=attempts, model=model, issues=issues)

    @staticmethod
    def _offer_risks(data: dict, blocks: dict) -> list[str]:
        risks = (blocks.get("risks") or {}).get("risks") or []
        index = data.get("risk_index")
        if index is not None and 1 <= index <= len(risks):
            return [risks[index - 1]["text"]]
        return [item["text"] for item in risks]

    def _verify(self, ctx: CardContext, blocks: dict, states: dict) -> tuple[int, str | None, bool]:
        """Проверщик → переписывание блоков с ошибкой → снова проверщик."""
        comment, rounds = None, 0
        for round_number in range(1, self.config["verify_rounds"] + 1):
            review = self.verifier.review(ctx, blocks, round_number)
            if review is None:
                return rounds, comment, False
            rounds, comment = round_number, review.comment
            for state in states.values():
                state.issues = [issue for issue in state.issues if issue.by != "verifier"]
            flagged = []
            for name, verdict in review.verdicts.items():
                state = states.get(name)
                if state is None or blocks.get(name) is None:
                    continue
                if not verdict.ok and (verdict.problem or verdict.fix):
                    state.issues.append(BlockIssue(code="verifier", severity=verdict.severity, by="verifier",
                                                   message=f"{verdict.problem} {verdict.fix}".strip()))
                if verdict.needs_rewrite:
                    flagged.append(name)
            if not flagged:
                return rounds, comment, True
            if round_number == self.config["verify_rounds"]:
                for name in flagged:
                    states[name].status = "unverified"
                    if name in OPTIONAL:
                        blocks[name] = None
                        states[name].status = "failed"
                return rounds, comment, True
            for stage in (1, 2, 3):
                names = [name for name in flagged if SPECS[name].stage == stage]
                if not names:
                    continue
                snapshot = dict(blocks)

                def rewrite(name: str):
                    verdict = review.verdicts[name]
                    note = f"Проверщик: {verdict.problem} Что исправить: {verdict.fix}".strip()
                    return self._write_checked(name, ctx, snapshot, [note], first_attempt=10 * round_number)

                with ThreadPoolExecutor(max(1, min(self.workers, len(names)))) as pool:
                    results = list(pool.map(rewrite, names))
                for name, (data, state) in zip(names, results):
                    previous = states[name]
                    if data is not None and state.status == "ok":
                        blocks[name] = data
                        previous.status = "fixed"
                    else:
                        previous.status = "unverified"
                    previous.attempts += state.attempts
                    previous.issues += [issue for issue in state.issues if issue.severity == "error"]
        return rounds, comment, True

    # --- сборка --------------------------------------------------------------------------

    def _header(self, ctx: CardContext, data: dict | None) -> Header:
        count = len(ctx.outlets)
        if count >= 2:
            reliability = Chip(value="confirmed", label=f"Подтверждено {count} источниками",
                               reason=", ".join(ctx.outlets))
        else:
            reliability = Chip(value="single_source", label="Пока один источник",
                               reason=f"{ctx.outlets[0] if ctx.outlets else 'источник'}, надёжность "
                                      f"{ctx.news[0].reliability}/5")
        if data is not None:
            return Header(
                impact=Chip(value=data["impact"]["value"], label=IMPACT[data["impact"]["value"]],
                            reason=data["impact"]["reason"] or None),
                priority=Chip(value=data["priority"]["value"], label=PRIORITY[data["priority"]["value"]],
                              reason=data["priority"]["reason"] or None),
                horizon=Chip(value=data["horizon"]["value"], label=HORIZON[data["horizon"]["value"]],
                             reason=data["horizon"]["reason"] or None),
                reliability=reliability, sources_count=count,
            )
        importance = ctx.importance or 3
        priority = "high" if importance >= 5 else "medium" if importance >= 3 else "low"
        return Header(
            impact=Chip(value="unknown", label="Не оценено", reason="оценка влияния недоступна"),
            priority=Chip(value=priority, label=PRIORITY[priority], reason=f"важность события {importance}/5"),
            horizon=Chip(value="unknown", label="Не оценено", reason="оценка горизонта недоступна"),
            reliability=reliability, sources_count=count,
        )

    def _assemble(self, ctx: CardContext, blocks: dict, states: dict, rounds: int, comment: str | None,
                  verified: bool, timings: dict[str, int], used_llm: bool) -> InsightCard:
        features = {item.id: item for item in ctx.facts}

        def feature_list(ids: list[str]) -> list[CompanyFeature]:
            return [CompanyFeature(id=fid, text=features[fid].text) for fid in ids if fid in features]

        data = blocks.get("fact")
        if data is not None:
            figures = []
            for item in data["key_figures"]:
                ids = item["source_ids"] or [doc.id for doc in ctx.news
                                             if numbers_in(item["value"]) <= numbers_in(f"{doc.title} {doc.text}")]
                figures.append(KeyFigure(value=item["value"], label=item["label"], source_ids=ids))
            fact = Fact(text=data["text"], decided=data["decided"], discussed=data["discussed"],
                        key_figures=figures, source_ids=data["source_ids"])
        else:
            body = " ".join(sentences(ctx.news[0].text)[:2])
            fact = Fact(text=body or ctx.title, source_ids=["N1"])
            states["fact"] = states.get("fact") or BlockVerification(status="fallback")
            if states["fact"].status != "skipped":
                states["fact"].status = "fallback"

        bank_view = self._bank_view(ctx, blocks.get("bank_view"), states)

        data = blocks.get("ai_conclusion")
        ai_conclusion = AIConclusion(text=data["text"], channels=data["channels"],
                                     company_features=feature_list(data["feature_ids"]),
                                     source_ids=data["source_ids"]) if data else None
        data = blocks.get("risks")
        risks = RisksOpportunities(risks=[RiskItem(**item) for item in data["risks"][:3]],
                                   opportunities=[RiskItem(**item) for item in data["opportunities"][:3]]) if data else None
        data = blocks.get("hypothesis")
        hypothesis = Hypothesis(condition=data["condition"], consequence=data["consequence"], text=data["text"]) \
            if data else None
        data = blocks.get("actions")
        actions = Actions(steps=data["steps"][:4], question=data["question"]) if data else None
        data = blocks.get("bank_offer")
        offer = None
        if data is not None and data["product_id"] in PRODUCTS:
            product = PRODUCTS[data["product_id"]]
            count = len(risks.risks) if risks else 0
            index = data["risk_index"] - 1 if data["risk_index"] and 1 <= data["risk_index"] <= count else None
            offer = BankOffer(product_id=product["id"], name=product["name"], description=product["description"],
                              reason=data["reason"], risk_index=index)
        data = blocks.get("headline")
        # интерес — точное название из профиля; модель могла написать его с другим регистром или не из списка
        aspects = {aspect.title.casefold(): aspect.title for aspect in ctx.profile.aspects}
        headline = Headline(
            text=data["text"], company=ctx.company, role=ctx.profile.role, event_date=ctx.published_at,
            aspect=aspects.get((data["aspect"] or "").casefold()),
            company_features=feature_list(data["feature_ids"]), source_ids=data["source_ids"],
            based_on_analytics=ctx.analytics is not None and ctx.analytics.id in data["source_ids"],
        ) if data else None

        evidence = self._evidence(ctx, fact, bank_view, ai_conclusion, risks, states, rounds, comment, verified,
                                  used_llm)
        if not used_llm:
            verdict = "not_verified"
        elif any(state.status in ("unverified", "failed") for name, state in states.items() if name not in OPTIONAL) \
                or headline is None or ai_conclusion is None:
            verdict = "partial"
        elif not verified:
            verdict = "not_verified"
        elif any(state.status == "fixed" for state in states.values()):
            verdict = "passed_with_fixes"
        else:
            verdict = "passed"
        status = "degraded" if not used_llm else "partial" if verdict == "partial" else "ready"
        return InsightCard(
            card_id=f"ic_{uuid.uuid4().hex[:12]}", user_id=ctx.user_id, news_id=ctx.news_id, event_id=ctx.event_id,
            status=status, generated_at=datetime.now(timezone.utc), pipeline_version=SETTINGS.pipeline_version,
            header=self._header(ctx, blocks.get("header")), headline=headline, fact=fact, bank_view=bank_view,
            ai_conclusion=ai_conclusion, risks=risks, hypothesis=hypothesis, actions=actions, bank_offer=offer,
            evidence=evidence,
            verification=Verification(verdict=verdict, rounds=rounds, comment=comment, blocks=states),
            timings_ms=timings,
        )

    def _bank_view(self, ctx: CardContext, data: dict | None, states: dict) -> BankView:
        doc, match = ctx.analytics, ctx.analytics_match
        if doc is None:
            return BankView(available=False, warning=NO_ANALYTICS_WARNING)
        material = dict(material_title=doc.title, material_date=doc.published_at, section_title=doc.section_title,
                        url=doc.url, relation=match.relation if match else None,
                        contradicts=bool(match and match.contradicts), source_ids=["A1"])
        if data is not None:
            return BankView(available=True, text=data["text"], **material)
        if match is not None and match.summary:
            # агент блока не справился — связка «новость ↔ аналитика» из рекомендаций
            states.setdefault("bank_view", BlockVerification(status="fallback")).status = "fallback"
            return BankView(available=True, text=match.summary, **material)
        return BankView(available=False, warning=NO_ANALYTICS_WARNING)

    def _evidence(self, ctx: CardContext, fact: Fact, bank_view: BankView, ai: AIConclusion | None,
                  risks: RisksOpportunities | None, states: dict, rounds: int, comment: str | None,
                  verified: bool, used_llm: bool) -> Evidence:
        cited = set(fact.source_ids) | set(ai.source_ids if ai else [])
        for item in (risks.risks + risks.opportunities) if risks else []:
            cited |= set(item.source_ids)
        about = " ".join([fact.text, ai.text if ai else ""])
        sources = [
            EvidenceSource(id=doc.id, type="news", title=doc.title, outlet=doc.outlet, url=doc.url,
                           published_at=doc.published_at, reliability=doc.reliability,
                           reliability_label=_RELIABILITY.get(doc.reliability, "средняя"), quote=_quote(doc, about))
            for doc in ctx.news if doc.id in cited or doc.id == "N1"
        ]
        material = None
        if ctx.analytics is not None and bank_view.available:
            doc = ctx.analytics
            material = EvidenceSource(id=doc.id, type="analytics", title=doc.title, outlet="Альфа Ресерч", url=doc.url,
                                      published_at=doc.published_at, reliability=5,
                                      reliability_label="аналитика Альфа-Банка",
                                      quote=_quote(doc, bank_view.text or ""))
        used = list(ai.company_features) if ai else []
        used_ids = {item.id for item in used}
        for item in (risks.risks + risks.opportunities) if risks else []:
            for fid in item.feature_ids:
                if fid not in used_ids and ctx.fact(fid):
                    used.append(CompanyFeature(id=fid, text=ctx.fact(fid).text))
                    used_ids.add(fid)
        return Evidence(sources=sources, analytics_material=material, company_data=used,
                        trace=self._trace(ctx, states, rounds, comment, verified, used, used_llm))

    def _trace(self, ctx: CardContext, states: dict, rounds: int, comment: str | None, verified: bool,
               used: list[CompanyFeature], used_llm: bool) -> list[TraceStep]:
        steps: list[tuple[str, str]] = []
        if ctx.rank is not None:
            detail = f"Место {ctx.rank} в вашей ленте, оценка {ctx.score:.2f}."
            if ctx.why_relevant:
                detail += f" {ctx.why_relevant}"
            steps.append(("Новость отобрана для вас", detail))
        else:
            steps.append(("Новость выбрана по запросу", "Карточка построена для новости, которую вы открыли."))
        methods = {"exact": "точные копии", "url": "та же ссылка", "minhash": "почти копии", "semantic": "пересказы"}
        merged = sorted({methods.get(m, m) for doc_id, m in ctx.methods.items() if m and m != "original"})
        steps.append(("Событие собрано из публикаций",
                      f"Изданий: {len(ctx.outlets)} ({', '.join(ctx.outlets)})."
                      + (f" Объединены {', '.join(merged)} одного события." if merged else "")))
        if ctx.analytics is not None:
            relation = ctx.analytics_match.relation if ctx.analytics_match else "общая тема"
            steps.append(("Подобрана аналитика Альфа-Банка",
                          f"«{ctx.analytics.title}», связь с новостью: {relation}. Фрагмент выбран под ваши интересы."))
        else:
            steps.append(("Аналитика Альфа-Банка", "Подходящего материала аналитиков банка по теме нет."))
        steps.append(("Учтены данные о компании",
                      "; ".join(item.text for item in used) if used else
                      ("данные есть, но в выводе не понадобились" if ctx.facts else "данных о компании нет")))
        if used_llm:
            written = [f"{SPECS[name].title} — {state.model}" for name, state in states.items()
                       if state.model and state.status not in ("skipped",)]
            steps.append(("Каждый блок написал отдельный агент", "; ".join(written) + "."))
            fixed_by_code = sum(state.attempts > 1 for state in states.values())
            steps.append(("Программная проверка",
                          "Числа сверены с источниками, проверены ссылки, тон и отсутствие прямых советов."
                          + (f" Переписано блоков: {fixed_by_code}." if fixed_by_code else "")))
            if verified:
                fixed = [SPECS[name].title for name, state in states.items() if state.status == "fixed"]
                detail = f"Модель {self.llm.model_for('insight_verifier')}, проверок: {rounds}."
                if fixed:
                    detail += f" Исправлены по замечаниям: {', '.join(fixed)}."
                if comment:
                    detail += f" {comment}"
                steps.append(("Агент-проверщик сверил карточку с источниками", detail))
            else:
                steps.append(("Агент-проверщик", "Проверка не выполнена — карточка показана без неё."))
        else:
            steps.append(("Упрощённый режим", "Языковая модель недоступна: показаны факт, позиция банка и источники."))
        return [TraceStep(step=number, title=title, detail=detail) for number, (title, detail) in enumerate(steps, 1)]
