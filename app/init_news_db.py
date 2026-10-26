"""Create the additive news schema using DATABASE_URL, without starting FastAPI."""

from database import Base, engine
from models import NewsQuote, NewsRaw, NewsSource


def init_news_db(bind=engine):
    Base.metadata.create_all(
        bind=bind,
        tables=[NewsSource.__table__, NewsRaw.__table__, NewsQuote.__table__],
    )


if __name__ == "__main__":
    init_news_db()
    print("News tables are ready")
