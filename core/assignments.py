# app/core/assignments.py
"""Актуальное назначение работника.

Назначение (employee_assignments) — где и кем работник числится в
период valid_from … valid_to. Правило «какое назначение действует на
дату» записано здесь один раз и используется всеми, кому оно нужно:
областью управления (app/core/scope.py) и меню пользователя в шапке
(require_user_html в app/core/guards.py).

Отдельный модуль, а не часть scope.py: scope.py импортирует guards.py
(require_user_html), а guards.py нужна карточка пользователя. Будь
карточка в scope.py, импорт замкнулся бы в круг. Этот модуль зависит
только от моделей, поэтому его могут импортировать оба.
"""

from dataclasses import dataclass
from datetime import date

from sqlalchemy import ColumnElement, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.composition import Department
from app.models.employee import EmployeeAssignment, Position
from app.models.user import User


def is_current_assignment(today: date) -> ColumnElement[bool]:
    """Условие WHERE: назначение действует на дату today.

    Граница valid_to ВКЛЮЧИТЕЛЬНАЯ — так же, как daterange(..., '[]') в
    EXCLUDE-ограничении employee_assignments_no_overlap: в последний день
    назначение ещё действует. NULL в valid_to — назначение открыто.
    Менять условие только вместе с ограничением в БД: иначе область
    управления и меню разойдутся с тем, что считает база.
    """
    return and_(
        EmployeeAssignment.valid_from <= today,
        or_(
            EmployeeAssignment.valid_to.is_(None),
            EmployeeAssignment.valid_to >= today,
        ),
    )


@dataclass(frozen=True)
class UserCard:
    """Должность и подразделение для меню пользователя в шапке."""

    position: str
    department: str


async def get_user_card(db: AsyncSession, user: User) -> UserCard | None:
    """Карточка пользователя по актуальному назначению или None.

    None — законное состояние: у служебной учётки нет кадровой карточки,
    у уволенного нет действующего назначения. Шапка тогда просто не
    показывает эти строки.
    """
    # Связь employee загружена вместе с пользователем (lazy="joined"),
    # поэтому проверка запроса в БД не порождает.
    if user.employee is None:
        return None

    stmt = (
        select(Position.name, Department.name)
        .select_from(EmployeeAssignment)
        .join(Position, Position.id == EmployeeAssignment.position_id)
        .join(Department, Department.id == EmployeeAssignment.department_id)
        .where(
            EmployeeAssignment.employee_id == user.employee.id,
            is_current_assignment(date.today()),
        )
        # Индекс idx_assignments_employee (employee_id, valid_from).
        # Единственность действующего назначения гарантирует EXCLUDE в БД.
        .limit(1)
    )
    row = (await db.execute(stmt)).first()
    if row is None:
        return None
    return UserCard(position=row[0], department=row[1])