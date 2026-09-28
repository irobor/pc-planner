# app/api/v1/composition/routes.py
"""JSON-API раздела «Состав проекта».

Слой 2.1 (read-only):
    GET  /refs           — справочник марок для формы (марка+стадия+отдел)
    GET  /{project_id}   — текущий состав проекта для наполнения превью

Слой 2.2 (запись):
    POST /{project_id}   — синхронизация состава (Решение X): приводит
                           состав к присланному списку марок

Все три защищены правом registry:edit_composition. Проверка — через
PermissionChecker (API-контур: при отсутствии права JSON 403, не HTML).

Роут — тонкий слой: вся логика в сервисе. Для POST роут ловит доменные
исключения оркестратора и маппит их в HTTP-коды (404/409/422); тексты
detail нейтральны, без утечки SQL/деталей БД (правило 18).

Резолвер шифра (вход B) и автодополнение проектов — отдельный под-слой
2.1-B, здесь их нет: они нужны только странице создания, которую строим
вместе с React (Слой 2.3).
"""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.composition.schemas import (
    CompositionRowOut,
    CompositionSyncIn,
    MarkaRefOut,
    ProjectSearchOut,
)
from app.api.v1.composition.service import (
    CompositionProtectedError,
    CompositionSaveError,
    MarkaResolveError,
    ProjectNotFoundError,
    get_composition_refs,
    get_project_composition,
    search_projects,
    sync_project_composition,
)
from app.core.dependencies import PermissionChecker, get_db
from app.models.user import User

router = APIRouter(tags=["composition"])

# Право на работу с составом проекта. Все эндпоинты модуля (справочник,
# чтение состава, автодополнение, POST-синхронизация) обслуживают редактор
# состава, поэтому требуют права редактирования "composition:manage"
EDIT_PERMISSION = "composition:manage"


# ВАЖНО: /refs объявлен РАНЬШE /{project_id}. Порядок критичен —
# /{project_id} ловит любой сегмент пути, и будь он выше, запрос к /refs
# ушёл бы в него с project_id="refs" → попытка int("refs") → 422, а
# сам /refs стал бы недостижим. Статические пути всегда выше динамических.
@router.get(
    "/refs",
    response_model=list[MarkaRefOut],
    summary="Справочник марок для формы состава",
)
async def read_composition_refs(
    db: Annotated[AsyncSession, Depends(get_db)],
    # current_user не используется в теле, но зависимость обязательна:
    # именно она проводит проверку права (иначе эндпоинт открыт всем).
    current_user: Annotated[User, Depends(PermissionChecker(EDIT_PERMISSION))],
) -> list[dict]:
    """Возвращает марки для формы редактора: марка + её стадия + её отдел.

    В справочник попадают только марки с ровно одним отделом и одной
    стадией (стратегия «строго один отдел», фильтр в сервисе). React
    грузит этот список при монтировании формы и дальше фильтрует локально
    (ГИП вводит код — dropdown сужается), к API повторно не обращаясь.
    """
    # Сервис отдаёт list[dict] с единичными department/stage; FastAPI
    # провалидирует каждый элемент схемой MarkaRefOut (response_model).
    return await get_composition_refs(db)


# /project-search — статический путь, объявлен ДО динамического
# /{project_id} (иначе "project-search" ушёл бы в него как project_id →
# int("project-search") → 422). Автодополнение шифра для входа B.
@router.get(
    "/project-search",
    response_model=list[ProjectSearchOut],
    summary="Автодополнение шифра проекта (создание состава)",
)
async def read_project_search(
    q: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(PermissionChecker(EDIT_PERMISSION))],
) -> list[dict]:
    """Ищет проекты «в работе» по началу шифра для страницы создания состава.

    Query-параметр q — введённый ГИПом фрагмент шифра. Возвращает проекты,
    чей шифр начинается на q (префиксный, регистронезависимый поиск), с
    флагом has_composition. Короткий q (< 3 символов) → пустой список.
    По выбранному элементу фронт открывает корзину: has_composition
    определяет — редактирование (состав есть) или создание (пусто).
    """
    # Сервис отдаёт list[dict] под ProjectSearchOut (response_model).
    return await search_projects(db, q)


