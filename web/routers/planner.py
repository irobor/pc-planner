# app/web/routers/planner.py
"""Web-роуты раздела «Планировщик».

Экран «Работы бюро» — рабочее место начальника бюро и начальника отдела:
шифры своих подразделений внутри выбранного состава проекта.

Отличие от «Состава проекта» (app/web/routers/composition.py): там витрина
ГИПа по проекту целиком, здесь — точка входа для заведения шифров и
планирования. Дерево строится тем же build_composition_tree, но набор
доступных составов ограничен областью управления пользователя.
"""
from dataclasses import dataclass
from typing import Annotated, Optional
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload
from sqlalchemy.exc import IntegrityError

from app.core.assignments import is_current_assignment
from app.core.dependencies import get_db
from app.core.dept_tree import build_composition_tree, collect_expanded_ids
from app.core.guards import RequirePermissionHtml
from app.core.scope import Scope, get_scope_html
from app.models.composition import (
    Department,
    PackageStatus,
    ProjectComposition,
    ProjectMarkSet,
    WorkPackage,
)
from app.models.employee import Employee, EmployeeAssignment
from app.models.project import Project
from app.models.user import User
from app.models.planning import FactCalendarCell, PlanCalendarCell
from app.web.templates import templates


router = APIRouter()

# Право на заведение шифров. Есть у обоих начальников; у инженера нет —
# он работы не создаёт, только исполняет.
SET_MANAGE_PERMISSION = "set:manage"

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

@dataclass(frozen=True)
class PlanSpan:
    """Сводка плана: сколько дней положено и в каких границах.

    Шапки плана в базе нет — план это только ячейки, поэтому границы и
    число дней вычисляются из положенного при каждом показе.
    """

    days: int
    start: date
    end: date


@dataclass(frozen=True)
class SetSummary:
    """Сводка комплекта для строки шифра в дереве.

    done / packages — готовность: сколько пакетов в статусе с признаком
    «разработка завершена». plan — общий срок по всем пакетам, None —
    ни один пакет ещё не спланирован.
    """

    packages: int
    done: int
    plan: Optional[PlanSpan]

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

