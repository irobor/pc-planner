# app/api/v1/fact/service.py
"""Сервисный слой факта: лента инженера, поиск пакета, отметка, статус.

Здесь только работа с БД. Правила — «можно ли отметить день» и «что
делать с кликом» — живут в fact/rules.py и проверяются в консоли без
базы. Снимок данных и загрузчики переиспользуются из планирования:
тот же AvailabilityContext, собранный по одному инженеру.

Четыре сценария:
  * load_fact_window   — лента от якорного дня (чтение);
  * search_packages    — поиск пакета для «+ добавить пакет» (чтение);
  * set_mark           — отметка одного дня (запись);
  * set_package_status — исполнитель меняет статус своего пакета (запись).

Факт ставится на пакет работ, а не на комплект: строка ленты — пакет.

Тексты для пользователя здесь не собираются: исключения несут причину
и данные для подстановки, формулировки живут в routes.py.
"""

import logging
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
    package_allows_fact,
)
from app.api.v1.fact.schemas import (
    FactCellOut,
    FactDayOut,
    FactRowOut,
    FactWindow,
    MarkResult,
    PackageFound,
    PackageStatusOut,
    StatusResult,
)
from app.api.v1.planning.rules import AvailabilityContext, package_label
from app.api.v1.planning.service import (
    load_calendar,
    load_context,
    resolve_employees_by_ids,
)
from app.models.composition import (
    Department,
    PackageStatus,
    ProjectComposition,
    ProjectMarkSet,
    WorkPackage,
)
from app.models.planning import FactCalendarCell

logger = logging.getLogger(__name__)

# Лента: сколько дней показывать до и после якорного дня (по умолчанию
# якорь — сегодня) и на сколько сдвигают стрелки. Сегодня оказывается
# на трети ленты: две недели истории (не забыл ли отметиться, нет ли
# отставания) и четыре недели плана впереди. 43 дня — шире месяца, так
# что на стыке месяцев вчерашний день всегда на экране.
DAYS_BACK: int = 14
DAYS_FORWARD: int = 28
SHIFT_DAYS: int = 7

# Поиск пакета: минимальная длина строки (после обрезки пробелов) и
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


class InvalidWindowError(FactError):
    """Лента целиком вне заполненного производственного календаря."""

    def __init__(self, horizon_from: date, horizon_to: date) -> None:
        super().__init__("Лента вне производственного календаря")
        self.horizon_from = horizon_from
        self.horizon_to = horizon_to


class SearchTooShortError(FactError):
    """Строка поиска короче SEARCH_MIN_LENGTH после обрезки пробелов."""


class PackageNotFoundError(FactError):
    """Пакет с таким id не существует."""


class PackageClosedError(FactError):
    """По пакету факт не ставится: пакет приостановлен или комплект выпущен.

    Причина — в полях: released и название статуса. Текст собирает
    routes.py, сервис формулировок не знает.
    """

    def __init__(self, label: str, released: bool, status_name: str) -> None:
        super().__init__(label)
        self.label = label
        self.released = released
        self.status_name = status_name


class NotExecutorError(FactError):
    """Статус пакета меняет инженер, который не его исполнитель."""


class UnknownStatusError(FactError):
    """Статуса с таким id нет в справочнике."""


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


