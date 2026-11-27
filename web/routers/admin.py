# app/web/routers/admin.py
"""Раздел «Администрирование» — страницы администратора данных.

    GET /admin               — вход в раздел: переадресация на первый экран
    GET /admin/data          — «План и факт сотрудников» (ремонтный режим)
    GET /admin/summary       — «Отчёты»: сводная таблица «проекты × бюро»
    GET /admin/summary.xlsx  — тот же отчёт файлом Excel
    GET /admin/marks         — редактор справочника марок

Раздел на layouts/section.html с боковым меню; меню описано один раз в
оболочке layouts/admin.html.

Права проверяются гардом RequirePermissionHtml через User.can():
- /admin, /admin/data, /admin/summary и /admin/summary.xlsx требуют
  plan:override и fact:override;
- /admin/marks требует reference:manage.

Роль admin проходит через обход прав, роль data_admin — по назначенным
ей правам. Проверять is_superuser нельзя: у администратора данных его
нет. Кадровая карточка не нужна.

/admin/data только отдаёт оболочку: сетку рисует React-остров, данные он
берёт сам через /api/admin/*. /admin/summary — серверная страница: отчёт
только читается, клиентского состояния нет, острову здесь делать нечего.
"""

from datetime import date, timedelta
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.api.v1.admin.export import (
    XLSX_MEDIA_TYPE,
    summary_report_filename,
    summary_report_xlsx,
)
from app.api.v1.admin.service import (
    InvalidIntervalError,
    ReportMode,
    SummaryReport,
    load_summary_report,
)
from app.core.dependencies import get_db
from app.core.guards import RequirePermissionHtml
from app.models import (
    Department,
    MarkaType,
    ProjectComposition,
    ProjectStage,
    marka_to_stage,
)
from app.models.user import User
from app.web.templates import flash, templates


router = APIRouter()

PLAN_OVERRIDE = "plan:override"
FACT_OVERRIDE = "fact:override"
REFERENCE_MANAGE = "reference:manage"


@router.get("/admin", response_class=RedirectResponse)
async def admin_index(
    _plan: Annotated[User, Depends(RequirePermissionHtml(PLAN_OVERRIDE))],
    _fact: Annotated[User, Depends(RequirePermissionHtml(FACT_OVERRIDE))],
) -> RedirectResponse:
    """Вход в раздел из шапки — на первый экран.

    Отдельный адрес у экрана нужен боковому меню: пункт подсвечивается
    по полному совпадению пути, и у каждого будущего экрана будет свой.
    Гарды и здесь: без прав не показываем даже переадресацию.
    """
    return RedirectResponse(url="/admin/data", status_code=303)


@router.get("/admin/data", response_class=HTMLResponse)
async def admin_data_page(
    request: Request,
    # Два гарда подряд: RequirePermissionHtml проверяет одно право.
    # Гость уходит на /login, без любого из прав — HTML-403.
    _plan: Annotated[User, Depends(RequirePermissionHtml(PLAN_OVERRIDE))],
    _fact: Annotated[User, Depends(RequirePermissionHtml(FACT_OVERRIDE))],
) -> HTMLResponse:
    """«План и факт сотрудников» — правка без процессных ограничений."""
    return templates.TemplateResponse("admin/data.html", {"request": request})


async def _load_mark_bureaus(db: AsyncSession) -> list[dict]:
    """Загружает только бюро третьего уровня и группирует по отделам."""

    division = aliased(Department)
    root = aliased(Department)

    stmt = (
        select(
            Department,
            division.id,
            division.code,
            division.name,
        )
        .join(division, division.id == Department.parent_id)
        .join(root, root.id == division.parent_id)
        .where(root.parent_id.is_(None))
        .order_by(division.code, Department.code)
    )

    rows = (await db.execute(stmt)).all()
    groups = {}

    for bureau, division_id, division_code, division_name in rows:
        group = groups.setdefault(
            division_id,
            {
                "code": division_code,
                "name": division_name,
                "bureaus": [],
            },
        )
        group["bureaus"].append(bureau)

    return list(groups.values())


