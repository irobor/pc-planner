# app/models/project.py
from datetime import datetime
from typing import Optional

from sqlalchemy import ForeignKey, ForeignKeyConstraint, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Project(Base):
    """Проект — объект проектирования (соответствует public.projects).

    Схема — источник истины (правило 26): маппинг сверен построчно
    с oulm.sql. Ссылки на ещё не реализованные модели (Enterprise,
    Workshop, ProjectStage, Designer, ProjectStatus) заведены как
    простые FK-колонки без relationship() — добавим при появлении
    соответствующих моделей.
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
