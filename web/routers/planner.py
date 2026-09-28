# app/web/routers/planner.py
"""Web-роуты раздела «Планировщик».

Экран «Работы бюро» — рабочее место начальника бюро и начальника отдела:
шифры своих подразделений внутри выбранного состава проекта.

Отличие от «Состава проекта» (app/web/routers/composition.py): там витрина
ГИПа по проекту целиком, здесь — точка входа для заведения шифров и
планирования. Дерево строится тем же build_composition_tree, но набор
доступных составов ограничен областью управления пользователя.
"""
from typing import Annotated, Optional
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload
from sqlalchemy.exc import IntegrityError

from app.core.dependencies import get_db
from app.core.dept_tree import build_composition_tree, collect_expanded_ids
from app.core.guards import RequirePermissionHtml
from app.core.scope import Scope, get_scope_html
from app.models.composition import Department, ProjectComposition, ProjectMarkSet, SetStatus
from app.models.employee import Employee, EmployeeAssignment
from app.models.project import Project
from app.models.user import User
from app.web.templates import templates


router = APIRouter()

# Право на заведение шифров. Есть у обоих начальников; у инженера нет —
# он работы не создаёт, только исполняет.
SET_MANAGE_PERMISSION = "set:manage"
# set_statuses: 1 «В плане», 2 «В работе», 3 «На проверке», 4 «Выпущен».
# Новый шифр всегда стартует с «В плане»: работа ещё не начата, плана
# может не быть вовсе (назначение отделено от планирования).
STATUS_SET_PLANNED = 1
# Право на просмотр плана. Есть у обоих начальников, инженера, ГИПа и
# начальника ПЦ; нет у табельщицы и registry_editor — им планировщик
# не нужен, и пункт меню у них скрыт тем же правом.
PLAN_VIEW_PERMISSION = "plan:view"

# Право записи в планировщике. Страницу открывает plan:view, но клики
# по ячейкам и сохранение требуют plan:manage — инженер свою загрузку
# только смотрит.
PLAN_MANAGE_PERMISSION = "plan:manage"

# Значения project_status: 1 «В работе», 2 «Выполнен», 3 «Не в работе»,
# 4 «Аннулирован», 5 «Архив».
STATUS_IN_PROGRESS = 1
STATUS_DONE = 2

# Наборы статусов под фильтр в панели. None = без фильтра (все статусы).
# Пользователь по умолчанию видит текущую работу, но может добавить
# завершённые проекты — по ним остаются план и факт, и их иногда нужно
# посмотреть, хотя планировать там уже нечего.
STATUS_FILTERS: dict[str, Optional[tuple[int, ...]]] = {
    "active": (STATUS_IN_PROGRESS,),
    "done": (STATUS_IN_PROGRESS, STATUS_DONE),
    "all": None,
}
DEFAULT_STATUS_FILTER = "active"


async def _load_bureau_projects(
    db: AsyncSession,
    scope: Scope,
    status_ids: Optional[tuple[int, ...]],
) -> list[Project]:
    """Проекты, в составе которых участвуют подразделения области.

    Именно это отличает планировщик от журнала: не «все проекты», а
    только те, где у бюро есть строка состава. Список наполняет
    выпадающий фильтр и пустое состояние экрана.

    Пустой результат — законное состояние (у бюро может не быть работы),
    а не ошибка.
    """
    # EXISTS, а не JOIN: у бюро в проекте бывает несколько марок, и
    # INNER JOIN размножил бы проект по их числу. Semi-join
    # останавливается на первой найденной строке — DISTINCT не нужен.
    composition_exists = exists().where(
        ProjectComposition.project_id == Project.id
    )

    # Область управления сужает состав. Админ (unrestricted) видит все
    # составы: у него нет подразделения, и ограничивать нечем.
    if not scope.unrestricted:
        composition_exists = composition_exists.where(
            ProjectComposition.department_id.in_(scope.department_ids)
        )

    stmt = select(Project).where(composition_exists)

    if status_ids is not None:
        stmt = stmt.where(Project.status_id.in_(status_ids))

    # Порядок реестра (project_registry_view): год из шифра, затем номер
    # внутри года. Формат шифра НОМЕР-ГОД-… → часть 2 это год.
    stmt = stmt.order_by(
        func.split_part(Project.code, "-", 2).asc(),
        func.split_part(Project.code, "-", 1).asc(),
    )

    return list((await db.execute(stmt)).scalars().all())

