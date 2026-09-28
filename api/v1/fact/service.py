# app/api/v1/fact/service.py
"""Сервисный слой факта: матрица инженера, поиск шифра, отметка дня.

Здесь только работа с БД. Правила — «можно ли отметить день» и «что
делать с кликом» — живут в fact/rules.py и проверяются в консоли без
базы. Снимок данных и загрузчики переиспользуются из планирования:
тот же AvailabilityContext, собранный по одному инженеру.

Три сценария:
  * load_fact_month  — матрица на месяц (чтение);
  * search_mark_sets — поиск шифра для «+ добавить шифр» (чтение);
  * set_mark         — отметка одного дня (запись).

Тексты для пользователя здесь не собираются: исключения несут причину
и данные для подстановки, формулировки живут в routes.py.
"""

import logging
from calendar import monthrange
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import case, func, insert, literal, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.api.v1.fact.rules import (
    CALENDAR_LOOKBACK_DAYS,
    FACT_STATUS_IDS,
    FactVerdict,
    MarkAction,
    decide_mark,
    fact_day_verdict,
    fact_window_start,
    set_allows_fact,
)
from app.api.v1.fact.schemas import (
    FactCellOut,
    FactDayOut,
    FactMonth,
    FactRowOut,
    MarkResult,
    MarkSetFound,
)
from app.api.v1.planning.rules import AvailabilityContext
from app.api.v1.planning.service import (
    load_calendar,
    load_context,
    resolve_employees_by_ids,
)
from app.models.composition import Department, ProjectComposition, ProjectMarkSet
from app.models.planning import FactCalendarCell

logger = logging.getLogger(__name__)

# Поиск шифра: минимальная длина строки (после обрезки пробелов) и
# предел выдачи. Короткая строка дала бы сотни совпадений по общему
# префиксу проекта — искать имеет смысл от номера проекта и глубже.
SEARCH_MIN_LENGTH: int = 5
SEARCH_LIMIT: int = 20

# Пространство advisory-блокировок факта. ОБЯЗАНО совпадать с константой
# в функциях триггеров trg_absence_displaces_fact и trg_fact_check_absence
# (миграция схемы факта): все, кто меняет факт или неявку инженера,
# встают в одну очередь по ключу (7301, employee_id).
FACT_LOCK_NAMESPACE: int = 7301

# SQLSTATE, по которым отличаем ожидаемые отказы БД от настоящих ошибок.
# UF001 — свой код триггера trg_fact_check_absence: день закрыт табелем.
SQLSTATE_DAY_CLOSED: str = "UF001"
# 23505 — unique_violation: вторая отметка на тот же день
# (unique_fact_cell), например из соседней вкладки.
SQLSTATE_UNIQUE: str = "23505"


# ---------------------------------------------------------------------------
# Доменные исключения. Роут переводит их в HTTP-коды и собирает тексты.
# ---------------------------------------------------------------------------


class FactError(Exception):
    """Базовое исключение сервиса факта."""


class InvalidMonthError(FactError):
    """Месяц целиком вне заполненного производственного календаря."""

    def __init__(self, horizon_from: date, horizon_to: date) -> None:
        super().__init__("Месяц вне производственного календаря")
        self.horizon_from = horizon_from
        self.horizon_to = horizon_to


class SearchTooShortError(FactError):
    """Строка поиска короче SEARCH_MIN_LENGTH после обрезки пробелов."""


class MarkSetNotFoundError(FactError):
    """Шифр с таким id не существует."""


class MarkSetClosedError(FactError):
    """Шифр в статусе, по которому факт не ставится (выпущен)."""

    def __init__(self, marka_full: str) -> None:
        super().__init__(marka_full)
        self.marka_full = marka_full


class DayRejectedError(FactError):
    """День не открыт для отметки. Причина — в verdict.state."""

    def __init__(self, work_date: date, verdict: FactVerdict) -> None:
        super().__init__(verdict.state.value)
        self.work_date = work_date
        self.verdict = verdict


class ConcurrentChangeError(FactError):
    """Данные дня изменились параллельно, и день при этом всё ещё открыт.

    Штатно почти невозможен (см. set_mark): оставлен, чтобы редкая гонка
    давала понятный ответ «обновите», а не 500.
    """

    def __init__(self, work_date: date) -> None:
        super().__init__(work_date.isoformat())
        self.work_date = work_date


