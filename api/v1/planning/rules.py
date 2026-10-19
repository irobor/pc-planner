# app/api/v1/planning/rules.py
"""Правила доступности рабочего дня — ядро планирования.

Единственное место, где записано, можно ли поставить план на день
конкретного инженера. И сетка загрузки (отрисовка), и запись изменений
(проверка) спрашивают ответ здесь. Если бы каждая считала сама, они
однажды разошлись бы — и разошлись бы тихо.

Модуль намеренно чистый: ни обращений к БД, ни async, ни Pydantic.
На входе — снимок данных (AvailabilityContext), на выходе — вердикт.
Следствие: правила проверяются в консоли словарями, без поднятия базы
и браузера.

Текстов сообщений здесь НЕТ. Вердикт несёт состояние и данные для
подстановки (шифр-конкурент, буква табеля, край календаря), а
формулировки собираются на границе — в routes.py. Иначе правку
формулировки пришлось бы вносить в ядро.

Структуры контекста лежат здесь же, а не в service.py: service их
наполняет из БД и потому импортирует этот модуль, обратной зависимости
быть не должно.
"""
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Optional

# Статусы шифра (set_statuses.id), в которые можно ДОБАВЛЯТЬ план:
# 1 — В плане, 2 — В работе.
# «На проверке» (3): документ у проверяющего, работы у разработчика нет;
# вернут на доработку — статус снова станет «В работе».
# «Выпущен» (4): комплект закрыт.
# Снимать план можно при любом статусе — так начальник освобождает
# инженеру будущие дни после выпуска.
# Эта же константа задаёт строки «я исполнитель» в сетке
# (service.GRID_STATUS_IDS): шифр, который нельзя планировать, незачем
# подтягивать в сетку как рабочий.
PLAN_STATUS_IDS: tuple[int, ...] = (1, 2)

class DayState(str, Enum):
    """Состояние дня инженера с точки зрения планирования.

    Наследование от str — чтобы значение без преобразований уходило в
    JSON и читалось в логах.

    Свободен только FREE. Остальные шесть — причины, по которым день
    занять нельзя; порядок их проверки задан в day_verdict.
    """

    # Даты нет в production_calendar — уехали за край заполненного
    # календаря. Технически это привело бы к ошибке FK (23503).
    OUT_OF_HORIZON = "out_of_horizon"

    # Инженер в этот день не числился в бюро (границы
    # employee_assignments). Остальные вопросы к нему не относятся.
    NOT_ASSIGNED = "not_assigned"

    # Выходной или праздник: day_types.is_working = false.
    NONWORK = "nonwork"

    # Отпуск, больничный, командировка с blocks_planning = true.
    ABSENCE = "absence"

    # День отработан — есть фактическая ячейка. План на таком дне не
    # правится: подгонка плана под факт обессмыслила бы сравнение.
    HAS_FACT = "has_fact"

    # День уже отдан другому шифру (плановая ячейка есть, факта нет).
    BUSY = "busy"

    # Свободен.
    FREE = "free"
    # Дата уже занята планом ЭТОГО шифра — возможно, за другим
    # человеком. Состояние только для отказа: day_verdict его не
    # выдаёт, потому что отвечает на вопрос про день инженера, а это
    # правило про день шифра.
    DAY_TAKEN = "day_taken"

    
    # Шифр в статусе вне PLAN_STATUS_IDS (на проверке, выпущен) —
    # добавлять план нельзя. Как и DAY_TAKEN, это не состояние дня, а
    # причина отказа операции: day_verdict его не выдаёт. Живёт в этом
    # перечислении, чтобы отказ шёл общим списком rejected и сетка
    # подсветила клетку без отдельной ветки кода.
    SET_CLOSED = "set_closed"


@dataclass(frozen=True, slots=True)
class DayInfo:
    """Строка производственного календаря."""

    # Мнемокод типа дня: WORK, WEEKEND, HOLIDAY, SHORT.
    day_type_code: str
    is_working: bool


@dataclass(frozen=True, slots=True)
class AbsenceInfo:
    """Неявка инженера, развёрнутая на конкретный день.

    В БД неявка хранится диапазоном (absence_periods) — непрерывным, без
    дыр, поэтому разворот на дни делается в памяти при сборке контекста.
    """

    absence_type_id: int
    # Буква табеля (Б, ОТ, К) — выводится в ячейке сетки.
    tab_letter: str
    # Только этот флаг решает, закрыт ли день для планирования.
    # Командировка может оставаться планируемой.
    blocks_planning: bool


@dataclass(frozen=True, slots=True)
class PlanCellInfo:
    """Плановая ячейка: день инженера, отданный под шифр."""

    work_plan_id: int
    mark_set_id: int
    marka_full: str


