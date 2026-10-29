# app/core/scope.py
"""Вычисление области управления пользователя.

RBAC в проекте двумерный, и это два независимых механизма:

    ЧТО делать  — права (User.can, PermissionChecker, RequirePermissionHtml)
    НАД ЧЕМ     — область управления (этот модуль)

Право отвечает на вопрос «может ли пользователь планировать вообще»,
область — «чьи работы он вправе планировать». Начальник бюро и начальник
отдела имеют ИДЕНТИЧНЫЙ набор прав (проверено по role_permissions обеих
БД); всё различие между ними создаёт исключительно область. Поэтому
ошибка здесь не будет поймана проверкой прав — она молча выдаст
начальнику бюро полномочия начальника отдела.

Область = поддерево подразделений от department_id АКТУАЛЬНОГО
назначения работника (employee_assignments). Считается на СЕГОДНЯ:
начальник, переведённый в другое бюро, теряет доступ к данным прежнего.
Ретроспективная область (на произвольную дату) сознательно не
реализована — параметр с датой протёк бы во все вызовы ради редкого
сценария.

Механизм обхода дерева — сборка в памяти, а НЕ рекурсивный CTE. Это
согласовано с build_composition_tree (app/web/routers/composition.py),
где принято то же решение: справочник отделов мал, а чистая функция
прозрачнее и тестируется без БД. Два разных способа ходить по одному
дереву в проекте заводить не нужно.

Роль суперпользователя обрабатывается ЗДЕСЬ, в get_scope, а не в
collect_subtree_ids: чистая функция обхода ничего не знает про
пользователей и права.
"""
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.assignments import is_current_assignment
from app.core.dependencies import get_current_user, get_db
from app.core.guards import require_user_html
from app.models.composition import Department
from app.models.employee import EmployeeAssignment
from app.models.user import User


@dataclass(frozen=True)
class Scope:
    """Область управления пользователя на текущий запрос.

    frozen=True и frozenset осознанно: область вычисляется один раз
    зависимостью и дальше только читается. Неизменяемость исключает
    случайную мутацию в глубине сервисного слоя — ошибку такого рода
    в проверках доступа искать тяжело, а последствия у неё серьёзные.

    Два состояния «всё разрешено» и «ничего не разрешено» различаются
    флагом unrestricted, а не размером множества:
        unrestricted=True             — админ, ограничений нет вовсе;
        unrestricted=False, ids=пусто — управлять нечем (нет назначения).
    """

    department_ids: frozenset[int]
    unrestricted: bool

    def can_manage(self, department_id: int) -> bool:
        """Входит ли подразделение в область управления.

        Единственная точка, где принимается решение о принадлежности.
        Вызывающий код не должен обращаться к department_ids напрямую —
        иначе обход для админа придётся дублировать в каждой проверке
        и однажды его забудут.
        """
        if self.unrestricted:
            return True
        return department_id in self.department_ids

    @property
    def is_empty(self) -> bool:
        """Управлять нечем: назначения нет и админом пользователь не является."""
        return not self.unrestricted and not self.department_ids


async def get_user_department_id(db: AsyncSession, user: User) -> Optional[int]:
    """department_id актуального назначения работника или None.

    None — законное состояние, а не ошибка, и означает «области нет»:
      * у учётной записи нет кадровой карточки (sys_admin, reader);
      * работник уволен, все назначения закрыты.
    Оба случая дают пустую область; исключение здесь бросать не за что.

    Актуальность — на сегодня. Граница valid_to ВКЛЮЧИТЕЛЬНАЯ: диапазон
    в EXCLUDE-ограничении создан как daterange(..., '[]'), поэтому в
    последний день назначение ещё действует.
    """
    # Технические учётки без работника до запроса не доходят: связь
    # employee загружена через lazy="joined" вместе с пользователем,
    # обращение к ней запроса не порождает.
    if user.employee is None:
        return None

    today = date.today()

    stmt = (
        select(EmployeeAssignment.department_id).where(
            EmployeeAssignment.employee_id == user.employee.id,
            # Правило актуальности (включительная граница valid_to) —
            # одно на проект, в app/core/assignments.py.
            is_current_assignment(today),
        )
        # Индекс idx_assignments_employee (employee_id, valid_from)
        # покрывает выборку целиком.
        .limit(1)
    )

    result = await db.execute(stmt)

    # first(), а не one_or_none(): единственность гарантирует
    # EXCLUDE-ограничение в БД. Если его когда-нибудь потеряют при
    # пересоздании таблицы, one_or_none() начал бы бросать исключение
    # на живом запросе — first() в том же случае вернёт одно из
    # назначений и позволит системе работать.
    return result.scalars().first()


