# app/api/v1/admin/service.py
"""Сервисный слой администратора данных: «ремонтный режим» плана и факта.

Здесь только работа с БД. Что можно сделать с ячейкой и что попадёт в
журнал — в admin/rules.py (чистый модуль, проверяется в консоли).
Снимок данных и запись плана переиспользуются из планирования: тот же
AvailabilityContext по одному сотруднику и та же rebuild_plan, что
пересчитывает work_plan, — администратор пишет план ТЕМ ЖЕ кодом, что
начальник, а не своей копией.

Четыре сценария:
  * list_employees      — список имён для выбора сотрудника;
  * load_admin_grid     — сетка одного сотрудника на интервал;
  * apply_admin_changes — пачка правок плана и факта + запись в журнал;
  * list_changesets     — журнал правок сотрудника.

Область управления (Scope) здесь не используется: администратор видит
всех сотрудников организации, граница доступа — только право
(plan:override, fact:override), проверяемое в routes.py.

Тексты для пользователя здесь не собираются: исключения несут причину
и данные, формулировки — в routes.py.
"""

import logging
from collections.abc import Iterable
from datetime import date, timedelta
from typing import Optional, cast

from sqlalchemy import delete, func, insert, or_, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin.rules import (
    AdminRejection,
    CellChange,
    DesiredCell,
    Layer,
    admin_cell_rejection,
    compute_diff,
    current_cells,
    value_rejection,
)
from app.api.v1.admin.schemas import (
    AdminApplyResult,
    AdminCellIn,
    AdminDayOut,
    AdminGrid,
    ChangeItemOut,
    ChangesetOut,
    EmployeeOption,
)
from app.api.v1.fact.service import (
    FACT_LOCK_NAMESPACE,
    SQLSTATE_DAY_CLOSED,
    SQLSTATE_UNIQUE,
)
from app.api.v1.planning.rules import AvailabilityContext
from app.api.v1.planning.service import (
    grid_rows_and_cells,
    load_context,
    load_set_status_names,
    rebuild_plan,
    resolve_employees_by_ids,
)
from app.models.admin import AdminChangeItem, AdminChangeset
from app.models.composition import Department, ProjectMarkSet
from app.models.employee import Employee, EmployeeAssignment
from app.models.planning import FactCalendarCell, PlanCalendarCell
from app.models.user import User

logger = logging.getLogger(__name__)

# Предел интервала сетки — как у сетки начальника: сетка подгружается
# окнами, запрос на весь горизонт вернул бы тысячи элементов.
MAX_GRID_DAYS: int = 120

# Сколько последних правок показывать в журнале сотрудника.
CHANGESETS_LIMIT: int = 50

# 23503 — foreign_key_violation: шифр удалили между проверкой и записью.
SQLSTATE_FOREIGN_KEY: str = "23503"


# ---------------------------------------------------------------------------
# Доменные исключения. Роут переводит их в HTTP-коды и собирает тексты.
# ---------------------------------------------------------------------------


class AdminError(Exception):
    """Базовое исключение сервиса администратора."""


class EmployeeNotFoundError(AdminError):
    """Сотрудника с таким id нет."""


class InvalidIntervalError(AdminError):
    """Интервал сетки бессмыслен или шире MAX_GRID_DAYS."""


class EmptyCommentError(AdminError):
    """Комментарий пуст после обрезки пробелов."""


class MarkSetsNotFoundError(AdminError):
    """В правке есть шифры, которых нет в базе."""

    def __init__(self, missing_ids: list[int]) -> None:
        super().__init__(", ".join(str(i) for i in missing_ids))
        self.missing_ids = missing_ids


class AdminRejectedError(AdminError):
    """Часть изменений нарушает целостность. Не записано ничего."""

    def __init__(self, rejections: list[AdminRejection]) -> None:
        super().__init__(f"{len(rejections)} отказ(ов)")
        self.rejections = rejections


class ConcurrentChangeError(AdminError):
    """Данные сотрудника изменились параллельно — нужно перечитать сетку."""