# ---------------------------------------------------------------------------
# Загрузка
# ---------------------------------------------------------------------------


def month_bounds(year: int, month: int) -> tuple[date, date]:
    """Первый и последний день месяца (включительно)."""
    last_day = monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


async def _load_window_from(db: AsyncSession, today: date) -> date:
    """Начало окна отметки — отдельным запросом календаря вокруг today.

    Окно зависит от сегодняшнего дня, а не от месяца на экране: инженер
    может листать август, а открыты всё равно «сегодня и предыдущий
    рабочий день». Поэтому календарь грузится не расширением контекста
    месяца (тогда в матрицу попали бы строки из чужого месяца), а
    маленьким запросом на CALENDAR_LOOKBACK_DAYS дней назад.
    """
    calendar = await load_calendar(
        db, today - timedelta(days=CALENDAR_LOOKBACK_DAYS), today
    )
    return fact_window_start(calendar, today)


async def _load_employee_context(
    db: AsyncSession,
    employee_id: int,
    date_from: date,
    date_to: date,
) -> AvailabilityContext:
    """Снимок доступности по одному инженеру — тот же, что у сетки.

    Отличие от сетки начальника — только в наборе статусов для строк
    «я исполнитель»: FACT_STATUS_IDS шире GRID_STATUS_IDS (включает
    «На проверке»).

    Если у инженера нет назначения, пересекающего интервал, employees
    окажется пустым — и все дни получат вердикт not_assigned. Это
    честный ответ, а не ошибка.
    """
    employees = await resolve_employees_by_ids(db, [employee_id], date_from, date_to)
    return await load_context(
        db,
        employees=employees,
        date_from=date_from,
        date_to=date_to,
        executor_status_ids=FACT_STATUS_IDS,
    )


# ---------------------------------------------------------------------------
# Сборка ответа из снимка — чистые функции, без БД.
# ---------------------------------------------------------------------------


def _collect_rows(ctx: AvailabilityContext, employee_id: int) -> list[FactRowOut]:
    """Строки матрицы: объединение трёх серверных источников.

    1. Инженер — исполнитель шифра в статусе FACT_STATUS_IDS (этот
       фильтр уже применил _load_mark_sets).
    2. У инженера есть плановая ячейка по шифру в интервале.
    3. У инженера есть факт по шифру в интервале.

    Четвёртый источник — ручное добавление через поиск — живёт в
    браузере до первой отметки, сервер о нём не знает.
    """
    mark_set_ids: set[int] = {
        mark_set.id
        for mark_set in ctx.mark_sets.values()
        if mark_set.id_executor == employee_id
    }
    mark_set_ids |= {
        cell.mark_set_id
        for (emp_id, _day), cell in ctx.plan_cells.items()
        if emp_id == employee_id
    }
    mark_set_ids |= {
        cell.mark_set_id
        for (emp_id, _day), cell in ctx.fact_cells.items()
        if emp_id == employee_id
    }

    rows: list[FactRowOut] = []
    for mark_set_id in mark_set_ids:
        mark_set = ctx.mark_sets.get(mark_set_id)
        # status_id в MarkSetRow объявлен Optional, хотя в БД NOT NULL;
        # проверка сужает тип для mypy и страхует от неполных данных.
        if mark_set is None or mark_set.status_id is None:
            # Штатно невозможно: _load_mark_sets дотягивает шифры из
            # ячеек. Пропуск безопаснее падения на отрисовке.
            logger.warning("Шифр %s отсутствует в контексте факта", mark_set_id)
            continue
        rows.append(
            FactRowOut(
                mark_set_id=mark_set.id,
                marka_full=mark_set.marka_full,
                status_id=mark_set.status_id,
                is_executor=mark_set.id_executor == employee_id,
                allows_fact=set_allows_fact(mark_set.status_id),
            )
        )

    # Устойчивый порядок: без сортировки обход множества менял бы порядок
    # строк от запроса к запросу, и матрица «прыгала» бы.
    rows.sort(key=lambda row: row.marka_full)
    return rows