@dataclass(frozen=True, slots=True)
class FactCellInfo:
    """Фактическая ячейка: отметка инженера о работе за день.

    На план не ссылается: факт возможен там, где плана не было вовсе —
    «факт вне плана» в сетке.
    """

    mark_set_id: int
    marka_full: str


@dataclass(frozen=True, slots=True)
class WorkPlanInfo:
    """Шапка плана: границы, посчитанные из ячеек.

    Ключ хранения — пара (mark_set_id, employee_id), тот же состав, что
    у idx_one_active_plan после его замены. Один шифр может пройти через
    нескольких исполнителей последовательно, и у каждого будет свой план.

    work_days — объём плана целиком, а не число дней внутри интервала:
    план может выходить за края запрошенного окна, и обрезанная цифра
    ввела бы в заблуждение.
    """

    id: int
    mark_set_id: int
    employee_id: int
    start_date: date
    end_date: Optional[date]
    work_days: int
    status: str


@dataclass(frozen=True, slots=True)
class EmployeeRow:
    """Инженер и границы его назначения в бюро.

    valid_from / valid_to нужны, чтобы человек был виден в сетке только
    в те дни, когда он действительно числился в этом бюро. valid_to =
    None — назначение открыто (работает сейчас).
    """

    id: int
    full_name: str
    department_id: int
    valid_from: date
    valid_to: Optional[date]

    def is_assigned_on(self, work_date: date) -> bool:
        """Числился ли инженер в бюро на эту дату (границы включительно)."""
        if work_date < self.valid_from:
            return False
        return self.valid_to is None or work_date <= self.valid_to


@dataclass(frozen=True, slots=True)
class MarkSetRow:
    """Шифр (комплект) — строка сетки под инженером."""

    id: int
    marka_full: str
    # Может быть None: шифр заведён, исполнитель не назначен. Такой шифр
    # спланировать нельзя — work_plan.employee_id объявлен NOT NULL.
    id_executor: Optional[int]
    composition_id: int
    status_id: Optional[int]


@dataclass(frozen=True, slots=True)
class DayVerdict:
    """Ответ на вопрос «что происходит у инженера в этот день».

    Кроме состояния несёт данные для подстановки в сообщение. Какие поля
    заполнены, зависит от state: шифр-конкурент — при BUSY и HAS_FACT,
    буква табеля — при ABSENCE, и так далее. Незаполненные остаются None.
    """

    state: DayState

    # BUSY / HAS_FACT: чем именно занят день.
    mark_set_id: Optional[int] = None
    marka_full: Optional[str] = None

    # BUSY: план, которому принадлежит ячейка. Нужен, чтобы при удалении
    # дня понять, какой work_plan пересчитывать.
    work_plan_id: Optional[int] = None

    # ABSENCE: буква табеля для сообщения и для отрисовки ячейки.
    tab_letter: Optional[str] = None

    # NONWORK: код типа дня (WEEKEND / HOLIDAY).
    day_type_code: Optional[str] = None

    @property
    def is_free(self) -> bool:
        """День доступен под новую плановую ячейку."""
        return self.state is DayState.FREE


@dataclass(frozen=True, slots=True)
class AvailabilityContext:
    """Снимок данных на интервал — всё, что нужно правилам.

    Наполняется одним заходом в БД (семь запросов, каждый на весь
    интервал), дальше используется только в памяти. Тот же приём, что в
    dept_tree.py и scope.py: справочник читается целиком, обход в памяти.
    Иначе проверка каждого дня била бы в базу отдельным запросом.

    Ключ (employee_id, work_date) в трёх словарях повторяет состав
    ограничения unique_plan_cell. Совпадение неслучайное: структура в
    памяти описывает то же правило, что и индекс в БД.

    Умолчаний у словарей нет намеренно. Забытая загрузка и «данных нет»
    выглядели бы одинаково и дали бы одинаковый вердикт FREE — то есть
    молчаливое разрешение планировать на занятый день. Пусть лучше
    падает на сборке контекста.
    """

    date_from: date
    date_to: date

    # Края заполненного production_calendar. Нужны, чтобы отказ звучал
    # как «календарь заполнен по 31.12.2026», а не как ошибка FK.
    horizon_from: date
    horizon_to: date

    employees: dict[int, EmployeeRow]
    calendar: dict[date, DayInfo]
    absences: dict[tuple[int, date], AbsenceInfo]
    plan_cells: dict[tuple[int, date], PlanCellInfo]
    fact_cells: dict[tuple[int, date], FactCellInfo]
    mark_sets: dict[int, MarkSetRow]
    # Ключ — пара (mark_set_id, employee_id), как у idx_one_active_plan.
    work_plans: dict[tuple[int, int], WorkPlanInfo]


