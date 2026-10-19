# app/api/v1/planning/schemas.py
"""Pydantic-схемы границы API планирования.

Здесь только контракт с React: что уходит в сетку и что приходит от неё.
Правила живут в rules.py, работа с БД — в service.py.

Ответ сетки устроен ПЛОСКИМИ слоями, а не вложенным деревом
(инженер → шифр → дни). Причина: дни инженера нужны и сами по себе —
выходные, отсутствия, доступность ячейки под клик, — и не принадлежат
конкретному шифру. Во вложенной структуре их пришлось бы дублировать в
каждой строке шифра либо заводить отдельную ветку рядом. Плоские слои
с общим ключом (employee_id, work_date) собираются на клиенте в любую
раскладку без дублирования.

Вердикт доступности отдаётся сервером явно (DayOut.state), а не
вычисляется фронтом: иначе правила оказались бы записаны дважды, на
двух языках, и однажды разошлись бы.

Стиль конфигурации — model_config = ConfigDict(...) (pydantic v2),
как в composition/schemas.py.
"""
from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict

from app.api.v1.planning.rules import DayState


# ---------------------------------------------------------------------------
# Ответ GET /api/planning/workload — сетка загрузки.
# ---------------------------------------------------------------------------


class EmployeeOut(BaseModel):
    """Инженер бюро — верхний уровень сетки."""

    id: int
    full_name: str

    # Границы назначения в бюро. Фронт по ним гасит дни, когда человек
    # здесь ещё не числился или уже не числится: сервер для таких дней
    # отдаёт state = not_assigned.
    valid_from: date
    valid_to: Optional[date] = None

    model_config = ConfigDict(from_attributes=True)


class RowOut(BaseModel):
    """Строка сетки: шифр под конкретным инженером.

    Строка — это ПАРА (инженер, шифр), а не шифр сам по себе: один шифр
    может встретиться под разными людьми (передали работу, сменили
    исполнителя), и у каждого будет свой план.
    """

    employee_id: int
    mark_set_id: int
    marka_full: str
    status_id: Optional[int] = None
    
    # Название статуса для метки в строке («В работе», «Выпущен»).
    # Цвет метки задаётся в style.css классом по status_id
    # (.set-status--N), а не цветом из set_statuses.color: hex-цвет
    # пришлось бы подставлять inline-стилем.
    status_name: Optional[str] = None

    # Можно ли добавлять план в этот шифр (rules.set_allows_plan).
    # False — на проверке или выпущен: строка видна как история,
    # клетки с планом можно снять, пустые не кликаются.
    allows_plan: bool

    # Инженер назначен исполнителем этого шифра. False означает, что
    # строка подтянулась из ячеек: чужая работа или факт вне плана.
    # Фронт по флагу отличает такие строки (на макете — иконка огня).
    is_executor: bool

    # Границы плана этого инженера по этому шифру. None во всех четырёх
    # полях — шифр не спланирован.
    # ВАЖНО: work_days — объём плана ЦЕЛИКОМ, а не число дней внутри
    # запрошенного окна. План может выходить за края интервала, и
    # обрезанная цифра ввела бы в заблуждение.
    work_plan_id: Optional[int] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    work_days: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)


class DayOut(BaseModel):
    """Состояние дня инженера — независимо от шифров.

    Один элемент на каждую пару (инженер, дата) внутри интервала.
    Отсюда фронт красит выходные, ставит букву табеля и решает, можно ли
    вообще кликнуть по ячейке.
    """

    employee_id: int
    work_date: date

    # Вердикт доступности. free — день можно занять; остальные шесть
    # значений несут причину отказа.
    state: DayState

    # Буква табеля при отсутствии. Заполняется ДАЖЕ когда отсутствие не
    # блокирует планирование (командировка): день планируемый, но
    # показать его иначе, чем обычный, всё равно нужно. Поэтому берётся
    # не из вердикта, а напрямую из контекста.
    tab_letter: Optional[str] = None

    # Код типа дня: WORK, WEEKEND, HOLIDAY, SHORT.
    day_type_code: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class CellOut(BaseModel):
    """Заполненная ячейка на пересечении строки и дня.

    Отдаются только непустые: где нет ни плана, ни факта, элемента нет
    вовсе. На двухнедельном окне это на порядок меньше данных, чем
    полная матрица.

    На макете пара has_plan/has_fact даёт три цвета: только план —
    синий, план и факт — зелёный («факт по плану»), только факт —
    оранжевый («факт вне плана»).
    """

    employee_id: int
    mark_set_id: int
    work_date: date
    has_plan: bool
    has_fact: bool

    model_config = ConfigDict(from_attributes=True)


class WorkloadGrid(BaseModel):
    """Полный ответ сетки загрузки на запрошенный интервал."""

    date_from: date
    date_to: date

    # Края заполненного production_calendar. Фронт по ним ограничивает
    # горизонтальную прокрутку: дальше планировать физически нельзя
    # (внешний ключ ячеек на календарь).
    horizon_from: date
    horizon_to: date

    employees: list[EmployeeOut]
    rows: list[RowOut]
    days: list[DayOut]
    cells: list[CellOut]

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Запрос и ответ POST /api/planning/cells — применение изменений.
#
# Клиент шлёт СПИСОК ИЗМЕНЕНИЙ, а не снимок интервала. Снимок при
# свободной прокрутке не имеет внятных границ, и двое начальников,
# работающих одновременно, затирали бы работу друг друга. Список
# операций от прокрутки не зависит вовсе.
# ---------------------------------------------------------------------------


class CellOperation(BaseModel):
    """Одна операция над плановой ячейкой.

    employee_id НЕ передаётся: сервер выводит его из
    project_mark_sets.id_executor. Так подмена id в запросе не позволит
    запланировать работу чужому человеку — исполнитель определяется
    состоянием БД, а не содержимым запроса.
    """

    op: Literal["add", "remove"]
    mark_set_id: int
    work_date: date

    model_config = ConfigDict(from_attributes=True)


class CellChangeIn(BaseModel):
    """Пачка изменений, применяемая одной транзакцией.

    Правило «всё или ничего»: если хоть одна операция не проходит, не
    записывается ничего. Клиент проверил правила перед отправкой, и
    отказ означает, что его картина устарела — честный ответ «перечитай
    и повтори», а не частичная запись, о составе которой пользователь
    не догадывается. Разложенное при этом не теряется: оно живёт в
    браузере.
    """

    operations: list[CellOperation]

    model_config = ConfigDict(from_attributes=True)


class Rejection(BaseModel):
    """Причина, по которой операция не прошла.

    Формат одинаков и для отказа на проверке, и для проигранной гонки:
    React обрабатывает оба случая одним кодом.
    """

    mark_set_id: int
    work_date: date
    state: DayState
    # Готовый текст для показа. Собирается на границе (routes.py), а не
    # в ядре: правка формулировки не должна трогать правила.
    message: str

    model_config = ConfigDict(from_attributes=True)


class ApplyResult(BaseModel):
    """Результат применения пачки изменений.

    При успехе rejected пуст, а rows/days/cells несут свежее состояние
    затронутых строк и дней — чтобы React перерисовал без второго GET.
    """

    applied: int
    rows: list[RowOut]
    days: list[DayOut]
    cells: list[CellOut]
    rejected: list[Rejection]

    model_config = ConfigDict(from_attributes=True)