def _form_int(value: Optional[str]) -> Optional[int]:
    """Пустая строка из <select> → None, иначе int.

    HTML-форма шлёт незаполненный select как "", а не как отсутствующее
    поле. Без этой нормализации Optional[int] в сигнатуре получил бы ""
    и упал бы на приведении типа.
    """
    if value is None or not value.strip():
        return None
    return int(value)

@router.get(
    "/planner/works",
    response_class=HTMLResponse,
    tags=["Web Планировщик"],
    summary="Работы бюро: шифры своих подразделений в составе проекта",
)
async def planner_works(
    request: Request,
    # Каскад RequirePermissionHtml: гость → /login, без права → 403,
    # обладатель → сюда. user в шаблон подставит инъектор, но зависимость
    # объявить обязательно, иначе защита не подключится.
    user: Annotated[User, Depends(RequirePermissionHtml(PLAN_VIEW_PERMISSION))],
    # Область управления. Здесь она работает ДВОЯКО: отбирает список
    # доступных составов и размечает ветки дерева. В «Составе проекта»
    # только размечает — там видимость шире управления по замыслу.
    scope: Annotated[Scope, Depends(get_scope_html)],
    db: Annotated[AsyncSession, Depends(get_db)],
    # Выбранный состав. None = состав ещё не выбран, показываем список.
    project: Optional[int] = None,
    # Фильтр статуса проекта. alias, потому что имя status занято
    # импортом fastapi.status в этом модуле.
    status_key: str = Query(DEFAULT_STATUS_FILTER, alias="status"),
) -> HTMLResponse:
    """Экран «Работы бюро» в двух состояниях.

    Без параметра project — список составов, где участвует подразделение
    пользователя. С параметром — дерево выбранного состава: отдел → бюро
    → марка → шифр, где чужие ветки приглушены и свёрнуты, а свои
    раскрыты.

    Дерево показывает состав ЦЕЛИКОМ, включая чужие отделы: начальник
    бюро обязан видеть смежников, иначе не поймёт, откуда придёт задание.
    Область ограничивает не видимость, а управление.
    """
    # Неизвестное значение фильтра трактуем как умолчание, а не как
    # ошибку: параметр приходит из URL, и 422 на опечатку в адресе —
    # избыточно жёсткая реакция.
    if status_key not in STATUS_FILTERS:
        status_key = DEFAULT_STATUS_FILTER
    status_ids = STATUS_FILTERS[status_key]

    # Список составов нужен в обоих состояниях: как содержимое пустого
    # экрана и как наполнение выпадающего фильтра над деревом.
    projects = await _load_bureau_projects(db, scope, status_ids)

    # Состояние 1: состав не выбран — предлагаем выбрать.
    if project is None:
        return templates.TemplateResponse(
            "planner/works_select.html",
            {
                "request": request,
                "projects": projects,
                "status_key": status_key,
            },
        )

    # Состояние 2: дерево выбранного состава.
    #
    # Проект ищем БЕЗ фильтра по статусу: фильтр управляет списком, а не
    # правом открыть конкретный состав. Иначе ссылка на завершённый
    # проект давала бы 404 при активном фильтре — при том что сам проект
    # существует и виден при другом значении фильтра.
    selected = (
        await db.execute(select(Project).where(Project.id == project))
    ).scalar_one_or_none()

    if selected is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Проект не найден",
        )

    # Строки состава со связями. joinedload для «-к-одному» (дёшево, без
    # размножения строк), selectinload для коллекции mark_sets — отдельным
    # батч-запросом, чтобы не плодить декартово произведение.
    comp_stmt = (
        select(ProjectComposition)
        .where(ProjectComposition.project_id == project)
        .options(
            joinedload(ProjectComposition.department),
            joinedload(ProjectComposition.marka_type),
            joinedload(ProjectComposition.stage),
            selectinload(ProjectComposition.mark_sets).selectinload(
                ProjectMarkSet.executor
            ),
        )
    )
    compositions = list((await db.execute(comp_stmt)).scalars().all())

    if not compositions:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Состав проекта не сформирован",
        )

    # Справочник отделов целиком — нужен для восстановления промежуточных
    # родителей на пути к корню при сборке дерева.
    departments = list((await db.execute(select(Department))).scalars().all())

    tree = build_composition_tree(compositions, departments)

    # Разметка области. Множествами, а не полями в узлах: узел остаётся
    # чистой структурой (то же решение, что для relevant внутри
    # build_composition_tree).
    if scope.unrestricted:
        # У админа department_ids пусто — короткое замыкание в _build_scope
        # не считает поддерево. Разворачиваем здесь, иначе он увидел бы
        # всё дерево приглушённым.
        manageable_ids: set[int] = {d.id for d in departments}
    else:
        manageable_ids = set(scope.department_ids)

    # Кандидаты в исполнители: сотрудники подразделений области с
    # актуальным назначением. Отдаём вместе с department_id — форма
    # покажет только тех, кто числится в бюро выбранной марки.
    today = date.today()
    executors = list(
        (
            await db.execute(
                select(
                    Employee.id,
                    Employee.full_name,
                    EmployeeAssignment.department_id,
                )
                .join(
                    EmployeeAssignment,
                    EmployeeAssignment.employee_id == Employee.id,
                )
                .where(
                    EmployeeAssignment.department_id.in_(manageable_ids),
                    EmployeeAssignment.valid_from <= today,
                    (EmployeeAssignment.valid_to.is_(None))
                    | (EmployeeAssignment.valid_to >= today),
                )
                .order_by(Employee.full_name)
            )
        ).all()
    )
        # Кандидаты в адресаты заданий: все строки состава этого проекта,
    # включая чужие бюро — задание тем и является, работой для смежника.
    # Второго запроса не нужно: compositions уже загружены выше вместе
    # со связями marka_type и department.
    recipients = [
        {
            "id": c.id,
            "label": f"{c.marka_type.code} — {c.department.name}",
        }
        for c in compositions
    ]
        # Справочник статусов для карточки шифра. Порядок — sort_order,
    # он для того и заведён.
    set_statuses = list(
        (
            await db.execute(
                select(SetStatus).order_by(SetStatus.sort_order, SetStatus.id)
            )
        ).scalars().all()
    )
    expanded_ids = collect_expanded_ids(tree, manageable_ids)

    return templates.TemplateResponse(
        "planner/works.html",
                {
            "request": request,
            "project": selected,
            "projects": projects,
            "status_key": status_key,
            "tree": tree,            
            "manageable_ids": manageable_ids,
            "expanded_ids": expanded_ids,
            # Право на заведение шифров. Передаётся флагом, потому что
            # макрос дерева подключён через import и инъектор can не
            # видит — контекст страницы ему недоступен.
            "can_manage_sets": user.can(SET_MANAGE_PERMISSION),
            "executors": executors,
            "recipients": recipients,
            "set_statuses": set_statuses,
        },
    )