async def _load_marks_page_data(db: AsyncSession) -> dict:
    """Загружает список марок и справочники для формы."""

    stmt = (
        select(
            MarkaType,
            ProjectStage.stage_name,
            func.count(ProjectComposition.id).label("composition_count"),
        )
        .join(
            marka_to_stage,
            marka_to_stage.c.marka_type_id == MarkaType.id,
        )
        .join(
            ProjectStage,
            ProjectStage.id == marka_to_stage.c.stage_id,
        )
        .outerjoin(
            ProjectComposition,
            ProjectComposition.marka_type_id == MarkaType.id,
        )
        .group_by(
            MarkaType.id,
            ProjectStage.id,
            ProjectStage.stage_name,
        )
        .order_by(MarkaType.code, MarkaType.id)
    )

    rows = (await db.execute(stmt)).all()

    stage_order = ("РД", "ПД", "Без стадии", "ПД/РД")
    grouped = {}

    for marka, stage_name, composition_count in rows:
        grouped.setdefault(stage_name, []).append(
            {
                "marka": marka,
                "composition_count": composition_count,
            }
        )

    marka_groups = [
        {
            "stage_name": stage_name,
            "markas": grouped[stage_name],
        }
        for stage_name in stage_order
        if stage_name in grouped
    ]

    stages = (
        await db.execute(
            select(ProjectStage).order_by(ProjectStage.stage_name)
        )
    ).scalars().all()

    bureaus = await _load_mark_bureaus(db)

    return {
        "marka_groups": marka_groups,
        "stages": stages,
        "bureau_groups": bureaus,
    }


@router.get(
    "/admin/marks",
    response_class=HTMLResponse,
    tags=["Web Администрирование"],
    summary="Редактор справочника марок",
    status_code=200,
)
async def admin_marks_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    _reference: Annotated[
        User, Depends(RequirePermissionHtml(REFERENCE_MANAGE))
    ],
) -> HTMLResponse:
    """Список марок с количеством записей состава."""

    page_data = await _load_marks_page_data(db)

    return templates.TemplateResponse(
        request,
        "admin/marks.html",
        {
            **page_data,
            "error": None,
            "form_values": {},
        },
    )


@router.post(
    "/admin/marks",
    response_class=HTMLResponse,
    tags=["Web Администрирование"],
    summary="Создание марки",
    status_code=303,
)
async def admin_marks_create(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    _reference: Annotated[
        User, Depends(RequirePermissionHtml(REFERENCE_MANAGE))
    ],
    code: Annotated[str, Form()],
    name: Annotated[str, Form()],
    stage_id: Annotated[str, Form()],
    bureau_ids: Annotated[list[int] | None, Form()] = None,
) -> Response:
    """Создаёт марку и её связи со стадией и бюро."""

    code = code.strip()
    name = name.strip()
    stage_id = stage_id.strip()
    selected_ids = set(bureau_ids or [])

    error = None

    if not code or not name or not stage_id or not selected_ids:
        error = "empty"
    elif len(code) > 20:
        error = "too_long"

    stage = None

    if error is None:
        try:
            parsed_stage_id = int(stage_id)
        except ValueError:
            error = "not_found"
        else:
            stage = await db.get(ProjectStage, parsed_stage_id)
            if stage is None:
                error = "not_found"

    selected_bureaus = []

    if selected_ids:
        bureau_rows = await db.execute(
            select(Department).where(Department.id.in_(selected_ids))
        )
        selected_bureaus = bureau_rows.scalars().all()

        # Проверяем, что переданные id принадлежат именно бюро текущей
        # иерархии, а не произвольным подразделениям.
        allowed_bureaus = {
            bureau.id
            for group in await _load_mark_bureaus(db)
            for bureau in group["bureaus"]
        }

        if selected_ids - allowed_bureaus:
            error = "not_found"

    # Не допускаем повтор одной пары «код марки + стадия».
    # Сам по себе код не уникален: он может использоваться на разных стадиях.
    if error is None:
        duplicate_stmt = (
            select(MarkaType.id)
            .join(
                marka_to_stage,
                marka_to_stage.c.marka_type_id == MarkaType.id,
            )
            .where(
                MarkaType.code == code,
                marka_to_stage.c.stage_id == parsed_stage_id,
            )
            .limit(1)
        )
        duplicate_id = await db.scalar(duplicate_stmt)

        if duplicate_id is not None:
            error = "mark_stage_exists"

    if error:
        page_data = await _load_marks_page_data(db)
        return templates.TemplateResponse(
            request,
            "admin/marks.html",
            {
                **page_data,
                "created": False,
                "error": error,
                "form_values": {
                    "code": code,
                    "name": name,
                    "stage_id": stage_id,
                    "bureau_ids": selected_ids,
                },
            },
            status_code=200,
        )

    marka = MarkaType(
        code=code,
        name=name,
        allowed_stages=[stage],
        departments=selected_bureaus,
    )

    db.add(marka)

    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    flash(request, "success", "Марка успешно создана.")

    return RedirectResponse(
        url="/admin/marks",
        status_code=303,
    )