def set_allows_plan(status_id: Optional[int]) -> bool:
    """Можно ли добавить план в шифр с этим статусом.

    None (статус не загружен) — запрет: безопаснее отказать, чем
    запланировать в закрытый комплект.
    """
    return status_id is not None and status_id in PLAN_STATUS_IDS

def base_day_verdict(
    ctx: AvailabilityContext,
    employee_id: int,
    work_date: date,
) -> Optional[DayVerdict]:
    """Общие проверки дня — одинаковы для планирования и для факта.

    Отвечает на вопрос «существует ли этот день для инженера как рабочий»:
    есть ли дата в календаре, числится ли человек, рабочий ли день.
    Возвращает вердикт, если одна из проверок сработала, иначе None —
    и тогда каждый потребитель продолжает своими правилами.

    Вынесено из day_verdict, чтобы определение рабочего дня было записано
    один раз: его импортирует app/api/v1/fact/rules.py.
    """
    # 1. Горизонт календаря. Проверяем первым: на дату вне
    # production_calendar ячейку не создать физически (FK), поэтому
    # остальные причины для неё бессмысленны.
    day_info = ctx.calendar.get(work_date)
    if day_info is None:
        return DayVerdict(state=DayState.OUT_OF_HORIZON)

    # 2. Границы назначения в бюро. Инженера, которого в этот день здесь
    # не было, планировать нельзя — и показывать его занятость тоже
    # некорректно.
    employee = ctx.employees.get(employee_id)
    if employee is None or not employee.is_assigned_on(work_date):
        return DayVerdict(state=DayState.NOT_ASSIGNED)

    # 3. Выходной или праздник.
    if not day_info.is_working:
        return DayVerdict(
            state=DayState.NONWORK,
            day_type_code=day_info.day_type_code,
        )

    return None


def day_verdict(
    ctx: AvailabilityContext,
    employee_id: int,
    work_date: date,
) -> DayVerdict:
    """Определяет состояние дня инженера по снимку контекста.

    Порядок проверок задаёт, какую причину увидит пользователь, когда их
    несколько одновременно. Он выбран так, чтобы причина объясняла суть,
    а не первое попавшееся совпадение:

        1. нет в календаре      — за краем горизонта, остальное неважно
        2. не числился в бюро   — человека здесь в этот день не было
        3. нерабочий день       — внешнее обстоятельство
        4. отсутствие           — внешнее обстоятельство
        5. есть факт            — день отработан, план заперт
        6. занят планом         — рабочая причина, устранимая
        7. свободен

    Пункт 5 стоит ВЫШЕ пункта 6 сознательно. Обычный случай «факт по
    плану» удовлетворяет обоим, и правильный ответ — «день отработан»:
    он объясняет, почему день нельзя трогать вообще. Ответ «занят
    шифром» навёл бы на мысль, что достаточно освободить день.

    Отсутствие с blocks_planning = false (например, командировка) день
    НЕ закрывает — функция пройдёт мимо пункта 4. Буква табеля для такого
    дня берётся сеткой напрямую из ctx.absences: отрисовка и доступность
    это разные вопросы.
    """
    key = (employee_id, work_date)

    # 1–3. Горизонт, назначение, выходной — общие с фактом, см.
    # base_day_verdict.
    base = base_day_verdict(ctx, employee_id, work_date)
    if base is not None:
        return base

    # 4. Неявка. Решает не факт наличия периода, а флаг справочника:
    # командировка может считаться рабочим днём.
    absence = ctx.absences.get(key)
    if absence is not None and absence.blocks_planning:
        return DayVerdict(
            state=DayState.ABSENCE,
            tab_letter=absence.tab_letter,
        )

    # 5. День отработан. Выше проверки плана — см. докстринг.
    fact = ctx.fact_cells.get(key)
    if fact is not None:
        return DayVerdict(
            state=DayState.HAS_FACT,
            mark_set_id=fact.mark_set_id,
            marka_full=fact.marka_full,
        )

    # 6. День занят другим шифром. Шифр может принадлежать чужому бюро —
    # отбор ячеек идёт по инженеру, а не по подразделению, иначе чужая
    # занятость осталась бы невидимой.
    plan = ctx.plan_cells.get(key)
    if plan is not None:
        return DayVerdict(
            state=DayState.BUSY,
            mark_set_id=plan.mark_set_id,
            marka_full=plan.marka_full,
            work_plan_id=plan.work_plan_id,
        )

    # 7. Свободен.
    return DayVerdict(state=DayState.FREE)