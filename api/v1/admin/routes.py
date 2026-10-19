# app/api/v1/admin/routes.py
"""JSON-API администратора данных — «ремонтный режим» плана и факта.

    GET  /employees   — список имён для выбора сотрудника
    GET  /grid        — сетка одного сотрудника на интервал
    GET  /mark-sets   — поиск шифра (любой статус) для «+ добавить шифр»
    POST /changes     — пачка правок плана и факта + комментарий
    GET  /changesets  — журнал правок сотрудника

Все ручки требуют ОБА права: plan:override и fact:override. Проверка —
только через PermissionChecker, то есть через User.can(). Поэтому
работают оба администратора: роль admin проходит через обход прав, роль
data_admin — по своим правам, без отдельных веток в коде. Проверять
is_superuser здесь нельзя: администратор данных его не имеет.

Область управления (get_scope) не используется: администратор видит
всех сотрудников организации, граница доступа — только право.
Кадровая карточка администратору не нужна: автор правки записывается
как пользователь (current_user.id).

Роут — тонкий слой: логика в admin/service.py, здесь перевод доменных
исключений в HTTP-коды и сборка текстов отказов.
"""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin.rules import AdminRejection, AdminRejectReason, DuplicateCellError
from app.api.v1.admin.schemas import (
    AdminApplyResult,
    AdminChangesIn,
    AdminGrid,
    AdminRejectionOut,
    ChangesetOut,
    EmployeeOption,
)
from app.api.v1.admin.service import (
    AdminRejectedError,
    ConcurrentChangeError,
    EmployeeNotFoundError,
    EmptyCommentError,
    InvalidIntervalError,
    MarkSetsNotFoundError,
    apply_admin_changes,
    list_changesets,
    list_employees,
    load_admin_grid,
)
from app.api.v1.fact.schemas import MarkSetFound
from app.api.v1.fact.service import (
    SEARCH_MIN_LENGTH,
    SearchTooShortError,
    search_mark_sets,
)
from app.core.dependencies import PermissionChecker, get_db
from app.models.user import User

router = APIRouter(tags=["admin"])

PLAN_OVERRIDE = "plan:override"
FACT_OVERRIDE = "fact:override"

# По одному экземпляру проверки на модуль: FastAPI кэширует зависимость
# в пределах запроса по самому объекту — пользователь проверяется один
# раз на каждое право, как бы ни были вложены зависимости.
require_plan_override = PermissionChecker(PLAN_OVERRIDE)
require_fact_override = PermissionChecker(FACT_OVERRIDE)

DATE_FORMAT = "%d.%m.%Y"

# Названия полос для текстов отказов.
_LAYER_NAMES = {"plan": "план", "fact": "факт"}


async def require_data_admin(
    user: Annotated[User, Depends(require_plan_override)],
    _fact: Annotated[User, Depends(require_fact_override)],
) -> User:
    """Оба права сразу — экран правит и план, и факт.

    Если права нет, отказ вернёт сам PermissionChecker (JSON 403 с
    именем недостающего права).
    """
    return user


def _describe(rejection: AdminRejection) -> str:
    """Текст отказа по одной ячейке — без SQL и id (правило 18)."""
    day = rejection.work_date.strftime(DATE_FORMAT)
    layer = _LAYER_NAMES[rejection.layer]
    reason = rejection.reason

    if reason is AdminRejectReason.OUT_OF_HORIZON:
        return f"{day}, {layer}: производственный календарь не заполнен."
    if reason is AdminRejectReason.NOT_ASSIGNED:
        return (
            f"{day}, {layer}: сотрудник не числится в штате на эту дату. "
            "Если дата приёма ошибочна, исправьте её в кадровых данных."
        )
    if reason is AdminRejectReason.NONWORK:
        return f"{day}, {layer}: нерабочий день."
    if reason is AdminRejectReason.ABSENCE:
        letter = rejection.tab_letter or "неявка"
        return (
            f"{day}, {layer}: день закрыт табелем ({letter}). "
            "Если неявка ошибочна, сначала её исправляет табельщица."
        )
    # FUTURE
    return f"{day}, {layer}: день ещё не наступил — факта о нём быть не может."


@router.get(
    "/employees",
    response_model=list[EmployeeOption],
    status_code=status.HTTP_200_OK,
    summary="Список сотрудников для выбора",
)
async def read_employees(
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: Annotated[User, Depends(require_data_admin)],
) -> list[EmployeeOption]:
    """Все сотрудники с действующим назначением: id, ФИО, код подразделения.

    Только имена — ни шифров, ни ячеек. Сетка грузится отдельно, по
    выбранному сотруднику.
    """
    return await list_employees(db, today=date.today())


@router.get(
    "/grid",
    response_model=AdminGrid,
    status_code=status.HTTP_200_OK,
    summary="Сетка плана и факта одного сотрудника",
)
async def read_grid(
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: Annotated[User, Depends(require_data_admin)],
    employee_id: Annotated[int, Query(gt=0, description="Сотрудник")],
    date_from: Annotated[date, Query(description="Начало интервала")],
    date_to: Annotated[date, Query(description="Конец интервала")],
) -> AdminGrid:
    """Строки-шифры, клетки плана и факта, флаги дней.

    days[].plan_allowed / fact_allowed — можно ли ПОСТАВИТЬ значение в
    полосу. Очистить ячейку можно всегда. Процессные ограничения (окно
    отметки, блокировка плана фактом, статус шифра) не действуют.

    Коды ошибок:
      403 — нет права plan:override или fact:override;
      404 — сотрудник не найден;
      422 — интервал бессмыслен или шире 120 дней.
    """
    try:
        return await load_admin_grid(
            db,
            employee_id=employee_id,
            date_from=date_from,
            date_to=date_to,
            today=date.today(),
        )
    except EmployeeNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Сотрудник не найден.",
        )
    except InvalidIntervalError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )


@router.get(
    "/mark-sets",
    response_model=list[MarkSetFound],
    status_code=status.HTTP_200_OK,
    summary="Поиск шифра любого статуса",
)
async def find_mark_sets(
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: Annotated[User, Depends(require_data_admin)],
    q: Annotated[
        str,
        Query(
            min_length=SEARCH_MIN_LENGTH,
            max_length=100,
            description="Фрагмент шифра",
        ),
    ],
) -> list[MarkSetFound]:
    """Шифры по подстроке — вся организация, ЛЮБОЙ статус.

    Тот же поиск, что у инженера, но без фильтра статусов: администратор
    исправляет и выпущенные комплекты. Близости к подразделению нет —
    администратор ни к какому подразделению не привязан.

    Коды ошибок:
      403 — нет прав;
      422 — строка поиска короче 5 символов.
    """
    try:
        return await search_mark_sets(db, department_id=None, query=q, status_ids=None)
    except SearchTooShortError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Введите не меньше {SEARCH_MIN_LENGTH} символов шифра.",
        )


@router.post(
    "/changes",
    response_model=AdminApplyResult,
    status_code=status.HTTP_200_OK,
    summary="Сохранение пачки правок плана и факта",
)
async def write_changes(
    payload: AdminChangesIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[User, Depends(require_data_admin)],
) -> AdminApplyResult:
    """Применяет правки одного сотрудника одной транзакцией и пишет журнал.

    cells — ЖЕЛАЕМОЕ ИТОГОВОЕ СОСТОЯНИЕ затронутых ячеек
    (mark_set_id = null — очистить). Diff сервер считает сам, сравнивая
    со снимком базы; ячейки, совпавшие с базой, в журнал не попадают.
    Если не изменилось ничего, changeset_id = null.

    comment — обязательный комментарий к правке (как сообщение коммита).

    Коды ошибок:
      403 — нет прав;
      404 — сотрудник или шифр не найден;
      409 — изменения нарушают целостность (тело: message и rejected
            по каждой ячейке) или данные изменились параллельно;
      422 — пустой комментарий или две операции на одну ячейку.
    """
    try:
        return await apply_admin_changes(
            db,
            employee_id=payload.employee_id,
            changed_by=admin.id,
            comment=payload.comment,
            cells=payload.cells,
            today=date.today(),
        )

    except EmptyCommentError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Комментарий к правке обязателен.",
        )

    except DuplicateCellError as exc:
        layer = _LAYER_NAMES[exc.layer]
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"{exc.work_date.strftime(DATE_FORMAT)}, {layer}: в правке две "
                "операции на одну ячейку — обновите страницу."
            ),
        )

    except EmployeeNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Сотрудник не найден.",
        )

    except MarkSetsNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Шифр не найден — возможно, его удалили. Обновите страницу.",
        )

    except AdminRejectedError as exc:
        # Формат как у планирования ({message, rejected}): parseResponse
        # на фронте разбирает оба одинаково.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Изменения не применены",
                "rejected": [
                    AdminRejectionOut(
                        layer=item.layer,
                        work_date=item.work_date,
                        reason=item.reason,
                        message=_describe(item),
                    ).model_dump(mode="json")
                    for item in exc.rejections
                ],
            },
        )

    except ConcurrentChangeError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Данные сотрудника изменились — обновите страницу.",
        )


@router.get(
    "/changesets",
    response_model=list[ChangesetOut],
    status_code=status.HTTP_200_OK,
    summary="Журнал правок сотрудника",
)
async def read_changesets(
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: Annotated[User, Depends(require_data_admin)],
    employee_id: Annotated[int, Query(gt=0, description="Сотрудник")],
) -> list[ChangesetOut]:
    """Последние правки сотрудника, свежие сверху, каждая со своим diff.

    Коды ошибок:
      403 — нет прав;
      404 — сотрудник не найден.
    """
    try:
        return await list_changesets(db, employee_id=employee_id)
    except EmployeeNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Сотрудник не найден.",
        )