# ---------------------------------------------------------------------------
# Сценарий 1. Список имён для выбора сотрудника
# ---------------------------------------------------------------------------


async def list_employees(db: AsyncSession, *, today: date) -> list[EmployeeOption]:
    """Все сотрудники с действующим на сегодня назначением, по ФИО.

    Только имена и код подразделения — ни шифров, ни ячеек. Полный
    список, без поиска на сервере: людей десятки–сотни, фильтр по вводу
    справляется на фронте.

    Если у человека на сегодня два назначения (пересечение при
    переводе), берётся первое по порядку — для списка выбора важно имя,
    а не точное бюро.
    """
    stmt = (
        select(Employee.id, Employee.full_name, Department.code)
        .join(EmployeeAssignment, EmployeeAssignment.employee_id == Employee.id)
        .join(Department, Department.id == EmployeeAssignment.department_id)
        .where(
            EmployeeAssignment.valid_from <= today,
            or_(
                EmployeeAssignment.valid_to.is_(None),
                EmployeeAssignment.valid_to >= today,
            ),
        )
        .order_by(Employee.full_name, Employee.id)
    )
    options: dict[int, EmployeeOption] = {}
    for emp_id, full_name, code in (await db.execute(stmt)).all():
        options.setdefault(
            emp_id,
            EmployeeOption(id=emp_id, full_name=full_name, department_code=code),
        )
    return list(options.values())


# ---------------------------------------------------------------------------
# Общая загрузка
# ---------------------------------------------------------------------------


async def _employee_name(db: AsyncSession, employee_id: int) -> str:
    """ФИО сотрудника или EmployeeNotFoundError."""
    name = (
        await db.execute(select(Employee.full_name).where(Employee.id == employee_id))
    ).scalar_one_or_none()
    if name is None:
        raise EmployeeNotFoundError(str(employee_id))
    return name


async def _load_employee_context(
    db: AsyncSession,
    employee_id: int,
    date_from: date,
    date_to: date,
) -> AvailabilityContext:
    """Снимок по одному сотруднику — тот же, что у начальника и инженера.

    Если у сотрудника нет назначения, пересекающего интервал, employees
    пуст: все дни получат «вне штата», а очистка ячеек останется
    доступной — удалить ошибочные данные можно всегда.
    """
    employees = await resolve_employees_by_ids(db, [employee_id], date_from, date_to)
    return await load_context(
        db, employees=employees, date_from=date_from, date_to=date_to
    )


# ---------------------------------------------------------------------------
# Сценарий 2. Сетка сотрудника
# ---------------------------------------------------------------------------


def _collect_admin_days(
    ctx: AvailabilityContext,
    employee_id: int,
    today: date,
) -> list[AdminDayOut]:
    """Флаги «план можно / факт можно» на каждый день интервала.

    Считает value_rejection — правила записаны в одном месте, фронт их
    не вычисляет.
    """
    days: list[AdminDayOut] = []
    day = ctx.date_from
    while day <= ctx.date_to:
        plan_no = value_rejection(ctx, employee_id, "plan", day, today)
        fact_no = value_rejection(ctx, employee_id, "fact", day, today)
        absence = ctx.absences.get((employee_id, day))
        info = ctx.calendar.get(day)
        days.append(
            AdminDayOut(
                work_date=day,
                plan_allowed=plan_no is None,
                fact_allowed=fact_no is None,
                plan_reason=plan_no.reason if plan_no else None,
                fact_reason=fact_no.reason if fact_no else None,
                tab_letter=absence.tab_letter if absence else None,
                day_type_code=info.day_type_code if info else None,
            )
        )
        day += timedelta(days=1)
    return days


