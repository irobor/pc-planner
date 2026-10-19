# app/models/admin.py
"""ORM-модели журнала правок администратора.

Построены строго по DDL миграции admin_journal_up.sql (правило 26 —
схема как источник истины). Устроены как коммит в Git:

    AdminChangeset  — одна запись на одно сохранение: кто, когда, чьи
                      данные, комментарий (сообщение коммита);
    AdminChangeItem — изменённая ячейка этого сохранения: полоса, день,
                      было → стало (diff).

Ограничения целостности (непустой комментарий, «только настоящие
изменения», одна ячейка — одна строка на правку) живут в базе. Здесь
они повторены в __table_args__ для полноты описания: ORM таблиц не
создаёт, схема меняется миграциями.
"""

from datetime import date, datetime
from typing import List, Optional

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class AdminChangeset(Base):
    """Правка администратора — сообщение коммита.

    employee_id — ЧЬИ данные правили; changed_by — КТО правил
    (пользователь, а не сотрудник: у администратора может не быть
    кадровой карточки). Оба RESTRICT: история правок не пропадает молча.
    """

    __tablename__ = "admin_changesets"

    id: Mapped[int] = mapped_column(primary_key=True)

    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="RESTRICT"),
        nullable=False,
    )

    changed_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    comment: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # Ячейки правки. lazy="raise_on_sql": в async-сессии ленивая загрузка
    # упала бы с MissingGreenlet; явная ошибка лучше неявной. Читать
    # ячейки — отдельным запросом или через selectinload.
    items: Mapped[List["AdminChangeItem"]] = relationship(
        back_populates="changeset",
        lazy="raise_on_sql",
    )

    __table_args__ = (
        CheckConstraint(
            "btrim(comment) <> ''",
            name="admin_changesets_comment_not_blank",
        ),
        # DESC: журнал сотрудника — свежие правки сверху.
        Index(
            "idx_admin_changesets_employee_created",
            "employee_id",
            text("created_at DESC"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"AdminChangeset(id={self.id!r}, "
            f"employee_id={self.employee_id!r}, "
            f"changed_by={self.changed_by!r})"
        )


class AdminChangeItem(Base):
    """Изменённая ячейка правки — строка diff.

    old / new = NULL — ячейка была пустой / стала пустой. Снимки шифра
    (old_marka, new_marka) хранятся текстом: внешний ключ при удалении
    шифра обнулится (SET NULL), а читаемая запись останется.
    """

    __tablename__ = "admin_change_items"

    id: Mapped[int] = mapped_column(primary_key=True)

    changeset_id: Mapped[int] = mapped_column(
        ForeignKey("admin_changesets.id", ondelete="CASCADE"),
        nullable=False,
    )

    # 'plan' или 'fact'.
    layer: Mapped[str] = mapped_column(String(4), nullable=False)

    work_date: Mapped[date] = mapped_column(Date, nullable=False)

    old_mark_set_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("project_mark_sets.id", ondelete="SET NULL")
    )
    new_mark_set_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("project_mark_sets.id", ondelete="SET NULL")
    )

    old_marka: Mapped[Optional[str]] = mapped_column(Text)
    new_marka: Mapped[Optional[str]] = mapped_column(Text)

    changeset: Mapped["AdminChangeset"] = relationship(
        back_populates="items",
        lazy="raise_on_sql",
    )

    __table_args__ = (
        CheckConstraint(
            "layer IN ('plan', 'fact')",
            name="admin_change_items_layer_check",
        ),
        CheckConstraint(
            "old_mark_set_id IS NULL OR old_marka IS NOT NULL",
            name="admin_change_items_old_snapshot",
        ),
        CheckConstraint(
            "new_mark_set_id IS NULL OR new_marka IS NOT NULL",
            name="admin_change_items_new_snapshot",
        ),
        CheckConstraint(
            "old_marka IS DISTINCT FROM new_marka",
            name="admin_change_items_is_change",
        ),
        UniqueConstraint(
            "changeset_id",
            "layer",
            "work_date",
            name="admin_change_items_unique_cell",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"AdminChangeItem(changeset_id={self.changeset_id!r}, "
            f"layer={self.layer!r}, work_date={self.work_date!r}, "
            f"{self.old_marka!r} -> {self.new_marka!r})"
        )