def _collect_days(
    ctx: AvailabilityContext,
    employee_id: int,
    today: date,
    window_from: date,
) -> list[FactDayOut]:
    """Вердикт каждого дня интервала — из fact_day_verdict, и только оттуда."""
    days: list[FactDayOut] = []
    day = ctx.date_from
    while day <= ctx.date_to:
        verdict = fact_day_verdict(ctx, employee_id, day, today, window_from)
        days.append(
            FactDayOut(
                work_date=day,
                state=verdict.state,
                tab_letter=verdict.tab_letter,
                day_type_code=verdict.day_type_code,
            )
        )
        day += timedelta(days=1)
    return days


def _collect_cells(ctx: AvailabilityContext, employee_id: int) -> list[FactCellOut]:
    """Непустые клетки: план и факт, сведённые по паре (шифр, день).

    В день у инженера не больше одной плановой и одной фактической
    ячейки. Если шифр один — это одна клетка с обоими флагами (факт по
    плану); если разные — две клетки (план на одном шифре, факт вне
    плана на другом).
    """
    flags: dict[tuple[int, date], list[bool]] = {}
    for (emp_id, day), plan in ctx.plan_cells.items():
        if emp_id == employee_id:
            flags.setdefault((plan.mark_set_id, day), [False, False])[0] = True
    for (emp_id, day), fact in ctx.fact_cells.items():
        if emp_id == employee_id:
            flags.setdefault((fact.mark_set_id, day), [False, False])[1] = True

    cells = [
        FactCellOut(
            mark_set_id=mark_set_id,
            work_date=day,
            has_plan=has_plan,
            has_fact=has_fact,
        )
        for (mark_set_id, day), (has_plan, has_fact) in flags.items()
    ]
    cells.sort(key=lambda cell: (cell.work_date, cell.mark_set_id))
    return cells


# ---------------------------------------------------------------------------
# Сценарий 1. Матрица на месяц
# ---------------------------------------------------------------------------


async def load_fact_month(
    db: AsyncSession,
    *,
    employee_id: int,
    year: int,
    month: int,
    today: date,
) -> FactMonth:
    """Матрица инженера на месяц: строки, вердикты дней, клетки.

    Контекст грузится РОВНО на месяц — так строки определяются только
    этим месяцем. Окно отметки считается отдельно, от today.

    Месяц, частично выходящий за календарь, допустим: дни за краем
    получат out_of_horizon. Целиком за краем — InvalidMonthError.
    """
    date_from, date_to = month_bounds(year, month)
    ctx = await _load_employee_context(db, employee_id, date_from, date_to)

    if date_to < ctx.horizon_from or date_from > ctx.horizon_to:
        raise InvalidMonthError(ctx.horizon_from, ctx.horizon_to)

    window_from = await _load_window_from(db, today)

    return FactMonth(
        year=year,
        month=month,
        date_from=date_from,
        date_to=date_to,
        today=today,
        window_from=window_from,
        horizon_from=ctx.horizon_from,
        horizon_to=ctx.horizon_to,
        rows=_collect_rows(ctx, employee_id),
        days=_collect_days(ctx, employee_id, today, window_from),
        cells=_collect_cells(ctx, employee_id),
    )


# ---------------------------------------------------------------------------
# Сценарий 2. Поиск шифра
# ---------------------------------------------------------------------------