def window_bounds(anchor: date) -> tuple[date, date]:
    """Первый и последний день ленты (включительно) для якоря."""
    return (
        anchor - timedelta(days=DAYS_BACK),
        anchor + timedelta(days=DAYS_FORWARD),
    )


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
    «я исполнитель»: FACT_STATUS_IDS шире GRID_STATUS_IDS — включает
    готовые пакеты и стадии контроля и согласования.

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
    """Строки ленты: объединение трёх серверных источников.

    1. Инженер — исполнитель пакета в статусе FACT_STATUS_IDS (этот
       фильтр уже применил загрузчик пакетов планирования).
    2. У инженера есть плановая ячейка по пакету в интервале.
    3. У инженера есть факт по пакету в интервале.

    Четвёртый источник — ручное добавление через поиск — живёт в
    браузере до первой отметки, сервер о нём не знает.
    """
    package_ids: set[int] = {
        package.id
        for package in ctx.packages.values()
        if package.executor_id == employee_id
    }
    package_ids |= {
        cell.package_id
        for (emp_id, _day), cell in ctx.plan_cells.items()
        if emp_id == employee_id
    }
    package_ids |= {
        cell.package_id
        for (emp_id, _day), cell in ctx.fact_cells.items()
        if emp_id == employee_id
    }

    rows: list[FactRowOut] = []
    for package_id in package_ids:
        package = ctx.packages.get(package_id)
        if package is None:
            # Штатно невозможно: загрузчик пакетов дотягивает их из
            # ячеек. Пропуск безопаснее падения на отрисовке.
            logger.warning("Пакет %s отсутствует в контексте факта", package_id)
            continue
        rows.append(
            FactRowOut(
                package_id=package.id,
                label=package.label,
                status_id=package.status_id,
                is_executor=package.executor_id == employee_id,
                allows_fact=package_allows_fact(package.status_id, package.released),
            )
        )

    # Устойчивый порядок: без сортировки обход множества менял бы порядок
    # строк от запроса к запросу, и лента «прыгала» бы. Подпись
    # начинается с шифра — пакеты одного комплекта идут подряд.
    rows.sort(key=lambda row: row.label)
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
    """Непустые клетки: план и факт, сведённые по паре (пакет, день).

    В день у инженера не больше одной плановой и одной фактической
    ячейки. Если пакет один — это одна клетка с обоими флагами (факт по
    плану); если разные — две клетки (план на одном пакете, факт вне
    плана на другом).
    """
    flags: dict[tuple[int, date], list[bool]] = {}
    for (emp_id, day), plan in ctx.plan_cells.items():
        if emp_id == employee_id:
            flags.setdefault((plan.package_id, day), [False, False])[0] = True
    for (emp_id, day), fact in ctx.fact_cells.items():
        if emp_id == employee_id:
            flags.setdefault((fact.package_id, day), [False, False])[1] = True

    cells = [
        FactCellOut(
            package_id=package_id,
            work_date=day,
            has_plan=has_plan,
            has_fact=has_fact,
        )
        for (package_id, day), (has_plan, has_fact) in flags.items()
    ]
    cells.sort(key=lambda cell: (cell.work_date, cell.package_id))
    return cells


async def _load_package_statuses(db: AsyncSession) -> list[PackageStatusOut]:
    """Справочник статусов пакета по sort_order — шесть строк."""
    rows = (
        await db.execute(
            select(PackageStatus).order_by(PackageStatus.sort_order, PackageStatus.id)
        )
    ).scalars()
    return [
        PackageStatusOut(id=status.id, name=status.name, note=status.note)
        for status in rows
    ]


# ---------------------------------------------------------------------------
# Сценарий 1. Лента от якорного дня
# ---------------------------------------------------------------------------


async def load_fact_window(
    db: AsyncSession,
    *,
    employee_id: int,
    anchor: date,
    today: date,
) -> FactWindow:
    """Лента инженера: DAYS_BACK дней до якоря и DAYS_FORWARD после.

    Контекст грузится РОВНО на ленту — строки определяются только ею.
    Окно отметки считается отдельно, от today: инженер может сдвинуть
    ленту в прошлое, а открыты всё равно сегодня и предыдущий рабочий
    день.

    Якоря соседних положений (±SHIFT_DAYS) считаются здесь же: стрелка
    гаснет (None), если лента уже упёрлась в край календаря.

    Лента, частично выходящая за календарь, допустима: дни за краем
    получат out_of_horizon. Целиком за краем — InvalidWindowError.
    """
    date_from, date_to = window_bounds(anchor)
    ctx = await _load_employee_context(db, employee_id, date_from, date_to)

    if date_to < ctx.horizon_from or date_from > ctx.horizon_to:
        raise InvalidWindowError(ctx.horizon_from, ctx.horizon_to)

    window_from = await _load_window_from(db, today)
    shift = timedelta(days=SHIFT_DAYS)

    return FactWindow(
        anchor=anchor,
        # Назад — пока левый край ленты не дошёл до начала календаря;
        # вперёд — пока правый не дошёл до конца.
        prev_anchor=anchor - shift if date_from > ctx.horizon_from else None,
        next_anchor=anchor + shift if date_to < ctx.horizon_to else None,
        date_from=date_from,
        date_to=date_to,
        today=today,
        mark_window_from=window_from,
        horizon_from=ctx.horizon_from,
        horizon_to=ctx.horizon_to,
        rows=_collect_rows(ctx, employee_id),
        days=_collect_days(ctx, employee_id, today, window_from),
        cells=_collect_cells(ctx, employee_id),
        package_statuses=await _load_package_statuses(db),
    )


