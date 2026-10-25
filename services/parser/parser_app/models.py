from datetime import date, datetime

from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, Enum, ForeignKey, Index, Integer, String,
    Text, UniqueConstraint, func, true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class NewsSource(Base):
    __tablename__ = "news_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    base_url: Mapped[str] = mapped_column(Text)
    listing_url: Mapped[str] = mapped_column(Text)
    parser_key: Mapped[str] = mapped_column(String(80))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    publications: Mapped[list["NewsRaw"]] = relationship(back_populates="source")


class NewsRaw(Base):
    __tablename__ = "news_raw"
    __table_args__ = (
        UniqueConstraint("source_id", "url", name="uq_news_raw_source_url"),
        CheckConstraint(
            "status IN ('pending', 'processing', 'ready', 'retry', 'failed')",
            name="ck_news_raw_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_news_raw_attempt_count"),
        CheckConstraint(
            "content_hash IS NULL OR length(content_hash) = 64",
            name="ck_news_raw_content_hash",
        ),
        Index("ix_news_raw_queue", "status", "next_retry_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("news_sources.id"), index=True)
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    raw_html: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_date: Mapped[date | None] = mapped_column(Date)
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    parser_version: Mapped[str | None] = mapped_column(String(80))

    source: Mapped[NewsSource] = relationship(back_populates="publications")
    quotes: Mapped[list["NewsQuote"]] = relationship(back_populates="publication")


class NewsQuote(Base):
    __tablename__ = "news_quotes"
    __table_args__ = (
        CheckConstraint("start_char >= 0", name="ck_news_quotes_start"),
        CheckConstraint("end_char > start_char", name="ck_news_quotes_end"),
        CheckConstraint(
            "length(quote_text) = end_char - start_char", name="ck_news_quotes_length"
        ),
        CheckConstraint("length(text_hash) = 64", name="ck_news_quotes_text_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    raw_id: Mapped[int] = mapped_column(ForeignKey("news_raw.id"), index=True)
    quote_text: Mapped[str] = mapped_column(Text)
    start_char: Mapped[int] = mapped_column(Integer)
    end_char: Mapped[int] = mapped_column(Integer)
    text_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    publication: Mapped[NewsRaw] = relationship(back_populates="quotes")


class QueueSourceState(Base):
    """Shared request-start reservation for queue workers (no existing columns change)."""
    __tablename__ = "queue_source_state"

    source_id: Mapped[int] = mapped_column(ForeignKey("news_sources.id"), primary_key=True)
    next_request_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BrokerDispatch(Base):
    """Recoverable message-publish reservation; results remain in news_raw."""
    __tablename__ = "parser_broker_dispatch"

    raw_id: Mapped[int] = mapped_column(ForeignKey("news_raw.id"), primary_key=True)
    token: Mapped[str | None] = mapped_column(String(32))
    reserved_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class RSSPollState(Base):
    """Shared, recoverable polling state for one source/feed URL."""
    __tablename__ = "parser_rss_state"
    feed_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_code: Mapped[str] = mapped_column(String(80))
    feed_url: Mapped[str] = mapped_column(Text)
    lease_token: Mapped[str | None] = mapped_column(String(32))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    etag: Mapped[str | None] = mapped_column(Text)
    last_modified: Mapped[str | None] = mapped_column(Text)
    consecutive_errors: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    last_http_status: Mapped[int | None] = mapped_column(Integer)
    last_discovered: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_queued: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