def _form_date(value: Optional[str]) -> Optional[date]:
    """Пустая строка из <input type="date"> → None, иначе дата.

    Браузер шлёт дату в ISO (ГГГГ-ММ-ДД). Неверная строка даёт
    ValueError — роут превращает её в понятное сообщение, а не в 500.
    """
    if value is None or not value.strip():
        return None
    return date.fromisoformat(value.strip())

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
    # размножения строк), selectinload для коллекций — отдельными
    # батч-запросами, чтобы не плодить декартово произведение.
    # Цепочка: строка состава → шифры → пакеты → исполнитель и статус
    # пакета. Связи объявлены с lazy="raise_on_sql", поэтому всё, что
    # прочтёт шаблон, должно быть загружено здесь явно.
    comp_stmt = (
        select(ProjectComposition)
        .where(ProjectComposition.project_id == project)
        .options(
            joinedload(ProjectComposition.department),
            joinedload(ProjectComposition.marka_type),
            joinedload(ProjectComposition.stage),
            selectinload(ProjectComposition.mark_sets)
            .selectinload(ProjectMarkSet.packages)
            .options(
                joinedload(WorkPackage.executor),
                joinedload(WorkPackage.status),
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

    # Кандидаты в исполнители пакета: сотрудники подразделений области с
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
                    is_current_assignment(today),
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

    # Справочник статусов пакета для карточки пакета. Порядок — sort_order,
    # он для того и заведён.
    package_statuses = list(
        (
            await db.execute(
                select(PackageStatus).order_by(
                    PackageStatus.sort_order, PackageStatus.id
                )
            )
        )
        .scalars()
        .all()
    )

    # Сводка плана по пакетам — для колонок «Дни» и «Срок». Одним
    # агрегирующим запросом по плановым ячейкам, а не по запросу на пакет.
    mark_sets = [ms for c in compositions for ms in c.mark_sets]
    package_ids = [p.id for ms in mark_sets for p in ms.packages]
    package_summary: dict[int, PlanSpan] = {}
    if package_ids:
        plan_rows = await db.execute(
            select(
                PlanCalendarCell.package_id,
                func.count(PlanCalendarCell.id),
                func.min(PlanCalendarCell.work_date),
                func.max(PlanCalendarCell.work_date),
            )
            .where(PlanCalendarCell.package_id.in_(package_ids))
            .group_by(PlanCalendarCell.package_id)
        )
        package_summary = {
            package_id: PlanSpan(days=days, start=first_day, end=last_day)
            for package_id, days, first_day, last_day in plan_rows.all()
        }

    # Сводка по шифру — из уже загруженного, без запросов к базе.
    # Готовность берёт признак «разработка завершена» из справочника
    # (status.is_done), а не из списка id в коде. Срок шифра — от самого
    # раннего первого дня его пакетов до самого позднего последнего.
    set_summary: dict[int, SetSummary] = {}
    for ms in mark_sets:
        spans = [package_summary[p.id] for p in ms.packages if p.id in package_summary]
        set_summary[ms.id] = SetSummary(
            packages=len(ms.packages),
            done=sum(1 for p in ms.packages if p.status.is_done),
            plan=(
                PlanSpan(
                    days=sum(sp.days for sp in spans),
                    start=min(sp.start for sp in spans),
                    end=max(sp.end for sp in spans),
                )
                if spans
                else None
            ),
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
            "package_statuses": package_statuses,
            # Для макроса дерева — он подключён через import и контекст
            # страницы не видит, поэтому данные передаются аргументами.
            "package_summary": package_summary,
            "set_summary": set_summary,
            # Верхняя граница даты выпуска в карточке шифра: выпуск в
            # будущем невозможен по смыслу.
            "today": today,
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
    recipient_composition_id: Annotated[Optional[str], Form()] = None,
) -> RedirectResponse:
    """Заводит комплект или задание под строкой состава своего бюро.

    Комплект и задание — одна сущность: различает их только заполненный
    recipient_composition_id. Отдельной ветки для «изменения» (#2) нет —
    это обычный комплект с другим текстом в шифре.

    Комплект заводится без исполнителя и без статуса: работу внутри него
    делают пакеты, их начальник заводит следом. У комплекта остаётся
    только дата выпуска — она пуста, пока комплект не выпущен.

    Все проверки выполняются на сервере повторно, даже если интерфейс
    уже погасил кнопку: гашение — удобство, а не защита.
    """
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
    #    что он непустой. Уникальность обеспечивает БД — см. шаг 6.
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

    # 5. Создание.
    db.add(
        ProjectMarkSet(
            composition_id=composition_id,
            marka_full=code,
            recipient_composition_id=recipient_id,
        )
    )

    # 6. marka_full уникален ПО ВСЕЙ БАЗЕ (project_mark_sets_marka_full_key),
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
    # Пусто — комплект не выпущен; дата — выпущен в этот день.
    released_on: Annotated[Optional[str], Form()] = None,
    recipient_composition_id: Annotated[Optional[str], Form()] = None,
) -> RedirectResponse:
    """Правит шифр, адресат задания и дату выпуска.

    Исполнителя и статуса у комплекта больше нет — они у пакетов и
    правятся в карточке пакета.

    «Выпущен» ставится при любой готовности пакетов: начальник видит
    готовность рядом с датой и решает сам. Запрет отнял бы возможность
    выпустить комплект, если исполнитель забыл закрыть свой пакет.

    Перенос шифра под другую марку здесь не делается: это операция с
    другими последствиями, и composition_id формой не принимается.
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

    composition_id = mark_set.composition_id

    # 2. Область управления. Гашение строки в интерфейсе защитой не
    #    является — чужой шифр не кликабелен, но форму можно отправить
    #    и напрямую. Нужен только отдел строки состава — одним полем.
    department_id = (
        await db.execute(
            select(ProjectComposition.department_id).where(
                ProjectComposition.id == composition_id
            )
        )
    ).scalar_one()

    if not scope.can_manage(department_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Подразделение вне вашей области управления",
        )

    # 3. Шифр: непустой. Уникальность обеспечивает БД — см. шаг 7.
    code = marka_full.strip()
    if not code:
        return _back(project_id, composition_id, "empty")

    # 4. Дата выпуска: не позже сегодняшней — выпуск в будущем
    #    невозможен по смыслу. Неразборчивая дата — понятное сообщение.
    try:
        release_date = _form_date(released_on)
    except ValueError:
        return _back(project_id, composition_id, "release_date")

    if release_date is not None and release_date > date.today():
        return _back(project_id, composition_id, "release_future")

    # 5. Адресат задания. Поле есть всегда (галочка лишь прячет его),
    #    поэтому пустое значение означает «это обычный комплект».
    recipient_id = _form_int(recipient_composition_id)

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

    # 6. Присваиваем. Объект уже в сессии — отдельный UPDATE не нужен,
    #    SQLAlchemy соберёт его сам при commit по изменённым полям.
    mark_set.marka_full = code
    mark_set.released_on = release_date
    mark_set.recipient_composition_id = recipient_id

    # 7. marka_full уникален ПО ВСЕЙ БАЗЕ. Переименование в уже занятый
    #    шифр даёт SQLSTATE 23505 — перехватываем, иначе 500 вместо
    #    объяснения.
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return _back(project_id, composition_id, "duplicate")

    return _back(project_id, composition_id, None)


@router.post(
    "/planner/works/{project_id}/sets/{set_id}/delete",
    tags=["Web Планировщик"],
    summary="Удаление ошибочно заведённого шифра",
)
async def planner_delete_set(
    project_id: int,
    set_id: int,
    user: Annotated[User, Depends(RequirePermissionHtml(SET_MANAGE_PERMISSION))],
    scope: Annotated[Scope, Depends(get_scope_html)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RedirectResponse:
    """Удаляет шифр, по которому ничего не делалось.

    Удалять можно только ошибочно заведённый шифр: не выпущенный, и ни
    у одного его пакета нет ни плана, ни факта. Всё остальное — через
    выпуск или, в будущем, через администратора данных.

    Внешние ключи на шифр и на пакеты — CASCADE, и это решение
    сохраняется: администратору понадобится удалять шифры вместе с
    историей одной операцией. Поэтому запрет «план и факт не трогать»
    держит этот роут, а не схема.
    """
    # 1. Шифр существует в этом проекте — и СРАЗУ блокируется FOR UPDATE.
    #    Новый пакет под этим шифром проверяет внешний ключ, беря на
    #    строку шифра FOR KEY SHARE, — она конфликтует с нашим FOR UPDATE,
    #    и пакет не появится между проверками и удалением.
    #    of=ProjectMarkSet: блокируем только шифр, строку состава — нет.
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
            .with_for_update(of=ProjectMarkSet)
        )
    ).scalar_one_or_none()

    if mark_set is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Шифр не найден",
        )

    # Нужен для редиректа: после DELETE объект читать уже нельзя.
    composition_id = mark_set.composition_id

    # 2. Пакеты шифра — тоже FOR UPDATE. Без этого защита от гонки не
    #    работает: план и факт ссылаются на ПАКЕТ, и вставка отметки
    #    берёт FOR KEY SHARE на строку пакета, а не шифра. Блокировка
    #    одного шифра её не остановила бы: инженер поставил бы отметку
    #    между проверкой «факта нет» и DELETE, и каскад унёс бы её молча.
    #    С блокировкой пакетов конкурент ждёт конца нашей транзакции и
    #    получает ошибку внешнего ключа вместо тихой потери.
    await db.execute(
        select(WorkPackage.id)
        .where(WorkPackage.mark_set_id == set_id)
        .with_for_update()
    )

    # 3. Область управления. Строка шифра чужого бюро не кликабельна, но
    #    форму можно отправить и напрямую.
    department_id = (
        await db.execute(
            select(ProjectComposition.department_id).where(
                ProjectComposition.id == composition_id
            )
        )
    ).scalar_one()

    if not scope.can_manage(department_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Подразделение вне вашей области управления",
        )

    # 4. Выпущенный шифр — документ, сданный заказчику. Не удаляется
    #    никогда, ни с историей, ни без.
    if mark_set.released_on is not None:
        return _back(project_id, composition_id, "released")

    # 5. План хоть одного пакета шифра.
    has_plan = (
        await db.execute(
            select(PlanCalendarCell.id)
            .join(WorkPackage, WorkPackage.id == PlanCalendarCell.package_id)
            .where(WorkPackage.mark_set_id == set_id)
            .limit(1)
        )
    ).scalar_one_or_none()

    if has_plan is not None:
        return _back(project_id, composition_id, "has_plan")

    # 6. Факт хоть одного пакета шифра — свидетельство инженера о своей
    #    работе, восстановить его неоткуда. Шифр с фактом удаляет только
    #    администратор данных.
    has_fact = (
        await db.execute(
            select(FactCalendarCell.id)
            .join(WorkPackage, WorkPackage.id == FactCalendarCell.package_id)
            .where(WorkPackage.mark_set_id == set_id)
            .limit(1)
        )
    ).scalar_one_or_none()

    if has_fact is not None:
        return _back(project_id, composition_id, "has_fact")

    # 7. Удаление. Core-запросом, а не session.delete(): ORM-удаление
    #    стало бы разбирать связи объекта, а каскад на пакеты, а с ними
    #    на план и факт, делает база.
    await db.execute(delete(ProjectMarkSet).where(ProjectMarkSet.id == set_id))
    await db.commit()

    return _back(project_id, composition_id, None)


@dataclass(frozen=True)
class PackageInProject:
    """Пакет, найденный в проекте, с тем, что нужно для проверок."""

    package: WorkPackage
    composition_id: int
    department_id: int


async def _load_package_in_project(
    db: AsyncSession,
    project_id: int,
    package_id: int,
    *,
    lock: bool = False,
) -> PackageInProject:
    """Пакет вместе со строкой состава его шифра или 404.

    Проверка «шифр пакета лежит в этом проекте» обязательна: package_id
    приходит из адреса, и без неё можно было бы править пакет чужого
    проекта, подставив другой id.

    lock=True — пакет блокируется FOR UPDATE. Нужно удалению: вставка
    плановой или фактической ячейки берёт FOR KEY SHARE на строку пакета
    и будет ждать, пока мы проверяем «плана и факта нет» и удаляем.
    """
    stmt = (
        select(
            WorkPackage,
            ProjectComposition.id,
            ProjectComposition.department_id,
        )
        .join(ProjectMarkSet, ProjectMarkSet.id == WorkPackage.mark_set_id)
        .join(
            ProjectComposition,
            ProjectComposition.id == ProjectMarkSet.composition_id,
        )
        .where(
            WorkPackage.id == package_id,
            ProjectComposition.project_id == project_id,
        )
    )
    if lock:
        # of=WorkPackage: блокируем только пакет, шифр и состав — нет.
        stmt = stmt.with_for_update(of=WorkPackage)

    row = (await db.execute(stmt)).one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Пакет не найден",
        )
    package, composition_id, department_id = row
    return PackageInProject(package, composition_id, department_id)


async def _executor_belongs(
    db: AsyncSession, employee_id: int, department_id: int
) -> bool:
    """Числится ли сотрудник в подразделении на сегодня.

    Без этой проверки в форму можно подставить любой id и назначить
    пакет сотруднику чужого бюро. Правило «назначение действует на дату»
    берётся из core/assignments.py — там оно записано один раз.
    """
    found = (
        await db.execute(
            select(EmployeeAssignment.id).where(
                EmployeeAssignment.employee_id == employee_id,
                EmployeeAssignment.department_id == department_id,
                is_current_assignment(date.today()),
            )
        )
    ).first()
    return found is not None


@router.post(
    "/planner/works/{project_id}/sets/{set_id}/packages",
    tags=["Web Планировщик"],
    summary="Создание пакета работ в комплекте",
)
async def planner_create_package(
    project_id: int,
    set_id: int,
    user: Annotated[User, Depends(RequirePermissionHtml(SET_MANAGE_PERMISSION))],
    scope: Annotated[Scope, Depends(get_scope_html)],
    db: Annotated[AsyncSession, Depends(get_db)],
    name: Annotated[str, Form()],
    description: Annotated[Optional[str], Form()] = None,
    executor_id: Annotated[Optional[str], Form()] = None,
) -> RedirectResponse:
    """Заводит пакет работ в комплекте своего бюро.

    Начальник называет пакет по смыслу работы («поддоны», «опросник»,
    «полный») и может сразу назначить исполнителя — или позже: пакет без
    исполнителя законен, его только нельзя спланировать.

    Статус не задаётся: новый пакет — «В работе» по умолчанию базы, пакет
    создают уже для планирования.
    """
    # 1. Шифр существует и лежит в этом проекте; отдел — для области.
    row = (
        await db.execute(
            select(ProjectMarkSet.composition_id, ProjectComposition.department_id)
            .join(
                ProjectComposition,
                ProjectComposition.id == ProjectMarkSet.composition_id,
            )
            .where(
                ProjectMarkSet.id == set_id,
                ProjectComposition.project_id == project_id,
            )
        )
    ).one_or_none()

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Шифр не найден",
        )
    composition_id, department_id = row

    # 2. Область управления.
    if not scope.can_manage(department_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Подразделение вне вашей области управления",
        )

    # 3. Название непустое. То же запрещает и CHECK в базе — проверяем
    #    здесь, чтобы вернуть понятное сообщение, а не 500.
    package_name = name.strip()
    if not package_name:
        return _back(project_id, composition_id, "package_empty")

    # 4. Исполнитель, если выбран, числится в бюро этой марки.
    new_executor = _form_int(executor_id)
    if new_executor is not None and not await _executor_belongs(
        db, new_executor, department_id
    ):
        return _back(project_id, composition_id, "executor")

    db.add(
        WorkPackage(
            mark_set_id=set_id,
            name=package_name,
            description=(description or "").strip() or None,
            executor_id=new_executor,
        )
    )

    # 5. Название уникально в пределах шифра без учёта регистра — это
    #    держит уникальный индекс базы. Повтор даёт SQLSTATE 23505.
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return _back(project_id, composition_id, "package_duplicate")

    return _back(project_id, composition_id, None)


@router.post(
    "/planner/works/{project_id}/packages/{package_id}",
    tags=["Web Планировщик"],
    summary="Сохранение карточки пакета работ",
)
async def planner_update_package(
    project_id: int,
    package_id: int,
    user: Annotated[User, Depends(RequirePermissionHtml(SET_MANAGE_PERMISSION))],
    scope: Annotated[Scope, Depends(get_scope_html)],
    db: Annotated[AsyncSession, Depends(get_db)],
    name: Annotated[str, Form()],
    status_id: Annotated[int, Form()],
    description: Annotated[Optional[str], Form()] = None,
    # ВАЖНО: None — «поле не пришло вовсе», пустая строка — «пришло
    # пустым». Для исполнителя это разные команды: не трогать против
    # снять. Поле отправляется только после нажатия «Сменить», поэтому
    # карточка пакета уволенного сохраняется без потери исполнителя.
    executor_id: Annotated[Optional[str], Form()] = None,
) -> RedirectResponse:
    """Правит название, описание, статус и исполнителя пакета.

    Статус меняет и начальник, не только исполнитель: иначе пакет
    уволенного или заболевшего застрял бы до правки администратором.

    Смена исполнителя здесь — только поле пакета. Что делать с уже
    разложенным планом прежнего исполнителя, решает планирование:
    прошлые ячейки остаются за ним, будущие перекладываются.
    """
    found = await _load_package_in_project(db, project_id, package_id)
    package = found.package

    if not scope.can_manage(found.department_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Подразделение вне вашей области управления",
        )

    package_name = name.strip()
    if not package_name:
        return _back(project_id, found.composition_id, "package_empty")

    # Статус — значение из справочника, а не произвольное число.
    known_status = (
        await db.execute(select(PackageStatus.id).where(PackageStatus.id == status_id))
    ).first()
    if known_status is None:
        return _back(project_id, found.composition_id, "status")

    # Исполнитель — три случая, и различие принципиально.
    if executor_id is None:
        # Поля нет: «Сменить» не нажимали — значение остаётся как есть.
        new_executor = package.executor_id
    else:
        new_executor = _form_int(executor_id)
        if new_executor is not None and not await _executor_belongs(
            db, new_executor, found.department_id
        ):
            return _back(project_id, found.composition_id, "executor")

    package.name = package_name
    package.description = (description or "").strip() or None
    package.status_id = status_id
    package.executor_id = new_executor

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return _back(project_id, found.composition_id, "package_duplicate")

    return _back(project_id, found.composition_id, None)


@router.post(
    "/planner/works/{project_id}/packages/{package_id}/delete",
    tags=["Web Планировщик"],
    summary="Удаление пакета работ без плана и факта",
)
async def planner_delete_package(
    project_id: int,
    package_id: int,
    user: Annotated[User, Depends(RequirePermissionHtml(SET_MANAGE_PERMISSION))],
    scope: Annotated[Scope, Depends(get_scope_html)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RedirectResponse:
    """Удаляет пакет, по которому ничего не делалось.

    Правило то же, что у шифра: ни плана, ни факта. Внешние ключи плана
    и факта на пакет — CASCADE, поэтому запрет держит этот роут.
    """
    # Пакет блокируется сразу: вставка плана или факта на него ждёт конца
    # нашей транзакции и получает ошибку внешнего ключа вместо тихой
    # потери, если пакет к тому моменту удалён.
    found = await _load_package_in_project(db, project_id, package_id, lock=True)

    if not scope.can_manage(found.department_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Подразделение вне вашей области управления",
        )

    has_plan = (
        await db.execute(
            select(PlanCalendarCell.id)
            .where(PlanCalendarCell.package_id == package_id)
            .limit(1)
        )
    ).first()
    if has_plan is not None:
        return _back(project_id, found.composition_id, "package_has_plan")

    has_fact = (
        await db.execute(
            select(FactCalendarCell.id)
            .where(FactCalendarCell.package_id == package_id)
            .limit(1)
        )
    ).first()
    if has_fact is not None:
        return _back(project_id, found.composition_id, "package_has_fact")

    await db.execute(delete(WorkPackage).where(WorkPackage.id == package_id))
    await db.commit()

    return _back(project_id, found.composition_id, None)

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