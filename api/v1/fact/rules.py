# app/api/v1/fact/rules.py
"""Правила отметки факта инженером — ядро экрана на главной.

Единственное место, где записано, можно ли инженеру отметить день и что
произойдёт при клике. И матрица (отрисовка кликабельных клеток), и
запись отметки (проверка) спрашивают ответ здесь.

Модуль чистый, как planning/rules.py: ни БД, ни async, ни Pydantic.
Снимок данных — тот же AvailabilityContext, что у планирования, только
собранный по одному инженеру. Новый контекст не нужен: в нём уже есть
календарь, ВСЕ неявки (включая неблокирующие) и факт-ячейки.

Чем правила факта отличаются от правил плана:
  * неявка закрывает день ВСЕГДА — флаг blocks_planning не читается:
    табель описывает доступность ресурса, и если ресурса не было, нет
    и занятости;
  * существующий факт — не отказ, а текущая отметка дня: клик по
    другому шифру её ПЕРЕНОСИТ (одна отметка в день, unique_fact_cell);
  * плановая ячейка не участвует вовсе: факт вне плана разрешён,
    цвет «по плану / вне плана» определяется отрисовкой, а не правилами;
  * окно отметки — сегодня и предыдущий рабочий день.

«Сегодня» приходит параметром, а не из date.today(): так любой день
(понедельник после праздников, край календаря) проверяется в консоли.

Текстов сообщений здесь нет — они собираются в routes.py.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from typing import Optional

from app.api.v1.planning.rules import (
    AvailabilityContext,
    DayInfo,
    DayState,
    base_day_verdict,
)

# На сколько дней назад ОТ СЕГОДНЯ сервис грузит календарь для расчёта
# окна отметки. Окно зависит от сегодняшнего дня, а не от месяца на
# экране (инженер может листать август), поэтому календарь для окна
# грузится отдельным запросом, а не расширением контекста месяца.
# После новогодних каникул предыдущий рабочий день лежит ~12 дней назад;
# две недели покрывают самый длинный разрыв.
CALENDAR_LOOKBACK_DAYS: int = 14

# Статусы шифра (set_statuses.id), по которым можно отметить факт:
# 1 — В плане, 2 — В работе, 3 — На проверке. «Выпущен» (4) закрыт:
# изменения выпускаются новым комплектом со своим marka_no.
# Шире GRID_STATUS_IDS планирования намеренно: начальник планирует то,
# что в работе, а инженер отмечает всё, над чем реально работал, —
# включая исправление замечаний и работу до смены статуса.
FACT_STATUS_IDS: tuple[int, ...] = (1, 2, 3)


class FactDayState(str, Enum):
    """Состояние дня инженера с точки зрения отметки факта.

    Отдельное перечисление, а не расширение DayState: новые значения
    попали бы в _describe планирования, где им нечего делать.

    Кликабелен только OPEN. Порядок проверок задан в fact_day_verdict.
    """

    # Общие с планированием (см. base_day_verdict). Значения совпадают
    # с DayState намеренно: фронт читает их одинаково в обеих сетках.
    OUT_OF_HORIZON = "out_of_horizon"
    NOT_ASSIGNED = "not_assigned"
    NONWORK = "nonwork"

    # Любая неявка из табеля. Буква стоит вместо факта.
    ABSENCE = "absence"

    # День ещё не наступил: факт за завтра не бывает. План на нём виден.
    FUTURE = "future"

    # День раньше предыдущего рабочего — окно отметки закрыто.
    # Исправление возможно только администратором.
    CLOSED = "closed"

    # Сегодня или предыдущий рабочий день: можно отметить или перенести.
    OPEN = "open"


# Перевод общих вердиктов в состояния факта. Явный словарь, а не
# FactDayState(state.value): если base_day_verdict однажды начнёт
# возвращать новое состояние, здесь случится KeyError на первой же
# проверке в консоли, а не молчаливая подмена смысла.
_BASE_STATES: dict[DayState, FactDayState] = {
    DayState.OUT_OF_HORIZON: FactDayState.OUT_OF_HORIZON,
    DayState.NOT_ASSIGNED: FactDayState.NOT_ASSIGNED,
    DayState.NONWORK: FactDayState.NONWORK,
}


class MarkAction(str, Enum):
    """Что делать с кликом инженера по клетке (шифр, день)."""

    # День не открыт для отметки.
    REJECT = "reject"
    # Отметки на день нет — создать.
    INSERT = "insert"
    # Отметка стоит на другом шифре — перенести (UPDATE, не DELETE +
    # INSERT: иначе каскадом пропадут связи в fact_kanban_cells).
    MOVE = "move"
    # Клик по собственной отметке. Снять её нельзя: пустой рабочий день
    # означает неявку, а неявку оформляет табельщица.
    NOOP = "noop"


@dataclass(frozen=True, slots=True)
class FactVerdict:
    """Ответ на вопрос «может ли инженер отметить этот день».

    Какие поля заполнены, зависит от state; незаполненные остаются None.
    """

    state: FactDayState

    # ABSENCE: буква табеля (Б, ОТ, К, НН, ...) — для строки «Табель».
    tab_letter: Optional[str] = None

    # NONWORK: код типа дня (WEEKEND / HOLIDAY).
    day_type_code: Optional[str] = None

    # OPEN: текущая отметка дня, если она уже есть. По ней decide_mark
    # отличает создание от переноса. Для закрытых дней не заполняется:
    # отметки для отрисовки идут отдельным слоем ячеек, как в сетке
    # начальника.
    current_mark_set_id: Optional[int] = None
    current_marka_full: Optional[str] = None

    @property
    def can_mark(self) -> bool:
        """День открыт для отметки или переноса."""
        return self.state is FactDayState.OPEN


def set_allows_fact(status_id: int) -> bool:
    """Можно ли отметить факт по шифру в этом статусе.

    Проверяется только в момент новой записи. Если шифр выпустили после
    отметки, история остаётся нетронутой.
    """
    return status_id in FACT_STATUS_IDS


def previous_working_day(
    calendar: Mapping[date, DayInfo],
    today: date,
) -> Optional[date]:
    """Находит ближайший рабочий день строго раньше today.

    Идёт назад по календарю, пропуская выходные и праздники: в понедельник
    это пятница, после каникул — последний рабочий день перед ними.

    Возвращает None, если календарь закончился раньше, чем нашёлся
    рабочий день (не загружен или не заполнен). Граница цикла — сам
    календарь: отсутствующая дата останавливает поиск, поэтому
    бесконечного цикла быть не может.
    """
    day = today - timedelta(days=1)
    info = calendar.get(day)
    while info is not None:
        if info.is_working:
            return day
        day -= timedelta(days=1)
        info = calendar.get(day)
    return None


def fact_window_start(
    calendar: Mapping[date, DayInfo],
    today: date,
) -> date:
    """Первый день окна отметки (включительно); последний — today.

    Считается один раз на запрос, а не для каждой клетки. Если
    предыдущий рабочий день не найден, окно сужается до одного
    сегодняшнего дня — безопасный вариант: лучше не дать отметить
    прошлое, чем открыть его по неполным данным.
    """
    previous = previous_working_day(calendar, today)
    return previous if previous is not None else today


def fact_day_verdict(
    ctx: AvailabilityContext,
    employee_id: int,
    work_date: date,
    today: date,
    window_from: date,
) -> FactVerdict:
    """Определяет состояние дня инженера для отметки факта.

    Порядок проверок (первое совпадение побеждает):

        1–3. календарь, штат, выходной — общие, base_day_verdict
        4.   неявка любого вида       — табель вместо факта
        5.   будущее                  — факт ещё невозможен
        6.   раньше окна              — окно отметки закрыто
        7.   открыт                   — с текущей отметкой, если есть

    Неявка стоит ВЫШЕ «будущего» и «закрытого» сознательно: отпуск на
    следующей неделе и больничный месяц назад должны объясняться буквой
    табеля, а не словами «ещё не наступил» или «окно закрыто».

    window_from получают через fact_window_start(ctx.calendar, today).
    """
    key = (employee_id, work_date)

    # 1–3. Общие проверки — определение рабочего дня живёт в одном месте.
    base = base_day_verdict(ctx, employee_id, work_date)
    if base is not None:
        return FactVerdict(
            state=_BASE_STATES[base.state],
            day_type_code=base.day_type_code,
        )

    # 4. Неявка. В отличие от планирования, флаг blocks_planning не
    # читается: любая буква табеля закрывает день для факта (К, НН и ПР
    # включительно). Этот же запрет продублирован триггером
    # trg_fact_check_absence — здесь он ради серой клетки и понятного
    # отказа, в БД — ради целостности при любом писателе.
    absence = ctx.absences.get(key)
    if absence is not None:
        return FactVerdict(
            state=FactDayState.ABSENCE,
            tab_letter=absence.tab_letter,
        )

    # 5. Будущее. Строгое сравнение: сегодняшний день открыт.
    if work_date > today:
        return FactVerdict(state=FactDayState.FUTURE)

    # 6. Раньше окна. Выходные между окном и сегодня сюда не попадут —
    # их уже отсеял пункт 3.
    if work_date < window_from:
        return FactVerdict(state=FactDayState.CLOSED)

    # 7. Открыт. Текущая отметка нужна decide_mark: создать или перенести.
    fact = ctx.fact_cells.get(key)
    if fact is None:
        return FactVerdict(state=FactDayState.OPEN)
    return FactVerdict(
        state=FactDayState.OPEN,
        current_mark_set_id=fact.mark_set_id,
        current_marka_full=fact.marka_full,
    )


def decide_mark(verdict: FactVerdict, mark_set_id: int) -> MarkAction:
    """Решает, что сделать с кликом по клетке (mark_set_id, день).

    Сервис только исполняет решение — правило «одна отметка в день,
    клик переносит» записано здесь, а не размазано по service.py.
    """
    if not verdict.can_mark:
        return MarkAction.REJECT
    if verdict.current_mark_set_id is None:
        return MarkAction.INSERT
    if verdict.current_mark_set_id == mark_set_id:
        return MarkAction.NOOP
    return MarkAction.MOVE