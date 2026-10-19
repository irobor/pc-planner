# app/api/v1/fact/routes.py
"""JSON-API отметки факта инженером — раздел «Мой факт» (/fact).

    GET  /window     — лента инженера от якорного дня (чтение)
    GET  /mark-sets  — поиск шифра для «+ добавить шифр» (чтение)
    POST /mark       — отметка одного дня (запись)

Все три требуют право fact:own И кадровую карточку. Второе условие
нужно из-за обхода прав администратора: user.can() пропускает роль
admin куда угодно, а у служебной учётки sys_admin записи в employees
нет — без проверки карточки сервис получил бы инженера None.

Инженер нигде не передаётся в запросе: он берётся из current_user.
Отметить факт за другого подменой id нельзя.

Роут — тонкий слой, как planning/routes.py: логика в сервисе, здесь
перевод доменных исключений в HTTP-коды и сборка ТЕКСТОВ отказов.
Формулировки меняются чаще правил и не должны тянуть правку ядра.
"""

from datetime import date
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.fact.rules import FactDayState, FactVerdict
from app.api.v1.fact.schemas import FactWindow, MarkIn, MarkResult, MarkSetFound
from app.api.v1.fact.service import (
    SEARCH_MIN_LENGTH,
    ConcurrentChangeError,
    DayRejectedError,
    InvalidWindowError,
    MarkSetClosedError,
    MarkSetNotFoundError,
    SearchTooShortError,
    load_fact_window,
    search_mark_sets,
    set_mark,
)
from app.core.dependencies import PermissionChecker, get_db
from app.core.scope import get_user_department_id
from app.models.user import User

router = APIRouter(tags=["fact"])

# Право отметки своего факта — у роли planner_executor.
FACT_PERMISSION = "fact:own"

# ОДИН экземпляр проверки на модуль, а не PermissionChecker(...) в каждой
# ручке: FastAPI кэширует зависимость в пределах запроса по самому
# объекту. Если get_fact_employee_id и ручка ссылаются на один и тот же
# экземпляр, пользователь проверяется один раз, а не дважды.
require_fact = PermissionChecker(FACT_PERMISSION)

DATE_FORMAT = "%d.%m.%Y"


async def get_fact_employee_id(
    current_user: Annotated[User, Depends(require_fact)],
) -> int:
    """Id кадровой карточки текущего пользователя — или 403.

    Связь user.employee загружается вместе с пользователем
    (lazy="joined"), поэтому лишнего запроса здесь нет.
    """
    employee = current_user.employee
    if employee is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Отметка факта доступна сотрудникам с кадровой карточкой.",
        )
    return employee.id


def _describe_day(work_date: date, verdict: FactVerdict) -> str:
    """Текст отказа по состоянию дня.

    Нейтрально, без SQL и id (правило 18). Для закрытого окна сразу
    говорим, что делать: иначе инженер не поймёт, почему вчерашний день
    открыт, а позавчерашний — нет.
    """
    day = work_date.strftime(DATE_FORMAT)
    state = verdict.state

    if state is FactDayState.OUT_OF_HORIZON:
        return (
            f"{day}: производственный календарь не заполнен на эту дату. "
            "Обратитесь к администратору."
        )
    if state is FactDayState.NOT_ASSIGNED:
        return f"{day}: вы не числитесь в штате на эту дату."
    if state is FactDayState.NONWORK:
        return f"{day}: нерабочий день."
    if state is FactDayState.ABSENCE:
        letter = verdict.tab_letter or "отсутствие"
        return f"{day}: день закрыт табелем ({letter})."
    if state is FactDayState.FUTURE:
        return f"{day}: день ещё не наступил."
    if state is FactDayState.CLOSED:
        return (
            f"{day}: срок отметки истёк. Отметить можно сегодня и "
            "предыдущий рабочий день; для исправления обратитесь "
            "к администратору."
        )
    # OPEN в отказе штатно не бывает — значит, данные изменились.
    return f"{day}: данные изменились — обновите страницу."


