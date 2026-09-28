# app/web/routers/main.py
from typing import Annotated, Optional

from fastapi import APIRouter, Request, Depends, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.guards import require_user_html
from app.models.user import User
from app.web.templates import templates
from urllib.parse import urlencode

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index_page(
    request: Request,
    user: Optional[User] = Depends(require_user_html),
):
    """Главная. Защита и редирект — внутри require_user_html."""
    return templates.TemplateResponse(
        "index.html",
        {"request": request},
    )