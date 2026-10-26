import enum
from datetime import datetime

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, Enum, ForeignKey, Index, Integer, String,
    Text, UniqueConstraint, func, true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class BusinessCategory(str, enum.Enum):
    projects = "Проекты"
    trade = "Торговля"
    services = "Услуги"
    construction = "Строительство и недвижимость"
    agriculture = "Сельское хозяйство и производство"
    it_telecom = "IT и телеком"
    energy = "Энергетика и ресурсы"
    cooperative = "Кооперативные и другие"
    finance = "Финансы и страхование"
    credit_bureau = "Бюро кредитных историй"
    tourism = "Туризм и гостеприимство"
    other = "Другое"


class FsoExecution(str, enum.Enum):
    project_implementation = "Проектное внедрение"
    fso_management = "Управление в ФСО"
    group_support = "Групповая поддержка"
    internal_it = "Внутреннее ИТ-дело"


class PreviousSector(str, enum.Enum):
    public_sector = "В государственном секторе"
    private_sector = "В частном секторе"
    no_experience = "Не имел опыт работы"


class ServiceModel(str, enum.Enum):
    service_rental = "Аренда сервиса"
    own_development = "Собственная разработка"
    high_level_solution = "Высокоуровневое решение"
    ready_solution_custom = "Готовое решение с доработкой"
    box_based = "Решение на базе коробки"
    mixed = "Различные способы"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    profile: Mapped["UserProfile | None"] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )


class UserProfile(Base):
    __tablename__ = "user_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )

    categories: Mapped[str] = mapped_column(Text, default="")
    activity_description: Mapped[str] = mapped_column(Text, default="")
    fso_execution: Mapped[str | None] = mapped_column(String(120), nullable=True)
    previous_sector: Mapped[str | None] = mapped_column(String(120), nullable=True)
    holding_center_employee: Mapped[bool] = mapped_column(Boolean, default=False)
    service_models: Mapped[str] = mapped_column(Text, default="")

    user: Mapped[User] = relationship(back_populates="profile")


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