async def load_admin_grid(
    db: AsyncSession,
    *,
    employee_id: int,
    date_from: date,
    date_to: date,
    today: date,
) -> AdminGrid:
    """Сетка одного сотрудника: строки-шифры, флаги дней, клетки плана/факта.

    Строки и клетки собирает планирование (grid_rows_and_cells) — у
    одного сотрудника они дают обе полосы. Слой дней — свой.
    """
    if date_from > date_to:
        raise InvalidIntervalError("Начало интервала позже конца")
    if (date_to - date_from).days + 1 > MAX_GRID_DAYS:
        raise InvalidIntervalError(f"Интервал шире {MAX_GRID_DAYS} дней")

    full_name = await _employee_name(db, employee_id)
    ctx = await _load_employee_context(db, employee_id, date_from, date_to)
    status_names = await load_set_status_names(db)
    rows, cells = grid_rows_and_cells(ctx, status_names)

    return AdminGrid(
        employee_id=employee_id,
        full_name=full_name,
        date_from=date_from,
        date_to=date_to,
        today=today,
        horizon_from=ctx.horizon_from,
        horizon_to=ctx.horizon_to,
        rows=rows,
        days=_collect_admin_days(ctx, employee_id, today),
        cells=cells,
    )


# ---------------------------------------------------------------------------
# Сценарий 3. Пачка правок + журнал
# ---------------------------------------------------------------------------


def _sqlstate(exc: DBAPIError) -> Optional[str]:
    """SQLSTATE исходной ошибки драйвера (как в fact/service.py).

    Адаптер asyncpg в SQLAlchemy кладёт код в .sqlstate (и дублирует в
    .pgcode); на случай другой версии смотрим и исходное исключение.
    """
    for source in (exc.orig, getattr(exc.orig, "__cause__", None)):
        code = getattr(source, "sqlstate", None) or getattr(source, "pgcode", None)
        if code:
            return str(code)
    return None


def _snapshot_old(
    ctx: AvailabilityContext,
    employee_id: int,
) -> dict[tuple[str, date], str]:
    """Текстовые снимки ТЕКУЩИХ шифров ячеек — для «было» в журнале."""
    snapshot: dict[tuple[str, date], str] = {}
    for (emp_id, day), plan in ctx.plan_cells.items():
        if emp_id == employee_id:
            snapshot[("plan", day)] = plan.marka_full
    for (emp_id, day), fact in ctx.fact_cells.items():
        if emp_id == employee_id:
            snapshot[("fact", day)] = fact.marka_full
    return snapshot


async def _load_markas(db: AsyncSession, mark_set_ids: set[int]) -> dict[int, str]:
    """Шифры по id: id → marka_full. Отсутствующие в базе в словарь не входят."""
    if not mark_set_ids:
        return {}
    rows = await db.execute(
        select(ProjectMarkSet.id, ProjectMarkSet.marka_full).where(
            ProjectMarkSet.id.in_(mark_set_ids)
        )
    )
    return {mark_set_id: marka for mark_set_id, marka in rows.all()}


async def _write_plan(
    db: AsyncSession,
    ctx: AvailabilityContext,
    employee_id: int,
    changes: list[CellChange],
) -> None:
    """Запись плановых изменений — тем же порядком, что apply_changes.

    1. Сначала ВСЕ удаления по всем затронутым планам: иначе перенос дня
       с шифра на шифр упрётся в unique_plan_cell.
    2. Затем rebuild_plan по каждому затронутому плану: создаёт шапку,
       если плана не было, вставляет ячейки и пересчитывает начало,
       конец и число дней; пустой план удаляет.
    """
    removed: dict[int, list[date]] = {}
    added: dict[int, list[date]] = {}
    for change in changes:
        if change.old_mark_set_id is not None:
            removed.setdefault(change.old_mark_set_id, []).append(change.work_date)
        if change.new_mark_set_id is not None:
            added.setdefault(change.new_mark_set_id, []).append(change.work_date)

    for mark_set_id, work_dates in removed.items():
        plan = ctx.work_plans.get((mark_set_id, employee_id))
        if plan is None:
            # Ячейка была в снимке, значит, план был. Нет — данные
            # разошлись; вставка ниже упрётся в unique_plan_cell, и
            # правка откатится как параллельное изменение.
            continue
        await db.execute(
            delete(PlanCalendarCell).where(
                PlanCalendarCell.work_plan_id == plan.id,
                PlanCalendarCell.work_date.in_(work_dates),
            )
        )

    for mark_set_id in set(removed) | set(added):
        plan = ctx.work_plans.get((mark_set_id, employee_id))
        await rebuild_plan(
            db,
            mark_set_id=mark_set_id,
            employee_id=employee_id,
            plan_id=plan.id if plan else None,
            added=added.get(mark_set_id, []),
        )


