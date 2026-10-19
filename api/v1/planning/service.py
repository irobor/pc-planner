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

Запись изменений (apply_changes, rebuild_plan) появится следующим
шагом — поверх этого же контекста.
"""
import logging
from datetime import date, timedelta
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import delete, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.planning.rules import (
    AbsenceInfo,
    AvailabilityContext,
    DayInfo,
    EmployeeRow,
    FactCellInfo,
    MarkSetRow,
    PlanCellInfo,
    WorkPlanInfo,
    day_verdict,
    DayState,
    PLAN_STATUS_IDS,
    set_allows_plan,
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
from app.models.composition import ProjectComposition, ProjectMarkSet, SetStatus
from app.models.employee import Employee, EmployeeAssignment
from app.models.planning import (
    AbsencePeriod,
    AbsenceType,
    DayType,
    FactCalendarCell,
    PlanCalendarCell,
    ProductionCalendar,
    WorkPlan,
)

logger = logging.getLogger(__name__)


# Статусы комплектов, попадающие в сетку как строки «я исполнитель».
# Совпадают со статусами, в которые можно добавлять план
# (rules.PLAN_STATUS_IDS): шифр, который нельзя планировать, незачем
# подтягивать в сетку как рабочий. Одна константа — два правила не
# разойдутся. Шифры других статусов всё равно видны, если в окне у них
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

    ВАЖНО: отбор идёт по инженерам, а НЕ по бюро. Шифр, занявший день,
    может принадлежать чужому подразделению — инженера туда занимали.
    Ограничь мы выборку своим бюро, чужая занятость стала бы невидимой,
    и сетка предлагала бы планировать на день, который база не примет.

    Шифр-конкурент показываем целиком (решение пользователя): начальнику
    понятнее, с кем договариваться.
    """
    stmt = (
        select(
            PlanCalendarCell.employee_id,
            PlanCalendarCell.work_date,
            PlanCalendarCell.work_plan_id,
            WorkPlan.mark_set_id,
            ProjectMarkSet.marka_full,
        )
        .join(WorkPlan, WorkPlan.id == PlanCalendarCell.work_plan_id)
        .join(
            ProjectMarkSet,
            ProjectMarkSet.id == WorkPlan.mark_set_id,
        )
        .where(
            PlanCalendarCell.employee_id.in_(employee_ids),
            PlanCalendarCell.work_date >= date_from,
            PlanCalendarCell.work_date <= date_to,
        )
    )
    rows = (await db.execute(stmt)).all()
    return {
        (emp_id, work_date): PlanCellInfo(
            work_plan_id=plan_id,
            mark_set_id=mark_set_id,
            marka_full=marka_full,
        )
        for emp_id, work_date, plan_id, mark_set_id, marka_full in rows
    }


async def _load_fact_cells(
    db: AsyncSession,
    employee_ids: list[int],
    date_from: date,
    date_to: date,
) -> dict[tuple[int, date], FactCellInfo]:
    """Фактические ячейки инженеров на интервал.

    На план не ссылаются: факт возможен там, где плана не было вовсе.
    Именно поэтому join идёт сразу на шифр, а не через work_plan.

    Отбор по инженерам — по той же причине, что и у плановых ячеек.
    """
    stmt = (
        select(
            FactCalendarCell.employee_id,
            FactCalendarCell.work_date,
            FactCalendarCell.mark_set_id,
            ProjectMarkSet.marka_full,
        )
        .join(
            ProjectMarkSet,
            ProjectMarkSet.id == FactCalendarCell.mark_set_id,
        )
        .where(
            FactCalendarCell.employee_id.in_(employee_ids),
            FactCalendarCell.work_date >= date_from,
            FactCalendarCell.work_date <= date_to,
        )
    )
    rows = (await db.execute(stmt)).all()
    return {
        (emp_id, work_date): FactCellInfo(
            mark_set_id=mark_set_id,
            marka_full=marka_full,
        )
        for emp_id, work_date, mark_set_id, marka_full in rows
    }


