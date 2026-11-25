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

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

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
from app.models.user import User
from app.web.templates import templates

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

@router.get(
    "/admin/marks",
    response_class=HTMLResponse,
    tags=["Web Администрирование"],
    summary="Редактор справочника марок",
    status_code=200,
)
async def admin_marks_page(
    request: Request,
    _reference: Annotated[
        User, Depends(RequirePermissionHtml(REFERENCE_MANAGE))
    ],
) -> HTMLResponse:
    """Страница редактора справочника марок.

    Для справочника используется отдельное право: его редактирование
    не должно зависеть от прав на исправление плана и факта.
    """
    return templates.TemplateResponse(
        request,
        "admin/marks.html",
        {},
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