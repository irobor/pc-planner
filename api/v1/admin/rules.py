# app/api/v1/admin/rules.py
"""Правила правки плана и факта администратором — «ремонтный режим».

Единственное место, где записано, что администратор может сделать с
ячейкой и что попадёт в журнал правок. Модуль чистый, как
planning/rules.py и fact/rules.py: ни БД, ни async, ни Pydantic.
Снимок данных — тот же AvailabilityContext, собранный по одному
сотруднику.

Чем правила администратора отличаются от правил начальника и инженера:
проверяется ТОЛЬКО целостность данных. Процессные ограничения — окно
отметки, блокировка плана фактом, статус шифра, «только исполнитель»,
прошедшие даты — не проверяются вовсе: на практике ошибок много, и
исправлять их должно быть можно без SQL.

Целостность (для значения, а не для очистки):

    проверка                      план   факт
    дата в календаре               да     да
    сотрудник в штате              да     да
    рабочий день                   да     да
    нет неявки                     —      да
    не будущее                     —      да

Очистить ячейку можно ВСЕГДА: удаление ошибочных данных не должно
блокироваться ничем — если ячейка оказалась на выходном или вне штата,
это тем более повод её убрать.

Формат правки — ЖЕЛАЕМОЕ ИТОГОВОЕ СОСТОЯНИЕ ячейки, а не «добавь/убери».
Сервер сравнивает его со снимком базы и получает diff: ячейка, которая
совпала с базой, в журнал не попадает. Так правило «пишется итог, а не
каждый клик» выполняется само, и подменить «было» браузер не может.

Тексты отказов здесь не собираются — они в routes.py.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Literal, Optional

from app.api.v1.planning.rules import (
    AvailabilityContext,
    DayState,
    base_day_verdict,
)

# Полоса ячейки: верхняя — план, нижняя — факт.
Layer = Literal["plan", "fact"]

# Ключ ячейки сотрудника. Сотрудник один на правку, поэтому в ключе его
# нет — он указан в самой правке (admin_changesets.employee_id).
CellKey = tuple[Layer, date]


@dataclass(frozen=True, slots=True)
class DesiredCell:
    """Одна ячейка из запроса: какой она должна стать.

    mark_set_id = None — ячейку очистить.
    """

    layer: Layer
    work_date: date
    mark_set_id: Optional[int]


@dataclass(frozen=True, slots=True)
class CellChange:
    """Одна строка diff: ячейка, которая в итоге изменилась.

    old / new = None — ячейка была пустой / стала пустой. Оба None
    одновременно не бывает: это не изменение (compute_diff его отсеет,
    а CHECK admin_change_items_is_change не пропустит в базу).
    """

    layer: Layer
    work_date: date
    old_mark_set_id: Optional[int]
    new_mark_set_id: Optional[int]


class DuplicateCellError(ValueError):
    """В запросе две операции на одну ячейку.

    Неоднозначно, какая из них «итоговая», поэтому не угадываем, а
    отказываем: клиент обязан прислать по одной операции на ячейку.
    """

    def __init__(self, layer: Layer, work_date: date) -> None:
        super().__init__(f"{layer} {work_date.isoformat()}")
        self.layer = layer
        self.work_date = work_date


class AdminRejectReason(str, Enum):
    """Почему значение нельзя поставить в ячейку."""

    # Общие для плана и факта (base_day_verdict).
    OUT_OF_HORIZON = "out_of_horizon"
    NOT_ASSIGNED = "not_assigned"
    NONWORK = "nonwork"

    # Только для факта.
    ABSENCE = "absence"
    FUTURE = "future"


@dataclass(frozen=True, slots=True)
class AdminRejection:
    """Отказ по одной ячейке — с данными для текста в routes.py."""

    layer: Layer
    work_date: date
    reason: AdminRejectReason
    # ABSENCE: буква табеля.
    tab_letter: Optional[str] = None


# Перевод общих вердиктов. Явный словарь: если base_day_verdict начнёт
# возвращать новое состояние, здесь будет KeyError на первой проверке в
# консоли, а не молчаливая подмена смысла.
_BASE_REASONS: dict[DayState, AdminRejectReason] = {
    DayState.OUT_OF_HORIZON: AdminRejectReason.OUT_OF_HORIZON,
    DayState.NOT_ASSIGNED: AdminRejectReason.NOT_ASSIGNED,
    DayState.NONWORK: AdminRejectReason.NONWORK,
}


def current_cells(
    ctx: AvailabilityContext,
    employee_id: int,
) -> dict[CellKey, int]:
    """Текущее состояние ячеек сотрудника из снимка базы.

    Ключ — (полоса, день), значение — шифр. Пустых ячеек в словаре нет:
    отсутствие ключа и означает «пусто». В день у сотрудника не больше
    одного плана и одного факта (unique-ограничения БД), поэтому ключ
    однозначен.
    """
    cells: dict[CellKey, int] = {}
    for (emp_id, day), plan in ctx.plan_cells.items():
        if emp_id == employee_id:
            cells[("plan", day)] = plan.mark_set_id
    for (emp_id, day), fact in ctx.fact_cells.items():
        if emp_id == employee_id:
            cells[("fact", day)] = fact.mark_set_id
    return cells


def compute_diff(
    current: Mapping[CellKey, int],
    desired: Iterable[DesiredCell],
) -> list[CellChange]:
    """Итоговый diff: желаемое состояние против снимка базы.

    Ячейка, совпавшая с базой, в результат не попадает: правка «туда и
    обратно» не меняет ничего и в журнал не пишется. Порядок результата
    устойчивый — по дню, затем план раньше факта: так журнал и проверки
    в консоли читаются одинаково от запуска к запуску.

    Две операции на одну ячейку — DuplicateCellError.
    """
    seen: set[CellKey] = set()
    changes: list[CellChange] = []
    for cell in desired:
        key: CellKey = (cell.layer, cell.work_date)
        if key in seen:
            raise DuplicateCellError(cell.layer, cell.work_date)
        seen.add(key)

        old = current.get(key)
        if old == cell.mark_set_id:
            continue
        changes.append(
            CellChange(
                layer=cell.layer,
                work_date=cell.work_date,
                old_mark_set_id=old,
                new_mark_set_id=cell.mark_set_id,
            )
        )

    changes.sort(key=lambda c: (c.work_date, c.layer != "plan"))
    return changes


def value_rejection(
    ctx: AvailabilityContext,
    employee_id: int,
    layer: Layer,
    work_date: date,
    today: date,
) -> Optional[AdminRejection]:
    """Можно ли ПОСТАВИТЬ значение в полосу этого дня. None — можно.

    Проверяется только целостность; процессные правила администратора
    не касаются. Отдельно от admin_cell_rejection, потому что сетке
    нужен ответ «можно ли сюда поставить» для каждого дня ещё до того,
    как появилось конкретное изменение (флаги AdminDayOut).

    «Сегодня» приходит параметром, как в fact/rules.py: модуль чистый,
    любой день проверяется в консоли.
    """
    # Календарь, штат, выходной — одни для всех экранов (planning/rules).
    base = base_day_verdict(ctx, employee_id, work_date)
    if base is not None:
        return AdminRejection(
            layer=layer,
            work_date=work_date,
            reason=_BASE_REASONS[base.state],
        )

    if layer == "fact":
        # Табель — источник истины: факт поверх неявки не ставится.
        # Если неявка ошибочна, сначала её исправляет табельщица. Тот же
        # запрет держит триггер trg_fact_check_absence — здесь он ради
        # понятного отказа до записи.
        absence = ctx.absences.get((employee_id, work_date))
        if absence is not None:
            return AdminRejection(
                layer=layer,
                work_date=work_date,
                reason=AdminRejectReason.ABSENCE,
                tab_letter=absence.tab_letter,
            )
        # Факт — о том, что было. О завтрашнем дне факта не бывает и у
        # администратора.
        if work_date > today:
            return AdminRejection(
                layer=layer,
                work_date=work_date,
                reason=AdminRejectReason.FUTURE,
            )

    # План на день неявки администратору разрешён: это процессное
    # правило, а не целостность.
    return None


def admin_cell_rejection(
    ctx: AvailabilityContext,
    employee_id: int,
    change: CellChange,
    today: date,
) -> Optional[AdminRejection]:
    """Можно ли применить изменение ячейки. None — можно.

    Очистка (new = None) разрешена всегда — см. докстринг модуля.
    Для значения — value_rejection.
    """
    if change.new_mark_set_id is None:
        return None
    return value_rejection(ctx, employee_id, change.layer, change.work_date, today)