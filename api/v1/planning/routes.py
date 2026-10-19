# app/api/v1/planning/routes.py
"""JSON-API планирования — сетка загрузки и запись плановых ячеек.

    GET  /workload  — сетка бюро на интервал (чтение)
    POST /cells     — применение пачки изменений (запись)

Оба защищены правом plan:manage через PermissionChecker: API-контур, при
отсутствии права возвращается JSON 403, а не HTML-страница.

Область управления приходит отдельной зависимостью get_scope. Это её
первое применение в проекте: до сих пор все экраны планировщика были на
Jinja2 и пользовались get_scope_html. Сетка — React, значит JSON, значит
именно get_scope.

Роут — тонкий слой (как в composition/routes.py): вся логика в сервисе,
здесь только перевод доменных исключений в HTTP-коды и сборка ТЕКСТОВ
отказов. Тексты живут тут намеренно: сервис отдаёт причину и данные для
подстановки, а формулировка меняется чаще правил и не должна тянуть за
собой правку ядра.
"""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.planning.rules import DayState
from app.api.v1.planning.schemas import (
    ApplyResult,
    CellChangeIn,
    Rejection,
    WorkloadGrid,
)
from app.api.v1.planning.service import (
    DepartmentForbiddenError,
    InvalidIntervalError,
    MarkSetForbiddenError,
    MarkSetNotFoundError,
    NoExecutorError,
    OperationsRejectedError,
    RejectedOperation,
    apply_changes,
    get_availability,
)
from app.core.dependencies import PermissionChecker, get_db
from app.core.scope import Scope, get_scope
from app.models.user import User

router = APIRouter(tags=["planning"])

# Право на планирование. Есть у начальника бюро и начальника отдела;
# у инженера его нет — он только отмечает факт.
PLAN_PERMISSION = "plan:manage"
# Право просмотра сетки. Отдельно от plan:manage: смотреть загрузку
# может и тот, кто её не правит — ГИП, начальник производства,
# сам инженер. Запись требует plan:manage.
PLAN_VIEW_PERMISSION = "plan:view"


def _describe(rejection: RejectedOperation) -> str:
    """Собирает текст отказа по причине и данным подстановки.

    Формулировки нейтральны: без SQL, id и внутренних деталей (правило
    18). Шифр-конкурент показываем целиком — решение пользователя:
    начальнику понятнее, с кем договариваться, даже если шифр из чужого
    бюро.
    """
    day = rejection.work_date.strftime("%d.%m.%Y")
    state = rejection.state

    if state is DayState.OUT_OF_HORIZON:
        return (
            f"{day}: производственный календарь не заполнен на эту дату. "
            "Обратитесь к администратору."
        )
    if state is DayState.NOT_ASSIGNED:
        return f"{day}: инженер не числится в штате на эту дату."
    if state is DayState.NONWORK:
        return f"{day}: нерабочий день."
    if state is DayState.ABSENCE:
        letter = rejection.tab_letter or "отсутствие"
        return f"{day}: инженер отсутствует ({letter})."    
    if state is DayState.DAY_TAKEN:
        who = rejection.conflicting_employee
        suffix = f" за {who}" if who else ""
        return (
            f"{day}: дата уже запланирована по этому шифру{suffix}. "
            "Снимите тот день, чтобы поставить новый."
        )
    if state is DayState.HAS_FACT:
        marka = rejection.conflicting_marka
        suffix = f" по шифру {marka}" if marka else ""
        return (
            f"{day}: день уже отработан{suffix}. "
            "План на отработанный день не изменяется."
        )

    if state is DayState.SET_CLOSED:
        # Статус показываем как есть, в кавычках («Выпущен», «На
        # проверке»): так не нужно согласовывать падеж и род.
        marka = rejection.conflicting_marka or "Шифр"
        if rejection.status_name:
            reason = f"в статусе «{rejection.status_name}»"
        else:
            reason = "закрыт для планирования"
        return (
            f"{day}: {marka} {reason} — добавлять план нельзя, "
            "можно только снять."
        )
    
    if state is DayState.BUSY:
        marka = rejection.conflicting_marka
        if marka:
            return f"{day}: день занят шифром {marka}."
        return f"{day}: день занят."

    # FREE в отказе означает удаление несуществующей ячейки: на клиенте
    # она есть, в базе её уже нет.
    return f"{day}: плановой ячейки нет — обновите данные."