async def _write_fact(
    db: AsyncSession,
    employee_id: int,
    changes: list[CellChange],
) -> None:
    """Запись изменений факта.

    «пусто → Б» — INSERT; «А → Б» — UPDATE (не DELETE + INSERT: иначе
    каскадом пропали бы связи в fact_kanban_cells); «А → пусто» — DELETE.
    is_retro не передаётся: его вычисляет триггер trg_fact_check_absence.
    """
    for change in changes:
        where = (
            FactCalendarCell.employee_id == employee_id,
            FactCalendarCell.work_date == change.work_date,
        )
        if change.old_mark_set_id is None:
            await db.execute(
                insert(FactCalendarCell).values(
                    mark_set_id=change.new_mark_set_id,
                    employee_id=employee_id,
                    work_date=change.work_date,
                )
            )
        elif change.new_mark_set_id is None:
            await db.execute(delete(FactCalendarCell).where(*where))
        else:
            await db.execute(
                update(FactCalendarCell)
                .where(*where)
                .values(mark_set_id=change.new_mark_set_id)
            )


async def apply_admin_changes(
    db: AsyncSession,
    *,
    employee_id: int,
    changed_by: int,
    comment: str,
    cells: Iterable[AdminCellIn],
    today: date,
) -> AdminApplyResult:
    """Применяет пачку правок одного сотрудника и пишет её в журнал.

    Одна транзакция — всё или ничего:

    1. Advisory-блокировка по сотруднику ПЕРВЫМ действием — те же ключи
       берут триггеры факта и неявки, так что ни отметка инженера, ни
       неявка табельщицы не вклинятся между проверкой и записью.
    2. Снимок базы → diff (compute_diff). «Было» берётся из базы, а не
       из запроса: подменить его браузер не может.
    3. Пустой diff — ничего не пишем, правки в журнале нет.
    4. Проверка: шифры существуют, каждое изменение не нарушает
       целостность. Хоть один отказ — не пишем ничего.
    5. План (rebuild_plan), факт, журнал — и commit.
    """
    text = comment.strip()
    if not text:
        raise EmptyCommentError()

    desired = [
        DesiredCell(layer=c.layer, work_date=c.work_date, mark_set_id=c.mark_set_id)
        for c in cells
    ]
    if not desired:
        return AdminApplyResult(changeset_id=None, applied=0)

    await db.execute(
        select(func.pg_advisory_xact_lock(FACT_LOCK_NAMESPACE, employee_id))
    )
    await _employee_name(db, employee_id)

    dates = [c.work_date for c in desired]
    ctx = await _load_employee_context(db, employee_id, min(dates), max(dates))

    # DuplicateCellError из compute_diff пролетает наверх — роут → 422.
    diff = compute_diff(current_cells(ctx, employee_id), desired)
    if not diff:
        await db.rollback()  # отпускает блокировку
        return AdminApplyResult(changeset_id=None, applied=0)

    new_ids = {c.new_mark_set_id for c in diff if c.new_mark_set_id is not None}
    new_markas = await _load_markas(db, new_ids)
    missing = sorted(new_ids - set(new_markas))
    if missing:
        await db.rollback()
        raise MarkSetsNotFoundError(missing)

    rejections = [
        rejection
        for change in diff
        if (rejection := admin_cell_rejection(ctx, employee_id, change, today))
        is not None
    ]
    if rejections:
        await db.rollback()
        raise AdminRejectedError(rejections)

    old_markas = _snapshot_old(ctx, employee_id)
    plan_changes = [c for c in diff if c.layer == "plan"]
    fact_changes = [c for c in diff if c.layer == "fact"]

    try:
        await _write_plan(db, ctx, employee_id, plan_changes)
        await _write_fact(db, employee_id, fact_changes)

        changeset_id = (
            await db.execute(
                insert(AdminChangeset)
                .values(employee_id=employee_id, changed_by=changed_by, comment=text)
                .returning(AdminChangeset.id)
            )
        ).scalar_one()

        await db.execute(
            insert(AdminChangeItem),
            [
                {
                    "changeset_id": changeset_id,
                    "layer": change.layer,
                    "work_date": change.work_date,
                    "old_mark_set_id": change.old_mark_set_id,
                    "new_mark_set_id": change.new_mark_set_id,
                    "old_marka": old_markas.get((change.layer, change.work_date)),
                    "new_marka": (
                        new_markas[change.new_mark_set_id]
                        if change.new_mark_set_id is not None
                        else None
                    ),
                }
                for change in diff
            ],
        )
        await db.commit()

    except DBAPIError as exc:
        # Транзакция в аварийном состоянии: сначала откат, разбор после.
        await db.rollback()
        code = _sqlstate(exc)
        if code in (SQLSTATE_DAY_CLOSED, SQLSTATE_UNIQUE, SQLSTATE_FOREIGN_KEY):
            # Неявку внесли, шифр удалили или ячейку заняли мимо
            # блокировки — картина устарела, сетку нужно перечитать.
            logger.info("Отказ БД при правке администратора: %s", code)
            raise ConcurrentChangeError(code) from exc
        raise

    return AdminApplyResult(changeset_id=changeset_id, applied=len(diff))


