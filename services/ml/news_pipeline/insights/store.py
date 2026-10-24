"""Готовые карточки инсайтов — ``data/insight_cards.db`` (ORM-модель :class:`InsightCardRow`).

Строка на пару (``user_id``, ``news_id``): карточка целиком JSON-ом и ключевые
поля отдельными колонками (для ленты и отбора без разбора JSON).
``fingerprint`` — отпечаток входных данных: если он изменился (новая аналитика,
новые публикации о событии, другая анкета), карточка строится заново.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Index, Text, func, select
from sqlalchemy.orm import Mapped, mapped_column

from news_pipeline.config import DATA_DIR
from news_pipeline.insights.schemas import InsightCard, InsightCardSummary
from news_pipeline.storage import db

INSIGHT_CARDS_DB = DATA_DIR / "insight_cards.db"


class InsightCardRow(db.Base):
    __tablename__ = "insight_cards"
    __table_args__ = (Index("insight_cards_card_id", "card_id"),)

    user_id: Mapped[str] = mapped_column(Text, primary_key=True)
    news_id: Mapped[str] = mapped_column(Text, primary_key=True)
    card_id: Mapped[str] = mapped_column(Text)
    event_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    verdict: Mapped[str] = mapped_column(Text)
    headline: Mapped[str | None] = mapped_column(Text)
    impact: Mapped[str] = mapped_column(Text)
    priority: Mapped[str] = mapped_column(Text)
    horizon: Mapped[str] = mapped_column(Text)
    reliability: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(Text)
    card: Mapped[str] = mapped_column(Text)
    generated_at: Mapped[str] = mapped_column(Text)
    pipeline_version: Mapped[str] = mapped_column(Text)

    def summary(self) -> InsightCardSummary:
        return InsightCardSummary(card_id=self.card_id, status=self.status, headline=self.headline, impact=self.impact,
                                  priority=self.priority, horizon=self.horizon, reliability=self.reliability)


def summary(card: InsightCard) -> InsightCardSummary:
    return InsightCardSummary(
        card_id=card.card_id, status=card.status, headline=card.headline.text if card.headline else None,
        impact=card.header.impact.value, priority=card.header.priority.value, horizon=card.header.horizon.value,
        reliability=card.header.reliability.value,
    )


class InsightCardStore:
    def __init__(self, path: str | Path = INSIGHT_CARDS_DB) -> None:
        self.path = Path(path)
        self.engine = db.engine(self.path, wal=True)
        db.ensure_tables(self.engine, [InsightCardRow])

    def put(self, card: InsightCard, fingerprint: str) -> None:
        with db.session(self.engine) as session:   # ORM-upsert: карточку могут писать несколько воркеров
            session.execute(db.upsert(InsightCardRow), [{
                "user_id": card.user_id, "news_id": card.news_id, "card_id": card.card_id, "event_id": card.event_id,
                "status": card.status, "verdict": card.verification.verdict,
                "headline": card.headline.text if card.headline else None, "impact": card.header.impact.value,
                "priority": card.header.priority.value, "horizon": card.header.horizon.value,
                "reliability": card.header.reliability.value, "fingerprint": fingerprint,
                "card": card.model_dump_json(), "generated_at": card.generated_at.isoformat(),
                "pipeline_version": card.pipeline_version,
            }])

    def get(self, user_id: str, news_id: str) -> tuple[InsightCard, str] | None:
        """Карточка и её отпечаток."""
        with db.session(self.engine) as session:
            row = session.get(InsightCardRow, (user_id, news_id))
        return (InsightCard.model_validate_json(row.card), row.fingerprint) if row else None

    def by_id(self, card_id: str) -> InsightCard | None:
        with db.session(self.engine) as session:
            value = session.scalar(select(InsightCardRow.card).where(InsightCardRow.card_id == card_id).limit(1))
        return InsightCard.model_validate_json(value) if value else None

    def count(self, user_id: str) -> int:
        with db.session(self.engine) as session:
            return session.scalar(select(func.count()).select_from(InsightCardRow)
                                  .where(InsightCardRow.user_id == user_id))

    def summaries(self, user_id: str, news_ids: list[str]) -> dict[str, InsightCardSummary]:
        if not news_ids:
            return {}
        result = {}
        with db.session(self.engine) as session:
            for part in db.chunks(list(news_ids)):
                rows = session.scalars(select(InsightCardRow).where(InsightCardRow.user_id == user_id,
                                                                    InsightCardRow.news_id.in_(part)))
                result.update({row.news_id: row.summary() for row in rows})
        return result
