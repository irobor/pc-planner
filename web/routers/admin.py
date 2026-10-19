# app/web/routers/admin.py
"""Раздел «Администрирование» — страницы администратора данных.

    GET /admin       — вход в раздел: переадресация на первый экран
    GET /admin/data  — «План и факт сотрудников» (ремонтный режим)

Раздел сразу заведён на layouts/section.html с боковым меню, хотя экран
пока один: следующие административные экраны ожидаются, и переезд
шаблона откладывать незачем.

Права — ОБА: plan:override и fact:override, как у ручек /api/admin.
Проверка только через гард RequirePermissionHtml, то есть через
User.can(): роль admin проходит через обход прав, роль data_admin — по
своим правам. Проверять is_superuser нельзя: у администратора данных
его нет. Кадровая карточка не нужна.

Роут только отдаёт оболочку: сетку рисует React-остров, данные он берёт
сам через /api/admin/*.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.guards import RequirePermissionHtml
from app.models.user import User
from app.web.templates import templates

router = APIRouter()

PLAN_OVERRIDE = "plan:override"
FACT_OVERRIDE = "fact:override"


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