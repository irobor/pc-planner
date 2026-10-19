# app/web/routers/fact.py
"""Раздел «Мой факт» — ежедневная отметка работы инженером.

Отдельный раздел верхнего меню, а не пункт Планировщика: это
ежедневное действие инженера, и до него должен быть один клик с любой
страницы. Планировщик открывается экраном начальника («Работы бюро»),
и инженер каждый раз проходил бы через чужой экран.

Пока экран в разделе один, страница наследует layouts/app.html без
бокового меню — правило section.html: сайдбар из единственного пункта
ничего не сообщает. Когда появятся «История» и «Статистика», шаблон
переедет на layouts/section.html.

Роут только отдаёт оболочку: матрицу рисует React-остров
(src/fact.jsx), данные он берёт сам через GET /api/fact/month.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.core.guards import RequirePermissionHtml
from app.models.user import User
from app.web.templates import templates

router = APIRouter()

# Право отметки своего факта: planner_executor, bureau_chief,
# department_chief.
FACT_PERMISSION = "fact:own"


@router.get("/fact", response_class=HTMLResponse)
async def fact_page(
    request: Request,
    user: Annotated[User, Depends(RequirePermissionHtml(FACT_PERMISSION))],
) -> HTMLResponse:
    """Страница отметки факта.

    Право проверяет гард: гость уходит на /login, без права — HTML-403.

    Отдельно проверяется кадровая карточка. Администратор проходит
    гард через обход прав (User.can), но у служебной учётки записи в
    employees нет, и факт ставить не от кого. Вместо 403 страница
    объясняет причину: право-то у него есть, и «доступ запрещён» ввёл
    бы в заблуждение.
    """
    return templates.TemplateResponse(
        "fact/index.html",
        {
            "request": request,
            # Связь employee грузится вместе с пользователем
            # (lazy="joined") — лишнего запроса здесь нет.
            "can_mark": user.employee is not None,
        },
    )