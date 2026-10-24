"""Готовые карточки инсайтов — ``data/insight_cards.db`` (SQLAlchemy).

Строка на пару (``user_id``, ``news_id``): карточка целиком JSON-ом и ключевые
поля отдельными колонками (для ленты и отбора без разбора JSON).
``fingerprint`` — отпечаток входных данных: если он изменился (новая аналитика,
новые публикации о событии, другая анкета), карточка строится заново.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Column, Index, MetaData, Table, Text, select

from news_pipeline.config import DATA_DIR
from news_pipeline.insights.schemas import InsightCard, InsightCardSummary
from news_pipeline.storage import db

INSIGHT_CARDS_DB = DATA_DIR / "insight_cards.db"

metadata = MetaData()

INSIGHT_CARDS = Table(
    "insight_cards", metadata,
    Column("user_id", Text, primary_key=True),
    Column("news_id", Text, primary_key=True),
    Column("card_id", Text, nullable=False),
    Column("event_id", Text),
    Column("status", Text, nullable=False),
    Column("verdict", Text, nullable=False),
    Column("headline", Text),
    Column("impact", Text, nullable=False),
    Column("priority", Text, nullable=False),
    Column("horizon", Text, nullable=False),
    Column("reliability", Text, nullable=False),
    Column("fingerprint", Text, nullable=False),
    Column("card", Text, nullable=False),
    Column("generated_at", Text, nullable=False),
    Column("pipeline_version", Text, nullable=False),
    Index("insight_cards_card_id", "card_id"),
)

_SUMMARY_COLUMNS = ("card_id", "status", "headline", "impact", "priority", "horizon", "reliability")


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
        db.ensure_tables(self.engine, [INSIGHT_CARDS])

    def put(self, card: InsightCard, fingerprint: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(db.upsert(INSIGHT_CARDS), {
                "user_id": card.user_id, "news_id": card.news_id, "card_id": card.card_id, "event_id": card.event_id,
                "status": card.status, "verdict": card.verification.verdict,
                "headline": card.headline.text if card.headline else None, "impact": card.header.impact.value,
                "priority": card.header.priority.value, "horizon": card.header.horizon.value,
                "reliability": card.header.reliability.value, "fingerprint": fingerprint,
                "card": card.model_dump_json(), "generated_at": card.generated_at.isoformat(),
                "pipeline_version": card.pipeline_version,
            })

    def get(self, user_id: str, news_id: str) -> tuple[InsightCard, str] | None:
        """Карточка и её отпечаток."""
        query = select(INSIGHT_CARDS.c.card, INSIGHT_CARDS.c.fingerprint).where(
            INSIGHT_CARDS.c.user_id == user_id, INSIGHT_CARDS.c.news_id == news_id)
        with self.engine.connect() as connection:
            row = connection.execute(query).first()
        return (InsightCard.model_validate_json(row.card), row.fingerprint) if row else None

    def by_id(self, card_id: str) -> InsightCard | None:
        with self.engine.connect() as connection:
            value = connection.scalar(select(INSIGHT_CARDS.c.card).where(INSIGHT_CARDS.c.card_id == card_id))
        return InsightCard.model_validate_json(value) if value else None

    def summaries(self, user_id: str, news_ids: list[str]) -> dict[str, InsightCardSummary]:
        if not news_ids:
            return {}
        result = {}
        with self.engine.connect() as connection:
            for part in db.chunks(list(news_ids)):
                query = select(INSIGHT_CARDS.c.news_id, *(INSIGHT_CARDS.c[name] for name in _SUMMARY_COLUMNS)).where(
                    INSIGHT_CARDS.c.user_id == user_id, INSIGHT_CARDS.c.news_id.in_(part))
                for row in connection.execute(query):
                    result[row.news_id] = InsightCardSummary(**{name: row._mapping[name] for name in _SUMMARY_COLUMNS})
        return result