async def _load_mark_sets(
    db: AsyncSession,
    employee_ids: list[int],
    plan_cells: dict[tuple[int, date], PlanCellInfo],
    fact_cells: dict[tuple[int, date], FactCellInfo],
    executor_status_ids: tuple[int, ...] = GRID_STATUS_IDS,
) -> dict[int, MarkSetRow]:
    """Шифры — строки сетки. Объединение трёх источников.

    1. Инженер назначен исполнителем (фильтр по статусу: выпущенные
       комплекты в сетке не нужны, иначе за год накопятся десятки
       закрытых строк).
        Набор статусов задаётся executor_status_ids: планированию нужны
       GRID_STATUS_IDS, факту — FACT_STATUS_IDS (шире, с «На проверке»).
    2. На шифре есть плановые ячейки инженера — занятость чужим шифром,
       в том числе из другого бюро или проекта.
    3. На шифре есть фактические ячейки — «факт вне плана»: человек
       подхватил чужую работу, исполнителем не числясь (заменил
       заболевшего).

    Источники 2 и 3 фильтру по статусу НЕ подчиняются: если день уже
    занят или отработан, скрывать его из-за статуса нельзя — сетка
    показывала бы свободный день, который база не примет.
    """
    stmt = select(ProjectMarkSet).where(
        ProjectMarkSet.id_executor.in_(employee_ids),
        ProjectMarkSet.status_id.in_(executor_status_ids),
    )
    rows = list((await db.execute(stmt)).scalars().all())

    # Шифры, засветившиеся в ячейках, но не попавшие по исполнителю.
    referenced = {info.mark_set_id for info in plan_cells.values()}
    referenced |= {info.mark_set_id for info in fact_cells.values()}
    missing = referenced - {row.id for row in rows}
    if missing:
        extra = select(ProjectMarkSet).where(ProjectMarkSet.id.in_(missing))
        rows.extend((await db.execute(extra)).scalars().all())

    return {
        row.id: MarkSetRow(
            id=row.id,
            marka_full=row.marka_full,
            id_executor=row.id_executor,
            composition_id=row.composition_id,
            status_id=row.status_id,
        )
        for row in rows
    }


async def _load_work_plans(
    db: AsyncSession,
    employee_ids: list[int],
) -> dict[tuple[int, int], WorkPlanInfo]:
    """Активные планы инженеров — шапки с границами.

    Отбор по инженеру, а не по шифру: у человека все его планы, включая
    выходящие за края интервала. Их границы нужны сетке (колонка «дн.»
    показывает объём целиком, а не обрезанный окном).

    archived не берём: это история, в сетке ей места нет, и на
    idx_one_active_plan она не влияет.
    """
    stmt = select(WorkPlan).where(
        WorkPlan.employee_id.in_(employee_ids),
        WorkPlan.status.in_(("draft", "confirmed")),
    )
    rows = (await db.execute(stmt)).scalars().all()
    return {
        (row.mark_set_id, row.employee_id): WorkPlanInfo(
            id=row.id,
            mark_set_id=row.mark_set_id,
            employee_id=row.employee_id,
            start_date=row.start_date,
            end_date=row.end_date,
            work_days=row.work_days,
            status=row.status,
        )
        for row in rows
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
            mark_sets={},
            work_plans={},
        )

    absences = await _load_absences(db, employee_ids, date_from, date_to)
    plan_cells = await _load_plan_cells(
        db, employee_ids, date_from, date_to
    )
    fact_cells = await _load_fact_cells(
        db, employee_ids, date_from, date_to
    )
    # После ячеек: дотягивает шифры, засветившиеся только в них.
    mark_sets = await _load_mark_sets(
        db, employee_ids, plan_cells, fact_cells, executor_status_ids
    )
    work_plans = await _load_work_plans(db, employee_ids)

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
        mark_sets=mark_sets,
        work_plans=work_plans,
    )