def _current_month(today: date) -> tuple[date, date]:
    """Первый и последний день месяца, в который входит today."""
    first = today.replace(day=1)
    # +32 дня от первого числа — всегда следующий месяц, каким бы
    # коротким ни был текущий.
    next_first = (first + timedelta(days=32)).replace(day=1)
    return first, next_first - timedelta(days=1)


@router.get("/admin/summary", response_class=HTMLResponse)
async def admin_summary_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    _plan: Annotated[User, Depends(RequirePermissionHtml(PLAN_OVERRIDE))],
    _fact: Annotated[User, Depends(RequirePermissionHtml(FACT_OVERRIDE))],
    # Параметры в адресе, а не в теле: ссылку на отчёт можно сохранить и
    # переслать. «from» — слово Python, поэтому имя в коде другое.
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    # Режим приносит нажатая кнопка формы: «Сформировать Факт» или
    # «Плановые затраты». Нет режима — кнопку ещё не нажимали.
    mode: ReportMode | None = None,
) -> HTMLResponse:
    """«Отчёты» — сводная таблица «проекты × бюро» за период.

    Отчёт строится только по нажатию кнопки: когда в адресе есть обе
    даты и режим. Иначе — одна форма, даты которой заполнены текущим
    месяцем, чтобы чаще всего хватало одного клика.

    Начало позже конца — не страница ошибки: форма остаётся с введёнными
    датами, над ней сообщение, таблицы нет. Формат дат гарантирует поле
    type="date"; испорченный вручную адрес FastAPI отклонит сам (422).
    """
    report: SummaryReport | None = None
    error: str | None = None
    if date_from is not None and date_to is not None and mode is not None:
        try:
            report = await load_summary_report(
                db, mode=mode, date_from=date_from, date_to=date_to
            )
        except InvalidIntervalError:
            error = "Дата начала периода позже даты конца."

    # Подстановка — только для полей формы; на построение отчёта она не
    # влияет: без кнопки отчёта нет при любых датах.
    default_from, default_to = _current_month(date.today())
    # Новая форма вызова: request — первым аргументом (старая устарела в
    # Starlette, перевод остальных роутов — долг 3.4).
    return templates.TemplateResponse(
        request,
        "admin/summary.html",
        {
            "report": report,
            "error": error,
            "mode": mode,
            "date_from": date_from or default_from,
            "date_to": date_to or default_to,
        },
    )


@router.get("/admin/summary.xlsx")
async def admin_summary_xlsx(
    db: Annotated[AsyncSession, Depends(get_db)],
    _plan: Annotated[User, Depends(RequirePermissionHtml(PLAN_OVERRIDE))],
    _fact: Annotated[User, Depends(RequirePermissionHtml(FACT_OVERRIDE))],
    # Все три параметра обязательны: файл выгружают из уже построенного
    # отчёта, и кнопка на странице подставляет их сама.
    date_from: Annotated[date, Query(alias="from")],
    date_to: Annotated[date, Query(alias="to")],
    mode: ReportMode,
) -> Response:
    """Отчёт затраченных ресурсов файлом .xlsx — тот же, что на странице.

    Тот же сервис, что у страницы: файл не может разойтись с экраном.
    """
    try:
        report = await load_summary_report(
            db, mode=mode, date_from=date_from, date_to=date_to
        )
    except InvalidIntervalError as exc:
        # Через форму не получить: кнопка выгрузки есть только у
        # построенного отчёта. Остаётся испорченный вручную адрес.
        raise HTTPException(
            status_code=400, detail="Дата начала периода позже даты конца."
        ) from exc

    filename = summary_report_filename(report)
    # Кириллица в имени — через filename* (RFC 5987); filename без него —
    # запасное латинское имя для клиентов, которые filename* не знают.
    fallback = 'filename="report.xlsx"'
    disposition = f"attachment; {fallback}; filename*=UTF-8''{quote(filename)}"
    return Response(
        content=summary_report_xlsx(report),
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": disposition},
    )