# ---------------------------------------------------------------------------
# Сценарий 2. Поиск пакета
# ---------------------------------------------------------------------------


def _escape_like(value: str) -> str:
    """Экранирует спецсимволы LIKE, чтобы они искались буквально.

    Без этого «_» в шифре (или в строке поиска) совпадал бы с любым
    символом, а «%» — с любой подстрокой.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def search_packages(
    db: AsyncSession,
    *,
    department_id: Optional[int],
    query: str,
    only_markable: bool = True,
) -> list[PackageFound]:
    """Пакеты по подстроке подписи — вся организация, ближние сначала.

    Ищется по видимой подписи «053-22-14а-ЭМ (освещение)»: «14а-ЭМ»
    найдёт все пакеты комплекта, «освещение» — пакеты с таким названием.
    Искать по тому, что человек видит, честнее, чем по отдельным полям.

    department_id — текущее подразделение инженера
    (scope.get_user_department_id). None допустим: тогда все результаты
    равноудалены и сортируются по подписи.

    Близость (proximity):
      0 — пакет его подразделения;
      1 — его отдел: родительский узел или соседнее бюро под тем же
          родителем; если сам инженер числится на уровне отдела — бюро
          внутри него;
      2 — остальная организация.

    only_markable — только пакеты, по которым можно отметить факт: не
    приостановленные и не из выпущенного комплекта. Инженеру остальные
    не нужны. False — все: администратор данных исправляет и их.
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

    # Подпись собирается в SQL тем же видом, что package_label в Python:
    # «шифр (пакет)». Искать по ней — значит искать по тому, что видно.
    label = func.concat(ProjectMarkSet.marka_full, " (", WorkPackage.name, ")")
    conditions: list[ColumnElement[bool]] = [
        label.ilike(f"%{_escape_like(text_query)}%", escape="\\"),
    ]
    if only_markable:
        conditions += [
            WorkPackage.status_id.in_(FACT_STATUS_IDS),
            ProjectMarkSet.released_on.is_(None),
        ]

    stmt = (
        select(
            WorkPackage.id,
            ProjectMarkSet.marka_full,
            WorkPackage.name,
            Department.code,
            Department.name,
            proximity.label("proximity"),
        )
        .join(ProjectMarkSet, ProjectMarkSet.id == WorkPackage.mark_set_id)
        .join(
            ProjectComposition,
            ProjectComposition.id == ProjectMarkSet.composition_id,
        )
        .join(Department, Department.id == ProjectComposition.department_id)
        .where(*conditions)
        .order_by(proximity, ProjectMarkSet.marka_full, WorkPackage.name)
        .limit(SEARCH_LIMIT)
    )
    rows = (await db.execute(stmt)).all()
    return [
        PackageFound(
            id=package_id,
            label=package_label(marka_full, package_name),
            department_code=code,
            department_name=name,
            proximity=level,
        )
        for package_id, marka_full, package_name, code, name, level in rows
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
    package_id: int,
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

    rows = {row.package_id: row for row in _collect_rows(ctx, employee_id)}
    row = rows.get(package_id)
    if row is None:
        # После успешной отметки пакет обязан прийти через источник
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


async def _load_package_info(
    db: AsyncSession, package_id: int
) -> tuple[WorkPackage, str, bool, str]:
    """Пакет, его подпись, признак выпуска комплекта и название статуса.

    Одним запросом: всё это нужно и отметке (допуск факта и текст
    отказа), и смене статуса (строка ответа). Значения копируются из
    результата сразу — после commit/rollback ORM-объект «протухает».
    """
    row = (
        await db.execute(
            select(
                WorkPackage,
                ProjectMarkSet.marka_full,
                ProjectMarkSet.released_on,
                PackageStatus.name,
            )
            .join(ProjectMarkSet, ProjectMarkSet.id == WorkPackage.mark_set_id)
            .join(PackageStatus, PackageStatus.id == WorkPackage.status_id)
            .where(WorkPackage.id == package_id)
        )
    ).one_or_none()
    if row is None:
        raise PackageNotFoundError(str(package_id))
    package, marka_full, released_on, status_name = row
    return (
        package,
        package_label(marka_full, package.name),
        released_on is not None,
        status_name,
    )


async def set_mark(
    db: AsyncSession,
    *,
    employee_id: int,
    package_id: int,
    work_date: date,
    today: date,
) -> MarkResult:
    """Отметка одного дня: создать, перенести или ничего (свой же пакет).

    Порядок внутри транзакции важен:

    1. Advisory-блокировка по инженеру — ПЕРВЫМ действием, до любой
       записи. Те же ключи берут оба триггера, поэтому, пока мы держим
       блокировку, ни неявка, ни другая отметка этого инженера не
       вклинятся между проверкой и записью. Обратный порядок (сначала
       строка, потом блокировка) мог бы дать взаимную блокировку с
       табельщицей.
    2. Пакет существует и допускает факт: не приостановлен, комплект не
       выпущен.
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

    try:
        package, label, released, status_name = await _load_package_info(db, package_id)
    except PackageNotFoundError:
        await db.rollback()
        raise
    if not package_allows_fact(package.status_id, released):
        await db.rollback()
        raise PackageClosedError(label, released, status_name)

    ctx = await _load_employee_context(db, employee_id, work_date, work_date)
    window_from = await _load_window_from(db, today)
    verdict = fact_day_verdict(ctx, employee_id, work_date, today, window_from)
    action = decide_mark(verdict, package_id)

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
            package_id=package_id,
            work_date=work_date,
            today=today,
            action=action,
        )

    try:
        if action is MarkAction.INSERT:
            await db.execute(
                insert(FactCalendarCell).values(
                    package_id=package_id,
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
                    .values(package_id=package_id)
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
        package_id=package_id,
        work_date=work_date,
        today=today,
        action=action,
    )


# ---------------------------------------------------------------------------
# Сценарий 4. Статус своего пакета
# ---------------------------------------------------------------------------


async def set_package_status(
    db: AsyncSession,
    *,
    employee_id: int,
    package_id: int,
    status_id: int,
) -> StatusResult:
    """Исполнитель меняет статус своего пакета.

    Статус пакета ставит исполнитель, включая стадии согласования:
    комплект ушёл на согласование — каждый переводит свой пакет; вернули
    на доработку — обратно «В работе». Начальник может сделать то же в
    «Бюро» — последнее сохранение побеждает, как при любой правке.

    Только исполнитель: сверяется с текущим назначением пакета, а не с
    тем, что пришло в запросе. Порядок статусов свободный — ограничение
    переходов мешало бы исправлять ошибки.

    Ответ — строка целиком: вместе со статусом может измениться допуск
    факта («Приостановлен» закрывает клетки).
    """
    package, label, released, _old_status = await _load_package_info(db, package_id)

    if package.executor_id != employee_id:
        raise NotExecutorError(label)

    known = (
        await db.execute(select(PackageStatus.id).where(PackageStatus.id == status_id))
    ).first()
    if known is None:
        raise UnknownStatusError(str(status_id))

    package.status_id = status_id
    await db.commit()

    return StatusResult(
        row=FactRowOut(
            package_id=package_id,
            label=label,
            status_id=status_id,
            is_executor=True,
            allows_fact=package_allows_fact(status_id, released),
        )
    )
