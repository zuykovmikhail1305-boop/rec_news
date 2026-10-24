import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


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