@router.post(
    "/planner/works/{project_id}/sets",
    tags=["Web Планировщик"],
    summary="Создание шифра (комплекта или задания) под строкой состава",
)
async def planner_create_set(
    project_id: int,
    user: Annotated[User, Depends(RequirePermissionHtml(SET_MANAGE_PERMISSION))],
    scope: Annotated[Scope, Depends(get_scope_html)],
    db: Annotated[AsyncSession, Depends(get_db)],
    composition_id: Annotated[int, Form()],
    marka_full: Annotated[str, Form()],
    id_executor: Annotated[Optional[str], Form()] = None,
    recipient_composition_id: Annotated[Optional[str], Form()] = None,
) -> RedirectResponse:
    """Заводит комплект или задание под строкой состава своего бюро.

    Комплект и задание — одна сущность: различает их только заполненный
    recipient_composition_id. Отдельной ветки для «изменения» (#2) нет —
    это обычный комплект с другим текстом в шифре.

    Все проверки выполняются на сервере повторно, даже если интерфейс
    уже погасил кнопку: гашение — удобство, а не защита.
    """
    executor_id = _form_int(id_executor)
    recipient_id = _form_int(recipient_composition_id)

    # 1. Строка состава существует И принадлежит указанному проекту.
    #    Второе условие обязательно: composition_id приходит из формы,
    #    и без проверки шифр можно было бы завести в чужом проекте.
    composition = (
        await db.execute(
            select(ProjectComposition).where(
                ProjectComposition.id == composition_id,
                ProjectComposition.project_id == project_id,
            )
        )
    ).scalar_one_or_none()

    if composition is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Строка состава не найдена",
        )

    # 2. Область управления. Начальник отдела вправе заводить шифры во
    #    всех бюро своего поддерева — это следует из того, что права у
    #    него те же, а различает роли только область.
    if not scope.can_manage(composition.department_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Подразделение вне вашей области управления",
        )

    # 3. Шифр вводится вручную (автосборка отвергнута): проверяем только,
    #    что он непустой. Уникальность обеспечивает БД — см. шаг 7.
    code = marka_full.strip()
    if not code:
        return _back(project_id, composition_id, "empty")

    # 4. Адресат задания: должен существовать и лежать В ТОМ ЖЕ проекте.
    #    Проверка здесь, а не в БД: выразить её через CHECK нельзя
    #    (нужен подзапрос), а текст ошибки удобнее отдать отсюда.
    #    Самоадресацию ловит CHECK project_mark_sets_recipient_not_self,
    #    но проверяем и тут — чтобы вернуть внятное сообщение, а не 500.
    if recipient_id is not None:
        if recipient_id == composition_id:
            return _back(project_id, composition_id, "self")

        recipient = (
            await db.execute(
                select(ProjectComposition.id).where(
                    ProjectComposition.id == recipient_id,
                    ProjectComposition.project_id == project_id,
                )
            )
        ).scalar_one_or_none()

        if recipient is None:
            return _back(project_id, composition_id, "recipient")

    # 5. Исполнитель: обязан числиться в подразделении ЭТОЙ строки
    #    состава на сегодня. Без проверки в форму можно подставить любой
    #    id и назначить шифр сотруднику чужого бюро.
    if executor_id is not None:
        today = date.today()
        belongs = (
            await db.execute(
                select(EmployeeAssignment.id).where(
                    EmployeeAssignment.employee_id == executor_id,
                    EmployeeAssignment.department_id == composition.department_id,
                    EmployeeAssignment.valid_from <= today,
                    (EmployeeAssignment.valid_to.is_(None))
                    | (EmployeeAssignment.valid_to >= today),
                )
            )
        ).scalar_one_or_none()

        if belongs is None:
            return _back(project_id, composition_id, "executor")

    # 6. Создание. status_id обязателен и умолчания в схеме не имеет.
    mark_set = ProjectMarkSet(
        composition_id=composition_id,
        marka_full=code,
        id_executor=executor_id,
        status_id=STATUS_SET_PLANNED,
        recipient_composition_id=recipient_id,
    )
    db.add(mark_set)

    # 7. marka_full уникален ПО ВСЕЙ БАЗЕ (project_mark_sets_marka_full_key),
    #    а не в рамках проекта. Повтор даёт SQLSTATE 23505 — перехватываем,
    #    иначе пользователь получил бы 500 вместо объяснения.
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return _back(project_id, composition_id, "duplicate")

    return _back(project_id, composition_id, None)