def _escape_like(value: str) -> str:
    """Экранирует спецсимволы LIKE, чтобы они искались буквально.

    Без этого «_» в шифре (или в строке поиска) совпадал бы с любым
    символом, а «%» — с любой подстрокой.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def search_mark_sets(
    db: AsyncSession,
    *,
    department_id: Optional[int],
    query: str,
) -> list[MarkSetFound]:
    """Шифры по подстроке — вся организация, ближние сначала.

    department_id — текущее подразделение инженера
    (scope.get_user_department_id). None допустим: тогда все результаты
    равноудалены и сортируются по шифру.

    Близость (proximity):
      0 — шифр его подразделения;
      1 — его отдел: родительский узел или соседнее бюро под тем же
          родителем; если сам инженер числится на уровне отдела — бюро
          внутри него;
      2 — остальная организация.
    """
    text_query = query.strip()
    if len(text_query) < SEARCH_MIN_LENGTH:
        raise SearchTooShortError(text_query)

    # Родитель подразделения инженера — отдельным крошечным запросом:
    # условия близости от этого значения строятся по-разному, и в Python
    # это читается проще, чем скалярный подзапрос внутри CASE.
    parent_id: Optional[int] = None
    if department_id is not None:
        parent_id = (
            await db.execute(
                select(Department.parent_id).where(Department.id == department_id)
            )
        ).scalar_one_or_none()

    proximity: ColumnElement[int]
    if department_id is None:
        proximity = literal(2)
    else:
        # Условия «свой отдел»: бюро внутри моего узла всегда; если у
        # моего узла есть родитель — сам родитель и соседние бюро.
        near = [Department.parent_id == department_id]
        if parent_id is not None:
            near += [Department.id == parent_id, Department.parent_id == parent_id]
        proximity = case(
            (Department.id == department_id, 0),
            *[(condition, 1) for condition in near],
            else_=2,
        )

    stmt = (
        select(
            ProjectMarkSet.id,
            ProjectMarkSet.marka_full,
            Department.code,
            Department.name,
            proximity.label("proximity"),
        )
        .join(
            ProjectComposition,
            ProjectComposition.id == ProjectMarkSet.composition_id,
        )
        .join(Department, Department.id == ProjectComposition.department_id)
        .where(
            ProjectMarkSet.marka_full.ilike(
                f"%{_escape_like(text_query)}%", escape="\\"
            ),
            ProjectMarkSet.status_id.in_(FACT_STATUS_IDS),
        )
        .order_by(proximity, ProjectMarkSet.marka_full)
        .limit(SEARCH_LIMIT)
    )
    rows = (await db.execute(stmt)).all()
    return [
        MarkSetFound(
            id=mark_set_id,
            marka_full=marka_full,
            department_code=code,
            department_name=name,
            proximity=level,
        )
        for mark_set_id, marka_full, code, name, level in rows
    ]


# ---------------------------------------------------------------------------
# Сценарий 3. Отметка дня
# ---------------------------------------------------------------------------


def _sqlstate(exc: DBAPIError) -> Optional[str]:
    """SQLSTATE исходной ошибки драйвера.

    Адаптер asyncpg в SQLAlchemy кладёт код в .sqlstate (и дублирует в
    .pgcode); на случай другой версии смотрим и исходное исключение
    asyncpg в __cause__.
    """
    for source in (exc.orig, getattr(exc.orig, "__cause__", None)):
        code = getattr(source, "sqlstate", None) or getattr(source, "pgcode", None)
        if code:
            return str(code)
    return None


async def _day_result(
    db: AsyncSession,
    *,
    employee_id: int,
    mark_set_id: int,
    work_date: date,
    today: date,
    action: MarkAction,
) -> MarkResult:
    """Свежее состояние дня после отметки — ответ без повторного GET месяца.

    Отдельный снимок на один день: при переносе меняются две клетки
    (старая теряет факт, новая получает), поэтому отдаётся весь столбец.
    """
    ctx = await _load_employee_context(db, employee_id, work_date, work_date)
    window_from = await _load_window_from(db, today)

    rows = {row.mark_set_id: row for row in _collect_rows(ctx, employee_id)}
    row = rows.get(mark_set_id)
    if row is None:
        # После успешной отметки шифр обязан прийти через источник
        # «есть факт». Если нет — данные поменялись между commit и
        # чтением; честнее попросить обновить, чем отдать неполный ответ.
        raise ConcurrentChangeError(work_date)

    return MarkResult(
        action=action,
        day=_collect_days(ctx, employee_id, today, window_from)[0],
        cells=_collect_cells(ctx, employee_id),
        row=row,
    )


async def _fresh_verdict(
    db: AsyncSession,
    *,
    employee_id: int,
    work_date: date,
    today: date,
) -> FactVerdict:
    """Вердикт дня по свежим данным — для разбора отказа БД после rollback."""
    ctx = await _load_employee_context(db, employee_id, work_date, work_date)
    window_from = await _load_window_from(db, today)
    return fact_day_verdict(ctx, employee_id, work_date, today, window_from)


async def set_mark(
    db: AsyncSession,
    *,
    employee_id: int,
    mark_set_id: int,
    work_date: date,
    today: date,
) -> MarkResult:
    """Отметка одного дня: создать, перенести или ничего (свой же шифр).

    Порядок внутри транзакции важен:

    1. Advisory-блокировка по инженеру — ПЕРВЫМ действием, до любой
       записи. Те же ключи берут оба триггера, поэтому, пока мы держим
       блокировку, ни неявка, ни другая отметка этого инженера не
       вклинятся между проверкой и записью. Обратный порядок (сначала
       строка, потом блокировка) мог бы дать взаимную блокировку с
       табельщицей.
    2. Шифр существует и допускает факт.
    3. Вердикт дня по снимку, решение decide_mark.
    4. INSERT или UPDATE. Перенос — именно UPDATE: DELETE + INSERT
       каскадом стёр бы связи в fact_kanban_cells.

    is_retro не передаётся: его вычисляет триггер trg_fact_check_absence.

    Благодаря блокировке гонки с неявкой и второй вкладкой штатно
    невозможны; их обработка ниже — страховка от писателей, которые
    блокировку не берут (например, ручной DELETE в pgAdmin).
    """
    await db.execute(
        select(func.pg_advisory_xact_lock(FACT_LOCK_NAMESPACE, employee_id))
    )

    # Значения шифра копируются в локальные переменные сразу: после
    # commit/rollback ORM-объект «протухает», и обращение к его полям в
    # async-сессии упало бы с MissingGreenlet.
    mark_set = await db.get(ProjectMarkSet, mark_set_id)
    if mark_set is None:
        await db.rollback()
        raise MarkSetNotFoundError(str(mark_set_id))
    marka_full = mark_set.marka_full
    if not set_allows_fact(mark_set.status_id):
        await db.rollback()
        raise MarkSetClosedError(marka_full)

    ctx = await _load_employee_context(db, employee_id, work_date, work_date)
    window_from = await _load_window_from(db, today)
    verdict = fact_day_verdict(ctx, employee_id, work_date, today, window_from)
    action = decide_mark(verdict, mark_set_id)

    if action is MarkAction.REJECT:
        # rollback освобождает advisory-блокировку (она транзакционная).
        await db.rollback()
        raise DayRejectedError(work_date, verdict)

    if action is MarkAction.NOOP:
        # Клик по собственной отметке: писать нечего.
        await db.rollback()
        return await _day_result(
            db,
            employee_id=employee_id,
            mark_set_id=mark_set_id,
            work_date=work_date,
            today=today,
            action=action,
        )

    try:
        if action is MarkAction.INSERT:
            await db.execute(
                insert(FactCalendarCell).values(
                    mark_set_id=mark_set_id,
                    employee_id=employee_id,
                    work_date=work_date,
                )
            )
        else:
            # MOVE. Условие по (employee_id, work_date) — это ключ
            # unique_fact_cell, строка ровно одна.
            # RETURNING вместо rowcount: пустой результат означает, что
            # строки не оказалось.
            moved = (
                await db.execute(
                    update(FactCalendarCell)
                    .where(
                        FactCalendarCell.employee_id == employee_id,
                        FactCalendarCell.work_date == work_date,
                    )
                    .values(mark_set_id=mark_set_id)
                    .returning(FactCalendarCell.id)
                )
            ).scalar_one_or_none()
            if moved is None:
                # Отметку удалили мимо блокировки между чтением и записью.
                await db.rollback()
                raise ConcurrentChangeError(work_date)
        await db.commit()

    except DBAPIError as exc:
        # Транзакция в аварийном состоянии: сначала откат, разбор после.
        await db.rollback()
        code = _sqlstate(exc)
        if code not in (SQLSTATE_DAY_CLOSED, SQLSTATE_UNIQUE):
            raise
        logger.info("Отказ БД при отметке факта: %s, %s", code, work_date)
        fresh = await _fresh_verdict(
            db, employee_id=employee_id, work_date=work_date, today=today
        )
        if not fresh.can_mark:
            # Типичный случай UF001: табельщица успела внести неявку.
            raise DayRejectedError(work_date, fresh) from exc
        raise ConcurrentChangeError(work_date) from exc

    return await _day_result(
        db,
        employee_id=employee_id,
        mark_set_id=mark_set_id,
        work_date=work_date,
        today=today,
        action=action,
    )