# ---------------------------------------------------------------------------
# Сборка сетки загрузки
# ---------------------------------------------------------------------------

async def load_set_status_names(db: AsyncSession) -> dict[int, str]:
    """Справочник статусов шифра: id → название.

    Четыре строки — отдельный крошечный запрос дешевле, чем тянуть
    название через все загрузчики шифров. Нужен для метки статуса в
    строке сетки и для текста отказа SET_CLOSED.
    """
    rows = await db.execute(select(SetStatus.id, SetStatus.name))
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
    status_names = await load_set_status_names(db)
    return _build_grid(ctx, status_names)


def _collect_rows(
    ctx: AvailabilityContext,
    status_names: dict[int, str],
) -> list[RowOut]:
    """Строит строки сетки — пары (инженер, шифр) из трёх источников.

    Источники те же, что в _load_mark_sets: назначенный исполнитель,
    плановые ячейки, фактические ячейки. Здесь они раскладываются по
    инженерам, а там просто собирались шифры.
    status_names — справочник статусов (load_set_status_names): название
    для метки в строке. Допустимость плана (allows_plan) считается
    правилом set_allows_plan, а не фронтом.
    """
    # Множество шифров под каждым инженером. Порядок сборки неважен —
    # сортировка в конце.
    pairs: dict[int, set[int]] = {emp_id: set() for emp_id in ctx.employees}

    # Источник 1: назначенный исполнитель.
    for mark_set in ctx.mark_sets.values():
        if mark_set.id_executor in pairs:
            pairs[mark_set.id_executor].add(mark_set.id)

    # Источники 2 и 3: шифры, засветившиеся в ячейках инженера.
    for (emp_id, _work_date), plan in ctx.plan_cells.items():
        if emp_id in pairs:
            pairs[emp_id].add(plan.mark_set_id)
    for (emp_id, _work_date), fact in ctx.fact_cells.items():
        if emp_id in pairs:
            pairs[emp_id].add(fact.mark_set_id)

    rows: list[RowOut] = []
    for emp_id, mark_set_ids in pairs.items():
        for mark_set_id in mark_set_ids:
            mark_set = ctx.mark_sets.get(mark_set_id)
            if mark_set is None:
                # Шифр есть в ячейках, но не догрузился в mark_sets.
                # Штатно невозможно (_load_mark_sets дотягивает такие),
                # но пропуск безопаснее падения на отрисовке.
                logger.warning(
                    "Шифр %s отсутствует в контексте сетки", mark_set_id
                )
                continue

            plan = ctx.work_plans.get((mark_set_id, emp_id))
            rows.append(
                RowOut(
                    employee_id=emp_id,
                    mark_set_id=mark_set_id,
                    marka_full=mark_set.marka_full,
                    status_id=mark_set.status_id,
                    status_name=(
                        status_names.get(mark_set.status_id)
                        if mark_set.status_id is not None
                        else None
                    ),
                    allows_plan=set_allows_plan(mark_set.status_id),
                    is_executor=mark_set.id_executor == emp_id,
                    work_plan_id=plan.id if plan else None,
                    start_date=plan.start_date if plan else None,
                    end_date=plan.end_date if plan else None,
                    work_days=plan.work_days if plan else None,
                )
            )

    # Устойчивый порядок: инженер, затем шифр по алфавиту. Без сортировки
    # порядок строк менялся бы от запроса к запросу (обход множества), и
    # сетка «прыгала» бы при каждом обновлении.
    rows.sort(key=lambda row: (row.employee_id, row.marka_full))
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
    # Ключ (инженер, шифр, дата) → [есть план, есть факт].
    merged: dict[tuple[int, int, date], list[bool]] = {}

    for (emp_id, work_date), plan in ctx.plan_cells.items():
        key = (emp_id, plan.mark_set_id, work_date)
        merged.setdefault(key, [False, False])[0] = True

    for (emp_id, work_date), fact in ctx.fact_cells.items():
        key = (emp_id, fact.mark_set_id, work_date)
        merged.setdefault(key, [False, False])[1] = True

    cells = [
        CellOut(
            employee_id=emp_id,
            mark_set_id=mark_set_id,
            work_date=work_date,
            has_plan=has_plan,
            has_fact=has_fact,
        )
        for (emp_id, mark_set_id, work_date), (
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



# Статус создаваемого плана. Решено: сохранение сразу создаёт confirmed —
# черновика в интерфейсе нет, между «спланировал» и «подтвердил» никто
# ничего не делает. server_default='draft' в модели остаётся
# неиспользованным, поэтому статус пишем явно.
PLAN_STATUS_ACTIVE = "confirmed"


class MarkSetNotFoundError(PlanningError):
    """Шифр из операции не существует."""


class MarkSetForbiddenError(PlanningError):
    """Шифр принадлежит подразделению вне области управления."""


class NoExecutorError(PlanningError):
    """У шифра не назначен исполнитель — планировать некого."""


@dataclass(frozen=True, slots=True)
class RejectedOperation:
    """Операция, не прошедшая проверку.

    Текста сообщения здесь нет: причина и данные для подстановки —
    отдельно, формулировка собирается в routes.py. Так правка текста не
    трогает сервис, а один и тот же отказ выглядит одинаково независимо
    от того, поймала его проверка или проигранная гонка.
    """

    mark_set_id: int
    work_date: date
    state: DayState
    # BUSY / HAS_FACT: шифр, которым занят день.
    conflicting_marka: Optional[str] = None
    # DAY_TAKEN: чей план занимает дату.
    conflicting_employee: Optional[str] = None
    # ABSENCE: буква табеля.
    tab_letter: Optional[str] = None
    # SET_CLOSED: название статуса шифра («Выпущен», «На проверке»).
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
    подразделению. В контуре записи инженер берётся из id_executor шифра,
    и бюро тут вообще ни при чём — шифр может принадлежать одному
    подразделению, а человек числиться в другом.

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


async def _load_operation_mark_sets(
    db: AsyncSession,
    scope: Scope,
    mark_set_ids: set[int],
) -> dict[int, MarkSetRow]:
    """Шифры из операций с проверкой существования, области и исполнителя.

    Подразделение берётся через строку состава (project_composition):
    у самого комплекта отдела нет, он наследует его от задания ГИПа.

    Все три проверки — до записи и до сбора контекста: незачем читать
    календарь ради операции, которая всё равно будет отвергнута.
    """
    stmt = (
        select(ProjectMarkSet, ProjectComposition.department_id)
        .join(
            ProjectComposition,
            ProjectComposition.id == ProjectMarkSet.composition_id,
        )
        .where(ProjectMarkSet.id.in_(mark_set_ids))
    )
    rows = (await db.execute(stmt)).all()

    found: dict[int, MarkSetRow] = {}
    for mark_set, department_id in rows:
        # Область управления. can_manage сам обрабатывает админа.
        if not scope.can_manage(department_id):
            raise MarkSetForbiddenError("Шифр вне области управления")
        # Плана без человека не бывает: work_plan.employee_id NOT NULL.
        if mark_set.id_executor is None:
            raise NoExecutorError(
                f"У шифра {mark_set.marka_full} не назначен исполнитель"
            )
        found[mark_set.id] = MarkSetRow(
            id=mark_set.id,
            marka_full=mark_set.marka_full,
            id_executor=mark_set.id_executor,
            composition_id=mark_set.composition_id,
            status_id=mark_set.status_id,
        )

    missing = mark_set_ids - set(found)
    if missing:
        # Конкретные id наружу не отдаём (правило 18) — только в лог.
        logger.warning("Шифры не найдены: %s", sorted(missing))
        raise MarkSetNotFoundError("Шифр не найден")

    return found


# ПРАВКА app/api/v1/planning/service.py
#
# ШАГ 1. В rules.py, в конец класса DayState, добавить восьмое значение:
#
#     # Дата уже занята планом ЭТОГО шифра — возможно, за другим
#     # человеком. Состояние только для отказа: day_verdict его не
#     # выдаёт, потому что отвечает на вопрос про день инженера, а это
#     # правило про день шифра.
#     DAY_TAKEN = "day_taken"
#
# ШАГ 2. В service.py добавить поле в RejectedOperation, после
#        conflicting_marka:
#
#     # DAY_TAKEN: чей план занимает дату.
#     conflicting_employee: Optional[str] = None
#
# ШАГ 3. ЗАМЕНИТЬ функции _validate_operations и apply_changes на
#        версии ниже и добавить _load_mark_set_occupancy.
# ---------------------------------------------------------------------------


async def _load_mark_set_occupancy(
    db: AsyncSession,
    mark_set_ids: set[int],
    date_from: date,
    date_to: date,
) -> dict[tuple[int, date], tuple[int, int]]:
    """Кто занимает дни этих шифров: (шифр, дата) → (инженер, план).

    Отбор идёт по ШИФРУ, а не по инженеру — в этом вся суть. Плановая
    ячейка могла остаться за прежним исполнителем, и он не попадёт ни в
    одну выборку «по людям»: в сетке бюро его может не быть вовсе, если
    он перешёл в другое подразделение.

    На этой выборке держатся оба новых правила:
      - дата шифра планируется один раз, кем бы то ни было;
      - снять план может кто угодно, а не только текущий исполнитель.

    Второе следует из первого: раз ячейка на паре (шифр, дата) одна,
    цель удаления определяется однозначно, и передавать исполнителя в
    операции не нужно.
    """
    stmt = (
        select(
            WorkPlan.mark_set_id,
            PlanCalendarCell.work_date,
            PlanCalendarCell.employee_id,
            PlanCalendarCell.work_plan_id,
        )
        .join(WorkPlan, WorkPlan.id == PlanCalendarCell.work_plan_id)
        .where(
            WorkPlan.mark_set_id.in_(mark_set_ids),
            PlanCalendarCell.work_date >= date_from,
            PlanCalendarCell.work_date <= date_to,
        )
    )
    rows = (await db.execute(stmt)).all()
    return {
        (mark_set_id, work_date): (employee_id, plan_id)
        for mark_set_id, work_date, employee_id, plan_id in rows
    }


def _validate_operations(
    ctx: AvailabilityContext,
    mark_sets: dict[int, MarkSetRow],
    occupancy: dict[tuple[int, date], tuple[int, int]],
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
    seen: set[tuple[int, date]] = set()

    # Что освобождают удаления этой же пачки. Два среза: по паре
    # «шифр + дата» — для правила «дата шифра одна», и по паре
    # «инженер + дата» — для проверки занятости человека.
    freed_on_set: set[tuple[int, date]] = set()
    freed_on_day: set[tuple[int, date]] = set()
    for operation in operations:
        if operation.op != "remove":
            continue
        key = (operation.mark_set_id, operation.work_date)
        occupant = occupancy.get(key)
        if occupant is not None:
            freed_on_set.add(key)
            freed_on_day.add((occupant[0], operation.work_date))

    for operation in operations:
        mark_set = mark_sets[operation.mark_set_id]
        set_key = (operation.mark_set_id, operation.work_date)
        # Повтор ловим по ТРОЙКЕ, включая вид операции: снять день у
        # одного и отдать его другому — это две операции на одну пару
        # «шифр + дата», и они законны. Противоречием остаётся только
        # буквальный дубль: два add или два remove подряд.
        dup_key = (operation.op, operation.mark_set_id, operation.work_date)
        if dup_key in seen:

        # Повтор пары «шифр + дата» в одной пачке. Клиент прислал либо
        # дубль, либо add и remove на одну ячейку — в обоих случаях его
        # состояние противоречиво, и угадывать намерение мы не станем.        
            rejections.append(
                RejectedOperation(
                    mark_set_id=operation.mark_set_id,
                    work_date=operation.work_date,
                    state=DayState.DAY_TAKEN,
                )
            )
            continue
        seen.add(dup_key)

        occupant = occupancy.get(set_key)
        occupied = occupant is not None and set_key not in freed_on_set

        if operation.op == "add":
            # Закрытый шифр (на проверке, выпущен) планировать нельзя.
            # Проверяется первым: на закрытом комплекте занятость дня уже
            # ничего не решает. Снятие (remove) разрешено при любом
            # статусе — им начальник освобождает инженеру будущие дни.
            if not set_allows_plan(mark_set.status_id):
                rejections.append(
                    RejectedOperation(
                        mark_set_id=operation.mark_set_id,
                        work_date=operation.work_date,
                        state=DayState.SET_CLOSED,
                        conflicting_marka=mark_set.marka_full,
                        status_name=(
                            status_names.get(mark_set.status_id)
                            if mark_set.status_id is not None
                            else None
                        ),
                    )
                )
                continue
            # Исполнитель выводится из шифра, а не из запроса: подмена
            # id не позволит запланировать работу чужому человеку.
            employee_id = mark_set.id_executor

            # Правило «дата шифра планируется один раз». Проверяется
            # раньше вердикта: две записи об одном дне работы означали
            # бы двойной счёт трудозатрат, и это важнее того, свободен
            # ли день у самого исполнителя.
            if occupied:
                owner = ctx.employees.get(occupant[0])
                rejections.append(
                    RejectedOperation(
                        mark_set_id=operation.mark_set_id,
                        work_date=operation.work_date,
                        state=DayState.DAY_TAKEN,
                        conflicting_marka=mark_set.marka_full,
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
                    mark_set_id=operation.mark_set_id,
                    work_date=operation.work_date,
                    state=verdict.state,
                    conflicting_marka=verdict.marka_full,
                    tab_letter=verdict.tab_letter,
                )
            )
            continue

        # op == "remove". Цель определяется парой «шифр + дата», а не
        # исполнителем: план мог остаться за прежним. Снятие ничего не
        # создаёт и никого не занимает, поэтому текущее назначение здесь
        # не проверяется вовсе.
        if occupant is None:
            rejections.append(
                RejectedOperation(
                    mark_set_id=operation.mark_set_id,
                    work_date=operation.work_date,
                    state=DayState.FREE,
                )
            )
            continue

        # День с фактом заперт: удаление плана под фактом искажает
        # сравнение сильнее, чем добавление. Факт сверяется у ВЛАДЕЛЬЦА
        # ячейки, а не у текущего исполнителя.
        owner_id = occupant[0]
        fact = ctx.fact_cells.get((owner_id, operation.work_date))
        if fact is not None and fact.mark_set_id == operation.mark_set_id:
            rejections.append(
                RejectedOperation(
                    mark_set_id=operation.mark_set_id,
                    work_date=operation.work_date,
                    state=DayState.HAS_FACT,
                    conflicting_marka=fact.marka_full,
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

    Единственное место, которое меняет данные планирования.

    Порядок: проверка шифров → занятость дат шифра → снимок по всем
    причастным людям → проверка операций → удаления → добавления с
    пересчётом шапок → commit → свежее состояние в ответ.

    Причастные — это исполнители шифров ПЛЮС владельцы существующих
    ячеек. Вторые могут быть кем угодно, включая людей из других
    подразделений: план остаётся за прежним исполнителем, когда шифр
    передают. Без них снимок был бы неполон, и проверка факта на
    удаляемом дне сработала бы вхолостую.

    Гонка между проверкой и вставкой возможна: два начальника могут
    планировать один шифр одновременно. Данные при этом в безопасности —
    unique_plan_cell не даст одному человеку два плана на день. Поймав
    23505, откатываемся и ПОВТОРНО ПРОВЕРЯЕМ операции на свежем снимке:
    повторять саму запись смысла нет, под правилом «всё или ничего»
    результат был бы тем же отказом, зато диагностика превращает
    «конфликт записи» в «17.09 занят шифром 053-22-14а-ЭМ».
    """
    if not operations:
        raise OperationsRejectedError([])

    mark_set_ids = {operation.mark_set_id for operation in operations}
    mark_sets = await _load_operation_mark_sets(db, scope, mark_set_ids)
    # Справочник статусов: для отказа SET_CLOSED и для строк ответа.
    status_names = await load_set_status_names(db)    

    # Границы снимка — только затронутые даты, обычно несколько дней.
    dates = [operation.work_date for operation in operations]
    date_from, date_to = min(dates), max(dates)

    occupancy = await _load_mark_set_occupancy(
        db, mark_set_ids, date_from, date_to
    )

    employee_ids = sorted(
        {mark_set.id_executor for mark_set in mark_sets.values()}
        | {owner for owner, _plan_id in occupancy.values()}
    )

    employees = await resolve_employees_by_ids(
        db, employee_ids, date_from, date_to
    )
    ctx = await load_context(
        db, employees=employees, date_from=date_from, date_to=date_to
    )

    rejections = _validate_operations(
        ctx, mark_sets, occupancy, operations, status_names
    )
    if rejections:
        raise OperationsRejectedError(rejections)

    # Группировка по плану: пара (шифр, инженер) — тот же ключ, что у
    # idx_one_active_plan. Для добавлений инженер берётся из шифра, для
    # удалений — из владельца ячейки: он может быть другим человеком.
    added: dict[tuple[int, int], list[date]] = {}
    removed: dict[tuple[int, int], list[date]] = {}
    for operation in operations:
        if operation.op == "add":
            key = (
                operation.mark_set_id,
                mark_sets[operation.mark_set_id].id_executor,
            )
            added.setdefault(key, []).append(operation.work_date)
        else:
            owner_id = occupancy[
                (operation.mark_set_id, operation.work_date)
            ][0]
            key = (operation.mark_set_id, owner_id)
            removed.setdefault(key, []).append(operation.work_date)

    try:
        # Сначала ВСЕ удаления по всем планам, только потом добавления.
        # Межплановый порядок обязателен: перенос дня между шифрами или
        # между людьми иначе упрётся в unique_plan_cell.
        for (mark_set_id, employee_id), work_dates in removed.items():
            plan = ctx.work_plans.get((mark_set_id, employee_id))
            if plan is None:
                continue
            await db.execute(
                delete(PlanCalendarCell).where(
                    PlanCalendarCell.work_plan_id == plan.id,
                    PlanCalendarCell.work_date.in_(work_dates),
                )
            )

        # Затронутые планы: и те, куда добавляли, и те, откуда убирали —
        # вторым тоже нужен пересчёт шапки.
        touched = set(added) | set(removed)
        for mark_set_id, employee_id in touched:
            plan = ctx.work_plans.get((mark_set_id, employee_id))
            await rebuild_plan(
                db,
                mark_set_id=mark_set_id,
                employee_id=employee_id,
                plan_id=plan.id if plan else None,
                added=added.get((mark_set_id, employee_id), []),
            )

        await db.commit()

    except IntegrityError:
        # Транзакция в аварийном состоянии: любой запрос до rollback
        # упадёт. Поэтому сначала откат, диагностика — уже после.
        await db.rollback()
        logger.info("Конфликт записи плановых ячеек, повторная проверка")

        fresh_occupancy = await _load_mark_set_occupancy(
            db, mark_set_ids, date_from, date_to
        )
        employees = await resolve_employees_by_ids(
            db, employee_ids, date_from, date_to
        )
        fresh_ctx = await load_context(
            db, employees=employees, date_from=date_from, date_to=date_to
        )
        raise OperationsRejectedError(
            _validate_operations(
                fresh_ctx, mark_sets, fresh_occupancy, operations, status_names
              )
        )

    # Свежее состояние затронутых инженеров — чтобы React перерисовал
    # без второго GET. Читаем после commit, отдельным снимком.
    employees = await resolve_employees_by_ids(
        db, employee_ids, date_from, date_to
    )
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


async def rebuild_plan(
    db: AsyncSession,
    *,
    mark_set_id: int,
    employee_id: int,
    plan_id: Optional[int],
    added: list[date],
) -> None:
    """Досоздаёт ячейки одного плана и пересчитывает его шапку.

    Удаления к этому моменту уже выполнены вызывающей стороной — они
    обязаны пройти по ВСЕМ планам раньше любой вставки. Причина: перенос
    дня с шифра на шифр в пределах одной даты обычен, и положи мы новую
    ячейку до удаления старой, INSERT упёрся бы в unique_plan_cell.

    Порядок внутри функции:

    1. Создание шапки, если плана ещё нет. Пустую шапку завести нельзя:
       start_date объявлен NOT NULL, а CHECK требует work_days > 0.
       Поэтому границы считаются до вставки, пусть и предварительно.
    2. Вставка ячеек пачкой.
    3. Пересчёт границ ОДНИМ запросом, после всех вставок.

    Пункт 3 именно последний, пока жив триггер trg_update_end_date: он
    пересчитывает end_date на каждой вставленной строке и затёр бы
    значение, записанное раньше. После снятия триггера порядок
    останется верным.

    updated_at ставим здесь же: триггер писал его сам, и после дропа
    поле осталось бы осиротевшим — onupdate на стороне ORM в проекте не
    используется.
    """
    # 1. Шапка плана. Границы предварительные — шаг 3 их уточнит.
    if added and plan_id is None:
        result = await db.execute(
            insert(WorkPlan)
            .values(
                mark_set_id=mark_set_id,
                employee_id=employee_id,
                start_date=min(added),
                work_days=len(added),
                status=PLAN_STATUS_ACTIVE,
            )
            .returning(WorkPlan.id)
        )
        plan_id = result.scalar_one()

    # 2. Вставка ячеек. employee_id дублируется в ячейке при живом
    # work_plan.employee_id намеренно: без него не построить
    # unique_plan_cell (UNIQUE через join не умеет).
    if added:
        await db.execute(
            insert(PlanCalendarCell),
            [
                {
                    "work_plan_id": plan_id,
                    "employee_id": employee_id,
                    "work_date": work_date,
                }
                for work_date in added
            ],
        )

    if plan_id is None:
        # Ни добавлений, ни существующего плана — делать нечего.
        return

    # 3. Пересчёт шапки по фактическому составу ячеек. Считаем в БД, а
    # не в памяти: ячейки плана могут выходить за края интервала
    # операций, и набор из контекста был бы неполным.
    start_date, end_date, cell_count = (
        await db.execute(
            select(
                func.min(PlanCalendarCell.work_date),
                func.max(PlanCalendarCell.work_date),
                func.count(PlanCalendarCell.id),
            ).where(PlanCalendarCell.work_plan_id == plan_id)
        )
    ).one()

    if cell_count == 0:
        # Плана с нулём дней не бывает: CHECK (work_days > 0). Убрали
        # последний день — удаляем шапку. Архивировать нечего: у пустой
        # записи остались бы work_days от прежней раскладки, то есть
        # цифра, которая соврёт в любом отчёте.
        await db.execute(delete(WorkPlan).where(WorkPlan.id == plan_id))
        return

    await db.execute(
        update(WorkPlan)
        .where(WorkPlan.id == plan_id)
        .values(
            start_date=start_date,
            end_date=end_date,
            work_days=cell_count,
            updated_at=func.now(),
        )
    )