@router.get(
    "/window",
    response_model=FactWindow,
    status_code=status.HTTP_200_OK,
    summary="Лента факта инженера от якорного дня",
)
async def read_window(
    db: Annotated[AsyncSession, Depends(get_db)],
    employee_id: Annotated[int, Depends(get_fact_employee_id)],
    anchor: Annotated[
        Optional[date],
        Query(description="Якорный день ленты (ГГГГ-ММ-ДД); по умолчанию сегодня"),
    ] = None,
) -> FactWindow:
    """Отдаёт ленту инженера: 14 дней до якоря и 28 после.

    Строки — шифры, где у инженера в пределах ленты есть план или факт,
    либо он исполнитель шифра в статусе «В плане», «В работе» или
    «На проверке».

    Кликабельны только дни со state = open: сегодня и предыдущий
    рабочий день, если в них нет неявки. Окно отметки считается от
    серверной даты, а не от положения ленты.

    Без параметра лента строится от сегодняшнего дня — острову при
    первой загрузке не нужно знать дату. Для стрелок ответ несёт
    готовые prev_anchor / next_anchor: браузер дат не складывает.

    Коды ошибок:
      403 — нет права fact:own или нет кадровой карточки;
      422 — некорректная дата или лента целиком вне календаря.
    """
    today = date.today()
    try:
        return await load_fact_window(
            db,
            employee_id=employee_id,
            anchor=anchor if anchor is not None else today,
            today=today,
        )
    except InvalidWindowError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Производственный календарь заполнен с "
                f"{exc.horizon_from.strftime(DATE_FORMAT)} по "
                f"{exc.horizon_to.strftime(DATE_FORMAT)}."
            ),
        )


@router.get(
    "/mark-sets",
    response_model=list[MarkSetFound],
    status_code=status.HTTP_200_OK,
    summary="Поиск шифра для добавления в матрицу",
)
async def find_mark_sets(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_fact)],
    # employee_id в теле не нужен, но зависимость обязательна: она
    # отсекает пользователей без кадровой карточки.
    _employee_id: Annotated[int, Depends(get_fact_employee_id)],
    q: Annotated[
        str,
        Query(
            min_length=SEARCH_MIN_LENGTH,
            max_length=100,
            description="Фрагмент шифра",
        ),
    ],
) -> list[MarkSetFound]:
    """Ищет шифры по всей организации, ближние к инженеру — первыми.

    Порядок выдачи: шифры его бюро, затем его отдела, затем остальные;
    внутри группы — по алфавиту. Не больше 20 результатов. Выпущенные
    шифры не ищутся: факт по ним не ставится.

    Коды ошибок:
      403 — нет права fact:own или нет кадровой карточки;
      422 — строка поиска короче 5 символов (без учёта пробелов по краям).
    """
    department_id = await get_user_department_id(db, current_user)
    try:
        return await search_mark_sets(db, department_id=department_id, query=q)
    except SearchTooShortError:
        # Query(min_length) считает пробелы, сервис — нет: «  053 »
        # пройдёт первую проверку и остановится здесь.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Введите не меньше {SEARCH_MIN_LENGTH} символов шифра.",
        )


@router.post(
    "/mark",
    response_model=MarkResult,
    status_code=status.HTTP_200_OK,
    summary="Отметка факта за день",
)
async def write_mark(
    payload: MarkIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    employee_id: Annotated[int, Depends(get_fact_employee_id)],
) -> MarkResult:
    """Отмечает, над каким шифром инженер работал в этот день.

    Одна отметка в день. Клик по другому шифру того же дня ПЕРЕНОСИТ
    отметку, клик по своей же отметке ничего не меняет (action = noop).
    Снять отметку нельзя: пустой рабочий день — это неявка, её
    оформляет табельщица.

    Признак ретроввода (отметка за прошедший день) ставит база, в
    запросе он не передаётся.

    Ответ несёт свежее состояние дня — повторный GET месяца не нужен.

    Коды ошибок:
      403 — нет права fact:own или нет кадровой карточки;
      404 — шифр не существует;
      409 — день закрыт (выходной, неявка, будущее, истёк срок),
            шифр выпущен или данные изменились параллельно.
    """
    try:
        return await set_mark(
            db,
            employee_id=employee_id,
            mark_set_id=payload.mark_set_id,
            work_date=payload.work_date,
            today=date.today(),
        )

    except MarkSetNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Шифр не найден — обновите страницу.",
        )

    except MarkSetClosedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Шифр {exc.marka_full} выпущен, отметка по нему невозможна.",
        )

    except DayRejectedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_describe_day(exc.work_date, exc.verdict),
        )

    except ConcurrentChangeError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{exc.work_date.strftime(DATE_FORMAT)}: данные изменились "
                "— обновите страницу."
            ),
        )
