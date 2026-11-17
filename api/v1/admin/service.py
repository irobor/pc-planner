# app/api/v1/admin/service.py
"""Сервисный слой администратора данных: «ремонтный режим» плана и факта.

Здесь только работа с БД. Что можно сделать с ячейкой и что попадёт в
журнал — в admin/rules.py (чистый модуль, проверяется в консоли).
Снимок данных и запись плана переиспользуются из планирования: тот же
AvailabilityContext по одному сотруднику и та же add_plan_cells, что у
начальника, — администратор пишет план ТЕМ ЖЕ кодом, а не своей копией.
Значение ячейки — пакет работ.

Пять сценариев:
  * list_employees      — список имён для выбора сотрудника;
  * load_admin_grid     — сетка одного сотрудника на интервал;
  * apply_admin_changes — пачка правок плана и факта + запись в журнал;
  * list_changesets     — журнал правок сотрудника;
  * load_summary_report — сводный отчёт «проекты × бюро» за период.

Область управления (Scope) здесь не используется: администратор видит
всех сотрудников организации, граница доступа — только право
(plan:override, fact:override), проверяемое в routes.py.

Тексты для пользователя здесь не собираются: исключения несут причину
и данные, формулировки — в routes.py.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from typing import Optional, cast

from sqlalchemy import and_, delete, func, insert, or_, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

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
from app.api.v1.planning.rules import AvailabilityContext, package_label
from app.api.v1.planning.service import (
    add_plan_cells,
    grid_rows_and_cells,
    load_context,
    load_package_status_names,
    resolve_employees_by_ids,
)
from app.core.assignments import is_current_assignment
from app.models.admin import AdminChangeItem, AdminChangeset
from app.models.composition import (
    Department,
    ProjectComposition,
    ProjectMarkSet,
    WorkPackage,
)
from app.models.employee import Employee, EmployeeAssignment
from app.models.planning import (
    AbsencePeriod,
    AbsenceType,
    DayType,
    FactCalendarCell,
    PlanCalendarCell,
    ProductionCalendar,
)
from app.models.project import Enterprise, Project
from app.models.user import User
from app.models.views import VProjectRegistry

logger = logging.getLogger(__name__)

# Предел интервала сетки — как у сетки начальника: сетка подгружается
# окнами, запрос на весь горизонт вернул бы тысячи элементов.
MAX_GRID_DAYS: int = 120

# Сколько последних правок показывать в журнале сотрудника.
CHANGESETS_LIMIT: int = 50

# 23503 — foreign_key_violation: пакет удалили между проверкой и записью.
SQLSTATE_FOREIGN_KEY: str = "23503"


# ---------------------------------------------------------------------------
# Доменные исключения. Роут переводит их в HTTP-коды и собирает тексты.
# ---------------------------------------------------------------------------


class AdminError(Exception):
    """Базовое исключение сервиса администратора."""


class EmployeeNotFoundError(AdminError):
    """Сотрудника с таким id нет."""


class InvalidIntervalError(AdminError):
    """Интервал сетки или отчёта бессмыслен; у сетки — шире MAX_GRID_DAYS."""


class EmptyCommentError(AdminError):
    """Комментарий пуст после обрезки пробелов."""


class PackagesNotFoundError(AdminError):
    """В правке есть пакеты, которых нет в базе."""

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

    Только имена и код подразделения — ни пакетов, ни ячеек. Полный
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
    """Сетка одного сотрудника: строки-пакеты, флаги дней, клетки плана/факта.

    Строки и клетки собирает планирование (grid_rows_and_cells) — у
    одного сотрудника они дают обе полосы. Слой дней — свой.
    """
    if date_from > date_to:
        raise InvalidIntervalError("Начало интервала позже конца")
    if (date_to - date_from).days + 1 > MAX_GRID_DAYS:
        raise InvalidIntervalError(f"Интервал шире {MAX_GRID_DAYS} дней")

    full_name = await _employee_name(db, employee_id)
    ctx = await _load_employee_context(db, employee_id, date_from, date_to)
    status_names = await load_package_status_names(db)
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
    """Подписи ТЕКУЩИХ пакетов ячеек — для «было» в журнале.

    Подпись полная, «шифр (пакет)», а не один шифр: CHECK журнала
    admin_change_items_is_change отвергает строку, где «было» и «стало»
    совпадают, и перенос дня между пакетами одного шифра без названия
    пакета выглядел бы как «ничего не изменилось».
    """
    snapshot: dict[tuple[str, date], str] = {}
    for (emp_id, day), plan in ctx.plan_cells.items():
        if emp_id == employee_id:
            snapshot[("plan", day)] = plan.label
    for (emp_id, day), fact in ctx.fact_cells.items():
        if emp_id == employee_id:
            snapshot[("fact", day)] = fact.label
    return snapshot


async def _load_labels(db: AsyncSession, package_ids: set[int]) -> dict[int, str]:
    """Подписи пакетов по id. Отсутствующие в базе в словарь не входят."""
    if not package_ids:
        return {}
    rows = await db.execute(
        select(WorkPackage.id, ProjectMarkSet.marka_full, WorkPackage.name)
        .join(ProjectMarkSet, ProjectMarkSet.id == WorkPackage.mark_set_id)
        .where(WorkPackage.id.in_(package_ids))
    )
    return {
        package_id: package_label(marka_full, name)
        for package_id, marka_full, name in rows.all()
    }


async def _write_plan(
    db: AsyncSession,
    employee_id: int,
    changes: list[CellChange],
) -> None:
    """Запись плановых изменений — тем же порядком, что apply_changes.

    1. Сначала ВСЕ удаления: иначе перенос дня с пакета на пакет упрётся
       в unique_plan_cell. Удаляется по тройке «пакет + сотрудник +
       даты» — ячейка однозначна.
    2. Затем вставки — add_plan_cells планирования, тем же кодом, каким
       план пишет начальник. Шапки плана нет, пересчитывать нечего.
    """
    removed: dict[int, list[date]] = {}
    added: dict[int, list[date]] = {}
    for change in changes:
        if change.old_package_id is not None:
            removed.setdefault(change.old_package_id, []).append(change.work_date)
        if change.new_package_id is not None:
            added.setdefault(change.new_package_id, []).append(change.work_date)

    for package_id, work_dates in removed.items():
        await db.execute(
            delete(PlanCalendarCell).where(
                PlanCalendarCell.package_id == package_id,
                PlanCalendarCell.employee_id == employee_id,
                PlanCalendarCell.work_date.in_(work_dates),
            )
        )

    for package_id, work_dates in added.items():
        await add_plan_cells(
            db,
            package_id=package_id,
            employee_id=employee_id,
            work_dates=work_dates,
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
        if change.old_package_id is None:
            await db.execute(
                insert(FactCalendarCell).values(
                    package_id=change.new_package_id,
                    employee_id=employee_id,
                    work_date=change.work_date,
                )
            )
        elif change.new_package_id is None:
            await db.execute(delete(FactCalendarCell).where(*where))
        else:
            await db.execute(
                update(FactCalendarCell)
                .where(*where)
                .values(package_id=change.new_package_id)
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
    4. Проверка: пакеты существуют, каждое изменение не нарушает
       целостность. Хоть один отказ — не пишем ничего.
    5. План (add_plan_cells), факт, журнал — и commit.
    """
    text = comment.strip()
    if not text:
        raise EmptyCommentError()

    desired = [
        DesiredCell(layer=c.layer, work_date=c.work_date, package_id=c.package_id)
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

    new_ids = {c.new_package_id for c in diff if c.new_package_id is not None}
    new_labels = await _load_labels(db, new_ids)
    missing = sorted(new_ids - set(new_labels))
    if missing:
        await db.rollback()
        raise PackagesNotFoundError(missing)

    rejections = [
        rejection
        for change in diff
        if (rejection := admin_cell_rejection(ctx, employee_id, change, today))
        is not None
    ]
    if rejections:
        await db.rollback()
        raise AdminRejectedError(rejections)

    old_labels = _snapshot_old(ctx, employee_id)
    plan_changes = [c for c in diff if c.layer == "plan"]
    fact_changes = [c for c in diff if c.layer == "fact"]

    try:
        await _write_plan(db, employee_id, plan_changes)
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
                    "old_package_id": change.old_package_id,
                    "new_package_id": change.new_package_id,
                    # Колонки снимков называются по-старому (old_marka),
                    # лежит в них подпись «шифр (пакет)».
                    "old_marka": old_labels.get((change.layer, change.work_date)),
                    "new_marka": (
                        new_labels[change.new_package_id]
                        if change.new_package_id is not None
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
            # Неявку внесли, пакет удалили или ячейку заняли мимо
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
                # Колонки базы — old_marka / new_marka, поля ответа —
                # old_label / new_label: в снимке лежит подпись пакета.
                old_label=old_marka,
                new_label=new_marka,
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


# ---------------------------------------------------------------------------
# Сценарий 5. Сводный отчёт «проекты × бюро»
#
# Строка — проект (или вид неявки), колонка — бюро, в ячейке — дни факта
# или плана за период. День идёт в колонку бюро, где инженер числился В
# ЭТОТ ДЕНЬ, — чей это шифр, значения не имеет. Отчёт за прошлый период
# не меняется задним числом после перевода сотрудника.
#
# Бюро — узел третьего уровня дерева (ПЦ → отдел → бюро). Начальники
# отделов, ГИП и начальник ПЦ числятся выше, в плане и факте не
# участвуют и в отчёт не попадают — отдельного фильтра по ним нет, их
# отсекает условие «назначение — в бюро».
#
# Двойного счёта нет на уровне БД: EXCLUDE-ограничения дают сотруднику
# на день ровно одно назначение и не больше одной неявки.
# ---------------------------------------------------------------------------


class ReportMode(str, Enum):
    """Что суммирует отчёт."""

    FACT = "fact"
    PLAN = "plan"


# Виды неявок — строки отчёта, в этом порядке; показываются всегда, даже
# нулевые. Отбор по коду, а не по флагу blocks_planning: «Выходной по
# индивидуальному графику» блокирует планирование, но в отчёт не входит.
# Код — стабильная опора для логики (комментарий к absence_types.code),
# подпись строки берётся из справочника.
REPORT_ABSENCE_CODES: tuple[str, ...] = (
    "SICK",
    "VACATION_MAIN",
    "MATERNITY",
    "CHILDCARE",
    "STUDY",
    "UNPAID",
    "BUSINESS_TRIP",
)

# Условность отчёта: человеко-часы = дни × 8, короткие предпраздничные
# дни не учитываются.
HOURS_PER_DAY: int = 8


@dataclass(frozen=True)
class SummaryBureau:
    """Колонка отчёта: бюро и отдел, в который оно входит."""

    id: int
    code: str
    name: str
    department_code: str
    department_name: str


@dataclass(frozen=True)
class SummaryRow:
    """Строка отчёта: проект, вид неявки или итог.

    days — id бюро → дни. Бюро без дней в словаре нет: нули рисует
    шаблон. У неявки и итога нет номера проекта и заказчика.
    """

    title: str
    days: dict[int, int]
    code: str | None = None
    customer: str | None = None

    @property
    def total(self) -> int:
        return sum(self.days.values())

    @property
    def hours(self) -> int:
        return self.total * HOURS_PER_DAY


@dataclass(frozen=True)
class SummaryReport:
    """Сводный отчёт за период — всё, что нужно шаблону."""

    mode: ReportMode
    date_from: date
    date_to: date
    bureaus: list[SummaryBureau]
    projects: list[SummaryRow]
    absences: list[SummaryRow]
    projects_total: SummaryRow
    absences_total: SummaryRow
    grand_total: SummaryRow

    @property
    def departments(self) -> list[tuple[str, str, int]]:
        """Отделы над колонками бюро: код, название, сколько бюро под ним.

        Порядок — порядок колонок: bureaus уже отсортированы по отделу.
        Jinja-фильтр groupby здесь не годится — он пересортировал бы
        отделы по алфавиту, и шапка разошлась бы с колонками.
        """
        groups: list[tuple[str, str, int]] = []
        for b in self.bureaus:
            if groups and groups[-1][0] == b.department_code:
                code, name, span = groups[-1]
                groups[-1] = (code, name, span + 1)
            else:
                groups.append((b.department_code, b.department_name, 1))
        return groups

    
def _sum_rows(title: str, rows: Iterable[SummaryRow]) -> SummaryRow:
    """Итоговая строка: дни по каждому бюро сложены по всем строкам."""
    days: dict[int, int] = {}
    for row in rows:
        for bureau_id, count in row.days.items():
            days[bureau_id] = days.get(bureau_id, 0) + count
    return SummaryRow(title=title, days=days)


async def _load_bureaus(db: AsyncSession) -> list[SummaryBureau]:
    """Все бюро организации в порядке дерева: по отделу, затем по коду.

    Уровень считается от корня (parent_id IS NULL): бюро — внук корня.
    Если в бюро появятся группы, они уровнем ниже и колонками не станут.
    """
    division = aliased(Department)
    root = aliased(Department)
    stmt = (
        select(
            Department.id,
            Department.code,
            Department.name,
            division.code,
            division.name,
        )
        .join(division, division.id == Department.parent_id)
        .join(root, root.id == division.parent_id)
        .where(root.parent_id.is_(None))
        .order_by(division.code, Department.code)
    )
    rows = (await db.execute(stmt)).all()
    return [
        SummaryBureau(
            id=b_id,
            code=code,
            name=name,
            department_code=div_code,
            department_name=div_name,
        )
        for b_id, code, name, div_code, div_name in rows
    ]


async def _load_project_rows(
    db: AsyncSession,
    mode: ReportMode,
    date_from: date,
    date_to: date,
    bureau_ids: list[int],
) -> list[SummaryRow]:
    """Дни плана или факта: проект × бюро инженера на дату ячейки.

    Цепочка до проекта: ячейка → пакет → комплект → строка состава →
    проект. Заказчик — готовая строка Flask из project_registry_view
    («"Каустик" АО, цех 24, корпус 5»), чтобы отчёт показывал его так же,
    как Flask. Проекта вне реестра в представлении нет — тогда заказчик
    просто предприятие.
    """
    cells: type[FactCalendarCell] | type[PlanCalendarCell] = (
        FactCalendarCell if mode is ReportMode.FACT else PlanCalendarCell
    )
    customer = func.coalesce(VProjectRegistry.customer_display_name, Enterprise.name)
    stmt = (
        select(
            Project.id,
            Project.code,
            customer,
            Project.title,
            EmployeeAssignment.department_id,
            func.count(),
        )
        .select_from(cells)
        .join(WorkPackage, WorkPackage.id == cells.package_id)
        .join(ProjectMarkSet, ProjectMarkSet.id == WorkPackage.mark_set_id)
        .join(
            ProjectComposition,
            ProjectComposition.id == ProjectMarkSet.composition_id,
        )
        .join(Project, Project.id == ProjectComposition.project_id)
        .join(Enterprise, Enterprise.id == Project.enterprise_id)
        .outerjoin(VProjectRegistry, VProjectRegistry.project_id == Project.id)
        # Бюро — по назначению на дату ячейки, а не на сегодня.
        .join(
            EmployeeAssignment,
            and_(
                EmployeeAssignment.employee_id == cells.employee_id,
                is_current_assignment(cells.work_date),
            ),
        )
        .where(
            cells.work_date.between(date_from, date_to),
            EmployeeAssignment.department_id.in_(bureau_ids),
        )
        .group_by(
            Project.id,
            VProjectRegistry.customer_display_name,
            Enterprise.name,
            EmployeeAssignment.department_id,
        )
        .order_by(Project.code)
    )
    # Строки одного проекта идут подряд (сортировка по коду), словарь
    # сохраняет этот порядок.
    by_project: dict[int, SummaryRow] = {}
    for project_id, code, cust, title, bureau_id, count in await db.execute(stmt):
        row = by_project.get(project_id)
        if row is None:
            row = SummaryRow(title=title, days={}, code=code, customer=cust)
            by_project[project_id] = row
        row.days[bureau_id] = count
    return list(by_project.values())


async def _load_absence_rows(
    db: AsyncSession,
    date_from: date,
    date_to: date,
    bureau_ids: list[int],
) -> list[SummaryRow]:
    """Неявки из табеля: вид неявки × бюро, только рабочие дни.

    Неявка в табеле — период вместе с выходными. Считаются только дни
    периода отчёта, рабочие по производственному календарю: иначе 14
    дней отпуска весили бы больше 10 рабочих дней факта. Бюро — по
    назначению на каждый такой день.
    """
    type_rows = (
        await db.execute(
            select(AbsenceType.id, AbsenceType.code, AbsenceType.name).where(
                AbsenceType.code.in_(REPORT_ABSENCE_CODES)
            )
        )
    ).all()
    # Строки — в порядке REPORT_ABSENCE_CODES, а не в порядке выборки.
    order = {code: index for index, code in enumerate(REPORT_ABSENCE_CODES)}
    types = sorted(type_rows, key=lambda r: order[r.code])
    rows = {type_id: SummaryRow(title=name, days={}) for type_id, _, name in types}

    day = ProductionCalendar.work_date
    stmt = (
        select(
            AbsencePeriod.absence_type_id,
            EmployeeAssignment.department_id,
            func.count(),
        )
        .select_from(AbsencePeriod)
        .join(
            ProductionCalendar,
            day.between(AbsencePeriod.date_from, AbsencePeriod.date_to),
        )
        .join(
            DayType,
            and_(DayType.id == ProductionCalendar.day_type_id, DayType.is_working),
        )
        .join(
            EmployeeAssignment,
            and_(
                EmployeeAssignment.employee_id == AbsencePeriod.employee_id,
                is_current_assignment(day),
            ),
        )
        .where(
            AbsencePeriod.absence_type_id.in_(rows.keys()),
            day.between(date_from, date_to),
            EmployeeAssignment.department_id.in_(bureau_ids),
        )
        .group_by(AbsencePeriod.absence_type_id, EmployeeAssignment.department_id)
    )
    for type_id, bureau_id, count in await db.execute(stmt):
        rows[type_id].days[bureau_id] = count
    return list(rows.values())


async def load_summary_report(
    db: AsyncSession,
    *,
    mode: ReportMode,
    date_from: date,
    date_to: date,
) -> SummaryReport:
    """Сводный отчёт «проекты × бюро» за период включительно.

    Предела длины периода нет: запрос группирует, и размер ответа
    зависит от числа проектов и бюро, а не от числа дней.

    Raises:
        InvalidIntervalError: начало периода позже конца.
    """
    if date_from > date_to:
        raise InvalidIntervalError("начало периода позже конца")

    bureaus = await _load_bureaus(db)
    bureau_ids = [b.id for b in bureaus]
    projects = await _load_project_rows(db, mode, date_from, date_to, bureau_ids)
    # Неявки — в обоих режимах: табель знает и будущие отпуска, и в плане
    # видно, сколько дней бюро выпадает.
    absences = await _load_absence_rows(db, date_from, date_to, bureau_ids)

    projects_total = _sum_rows("Итого по проектам", projects)
    absences_total = _sum_rows("Итого неявки", absences)
    return SummaryReport(
        mode=mode,
        date_from=date_from,
        date_to=date_to,
        bureaus=bureaus,
        projects=projects,
        absences=absences,
        projects_total=projects_total,
        absences_total=absences_total,
        grand_total=_sum_rows("Всего", [projects_total, absences_total]),
    )