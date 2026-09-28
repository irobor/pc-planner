# app/web/routers/composition.py
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.core.dependencies import get_db
from app.core.guards import RequirePermissionHtml
from app.models import (
    Department,    
    Project,
    ProjectComposition,   
)
from app.core.dept_tree import (
    DeptNode,
    MarkaNode,
    build_composition_tree,
    collect_expanded_ids,
)
from app.models.user import User
from app.core.scope import Scope, get_scope_html
from app.web.templates import templates

router = APIRouter()

# Имя права из таблицы permissions, дающего доступ к составу проекта.
# Право на ПРОСМОТР состава (список, дерево). view_all = «сотрудник ПЦ»:
# любой внутренний пользователь видит внутренние страницы. Аналог
# require_user для внутренней части сайта (гости без view_all сюда не
# вхожи). Одно место правки, явная связь «этот URL под этим правом».
VIEW_PERMISSION = "composition:view"      # было registry:view_all
# id статуса «В работе» из справочника project_status.
# Состав показываем только для проектов в работе — завершённые,
# аннулированные и архивные (статусы 2–5) в списке не участвуют.
STATUS_IN_PROGRESS = 1
# ---------------------------------------------------------------------------
# Узлы дерева состава проекта.
#
# Дерево трёхуровневое: отдел → марка → комплект. Узлы — dataclass (а не
# словари), чтобы в Jinja2 обращаться через точку (node.department) и чтобы
# mypy проверял поля. Собираются функцией build_composition_tree ниже.
# ---------------------------------------------------------------------------

# Право на РЕДАКТИРОВАНИЕ состава (редактор-корзина, создание). Поверх
# просмотра: смотреть состав может любой сотрудник ПЦ (view_all),
# редактировать/создавать — только ГИП (project_composition). Двойной
# проверки не делаем: у обладателя project_composition по модели данных
# всегда есть и view_all (он сотрудник ПЦ).
EDIT_PERMISSION = "composition:manage"    # было registry:project_composition




# ---------------------------------------------------------------------------
# КРИТИЧНО — ПОРЯДОК: этот эндпоинт (/project-composition/new) должен быть
# объявлен РАНЬШЕ динамического /project-composition/{project_id}
# (project_composition_tree). Иначе "new" уйдёт в {project_id} как
# project_id="new" → FastAPI попытается int("new") → 422, а /new станет
# недостижим. Ставь его ВЫШЕ функции project_composition_tree в файле.
#
# Импорты — все уже есть в файле (Request, Depends, HTMLResponse,
# Annotated, RequirePermissionHtml, User, templates). Новых не нужно:
# эндпоинт не трогает БД. COMPOSITION_PERMISSION — добавлена в 2.3.2.
# ---------------------------------------------------------------------------


@router.get(
    "/project-composition/new",
    response_class=HTMLResponse,
    tags=["Web Состав проекта"],
    summary="Страница создания состава проекта (ввод шифра, вход B)",
)
async def project_composition_create_page(
    request: Request,
    # Право на редактирование (project_composition): создавать состав
    # может только ГИП, не любой сотрудник ПЦ.
    user: Annotated[
        User, Depends(RequirePermissionHtml(EDIT_PERMISSION))
    ],
) -> HTMLResponse:
    """Отдаёт каркас страницы создания состава (вход B).

    В отличие от редактора (/{id}/edit), проект здесь НЕ известен: ГИП
    выберет его, введя шифр (автодополнение /api/composition/project-search).
    Поэтому эндпоинт не грузит проект, не проверяет существование и не
    кладёт project_id в разметку — вся динамика (ввод шифра, выбор,
    монтирование корзины) в React. Задача эндпоинта — только отдать HTML
    с монтажным узлом и подключить бандл composition-create.bundle.js.
    """
    # БД не трогаем: создавать нечего, проверять нечего. Только каркас.
    return templates.TemplateResponse(
        "composition/create.html",
        {"request": request},
    )