def collect_subtree_ids(
    departments: list[Department],
    root_id: int,
) -> set[int]:
    """Все id подразделений поддерева, включая корень.

    Чистая функция: ни сессии, ни пользователя, ни прав. Тестируется
    списком объектов Department — так же, как build_composition_tree.

    Обход в ширину ВНИЗ по parent_id. Это зеркало подъёма к предкам в
    build_composition_tree: там от листа к корню, здесь от корня к
    листьям, справочник тот же.

    Корень включён в результат: начальник управляет и собственным
    подразделением, а не только дочерними.

    Несуществующий root_id (отдел удалён, висячая ссылка) даёт пустое
    множество, а не исключение — симметрично тому, как сборка дерева
    обрывает подъём на висячей ссылке.
    """
    # Индекс «родитель → дети». Один проход по справочнику; строится
    # заново на каждый вызов, потому что справочник живёт ровно один
    # запрос и кэшировать десяток строк смысла нет.
    children_by_parent: dict[int, list[int]] = {}
    for dept in departments:
        if dept.parent_id is not None:
            children_by_parent.setdefault(dept.parent_id, []).append(dept.id)

    # Корня нет в справочнике — управлять нечем.
    known_ids = {dept.id for dept in departments}
    if root_id not in known_ids:
        return set()

    # visited служит и результатом, и защитой от цикла в parent_id:
    # при A→B→A обход завершится, а не зациклится. То же назначение,
    # что у множества seen в build_composition_tree.
    visited: set[int] = set()
    queue: list[int] = [root_id]

    while queue:
        current = queue.pop()
        if current in visited:
            continue
        visited.add(current)
        queue.extend(children_by_parent.get(current, ()))

    return visited


async def _build_scope(db: AsyncSession, user: User) -> Scope:
    """Общее тело обеих зависимостей: API и HTML считают область одинаково.

    Вынесено, чтобы правило существовало в единственном экземпляре.
    Различие контуров — только в способе получить пользователя (401 JSON
    против редиректа на /login), а не в том, как считается область.
    """
    # Короткое замыкание для суперпользователя — по образцу User.can():
    # ни запроса к назначениям, ни чтения справочника отделов. Роль
    # admin обходит и права, и область.
    if user.is_superuser:
        return Scope(department_ids=frozenset(), unrestricted=True)

    root_id = await get_user_department_id(db, user)
    if root_id is None:
        return Scope(department_ids=frozenset(), unrestricted=False)

    # Справочник отделов читается целиком: он мал (десятки строк), а
    # обход поддерева всё равно требует связей parent_id по всей
    # структуре, не только под корнем.
    result = await db.execute(select(Department))
    departments = list(result.scalars().all())

    subtree = collect_subtree_ids(departments, root_id)
    return Scope(department_ids=frozenset(subtree), unrestricted=False)


async def get_scope(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> Scope:
    """Зависимость области для JSON-API. Гость получает 401.

    Объявляется точечно на тех роутах, где область реально нужна, а НЕ
    внутри get_optional_user: тот вызывается на каждом запросе, и два
    лишних обращения к БД платили бы все роуты, включая безразличные
    к области.
    """
    return await _build_scope(db, user)


async def get_scope_html(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user_html)],
) -> Scope:
    """Зависимость области для HTML-страниц. Гость уходит на /login.

    Отличается от get_scope ТОЛЬКО источником пользователя. Та же
    двойственность, что между PermissionChecker и RequirePermissionHtml:
    контуры по-разному обрабатывают неавторизованного, но правило,
    которое они проверяют, одно.
    """
    return await _build_scope(db, user)