def _back(
    project_id: int,
    composition_id: int,
    error: Optional[str],
) -> RedirectResponse:
    """Редирект обратно на дерево, при необходимости с кодом ошибки.

    303 See Other, а не 302: после POST браузер обязан сделать GET, иначе
    обновление страницы повторит отправку формы.

    Ошибка передаётся кодом, а не текстом: сообщение собирает шаблон, и
    его не приходится экранировать в URL.
    """
    url = f"/planner/works?project={project_id}"
    if error is not None:
        url += f"&error={error}&composition={composition_id}"
    return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)

@router.post(
    "/planner/works/{project_id}/sets/{set_id}",
    tags=["Web Планировщик"],
    summary="Сохранение карточки шифра",
)
async def planner_update_set(
    project_id: int,
    set_id: int,
    user: Annotated[User, Depends(RequirePermissionHtml(SET_MANAGE_PERMISSION))],
    scope: Annotated[Scope, Depends(get_scope_html)],
    db: Annotated[AsyncSession, Depends(get_db)],
    marka_full: Annotated[str, Form()],
    status_id: Annotated[int, Form()],
    # ВАЖНО: None здесь означает «поле не пришло вовсе», а пустая строка —
    # «пришло пустым». Для исполнителя это разные команды: не трогать
    # против снять. Поле отправляется только после нажатия «Сменить»,
    # поэтому карточка уволенного сотрудника сохраняется без потери
    # исполнителя — отправлять просто нечего.
    id_executor: Annotated[Optional[str], Form()] = None,
    recipient_composition_id: Annotated[Optional[str], Form()] = None,
) -> RedirectResponse:
    """Правит шифр, исполнителя, статус и адресат задания.

    Перенос шифра под другую марку здесь не делается: это операция с
    другими последствиями, и composition_id формой не принимается.

    План (даты, дни) не трогаем — он живёт в work_plan и меняется на
    экране планирования. Обрезка плана при смене исполнителя появится
    вместе с ним: сейчас обрезать нечего.
    """
    # 1. Шифр существует и лежит в указанном проекте. Второе условие
    #    обязательно: set_id приходит из формы, и без проверки можно было
    #    бы править шифр чужого проекта, подставив другой id в адрес.
    mark_set = (
        await db.execute(
            select(ProjectMarkSet)
            .join(
                ProjectComposition,
                ProjectComposition.id == ProjectMarkSet.composition_id,
            )
            .where(
                ProjectMarkSet.id == set_id,
                ProjectComposition.project_id == project_id,
            )
        )
    ).scalar_one_or_none()

    if mark_set is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Шифр не найден",
        )

    # 2. Строка состава нужна для проверки области и принадлежности
    #    исполнителя. Отдельным запросом, а не relationship: связь
    #    объявлена с lazy="raise_on_sql", и ленивая подгрузка тут
    #    осознанно запрещена.
    composition = (
        await db.execute(
            select(ProjectComposition).where(
                ProjectComposition.id == mark_set.composition_id
            )
        )
    ).scalar_one()

    # 3. Область управления. Гашение строки в интерфейсе защитой не
    #    является — чужой шифр не кликабелен, но форму можно отправить
    #    и напрямую.
    if not scope.can_manage(composition.department_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Подразделение вне вашей области управления",
        )

    # 4. Шифр: непустой. Уникальность обеспечивает БД — см. шаг 9.
    code = marka_full.strip()
    if not code:
        return _back(project_id, mark_set.composition_id, "empty")

    # 5. Статус: значение из справочника, а не произвольное число.
    #    Порядок переходов свободный — статусы пока ни на что не влияют,
    #    и ограничение мешало бы исправлять ошибки.
    status_exists = (
        await db.execute(select(SetStatus.id).where(SetStatus.id == status_id))
    ).scalar_one_or_none()

    if status_exists is None:
        return _back(project_id, mark_set.composition_id, "status")

    # 6. Адресат задания. Поле есть всегда (галочка лишь прячет его),
    #    поэтому пустое значение означает «это обычный комплект».
    recipient_id = _form_int(recipient_composition_id)

    if recipient_id is not None:
        if recipient_id == mark_set.composition_id:
            return _back(project_id, mark_set.composition_id, "self")

        recipient = (
            await db.execute(
                select(ProjectComposition.id).where(
                    ProjectComposition.id == recipient_id,
                    ProjectComposition.project_id == project_id,
                )
            )
        ).scalar_one_or_none()

        if recipient is None:
            return _back(project_id, mark_set.composition_id, "recipient")

    # 7. Исполнитель — три случая, и различие принципиально.
    if id_executor is None:
        # Поля нет: кнопку «Сменить» не нажимали. Значение сохраняется
        # как есть. Именно этот случай защищает шифры уволенных: их
        # исполнителя нет в списке, и обнулить его случайно нельзя.
        new_executor_id = mark_set.id_executor
    else:
        new_executor_id = _form_int(id_executor)

        # Назначение нового: сотрудник обязан числиться в подразделении
        # ЭТОЙ строки состава на сегодня.
        if new_executor_id is not None:
            today = date.today()
            belongs = (
                await db.execute(
                    select(EmployeeAssignment.id).where(
                        EmployeeAssignment.employee_id == new_executor_id,
                        EmployeeAssignment.department_id
                        == composition.department_id,
                        EmployeeAssignment.valid_from <= today,
                        (EmployeeAssignment.valid_to.is_(None))
                        | (EmployeeAssignment.valid_to >= today),
                    )
                )
            ).scalar_one_or_none()

            if belongs is None:
                return _back(project_id, mark_set.composition_id, "executor")

    # 8. Присваиваем. Объект уже в сессии — отдельный UPDATE не нужен,
    #    SQLAlchemy соберёт его сам при commit по изменённым полям.
    mark_set.marka_full = code
    mark_set.status_id = status_id
    mark_set.id_executor = new_executor_id
    mark_set.recipient_composition_id = recipient_id

    # 9. marka_full уникален ПО ВСЕЙ БАЗЕ. Переименование в уже занятый
    #    шифр даёт SQLSTATE 23505 — перехватываем, иначе 500 вместо
    #    объяснения.
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return _back(project_id, mark_set.composition_id, "duplicate")

    return _back(project_id, mark_set.composition_id, None)