# ---------------------------------------------------------------------------
# Сценарий 4. Журнал правок сотрудника
# ---------------------------------------------------------------------------


async def list_changesets(
    db: AsyncSession,
    *,
    employee_id: int,
    limit: int = CHANGESETS_LIMIT,
) -> list[ChangesetOut]:
    """Последние правки сотрудника, свежие сверху, каждая со своим diff.

    Два запроса: правки (с логином автора) и все их ячейки разом — без
    запроса на каждую правку.
    """
    await _employee_name(db, employee_id)

    head_rows = (
        await db.execute(
            select(
                AdminChangeset.id,
                AdminChangeset.created_at,
                User.username,
                AdminChangeset.comment,
            )
            .join(User, User.id == AdminChangeset.changed_by)
            .where(AdminChangeset.employee_id == employee_id)
            .order_by(AdminChangeset.created_at.desc(), AdminChangeset.id.desc())
            .limit(limit)
        )
    ).all()
    if not head_rows:
        return []

    ids = [row[0] for row in head_rows]
    items: dict[int, list[ChangeItemOut]] = {cs_id: [] for cs_id in ids}
    item_rows = await db.execute(
        select(
            AdminChangeItem.changeset_id,
            AdminChangeItem.layer,
            AdminChangeItem.work_date,
            AdminChangeItem.old_marka,
            AdminChangeItem.new_marka,
        ).where(AdminChangeItem.changeset_id.in_(ids))
        # По дню, план раньше факта — как в compute_diff ('plan' > 'fact'
        # по алфавиту, поэтому DESC ставит план первым).
        .order_by(
            AdminChangeItem.changeset_id,
            AdminChangeItem.work_date,
            AdminChangeItem.layer.desc(),
        )
    )
    for cs_id, layer, work_date, old_marka, new_marka in item_rows.all():
        items[cs_id].append(
            ChangeItemOut(
                # CHECK admin_change_items_layer_check гарантирует, что в
                # колонке только 'plan' и 'fact'.
                layer=cast(Layer, layer),
                work_date=work_date,
                old_marka=old_marka,
                new_marka=new_marka,
            )
        )

    return [
        ChangesetOut(
            id=cs_id,
            created_at=created_at,
            changed_by=username,
            comment=comment,
            items=items[cs_id],
        )
        for cs_id, created_at, username, comment in head_rows
    ]