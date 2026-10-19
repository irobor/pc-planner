# app/api/v1/admin/schemas.py
"""Pydantic-схемы границы API администратора данных.

Сетка сотрудника переиспользует слои планирования — строки (RowOut) и
клетки (CellOut): у одного сотрудника они дают ровно то, что нужно,
включая обе полосы (план и факт). Слой дней — свой (AdminDayOut):
вердикты планирования (HAS_FACT, BUSY и т. п.) — процессные запреты,
администратора они не касаются.

Правка присылается как ЖЕЛАЕМОЕ ИТОГОВОЕ СОСТОЯНИЕ затронутых ячеек
(AdminCellIn), а не «добавь/убери»: diff сервер считает сам, сравнивая
со снимком базы.
"""

from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.api.v1.admin.rules import AdminRejectReason
from app.api.v1.planning.schemas import CellOut, RowOut

# Предел пачки правок за одно сохранение. Защита от случайного запроса
# на десятки тысяч ячеек, а не бизнес-правило: правка «за квартал по
# двум полосам» — около 130 ячеек, с большим запасом.
MAX_CELLS_PER_SAVE = 1000

# ---------------------------------------------------------------------------
# Выбор сотрудника — GET /api/admin/employees
# ---------------------------------------------------------------------------


class EmployeeOption(BaseModel):
    """Сотрудник в списке выбора: только имя, без данных плана и факта."""

    id: int
    full_name: str
    # Код текущего подразделения — различает однофамильцев.
    department_code: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Сетка сотрудника — GET /api/admin/grid
# ---------------------------------------------------------------------------


class AdminDayOut(BaseModel):
    """День сотрудника глазами администратора.

    Два флага вместо одного состояния: план и факт подчиняются разным
    правилам (на день неявки план можно, факт нельзя; на будущее — то
    же). Флаги считает admin_cell_rejection; фронт их не вычисляет.
    """

    work_date: date

    # Можно ли ПОСТАВИТЬ значение в полосу. Очистить можно всегда —
    # это правило, а не флаг, поэтому в ответе его нет.
    plan_allowed: bool
    fact_allowed: bool

    # Причина запрета — для подсказки при наведении.
    plan_reason: Optional[AdminRejectReason] = None
    fact_reason: Optional[AdminRejectReason] = None

    # Буква табеля, если в день неявка (Б, ОТ, К, НН...).
    tab_letter: Optional[str] = None

    # Тип дня: WORK, WEEKEND, HOLIDAY, SHORT.
    day_type_code: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class AdminGrid(BaseModel):
    """Сетка одного сотрудника на интервал."""

    employee_id: int
    full_name: str

    date_from: date
    date_to: date
    today: date

    # Края заполненного production_calendar — ограничивают прокрутку.
    horizon_from: date
    horizon_to: date

    rows: list[RowOut]
    days: list[AdminDayOut]
    cells: list[CellOut]

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Сохранение пачки правок — POST /api/admin/changes
# ---------------------------------------------------------------------------


class AdminCellIn(BaseModel):
    """Одна ячейка: какой она должна стать. mark_set_id = None — очистить."""

    layer: Literal["plan", "fact"]
    work_date: date
    mark_set_id: Optional[int] = Field(default=None, gt=0)

    model_config = ConfigDict(from_attributes=True)


class AdminChangesIn(BaseModel):
    """Пачка правок одного сотрудника и комментарий к ней.

    Комментарий обязателен — как сообщение коммита. Проверка непустоты
    после обрезки пробелов — в сервисе и в CHECK базы; здесь — длина.
    """

    employee_id: int = Field(gt=0)
    comment: str = Field(min_length=1, max_length=2000)
    cells: list[AdminCellIn] = Field(min_length=1, max_length=MAX_CELLS_PER_SAVE)

    model_config = ConfigDict(from_attributes=True)


class AdminRejectionOut(BaseModel):
    """Почему ячейку нельзя изменить. Формат одинаков для всех отказов."""

    layer: Literal["plan", "fact"]
    work_date: date
    reason: AdminRejectReason
    # Готовый текст — собирается в routes.py.
    message: str

    model_config = ConfigDict(from_attributes=True)


class AdminApplyResult(BaseModel):
    """Результат сохранения.

    changeset_id = None — итоговый diff пуст (правка «туда и обратно»):
    ничего не записано, и в журнале этой правки нет.
    """

    changeset_id: Optional[int] = None
    # Сколько ячеек в итоге изменилось.
    applied: int

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Журнал правок — GET /api/admin/changesets
# ---------------------------------------------------------------------------


class ChangeItemOut(BaseModel):
    """Строка diff: полоса, день, было → стало (снимки шифров)."""

    layer: Literal["plan", "fact"]
    work_date: date
    # None — ячейка была пустой / стала пустой.
    old_marka: Optional[str] = None
    new_marka: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class ChangesetOut(BaseModel):
    """Правка в журнале: сообщение коммита и его diff."""

    id: int
    created_at: datetime
    # Логин автора. Логин, а не ФИО: у администратора может не быть
    # кадровой карточки.
    changed_by: str
    comment: str
    items: list[ChangeItemOut]

    model_config = ConfigDict(from_attributes=True)