@router.get(
    "/project-composition",
    response_class=HTMLResponse,
    tags=["Web Состав проекта"],
    summary="Список проектов с заполненным составом",
)
async def project_composition_page(
    request: Request,
    # Просмотр под VIEW_PERMISSION (view_all): любой сотрудник ПЦ видит
    # список составов. RequirePermissionHtml проводит каскад: гость →
    # /login, без права → 403, обладатель → сюда. user в шаблон не
    # прокидываем вручную — подставит инъектор (inject_user_context), но
    # зависимость объявить обязательно, иначе защита не подключится.
    user: Annotated[User, Depends(RequirePermissionHtml(VIEW_PERMISSION))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    """Страница списка составов проектов.

    Показывает только те проекты, у которых есть хотя бы одна строка в
    project_composition (проект без состава в списке не появляется).
    Сортировка воспроизводит порядок реестра (project_registry_view):
    сначала по году из шифра, затем по номеру внутри года.
    """
    # EXISTS-подзапрос: проект попадает в выборку, если для него существует
    # хотя бы одна строка состава. Это semi-join — планировщик PostgreSQL
    # останавливается на первой найденной строке, дублей нет, DISTINCT не
    # нужен (в отличие от INNER JOIN, который размножил бы проект по числу
    # строк состава).
    stmt = (
        select(Project)
        .where(Project.status_id == STATUS_IN_PROGRESS)
        .where(
            exists().where(ProjectComposition.project_id == Project.id)
        )
        # Сортировка по частям шифра, как в project_registry_view.
        # split_part возвращает text, поэтому порядок строковый (а не
        # числовой) — сознательно воспроизводим поведение реестра.
        # Формат шифра: НОМЕР-ГОД-... → часть 2 это год, часть 1 это номер.
        .order_by(
            func.split_part(Project.code, "-", 2).asc(),  # год: 15, 16…
            func.split_part(Project.code, "-", 1).asc(),  # номер в году
        )
    )

    result = await db.execute(stmt)
    # scalars().all() отдаёт список объектов Project, а не кортежей (Project,).
    projects = result.scalars().all()

    return templates.TemplateResponse(
        "composition/list.html",
        {"request": request, "projects": projects},
    )

@router.get(
    "/project-composition/{project_id}",
    response_class=HTMLResponse,
    tags=["Web Состав проекта"],
    summary="Дерево состава конкретного проекта",
)
async def project_composition_tree(
    request: Request,
    project_id: int,
    # Просмотр под VIEW_PERMISSION (view_all): дерево видит любой
    # сотрудник ПЦ.
    user: Annotated[User, Depends(RequirePermissionHtml(VIEW_PERMISSION))],
    # Область управления: ЧТО пользователь вправе менять внутри того,
    # что видит. Второе измерение RBAC, независимое от права выше.
    # Дерево ею не фильтруется — только размечается (см. шаг 5).
    scope: Annotated[Scope, Depends(get_scope_html)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    """Иерархическое дерево состава проекта (отдел → марка → комплект).

    Показывается только для проекта в статусе «В работе», у которого есть
    хотя бы одна строка состава. Если проект не существует, не в работе
    или его состав пуст — 404 (страница называется «Состав проекта»:
    планировать нечего, если состава нет).
    """
    # 1. Проект: должен существовать И быть в статусе «В работе».
    #    Оба условия в одном запросе — пустой результат трактуем как 404
    #    (не раскрываем, существует ли архивный/чужой проект, правило 18).
    project = (
        await db.execute(
            select(Project).where(
                Project.id == project_id,
                Project.status_id == STATUS_IN_PROGRESS,
            )
        )
    ).scalar_one_or_none()

    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Проект не найден",
        )

    # 2. Строки состава проекта с подгруженными связями.
    #    joinedload для «-к-одному» (department/marka_type/stage) — дёшево,
    #    без размножения строк. selectinload для коллекции mark_sets —
    #    отдельным батч-запросом, чтобы не плодить декартово произведение.
    comp_stmt = (
        select(ProjectComposition)
        .where(ProjectComposition.project_id == project_id)
        .options(
            joinedload(ProjectComposition.department),
            joinedload(ProjectComposition.marka_type),
            joinedload(ProjectComposition.stage),
            selectinload(ProjectComposition.mark_sets),
        )
    )
    compositions = list((await db.execute(comp_stmt)).scalars().all())

    # Пустой состав у валидного проекта → 404 (нечего планировать).
    # Экран о составе, а не о проекте: без состава показывать нечего.
    if not compositions:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Состав проекта не сформирован",
        )

    # 3. Все отделы справочника — для восстановления промежуточных
    #    родителей на пути к корню при сборке дерева.
    departments = list(
        (await db.execute(select(Department))).scalars().all()
    )

    # 4. Сборка дерева в Python (чистая функция, без БД/HTTP).
    tree = build_composition_tree(compositions, departments)

    # 5. Разметка области управления.
    #
    # Множество, а не флаг в DeptNode: узел остаётся чистой структурой,
    # как и решено для relevant внутри build_composition_tree. Признак
    # доступа — служебные данные, они живут рядом с деревом, а не в нём.
    #
    # Считаем от того же справочника departments, что уже прочитан для
    # сборки дерева. Второй SELECT внутри get_scope_html дублирует его —
    # осознанно: таблица в полтора десятка строк, а объявление области
    # в сигнатуре роута ценнее сэкономленного запроса.
    #
    # ВАЖНО: множество и узлы дерева не совпадают. У начальника ПЦ сюда
    # попадёт корень организации, которого в дереве нет (отбрасывается
    # как уровень отображения), а также отделы без марок. Лишние id
    # безвредны — проверка вхождения по ним просто не сработает.
    if scope.unrestricted:
        # Админ управляет всем, что видит: размечаем все отделы дерева.
        manageable_ids: set[int] = {d.id for d in departments}
    else:
        manageable_ids = set(scope.department_ids)

        # Какие ветки открыть при загрузке. Считается по УЖЕ собранному
    # дереву, а не по справочнику: раскрытие — свойство отображения.
    expanded_ids = collect_expanded_ids(tree, manageable_ids)

    return templates.TemplateResponse(
        "composition/tree.html",
        {
            "request": request,
            "project": project,
            "tree": tree,
            "expanded_ids": expanded_ids,
            # Отделы, доступные пользователю для управления. Шаблон гасит
            # по нему кнопки создания шифра и красит чужие ветки серым.
            "manageable_ids": manageable_ids,
        },
    )

   

# ---------------------------------------------------------------------------
# ДОПИСАТЬ в конец app/web/routers/composition.py
# (после project_composition_tree). Импорты — уже есть в файле:
# Request, Depends, HTTPException, status, select, AsyncSession,
# RequirePermissionHtml, get_db, Project, User, templates, Annotated,
# HTMLResponse, COMPOSITION_PERMISSION, STATUS_IN_PROGRESS.
#
# НОВОЕ: нужно право на РЕДАКТИРОВАНИЕ (не просмотр). Добавь рядом с
# COMPOSITION_PERMISSION константу:
#
#     COMPOSITION_PERMISSION = "registry:project_composition"
# ---------------------------------------------------------------------------


@router.get(
    "/project-composition/{project_id}/edit",
    response_class=HTMLResponse,
    tags=["Web Состав проекта"],
    summary="Страница-редактор состава проекта (React-корзина)",
)
async def project_composition_edit_page(
    request: Request,
    project_id: int,
    # Право на РЕДАКТИРОВАНИЕ (project_composition), а не на просмотр:
    # открыть корзину может только ГИП. RequirePermissionHtml проводит
    # каскад: гость → /login, без права → 403, обладатель → сюда.
    user: Annotated[
        User, Depends(RequirePermissionHtml(EDIT_PERMISSION))
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    """Отдаёт HTML-каркас редактора состава с монтажным узлом для React.

    Эта страница НЕ грузит состав — она лишь отдаёт разметку с
    <div id="composition-root">, куда монтируется бандл composition.bundle.js.
    Данные (справочник марок и текущий состав) React грузит сам через
    JSON-API (/api/composition/refs и /api/composition/{id}) при монтировании.

    Проект загружается только ради заголовка (шифр + название) и строгой
    проверки: несуществующий project_id → 404 (решение E1), чтобы не
    открывать редактор на мусорный id. project_id прокидывается в React
    через data-атрибут монтажного узла (решение E2), откуда компонент
    читает его как rootNode.dataset.projectId.
    """
    # Строгая проверка существования проекта (E1). В отличие от API-превью
    # (мягко отдаёт []), страницу-редактор на несуществующем проекте
    # открывать незачем — сразу 404. Грузим объект целиком: он нужен
    # шаблону для заголовка (project.code, project.title — решение E3).
    project = (
        await db.execute(
            select(Project).where(Project.id == project_id)
        )
    ).scalar_one_or_none()

    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Проект не найден",
        )

    # Отдаём каркас. project → в шаблон для заголовка и data-project-id.
    # user в шаблон вручную не кладём — его подставит inject_user_context,
    # но зависимость объявлена (иначе защита не подключится).
    return templates.TemplateResponse(
        "composition/edit.html",
        {"request": request, "project": project},
    )