@router.get(
    "/{project_id}",
    response_model=list[CompositionRowOut],
    summary="Текущий состав проекта",
)
async def read_project_composition(
    project_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(PermissionChecker(EDIT_PERMISSION))],
) -> list[CompositionRowOut]:
    """Возвращает текущий состав проекта для наполнения превью корзины.

    Пустой список — валидный ответ (у проекта ещё нет состава: вход B,
    создание с нуля). 404 здесь НЕ возвращаем сознательно: редактор
    должен открываться и на проекте без состава, чтобы ГИП начал его
    формировать. Проверку «проект существует и в работе» проводит POST-
    синхронизация (Слой 2.2), где это критично перед записью; для
    read-only превью достаточно отдать то, что есть (пусто — значит пусто).

    Существование проекта здесь не проверяем: несуществующий project_id
    даст пустой список (нет строк во вьюхе) — фронт трактует это как
    пустое превью. Отличать «проекта нет» от «состав пуст» на этом шаге
    не требуется (вход A всегда приходит с валидным id из дерева, вход B
    резолвит проект до открытия формы — под-слой 2.1-B).
    """
    # Сервис возвращает список ORM-объектов VProjectComposition; FastAPI
    # развернёт их в CompositionRowOut (has_mark_sets вычислит из
    # documents_count, сам счётчик в ответ не попадёт — Field(exclude=True)).
    return await get_project_composition(db, project_id)


@router.post(
    "/{project_id}",
    response_model=list[CompositionRowOut],
    status_code=status.HTTP_200_OK,
    summary="Синхронизация состава проекта",
)
async def sync_composition(
    project_id: int,
    payload: CompositionSyncIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(PermissionChecker(EDIT_PERMISSION))],
) -> list[CompositionRowOut]:
    """Приводит состав проекта к присланному списку пар марка+отдел (Решение X).

    Принимает финальный желаемый состав (items — пары marka_type_id +
    department_id) и синхронизирует строки project_composition: добавляет
    отсутствующие, удаляет лишние, совпадающие не трогает. Отдел приходит от
    клиента (ГИП выбрал его в форме для многоотдельных марок), стадию сервер
    выводит сам. Вся операция атомарна: при любой ошибке БД не меняется.
    Возвращает актуальный состав после синхронизации (status 200, не 201:
    состав — не новый ресурс с собственным URL, а приведённое к желаемому
    состояние).

    Коды ошибок:
      404 — проект не существует;
      409 — Защита A: среди удаляемых есть строки с комплектами (каскад
            снёс бы работу бюро) — операция отклонена целиком;
      422 — недопустимые пары (марки нет / отдел не вправе выпускать марку /
            стадия неоднозначна) ИЛИ ошибка БД при сохранении (нарушение
            триггера/уникальности).
    """
    # Роут — тонкая обёртка: вся работа в оркестраторе, здесь только
    # перевод доменных исключений в HTTP-коды (решение P1: try/except
    # локально, без глобальных handler'ов — эндпоинт единственный).
    try:
        # Оркестратор владеет транзакцией и возвращает готовые схемы
        # обновлённого состава (решение A6/model_validate в сервисе).
        return await sync_project_composition(
            db, project_id, payload.items
        )

    except ProjectNotFoundError:
        # Проекта нет — 404 с нейтральным текстом (id не раскрываем).
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Проект не найден",
        )

    except CompositionProtectedError as exc:
        # Защита A — 409 Conflict: запрос валиден, но конфликтует с
        # состоянием БД (есть зависимые комплекты). Текст исключения уже
        # нейтрален («Нельзя удалить марки с комплектами»), берём как есть.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        )

    except MarkaResolveError:
        # Недопустимые марки — 422. Список id наружу НЕ отдаём (он в логе,
        # правило 18), клиенту — обобщённое сообщение.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Обнаружены недопустимые марки",
        )

    except CompositionSaveError as exc:
        # Ошибка БД при записи (триггер/уникальность) — 422. Текст уже
        # нейтрален, без SQL/деталей (сформирован в сервисе, правило 18).
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )