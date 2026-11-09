# app/api/v1/planning/service.py
"""Сервисный слой планирования: снимок доступности и сборка сетки.

Здесь живёт работа с БД; правила — в rules.py. Разделение не
косметическое: правила проверяются словарями в консоли, без базы,
и остаются единственным местом, где записано «можно ли планировать».

Главный приём модуля — читать одним заходом на весь интервал, дальше
считать в памяти. Тот же, что в dept_tree.py и scope.py. Альтернатива
(запрос на каждый проверяемый день) при переразложении недели на
несколько шифров вырождается в сотни обращений: именно этим страдала
PL/pgSQL-функция fill_plan_cells, которую мы заменяем.

Запись изменений (apply_changes) работает поверх этого же контекста.

План ставится на пакет работ, а не на комплект: строка сетки — пара
«инженер + пакет». Шапки плана нет: план — это только ячейки, а его
границы и объём вычисляются запросом (_load_plan_spans).
"""
import logging
from datetime import date, timedelta
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import ColumnElement, delete, func, insert, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.planning.rules import (
    AbsenceInfo,
    AvailabilityContext,
    DayInfo,
    EmployeeRow,
    FactCellInfo,
    PackageRow,
    PlanCellInfo,
    PlanSpanInfo,
    day_verdict,
    DayState,
    PLAN_STATUS_IDS,
    package_allows_plan,
    package_label,
)
from app.api.v1.planning.schemas import (
    CellOut,
    DayOut,
    EmployeeOut,
    RowOut,
    WorkloadGrid,
    ApplyResult,
    CellOperation,
)
from app.core.scope import Scope
from app.models.composition import (
    PackageStatus,
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

logger = logging.getLogger(__name__)


# Статусы пакетов, попадающие в сетку как строки «я исполнитель».
# Совпадают со статусами, в которые можно добавлять план
# (rules.PLAN_STATUS_IDS): пакет, который нельзя планировать, незачем
# подтягивать в сетку как рабочий. Одна константа — два правила не
# разойдутся. Пакеты других статусов всё равно видны, если в окне у них
# есть план или факт: это история занятости инженера.
GRID_STATUS_IDS: tuple[int, ...] = PLAN_STATUS_IDS

# Предел ширины запрашиваемого интервала. Сетка прокручивается свободно,
# но подгружается окнами: держать в памяти год на бюро незачем, а
# случайный запрос на весь горизонт (730 дней) дал бы десятки тысяч
# элементов в ответе. 120 дней — примерно квартал, с запасом на любую
# осмысленную прокрутку.
MAX_INTERVAL_DAYS = 120


class PlanningError(Exception):
    """Базовое исключение сервиса планирования.

    Доменные ошибки, которые роут переводит в HTTP-коды. Тексты
    нейтральны: без SQL, путей и деталей БД (правило 18).
    """


class InvalidIntervalError(PlanningError):
    """Интервал бессмыслен или слишком широк."""


class DepartmentForbiddenError(PlanningError):
    """Подразделение вне области управления пользователя."""


# ---------------------------------------------------------------------------
# Загрузка снимка
# ---------------------------------------------------------------------------


async def load_horizon(db: AsyncSession) -> tuple[date, date]:
    """Возвращает края заполненного производственного календаря.

    Нужно, чтобы отказ звучал как «календарь заполнен по 31.12.2026», а
    не как ошибка внешнего ключа: у обеих таблиц ячеек work_date
    ссылается на production_calendar, и дата вне справочника даёт 23503
    на уровне БД.

    Календарь доливается вручную функцией fill_calendar_year(p_year) —
    автоматики нет намеренно, расстановка праздников требует сверки с
    производственным календарём РФ.
    """
    stmt = select(
        func.min(ProductionCalendar.work_date),
        func.max(ProductionCalendar.work_date),
    )
    horizon_from, horizon_to = (await db.execute(stmt)).one()
    return horizon_from, horizon_to


async def resolve_bureau_employees(
    db: AsyncSession,
    department_id: int,
    date_from: date,
    date_to: date,
) -> dict[int, EmployeeRow]:
    """Инженеры бюро, чьё назначение пересекается с интервалом.

    Пересечение, а не «числится сейчас»: если человек перешёл в другое
    подразделение в середине интервала, он должен быть виден в сетке за
    те дни, когда ещё числился здесь. Границы назначения едут в
    EmployeeRow и дальше проверяются правилом NOT_ASSIGNED.

    Условие пересечения двух отрезков в общем виде:
        начало_одного <= конец_другого И конец_одного >= начало_другого
    С поправкой на valid_to = NULL — это открытое назначение, у него
    конца нет, поэтому оно пересекается с любым будущим интервалом.

    Назначений в одном бюро у человека может быть несколько (перевод и
    возврат). Схлопываем их в один диапазон: минимальное начало и
    максимальный конец, причём открытое назначение поглощает остальные.
    Для сетки этого достаточно — дыра между двумя назначениями в одном
    бюро практического смысла не несёт.
    """
    stmt = (
        select(
            Employee.id,
            Employee.full_name,
            EmployeeAssignment.department_id,
            EmployeeAssignment.valid_from,
            EmployeeAssignment.valid_to,
        )
        .join(
            EmployeeAssignment,
            EmployeeAssignment.employee_id == Employee.id,
        )
        .where(
            EmployeeAssignment.department_id == department_id,
            EmployeeAssignment.valid_from <= date_to,
            or_(
                EmployeeAssignment.valid_to.is_(None),
                EmployeeAssignment.valid_to >= date_from,
            ),
        )
        .order_by(Employee.full_name)
    )
    rows = (await db.execute(stmt)).all()

    employees: dict[int, EmployeeRow] = {}
    for emp_id, full_name, dept_id, valid_from, valid_to in rows:
        existing = employees.get(emp_id)
        if existing is None:
            employees[emp_id] = EmployeeRow(
                id=emp_id,
                full_name=full_name,
                department_id=dept_id,
                valid_from=valid_from,
                valid_to=valid_to,
            )
            continue

        # Схлопывание нескольких назначений: берём самое раннее начало.
        merged_from = min(existing.valid_from, valid_from)
        # None означает «конца нет», поэтому поглощает любую дату.
        if existing.valid_to is None or valid_to is None:
            merged_to = None
        else:
            merged_to = max(existing.valid_to, valid_to)

        employees[emp_id] = EmployeeRow(
            id=emp_id,
            full_name=full_name,
            department_id=dept_id,
            valid_from=merged_from,
            valid_to=merged_to,
        )

    return employees


async def load_calendar(
    db: AsyncSession,
    date_from: date,
    date_to: date,
) -> dict[date, DayInfo]:
    """Производственный календарь на интервал: дата → тип дня.
    Публичная: сервис факта грузит ею календарь вокруг сегодняшнего
    дня для расчёта окна отметки — отдельно от контекста месяца.

    Признак рабочего дня лежит не в календаре, а в day_types.is_working
    (таблица нормализована), поэтому без join обойтись нельзя.

    Отсутствие даты в результате — не ошибка данных, а выход за край
    заполненного календаря. Правило трактует это как OUT_OF_HORIZON.
    """
    stmt = (
        select(
            ProductionCalendar.work_date,
            DayType.code,
            DayType.is_working,
        )
        .join(DayType, DayType.id == ProductionCalendar.day_type_id)
        .where(
            ProductionCalendar.work_date >= date_from,
            ProductionCalendar.work_date <= date_to,
        )
    )
    rows = (await db.execute(stmt)).all()
    return {
        work_date: DayInfo(day_type_code=code, is_working=is_working)
        for work_date, code, is_working in rows
    }


async def _load_absences(
    db: AsyncSession,
    employee_ids: list[int],
    date_from: date,
    date_to: date,
) -> dict[tuple[int, date], AbsenceInfo]:
    """Неявки, развёрнутые из диапазонов в отдельные дни.

    В БД неявка хранится периодом (date_from … date_to включительно), и
    этого достаточно: отсутствие непрерывно, поэтому проекция на дни
    восстанавливается однозначно. Разворот делаем в памяти — периодов на
    бюро за две недели единицы, любая хитрость в SQL тут дороже цикла.

    Наложение периодов одного работника исключено ограничением
    absence_periods_no_overlap (EXCLUDE USING gist), поэтому ключ
    (employee_id, work_date) не может прийти дважды с разными данными.

    Флаг blocks_planning тянем сюда, потому что решение «закрыт ли день»
    принимает именно он, а не факт наличия периода: командировка может
    оставаться планируемой.
    """
    stmt = (
        select(
            AbsencePeriod.employee_id,
            AbsencePeriod.date_from,
            AbsencePeriod.date_to,
            AbsenceType.id,
            AbsenceType.tab_letter,
            AbsenceType.blocks_planning,
        )
        .join(
            AbsenceType,
            AbsenceType.id == AbsencePeriod.absence_type_id,
        )
        .where(
            AbsencePeriod.employee_id.in_(employee_ids),
            AbsencePeriod.date_from <= date_to,
            AbsencePeriod.date_to >= date_from,
        )
    )
    rows = (await db.execute(stmt)).all()

    absences: dict[tuple[int, date], AbsenceInfo] = {}
    for emp_id, period_from, period_to, type_id, letter, blocks in rows:
        info = AbsenceInfo(
            absence_type_id=type_id,
            tab_letter=letter,
            blocks_planning=blocks,
        )
        # Обрезаем период по краям интервала: за его пределами дни
        # никого не интересуют, а лишние ключи только раздуют словарь.
        current = max(period_from, date_from)
        last = min(period_to, date_to)
        while current <= last:
            absences[(emp_id, current)] = info
            current += timedelta(days=1)

    return absences


async def _load_plan_cells(
    db: AsyncSession,
    employee_ids: list[int],
    date_from: date,
    date_to: date,
) -> dict[tuple[int, date], PlanCellInfo]:
    """Плановые ячейки инженеров на интервал.

    ВАЖНО: отбор идёт по инженерам, а НЕ по бюро. Пакет, занявший день,
    может принадлежать чужому подразделению — инженера туда занимали.
    Ограничь мы выборку своим бюро, чужая занятость стала бы невидимой,
    и сетка предлагала бы планировать на день, который база не примет.

    Пакет-конкурент показываем целиком, с шифром (решение пользователя):
    начальнику понятнее, с кем договариваться.
    """
    stmt = (
        select(
            PlanCalendarCell.employee_id,
            PlanCalendarCell.work_date,
            PlanCalendarCell.package_id,
            ProjectMarkSet.marka_full,
            WorkPackage.name,
        )
        .join(WorkPackage, WorkPackage.id == PlanCalendarCell.package_id)
        .join(ProjectMarkSet, ProjectMarkSet.id == WorkPackage.mark_set_id)
        .where(
            PlanCalendarCell.employee_id.in_(employee_ids),
            PlanCalendarCell.work_date >= date_from,
            PlanCalendarCell.work_date <= date_to,
        )
    )
    rows = (await db.execute(stmt)).all()
    return {
        (emp_id, work_date): PlanCellInfo(
            package_id=package_id,
            label=package_label(marka_full, package_name),
        )
        for emp_id, work_date, package_id, marka_full, package_name in rows
    }


async def _load_fact_cells(
    db: AsyncSession,
    employee_ids: list[int],
    date_from: date,
    date_to: date,
) -> dict[tuple[int, date], FactCellInfo]:
    """Фактические ячейки инженеров на интервал.

    Факт ссылается на пакет так же, как план, но от плана не зависит:
    факт возможен там, где плана не было вовсе.

    Отбор по инженерам — по той же причине, что и у плановых ячеек.
    """
    stmt = (
        select(
            FactCalendarCell.employee_id,
            FactCalendarCell.work_date,
            FactCalendarCell.package_id,
            ProjectMarkSet.marka_full,
            WorkPackage.name,
        )
        .join(WorkPackage, WorkPackage.id == FactCalendarCell.package_id)
        .join(ProjectMarkSet, ProjectMarkSet.id == WorkPackage.mark_set_id)
        .where(
            FactCalendarCell.employee_id.in_(employee_ids),
            FactCalendarCell.work_date >= date_from,
            FactCalendarCell.work_date <= date_to,
        )
    )
    rows = (await db.execute(stmt)).all()
    return {
        (emp_id, work_date): FactCellInfo(
            package_id=package_id,
            label=package_label(marka_full, package_name),
        )
        for emp_id, work_date, package_id, marka_full, package_name in rows
    }


# Пакет и то, что о нём нужно контексту: шифр (для подписи), строка
# состава и её отдел (для проверки области), дата выпуска комплекта.
PackageFetched = tuple[WorkPackage, str, int, int, Optional[date]]


async def _fetch_packages(
    db: AsyncSession, *conditions: ColumnElement[bool]
) -> list[PackageFetched]:
    """Пакеты по условиям — с шифром, строкой состава, отделом, выпуском.

    Один запрос вместо пакета, шифра и состава по отдельности: все три
    загрузчика пакетов читают одни и те же поля. Отдел нужен только
    проверке области в контуре записи, но строка состава всё равно
    присоединяется — лишняя колонка дешевле второго варианта запроса.
    """
    stmt = (
        select(
            WorkPackage,
            ProjectMarkSet.marka_full,
            ProjectMarkSet.composition_id,
            ProjectComposition.department_id,
            ProjectMarkSet.released_on,
        )
        .join(ProjectMarkSet, ProjectMarkSet.id == WorkPackage.mark_set_id)
        .join(
            ProjectComposition,
            ProjectComposition.id == ProjectMarkSet.composition_id,
        )
        .where(*conditions)
    )
    return [
        (package, marka_full, composition_id, department_id, released_on)
        for package, marka_full, composition_id, department_id, released_on in (
            await db.execute(stmt)
        ).all()
    ]


def _package_row(
    package: WorkPackage,
    marka_full: str,
    composition_id: int,
    released_on: Optional[date],
) -> PackageRow:
    """Строка контекста из пакета и его шифра."""
    return PackageRow(
        id=package.id,
        label=package_label(marka_full, package.name),
        executor_id=package.executor_id,
        composition_id=composition_id,
        status_id=package.status_id,
        released=released_on is not None,
    )


async def _load_packages(
    db: AsyncSession,
    employee_ids: list[int],
    plan_cells: dict[tuple[int, date], PlanCellInfo],
    fact_cells: dict[tuple[int, date], FactCellInfo],
    executor_status_ids: tuple[int, ...] = GRID_STATUS_IDS,
) -> dict[int, PackageRow]:
    """Пакеты — строки сетки. Объединение трёх источников.

    1. Инженер — исполнитель пакета (фильтр по статусу: завершённые и
       приостановленные пакеты в сетке не нужны, иначе за год накопятся
       десятки закрытых строк).
       Набор статусов задаётся executor_status_ids: планированию нужны
       GRID_STATUS_IDS, факту — свой набор.
    2. На пакете есть плановые ячейки инженера — занятость чужим
       пакетом, в том числе из другого бюро или проекта, и остаток плана
       после смены исполнителя.
    3. На пакете есть фактические ячейки — «факт вне плана»: человек
       подхватил чужую работу, исполнителем не числясь.

    Источники 2 и 3 фильтру по статусу НЕ подчиняются: если день уже
    занят или отработан, скрывать его из-за статуса нельзя — сетка
    показывала бы свободный день, который база не примет.
    """
    rows = await _fetch_packages(
        db,
        WorkPackage.executor_id.in_(employee_ids),
        WorkPackage.status_id.in_(executor_status_ids),
    )

    # Пакеты, засветившиеся в ячейках, но не попавшие по исполнителю.
    referenced = {info.package_id for info in plan_cells.values()}
    referenced |= {info.package_id for info in fact_cells.values()}
    missing = referenced - {package.id for package, *_ in rows}
    if missing:
        rows.extend(await _fetch_packages(db, WorkPackage.id.in_(missing)))

    return {
        package.id: _package_row(package, marka_full, composition_id, released_on)
        for package, marka_full, composition_id, _department_id, released_on in rows
    }


async def _load_plan_spans(
    db: AsyncSession,
    employee_ids: list[int],
) -> dict[tuple[int, int], PlanSpanInfo]:
    """Сводка плана инженеров: границы и объём по каждому пакету.

    Шапки плана нет — сводка считается одним агрегирующим запросом по
    ячейкам. Отбор по инженеру, без ограничения интервалом: колонка
    «дн.» показывает объём целиком, а не обрезанный окном.
    """
    stmt = (
        select(
            PlanCalendarCell.package_id,
            PlanCalendarCell.employee_id,
            func.min(PlanCalendarCell.work_date),
            func.max(PlanCalendarCell.work_date),
            func.count(PlanCalendarCell.id),
        )
        .where(PlanCalendarCell.employee_id.in_(employee_ids))
        .group_by(PlanCalendarCell.package_id, PlanCalendarCell.employee_id)
    )
    rows = (await db.execute(stmt)).all()
    return {
        (package_id, emp_id): PlanSpanInfo(
            start_date=first_day,
            end_date=last_day,
            work_days=days,
        )
        for package_id, emp_id, first_day, last_day, days in rows
    }


async def load_context(
    db: AsyncSession,
    *,
    employees: dict[int, EmployeeRow],
    date_from: date,
    date_to: date,
    executor_status_ids: tuple[int, ...] = GRID_STATUS_IDS,
) -> AvailabilityContext:
    """Собирает снимок доступности на интервал.

    Инженеры приходят готовыми, а не вычисляются внутри: вызывающие
    сценарии определяют их по-разному. Сетке нужны все инженеры бюро
    (resolve_bureau_employees), записи изменений — только затронутые
    операциями, и подразделение там вообще ни при чём.

    Семь запросов, каждый на весь интервал целиком. Дальше — только
    память.
    """
    horizon_from, horizon_to = await load_horizon(db)
    calendar = await load_calendar(db, date_from, date_to)

    employee_ids = list(employees)
    if not employee_ids:
        # Пустой IN () — синтаксическая ошибка в SQL, а результат и так
        # известен заранее. Пять запросов пропускаем.
        return AvailabilityContext(
            date_from=date_from,
            date_to=date_to,
            horizon_from=horizon_from,
            horizon_to=horizon_to,
            employees={},
            calendar=calendar,
            absences={},
            plan_cells={},
            fact_cells={},
            packages={},
            plan_spans={},
        )

    absences = await _load_absences(db, employee_ids, date_from, date_to)
    plan_cells = await _load_plan_cells(db, employee_ids, date_from, date_to)
    fact_cells = await _load_fact_cells(db, employee_ids, date_from, date_to)
    # После ячеек: дотягивает пакеты, засветившиеся только в них.
    packages = await _load_packages(
        db, employee_ids, plan_cells, fact_cells, executor_status_ids
    )
    plan_spans = await _load_plan_spans(db, employee_ids)

    return AvailabilityContext(
        date_from=date_from,
        date_to=date_to,
        horizon_from=horizon_from,
        horizon_to=horizon_to,
        employees=employees,
        calendar=calendar,
        absences=absences,
        plan_cells=plan_cells,
        fact_cells=fact_cells,
        packages=packages,
        plan_spans=plan_spans,
    )


# ---------------------------------------------------------------------------
# Сборка сетки загрузки
# ---------------------------------------------------------------------------

async def load_package_status_names(db: AsyncSession) -> dict[int, str]:
    """Справочник статусов пакета: id → название.

    Шесть строк — отдельный крошечный запрос дешевле, чем тянуть
    название через все загрузчики пакетов. Нужен для метки статуса в
    строке сетки и для текста отказа PACKAGE_CLOSED.
    """
    rows = await db.execute(select(PackageStatus.id, PackageStatus.name))
    return {status_id: name for status_id, name in rows.all()}


async def get_availability(
    db: AsyncSession,
    *,
    scope: Scope,
    department_id: int,
    date_from: date,
    date_to: date,
) -> WorkloadGrid:
    """Собирает сетку загрузки бюро на интервал.

    Проверка области идёт ДО обращения к БД: незачем читать данные
    подразделения, которое пользователю показывать нельзя.

    Дальше — два шага, разделённых сознательно: асинхронная загрузка
    снимка и синхронная сборка ответа (_build_grid). Сборка не знает ни
    про БД, ни про async, поэтому проверяется на рукописном контексте
    так же, как правила.
    """
    if date_from > date_to:
        raise InvalidIntervalError("Начало интервала позже его конца")

    span = (date_to - date_from).days + 1
    if span > MAX_INTERVAL_DAYS:
        raise InvalidIntervalError(
            f"Слишком широкий интервал: {span} дн. "
            f"(максимум {MAX_INTERVAL_DAYS})"
        )

    # Scope.can_manage сам обрабатывает случай администратора
    # (unrestricted), поэтому обходить его отдельной веткой не нужно.
    if not scope.can_manage(department_id):
        raise DepartmentForbiddenError(
            "Подразделение вне области управления"
        )

    employees = await resolve_bureau_employees(
        db, department_id, date_from, date_to
    )
    ctx = await load_context(
        db, employees=employees, date_from=date_from, date_to=date_to
    )
    status_names = await load_package_status_names(db)
    return _build_grid(ctx, status_names)


def _collect_rows(
    ctx: AvailabilityContext,
    status_names: dict[int, str],
) -> list[RowOut]:
    """Строит строки сетки — пары (инженер, пакет) из трёх источников.

    Источники те же, что в _load_packages: исполнитель пакета, плановые
    ячейки, фактические ячейки. Здесь они раскладываются по инженерам, а
    там просто собирались пакеты.
    status_names — справочник статусов пакета: название для метки в
    строке. Допустимость плана (allows_plan) считается правилом
    package_allows_plan, а не фронтом.
    """
    # Множество пакетов под каждым инженером. Порядок сборки неважен —
    # сортировка в конце.
    pairs: dict[int, set[int]] = {emp_id: set() for emp_id in ctx.employees}

    # Источник 1: исполнитель пакета.
    for candidate in ctx.packages.values():
        if candidate.executor_id in pairs:
            pairs[candidate.executor_id].add(candidate.id)

    # Источники 2 и 3: пакеты, засветившиеся в ячейках инженера.
    for (emp_id, _work_date), plan_cell in ctx.plan_cells.items():
        if emp_id in pairs:
            pairs[emp_id].add(plan_cell.package_id)
    for (emp_id, _work_date), fact in ctx.fact_cells.items():
        if emp_id in pairs:
            pairs[emp_id].add(fact.package_id)

    rows: list[RowOut] = []
    for emp_id, package_ids in pairs.items():
        for package_id in package_ids:
            package = ctx.packages.get(package_id)
            if package is None:
                # Пакет есть в ячейках, но не догрузился в packages.
                # Штатно невозможно (_load_packages дотягивает такие), но
                # пропуск безопаснее падения на отрисовке.
                logger.warning("Пакет %s отсутствует в контексте сетки", package_id)
                continue

            span = ctx.plan_spans.get((package_id, emp_id))
            rows.append(
                RowOut(
                    employee_id=emp_id,
                    package_id=package_id,
                    label=package.label,
                    status_id=package.status_id,
                    # Индексом, а не .get(): статус пакета обязателен и
                    # ссылается на справочник внешним ключом. Пропажа
                    # названия — ошибка загрузки, её лучше увидеть сразу.
                    status_name=status_names[package.status_id],
                    allows_plan=package_allows_plan(package.status_id),
                    is_executor=package.executor_id == emp_id,
                    start_date=span.start_date if span else None,
                    end_date=span.end_date if span else None,
                    work_days=span.work_days if span else None,
                )
            )

    # Устойчивый порядок: инженер, затем подпись пакета по алфавиту —
    # она начинается с шифра, поэтому пакеты одного комплекта идут
    # подряд. Без сортировки порядок строк менялся бы от запроса к
    # запросу (обход множества), и сетка «прыгала» бы.
    rows.sort(key=lambda row: (row.employee_id, row.label))
    return rows


def _collect_days(ctx: AvailabilityContext) -> list[DayOut]:
    """Состояние каждого дня каждого инженера внутри интервала.

    Вердикт берётся из day_verdict — единственного места, где записаны
    правила. Буква табеля добавляется отдельно, напрямую из контекста:
    непрерывающее планирование отсутствие (командировка) даёт вердикт
    free, но показать такой день как обычный было бы неверно.
    """
    days: list[DayOut] = []
    for emp_id in ctx.employees:
        current = ctx.date_from
        while current <= ctx.date_to:
            verdict = day_verdict(ctx, emp_id, current)
            absence = ctx.absences.get((emp_id, current))
            day_info = ctx.calendar.get(current)

            days.append(
                DayOut(
                    employee_id=emp_id,
                    work_date=current,
                    state=verdict.state,
                    # Буква из контекста, а не из вердикта: вердикт
                    # молчит о непреграждающем отсутствии.
                    tab_letter=absence.tab_letter if absence else None,
                    day_type_code=(
                        day_info.day_type_code if day_info else None
                    ),
                )
            )
            current += timedelta(days=1)

    return days


def _collect_cells(ctx: AvailabilityContext) -> list[CellOut]:
    """Заполненные ячейки: где есть план, факт или то и другое.

    Пустые пересечения не отдаются вовсе — на двухнедельном окне это на
    порядок меньше данных, чем полная матрица «строка × день».

    План и факт сводятся в один элемент, потому что фронту нужна именно
    их комбинация: только план — синий, план и факт — зелёный, только
    факт — оранжевый.
    """
    # Ключ (инженер, пакет, дата) → [есть план, есть факт].
    merged: dict[tuple[int, int, date], list[bool]] = {}

    for (emp_id, work_date), plan in ctx.plan_cells.items():
        key = (emp_id, plan.package_id, work_date)
        merged.setdefault(key, [False, False])[0] = True

    for (emp_id, work_date), fact in ctx.fact_cells.items():
        key = (emp_id, fact.package_id, work_date)
        merged.setdefault(key, [False, False])[1] = True

    cells = [
        CellOut(
            employee_id=emp_id,
            package_id=package_id,
            work_date=work_date,
            has_plan=has_plan,
            has_fact=has_fact,
        )
        for (emp_id, package_id, work_date), (
            has_plan,
            has_fact,
        ) in merged.items()
    ]
    cells.sort(key=lambda cell: (cell.employee_id, cell.work_date))
    return cells


def grid_rows_and_cells(
    ctx: AvailabilityContext,
    status_names: dict[int, str],
) -> tuple[list[RowOut], list[CellOut]]:
    """Строки и клетки сетки из снимка — для экрана администратора.

    Публичная обёртка над _collect_rows и _collect_cells: администратор
    показывает те же строки и клетки, что начальник (у одного сотрудника
    они дают обе полосы — план и факт), а слой дней у него свой.
    Обёртка, а не переименование: внутренние функции остаются
    внутренними, и чужой модуль зависит от одной явной точки входа.
    """
    return _collect_rows(ctx, status_names), _collect_cells(ctx)


def _build_grid(
    ctx: AvailabilityContext,
    status_names: dict[int, str],
) -> WorkloadGrid:
    """Раскладывает снимок контекста в плоские слои ответа.

    Функция синхронная и чистая: БД не трогает, на вход — контекст, на
    выход — готовая схема. Проверяется на рукописном контексте, как и
    правила.
    """
    employees = [
        EmployeeOut(
            id=emp.id,
            full_name=emp.full_name,
            valid_from=emp.valid_from,
            valid_to=emp.valid_to,
        )
        for emp in ctx.employees.values()
    ]

    return WorkloadGrid(
        date_from=ctx.date_from,
        date_to=ctx.date_to,
        horizon_from=ctx.horizon_from,
        horizon_to=ctx.horizon_to,
        employees=employees,
        rows=_collect_rows(ctx, status_names),
        days=_collect_days(ctx),
        cells=_collect_cells(ctx),
    )


class PackageNotFoundError(PlanningError):
    """Пакет из операции не существует."""


class PackageForbiddenError(PlanningError):
    """Пакет принадлежит подразделению вне области управления."""


class NoExecutorError(PlanningError):
    """У пакета не назначен исполнитель — планировать некого."""


def _executor_of(package: PackageRow) -> int:
    """Исполнитель пакета в контуре записи — гарантированно число.

    Нужен только для ДОБАВЛЕНИЯ: _load_operation_packages уже отказал
    (NoExecutorError) для пакетов без исполнителя, в которые что-то
    добавляют. Функция делает эту гарантию видимой: тип int говорит и
    читателю, и mypy, что исполнитель есть.
    """
    if package.executor_id is None:
        raise NoExecutorError(f"У пакета {package.label} не назначен исполнитель")
    return package.executor_id


@dataclass(frozen=True, slots=True)
class RejectedOperation:
    """Операция, не прошедшая проверку.

    Текста сообщения здесь нет: причина и данные для подстановки —
    отдельно, формулировка собирается в routes.py. Так правка текста не
    трогает сервис, а один и тот же отказ выглядит одинаково независимо
    от того, поймала его проверка или проигранная гонка.
    """

    package_id: int
    work_date: date
    state: DayState
    # BUSY / HAS_FACT / DAY_TAKEN / PACKAGE_CLOSED: подпись пакета,
    # которым занят день или который закрыт.
    conflicting_label: Optional[str] = None
    # DAY_TAKEN: чей план занимает дату.
    conflicting_employee: Optional[str] = None
    # ABSENCE: буква табеля.
    tab_letter: Optional[str] = None
    # PACKAGE_CLOSED: название статуса пакета («Готово», «Приостановлен»).
    status_name: Optional[str] = None


class OperationsRejectedError(PlanningError):
    """Пачка отклонена целиком — ни одна операция не применена.

    Правило «всё или ничего»: клиент проверил правила перед отправкой,
    и отказ означает, что его картина устарела. Частичная запись
    оставила бы пользователя с неверным представлением о том, что
    сохранилось.
    """

    def __init__(self, rejections: list[RejectedOperation]) -> None:
        super().__init__("Изменения отклонены")
        self.rejections = rejections


async def resolve_employees_by_ids(
    db: AsyncSession,
    employee_ids: list[int],
    date_from: date,
    date_to: date,
) -> dict[int, EmployeeRow]:
    """Инженеры по списку id с границами их назначений.

    Отличие от resolve_bureau_employees: отбор идёт по людям, а не по
    подразделению. В контуре записи инженер берётся из исполнителя
    пакета, и бюро тут вообще ни при чём — пакет может принадлежать
    одному подразделению, а человек числиться в другом.

    Назначения схлопываются по всем подразделениям сразу: правило
    NOT_ASSIGNED здесь отвечает на вопрос «работал ли человек в
    организации в этот день», а не «в каком именно бюро».
    """
    stmt = (
        select(
            Employee.id,
            Employee.full_name,
            EmployeeAssignment.department_id,
            EmployeeAssignment.valid_from,
            EmployeeAssignment.valid_to,
        )
        .join(
            EmployeeAssignment,
            EmployeeAssignment.employee_id == Employee.id,
        )
        .where(
            Employee.id.in_(employee_ids),
            EmployeeAssignment.valid_from <= date_to,
            or_(
                EmployeeAssignment.valid_to.is_(None),
                EmployeeAssignment.valid_to >= date_from,
            ),
        )
    )
    rows = (await db.execute(stmt)).all()

    employees: dict[int, EmployeeRow] = {}
    for emp_id, full_name, dept_id, valid_from, valid_to in rows:
        existing = employees.get(emp_id)
        if existing is None:
            employees[emp_id] = EmployeeRow(
                id=emp_id,
                full_name=full_name,
                department_id=dept_id,
                valid_from=valid_from,
                valid_to=valid_to,
            )
            continue

        merged_from = min(existing.valid_from, valid_from)
        if existing.valid_to is None or valid_to is None:
            merged_to = None
        else:
            merged_to = max(existing.valid_to, valid_to)

        employees[emp_id] = EmployeeRow(
            id=emp_id,
            full_name=full_name,
            department_id=existing.department_id,
            valid_from=merged_from,
            valid_to=merged_to,
        )

    return employees


async def _load_operation_packages(
    db: AsyncSession,
    scope: Scope,
    package_ids: set[int],
    adding_ids: set[int],
) -> dict[int, PackageRow]:
    """Пакеты из операций с проверкой существования, области и исполнителя.

    Подразделение берётся через строку состава (project_composition):
    у пакета и комплекта своего отдела нет, они наследуют его от
    задания ГИПа.

    Исполнитель обязателен только у пакетов из adding_ids — тех, куда
    что-то ДОБАВЛЯЮТ: плановой ячейке нужен человек. Снять день можно
    и с пакета без исполнителя — начальник мог снять назначение уже
    после раскладки, и остаток плана иначе стал бы неснимаемым.

    Все проверки — до записи и до сбора контекста: незачем читать
    календарь ради операции, которая всё равно будет отвергнута.
    """
    rows = await _fetch_packages(db, WorkPackage.id.in_(package_ids))

    found: dict[int, PackageRow] = {}
    for package, marka_full, composition_id, department_id, released_on in rows:
        # Область управления. can_manage сам обрабатывает админа.
        if not scope.can_manage(department_id):
            raise PackageForbiddenError("Пакет вне области управления")
        row = _package_row(package, marka_full, composition_id, released_on)
        if package.id in adding_ids and row.executor_id is None:
            raise NoExecutorError(f"У пакета {row.label} не назначен исполнитель")
        found[package.id] = row

    missing = package_ids - set(found)
    if missing:
        # Конкретные id наружу не отдаём (правило 18) — только в лог.
        logger.warning("Пакеты не найдены: %s", sorted(missing))
        raise PackageNotFoundError("Пакет не найден")

    return found


async def _load_package_occupancy(
    db: AsyncSession,
    package_ids: set[int],
    date_from: date,
    date_to: date,
) -> dict[tuple[int, date], int]:
    """Кто занимает дни этих пакетов: (пакет, дата) → инженер.

    Отбор идёт по ПАКЕТУ, а не по инженеру — в этом вся суть. Плановая
    ячейка могла остаться за прежним исполнителем, и он не попадёт ни в
    одну выборку «по людям»: в сетке бюро его может не быть вовсе, если
    он перешёл в другое подразделение.

    На этой выборке держатся два правила:
      - дата пакета планируется один раз, кем бы то ни было;
      - снять план может кто угодно, а не только текущий исполнитель.

    Второе следует из первого: раз ячейка на паре (пакет, дата) одна,
    цель удаления определяется однозначно, и передавать исполнителя в
    операции не нужно.
    """
    stmt = select(
        PlanCalendarCell.package_id,
        PlanCalendarCell.work_date,
        PlanCalendarCell.employee_id,
    ).where(
        PlanCalendarCell.package_id.in_(package_ids),
        PlanCalendarCell.work_date >= date_from,
        PlanCalendarCell.work_date <= date_to,
    )
    rows = (await db.execute(stmt)).all()
    return {
        (package_id, work_date): employee_id
        for package_id, work_date, employee_id in rows
    }


def _validate_operations(
    ctx: AvailabilityContext,
    packages: dict[int, PackageRow],
    occupancy: dict[tuple[int, date], int],
    operations: list[CellOperation],
    status_names: dict[int, str],
) -> list[RejectedOperation]:
    """Проверяет пачку операций против снимка. Пустой список = всё прошло.

    Используется дважды: до записи и после IntegrityError (диагностика
    проигранной гонки). Один код на оба случая — значит и сообщение
    пользователь увидит одинаковое.

    Внутри пачки операции видят друг друга. Снять день у Иванова и тут
    же отдать его Комарову — обычное действие, и проверка обязана
    считать дату освободившейся, иначе законный перенос отвергался бы
    из-за ячейки, которую сама же пачка и удаляет.
    """
    rejections: list[RejectedOperation] = []
    seen: set[tuple[str, int, date]] = set()

    # Что освобождают удаления этой же пачки. Два среза: по паре
    # «пакет + дата» — для правила «дата пакета одна», и по паре
    # «инженер + дата» — для проверки занятости человека.
    freed_on_package: set[tuple[int, date]] = set()
    freed_on_day: set[tuple[int, date]] = set()
    for operation in operations:
        if operation.op != "remove":
            continue
        key = (operation.package_id, operation.work_date)
        occupant = occupancy.get(key)
        if occupant is not None:
            freed_on_package.add(key)
            freed_on_day.add((occupant, operation.work_date))

    for operation in operations:
        package = packages[operation.package_id]
        package_key = (operation.package_id, operation.work_date)

        # Повтор ловим по ТРОЙКЕ, включая вид операции: снять день у
        # одного и отдать его другому — это две операции на одну пару
        # «пакет + дата», и они законны. Противоречием остаётся только
        # буквальный дубль: два add или два remove подряд.
        dup_key = (operation.op, operation.package_id, operation.work_date)
        if dup_key in seen:
            rejections.append(
                RejectedOperation(
                    package_id=operation.package_id,
                    work_date=operation.work_date,
                    state=DayState.DAY_TAKEN,
                )
            )
            continue
        seen.add(dup_key)

        occupant = occupancy.get(package_key)
        occupied = occupant is not None and package_key not in freed_on_package

        if operation.op == "add":
            # Пакет не «В работе» планировать нельзя. Проверяется первым:
            # на закрытом пакете занятость дня уже ничего не решает.
            # Снятие (remove) разрешено при любом статусе — им начальник
            # освобождает инженеру будущие дни.
            if not package_allows_plan(package.status_id):
                rejections.append(
                    RejectedOperation(
                        package_id=operation.package_id,
                        work_date=operation.work_date,
                        state=DayState.PACKAGE_CLOSED,
                        conflicting_label=package.label,
                        status_name=status_names.get(package.status_id),
                    )
                )
                continue

            # Исполнитель выводится из пакета, а не из запроса: подмена
            # id не позволит запланировать работу чужому человеку.
            employee_id = _executor_of(package)

            # Правило «дата пакета планируется один раз». Проверяется
            # раньше вердикта: две записи об одном дне работы означали бы
            # двойной счёт трудозатрат, и это важнее того, свободен ли
            # день у самого исполнителя.
            # occupant is not None уже входит в occupied; повторено явно,
            # чтобы проверка была видна и читателю, и mypy.
            if occupied and occupant is not None:
                owner = ctx.employees.get(occupant)
                rejections.append(
                    RejectedOperation(
                        package_id=operation.package_id,
                        work_date=operation.work_date,
                        state=DayState.DAY_TAKEN,
                        conflicting_label=package.label,
                        conflicting_employee=owner.full_name if owner else None,
                    )
                )
                continue

            verdict = day_verdict(ctx, employee_id, operation.work_date)
            if verdict.is_free:
                continue

            # BUSY на дне, который эта же пачка освобождает, — не
            # препятствие: удаления выполняются раньше вставок.
            if (
                verdict.state is DayState.BUSY
                and (employee_id, operation.work_date) in freed_on_day
            ):
                continue

            rejections.append(
                RejectedOperation(
                    package_id=operation.package_id,
                    work_date=operation.work_date,
                    state=verdict.state,
                    conflicting_label=verdict.label,
                    tab_letter=verdict.tab_letter,
                )
            )
            continue

        # op == "remove". Цель определяется парой «пакет + дата», а не
        # исполнителем: план мог остаться за прежним. Снятие ничего не
        # создаёт и никого не занимает, поэтому текущее назначение здесь
        # не проверяется вовсе.
        if occupant is None:
            rejections.append(
                RejectedOperation(
                    package_id=operation.package_id,
                    work_date=operation.work_date,
                    state=DayState.FREE,
                )
            )
            continue

        # День с фактом заперт: удаление плана под фактом искажает
        # сравнение сильнее, чем добавление. Факт сверяется у ВЛАДЕЛЬЦА
        # ячейки, а не у текущего исполнителя.
        fact = ctx.fact_cells.get((occupant, operation.work_date))
        if fact is not None and fact.package_id == operation.package_id:
            rejections.append(
                RejectedOperation(
                    package_id=operation.package_id,
                    work_date=operation.work_date,
                    state=DayState.HAS_FACT,
                    conflicting_label=fact.label,
                )
            )

    return rejections


async def apply_changes(
    db: AsyncSession,
    *,
    scope: Scope,
    operations: list[CellOperation],
) -> ApplyResult:
    """Применяет пачку изменений плановых ячеек одной транзакцией.

    Единственное место, которое меняет план из сетки начальника.

    Порядок: проверка пакетов → занятость дат пакета → снимок по всем
    причастным людям → проверка операций → удаления → добавления →
    commit → свежее состояние в ответ.

    Причастные — это исполнители пакетов, куда добавляют, ПЛЮС владельцы
    существующих ячеек. Вторые могут быть кем угодно, включая людей из
    других подразделений: план остаётся за прежним исполнителем, когда
    пакет передают. Без них снимок был бы неполон, и проверка факта на
    удаляемом дне сработала бы вхолостую.

    Гонка между проверкой и вставкой возможна: два начальника могут
    планировать одного инженера одновременно. Данные при этом в
    безопасности — unique_plan_cell не даст одному человеку два плана на
    день. Поймав 23505, откатываемся и ПОВТОРНО ПРОВЕРЯЕМ операции на
    свежем снимке: повторять саму запись смысла нет, под правилом «всё
    или ничего» результат был бы тем же отказом, зато диагностика
    превращает «конфликт записи» в «17.09 занят пакетом ...».
    """
    if not operations:
        raise OperationsRejectedError([])

    package_ids = {operation.package_id for operation in operations}
    adding_ids = {op.package_id for op in operations if op.op == "add"}
    packages = await _load_operation_packages(db, scope, package_ids, adding_ids)
    # Справочник статусов: для отказа PACKAGE_CLOSED и для строк ответа.
    status_names = await load_package_status_names(db)

    # Границы снимка — только затронутые даты, обычно несколько дней.
    dates = [operation.work_date for operation in operations]
    date_from, date_to = min(dates), max(dates)

    occupancy = await _load_package_occupancy(db, package_ids, date_from, date_to)

    employee_ids = sorted(
        {_executor_of(packages[package_id]) for package_id in adding_ids}
        | set(occupancy.values())
    )

    employees = await resolve_employees_by_ids(db, employee_ids, date_from, date_to)
    ctx = await load_context(
        db, employees=employees, date_from=date_from, date_to=date_to
    )

    rejections = _validate_operations(
        ctx, packages, occupancy, operations, status_names
    )
    if rejections:
        raise OperationsRejectedError(rejections)

    # Группировка по паре (пакет, инженер). Для добавлений инженер
    # берётся из пакета, для удалений — из владельца ячейки: он может
    # быть другим человеком.
    added: dict[tuple[int, int], list[date]] = {}
    removed: dict[tuple[int, int], list[date]] = {}
    for operation in operations:
        if operation.op == "add":
            key = (
                operation.package_id,
                _executor_of(packages[operation.package_id]),
            )
            added.setdefault(key, []).append(operation.work_date)
        else:
            owner_id = occupancy[(operation.package_id, operation.work_date)]
            key = (operation.package_id, owner_id)
            removed.setdefault(key, []).append(operation.work_date)

    try:
        # Сначала ВСЕ удаления, только потом добавления. Порядок
        # обязателен: перенос дня между пакетами или между людьми иначе
        # упрётся в unique_plan_cell.
        for (package_id, employee_id), work_dates in removed.items():
            await db.execute(
                delete(PlanCalendarCell).where(
                    PlanCalendarCell.package_id == package_id,
                    PlanCalendarCell.employee_id == employee_id,
                    PlanCalendarCell.work_date.in_(work_dates),
                )
            )

        for (package_id, employee_id), work_dates in added.items():
            await add_plan_cells(
                db,
                package_id=package_id,
                employee_id=employee_id,
                work_dates=work_dates,
            )

        await db.commit()

    except IntegrityError:
        # Транзакция в аварийном состоянии: любой запрос до rollback
        # упадёт. Поэтому сначала откат, диагностика — уже после.
        await db.rollback()
        logger.info("Конфликт записи плановых ячеек, повторная проверка")

        fresh_occupancy = await _load_package_occupancy(
            db, package_ids, date_from, date_to
        )
        employees = await resolve_employees_by_ids(db, employee_ids, date_from, date_to)
        fresh_ctx = await load_context(
            db, employees=employees, date_from=date_from, date_to=date_to
        )
        raise OperationsRejectedError(
            _validate_operations(
                fresh_ctx, packages, fresh_occupancy, operations, status_names
            )
        )

    # Свежее состояние затронутых инженеров — чтобы React перерисовал
    # без второго GET. Читаем после commit, отдельным снимком.
    employees = await resolve_employees_by_ids(db, employee_ids, date_from, date_to)
    result_ctx = await load_context(
        db, employees=employees, date_from=date_from, date_to=date_to
    )
    return ApplyResult(
        applied=len(operations),
        rows=_collect_rows(result_ctx, status_names),
        days=_collect_days(result_ctx),
        cells=_collect_cells(result_ctx),
        rejected=[],
    )


async def add_plan_cells(
    db: AsyncSession,
    *,
    package_id: int,
    employee_id: int,
    work_dates: list[date],
) -> None:
    """Вставляет плановые ячейки одного инженера по одному пакету.

    Одной вставкой на пачку дат, без пересчёта: шапки плана нет, сводка
    вычисляется при чтении. Удаления к этому моменту уже выполнены
    вызывающей стороной — они обязаны пройти раньше любой вставки:
    перенос дня с пакета на пакет в пределах одной даты обычен, и положи
    мы новую ячейку до удаления старой, INSERT упёрся бы в
    unique_plan_cell.

    Публичная: этой же функцией пишет план экран администратора.
    Проверки правил — на вызывающей стороне.
    """
    if not work_dates:
        return

    # employee_id хранится в ячейке, а не берётся из пакета: без него не
    # построить unique_plan_cell (UNIQUE через join не умеет), и ячейка
    # помнит, кто должен был работать, после смены исполнителя.
    await db.execute(
        insert(PlanCalendarCell),
        [
            {
                "package_id": package_id,
                "employee_id": employee_id,
                "work_date": work_date,
            }
            for work_date in work_dates
        ],
    )