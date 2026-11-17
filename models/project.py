# app/models/project.py
from datetime import datetime
from typing import Optional

from sqlalchemy import ForeignKey, ForeignKeyConstraint, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Project(Base):
    """Проект — объект проектирования (соответствует public.projects).

    Схема — источник истины (правило 26): маппинг сверен построчно
    с oulm.sql. Ссылки на предприятие, цех, стадию, проектировщика и
    статус заведены как простые FK-колонки без relationship(): запросы
    делают явный JOIN. Связь добавим, когда она кому-то понадобится.
    """

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    code_link: Mapped[Optional[str]] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, nullable=False)

    enterprise_id: Mapped[int] = mapped_column(
        ForeignKey("enterprises.id", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
    )

    # FK на workshop_id объявлен НЕ здесь, а композитно в __table_args__,
    # т.к. в БД это составной ключ (workshop_id, enterprise_id) —
    # гарантирует, что цех принадлежит тому же предприятию, что и проект.
    workshop_id: Mapped[Optional[int]]

    stage_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("project_stages.id", onupdate="CASCADE", ondelete="SET NULL"),
    )
    designer_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("designer.id", onupdate="CASCADE", ondelete="SET NULL"),
    )
    status_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("project_status.id", onupdate="CASCADE", ondelete="SET NULL"),
        server_default="1",
    )

    # server_default вместо default=datetime.utcnow: штамп времени
    # проставляет БД (CURRENT_TIMESTAMP), а не приложение — единый
    # источник истины для всех клиентов БД (включая Flask-приложение).
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    # Без onupdate на стороне ORM: обновление уже делает триггер
    # update_projects_timestamp в БД (см. oulm.sql). Дублирование
    # логики здесь привело бы к двум независимым источникам истины.
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        # Нужен для композитных FK из других таблиц (например,
        # project_buildings ссылается на пару id + enterprise_id).
        UniqueConstraint("id", "enterprise_id", name="projects_unique_pair"),
        # Композитный FK: workshop_id действителен только в паре
        # со своим enterprise_id — гарантирует, что цех и проект
        # принадлежат одному предприятию.
        ForeignKeyConstraint(
            ["workshop_id", "enterprise_id"],
            ["workshops.id", "workshops.enterprise_id"],
            name="projects_workshop_enterprise_fkey",
            onupdate="CASCADE",
            ondelete="SET NULL",
        ),
    )

    def __repr__(self) -> str:
        return f"Project(id={self.id!r}, code={self.code!r}, title={self.title!r})"


class Enterprise(Base):
    """Предприятие. Таблица контура Flask, наш контур только читает.

    Цель FK projects.enterprise_id. Сводному отчёту — запасная подпись
    «Заказчика» для проекта вне реестра Flask (VProjectRegistry). Поля —
    строго по DDL. Связи (relationship) не заведены: запросы делают явный
    JOIN.
    """

    __tablename__ = "enterprises"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    city: Mapped[str | None] = mapped_column(String(100))

    def __repr__(self) -> str:
        return f"Enterprise(id={self.id!r}, name={self.name!r})"


class Workshop(Base):
    """Цех предприятия. Таблица контура Flask, наш контур только читает.

    Цель FK projects.workshop_id: без модели metadata не знает этой
    таблицы, и сверка моделей со схемой (Alembic) падает. Поля и
    ограничения — строго по DDL.
    """

    __tablename__ = "workshops"

    id: Mapped[int] = mapped_column(primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(
        ForeignKey("enterprises.id", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
    )
    workshop_name: Mapped[str] = mapped_column(String(100), nullable=False)

    __table_args__ = (
        # Название цеха уникально в пределах предприятия.
        UniqueConstraint(
            "enterprise_id",
            "workshop_name",
            name="workshops_enterprise_id_workshop_name_key",
        ),
        # Цель композитного FK проекта (projects_workshop_enterprise_fkey):
        # цех проекта обязан принадлежать предприятию проекта.
        UniqueConstraint("id", "enterprise_id", name="workshops_id_enterprise_id_key"),
    )

    def __repr__(self) -> str:
        return f"Workshop(id={self.id!r}, workshop_name={self.workshop_name!r})"