def _to_rejections(
    rejections: list[RejectedOperation],
) -> list[dict[str, object]]:
    """Переводит отказы сервиса в тело ответа 409.

    Формат совпадает со схемой Rejection, поэтому React обрабатывает
    отказ проверки и проигранную гонку одним кодом.
    """
    return [
        Rejection(
            mark_set_id=item.mark_set_id,
            work_date=item.work_date,
            state=item.state,
            message=_describe(item),
        ).model_dump(mode="json")
        for item in rejections
    ]


@router.get(
    "/workload",
    response_model=WorkloadGrid,
    summary="Сетка загрузки бюро на интервал",
)
async def read_workload(
    db: Annotated[AsyncSession, Depends(get_db)],
    scope: Annotated[Scope, Depends(get_scope)],
    # current_user не используется в теле, но зависимость обязательна:
    # именно она проводит проверку права (иначе эндпоинт открыт всем).
        current_user: Annotated[
        User, Depends(PermissionChecker(PLAN_VIEW_PERMISSION))
    ],
    department_id: Annotated[int, Query(description="Бюро или отдел")],
    date_from: Annotated[str, Query(description="Начало интервала")],
    date_to: Annotated[str, Query(description="Конец интервала")],
) -> WorkloadGrid:
    """Отдаёт сетку загрузки: инженеры бюро, их шифры, дни и ячейки.

    Ответ устроен плоскими слоями (employees / rows / days / cells) —
    дни инженера нужны сами по себе, вне привязки к шифру, и во
    вложенном дереве их пришлось бы дублировать в каждой строке.

    Вердикт доступности каждого дня сервер считает сам и отдаёт в
    days[].state. Фронт его не вычисляет: правила должны быть записаны
    в одном месте.

    Интервал ограничен сверху (120 дней): сетка прокручивается свободно,
    но подгружается окнами, и запрос на весь горизонт вернул бы десятки
    тысяч элементов.

    Коды ошибок:
      403 — подразделение вне области управления;
      422 — интервал бессмыслен или слишком широк.
    """
    from datetime import date as date_type

    try:
        parsed_from = date_type.fromisoformat(date_from)
        parsed_to = date_type.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Некорректный формат даты, ожидается ГГГГ-ММ-ДД",
        )

    try:
        return await get_availability(
            db,
            scope=scope,
            department_id=department_id,
            date_from=parsed_from,
            date_to=parsed_to,
        )
    except DepartmentForbiddenError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        )
    except InvalidIntervalError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )


@router.post(
    "/cells",
    response_model=ApplyResult,
    status_code=status.HTTP_200_OK,
    summary="Применение изменений плановых ячеек",
)
async def write_cells(
    payload: CellChangeIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    scope: Annotated[Scope, Depends(get_scope)],
    current_user: Annotated[User, Depends(PermissionChecker(PLAN_PERMISSION))],
) -> ApplyResult:
    """Применяет пачку изменений одной транзакцией (всё или ничего).

    Принимает СПИСОК ОПЕРАЦИЙ, а не снимок интервала: сетка
    прокручивается свободно, у снимка нет внятных границ, и двое
    начальников, работающих одновременно, затирали бы работу друг друга.

    Исполнитель в запросе не передаётся — сервер выводит его из
    project_mark_sets.id_executor. Подмена id не позволит запланировать
    работу чужому человеку.

    Ответ несёт свежее состояние затронутых строк и дней, поэтому после
    сохранения повторный GET не нужен.
    
    GET  /workload  — сетка бюро на интервал (чтение, plan:view)
    POST /cells     — применение пачки изменений (запись, plan:manage)
    
    Коды ошибок:
      403 — шифр вне области управления;
      404 — шифр не существует;
      409 — операции отклонены (день занят, отработан, выходной,
            отсутствие) ИЛИ проиграна гонка с другим начальником;
      422 — у шифра не назначен исполнитель.

    Тело 409 содержит message и rejected — список причин по каждой
    непрошедшей операции. Ни одна операция при этом не применена.
    """
    try:
        return await apply_changes(
            db, scope=scope, operations=payload.operations
        )

    except MarkSetNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )

    except MarkSetForbiddenError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        )

    except NoExecutorError as exc:
        # Текст уже нейтрален и полезен (называет шифр) — берём как есть.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )

    except OperationsRejectedError as exc:
        # 409, а не 200 со списком: React ветвится по статусу, а в логах
        # видно, что запись не прошла.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Изменения не применены",
                "rejected": _to_rejections(exc.rejections),
            },
        )