async def _load_manageable_departments(
    db: AsyncSession,
    scope: Scope,
) -> list[Department]:
    """Подразделения, чью загрузку пользователь вправе смотреть.

    Отбираются только те, где КТО-ТО ЧИСЛИТСЯ: пустое подразделение в
    выпадающем списке даёт пустую сетку и выглядит как поломка. Проверка
    через EXISTS, а не через join — join размножил бы строки по числу
    назначений, и пришлось бы городить DISTINCT.

    Область применяется тем же правилом, что и везде: администратор
    (unrestricted) видит всё, остальные — своё поддерево. Пустая область
    отсекается заранее, чтобы не гонять запрос ради заведомо пустого
    результата.
    """
    if scope.is_empty:
        return []

    # Коррелированный подзапрос: связь с внешним Department идёт по
    # department_id, поэтому EXISTS вычисляется для каждой строки.
    has_staff = exists().where(
        EmployeeAssignment.department_id == Department.id
    )
    stmt = select(Department).where(has_staff).order_by(Department.code)

    if not scope.unrestricted:
        stmt = stmt.where(Department.id.in_(scope.department_ids))

    return list((await db.execute(stmt)).scalars().all())


@router.get(
    "/planner/workload",
    response_class=HTMLResponse,
    tags=["Web Планировщик"],
    summary="Загрузка бюро: календарная сетка плана и факта",
)
async def planner_workload(
    request: Request,
    # Каскад RequirePermissionHtml: гость → /login, без права → 403.
    # Право просмотра, а не управления: сетку видит и инженер — свою
    # занятость. Право plan:manage проверяется в API при записи.
    user: Annotated[User, Depends(RequirePermissionHtml(PLAN_VIEW_PERMISSION))],
    scope: Annotated[Scope, Depends(get_scope_html)],
    db: Annotated[AsyncSession, Depends(get_db)],
    # Выбранное подразделение. None = берём первое доступное.
    department: Optional[int] = None,
) -> HTMLResponse:
    """Оболочка экрана «Загрузка бюро» под React-остров.

    Страница данных сетки НЕ отдаёт: их островок забирает сам через
    GET /api/planning/workload. Здесь только каркас — выбор
    подразделения, точка монтирования и начальные параметры в data-*.

    Разделение сознательное. Отрисуй сервер сетку в Jinja2, островку
    пришлось бы её разбирать обратно, чтобы накапливать изменения. А
    отдай он ещё и JSON внутри страницы — данные существовали бы в двух
    экземплярах и разошлись бы при первом же сохранении.

    Выбор подразделения, наоборот, остаётся на сервере: список зависит
    от области управления, а её вычисление требует обхода дерева
    отделов. Дублировать это в браузере незачем.
    """
    departments = await _load_manageable_departments(db, scope)

    # Сверяем пришедший id со списком доступных. Чужой или несуществующий
    # id молча заменяем первым доступным, а не отвечаем 403: параметр
    # приходит из адресной строки, и жёсткая реакция на устаревшую
    # закладку избыточна. Реальную защиту держит API — там тот же scope.
    allowed_ids = {item.id for item in departments}
    if department not in allowed_ids:
        department = departments[0].id if departments else None

    return templates.TemplateResponse(
        "planner/workload.html",
        {
            "request": request,
            "departments": departments,
            "selected_department_id": department,
            # Дата центрирования сетки. Считается на сервере: часовой
            # пояс браузера может отличаться от рабочего, и «сегодня» у
            # пользователя в другом городе уехало бы на день.
            "today": date.today().isoformat(),
                        # Признак права записи для React-острова. Проверка здесь —
            # только для интерфейса: настоящую защиту держит API, где
            # PermissionChecker(plan:manage) стоит на обеих ручках.
            "can_edit": user.can(PLAN_MANAGE_PERMISSION),
        },
    )