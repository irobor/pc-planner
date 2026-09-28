# app/web/routers/auth.py
from typing import Annotated, Optional

from fastapi import APIRouter, Request, Depends, HTTPException, status, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_db, get_optional_user
from app.core.security import create_access_token, is_safe_next
from app.core.config import settings
from app.api.v1.auth.service import authenticate_user
from app.web.templates import templates
from app.models.user import User

router = APIRouter(tags=["Web Авторизация"])


@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    user: Annotated[Optional[User], Depends(get_optional_user)],
    # alias="next": в URL параметр зовётся next, но имя next затеняло бы
    # встроенную функцию Python — flake8-builtins (A002) на это ругается.
    next_url: Annotated[str | None, Query(alias="next")] = None,
):
    """Отдаёт HTML-форму входа.

    Если пользователь уже авторизован — формы не показываем, уводим на
    целевую страницу (next) или на главную. Это снимает и побочный эффект,
    когда залогиненный видел бы в навбаре кнопку «Войти».
    """
    # Уже вошёл — форма не нужна. Возвращаем на next (если безопасен) или на "/".
    if user is not None:
        return RedirectResponse(
            url=next_url if is_safe_next(next_url) else "/",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    # Гость: отражаем в форму только безопасный next, иначе пустую строку.
    safe_next = next_url if is_safe_next(next_url) else ""
    return templates.TemplateResponse(
        "auth/login.html",
        # error=None передаётся явно: при первом показе формы ошибки нет,
        # но шаблон её проверяет. В строгом режиме Jinja отсутствие ключа
        # в контексте — исключение, а не тихое False.
        {"request": request, "error": None, "next_url": safe_next},
    )

@router.post("/login")
async def login_submit(
    request: Request,
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    db: Annotated[AsyncSession, Depends(get_db)],
    next_url: Annotated[str | None, Form(alias="next")] = None,
):
    """Проверяет логин/пароль, кладёт JWT в httpOnly-cookie и редиректит."""
    # next валидируем заранее: и при ошибке (вернуть в форму), и при успехе.
    safe_next = next_url if is_safe_next(next_url) else ""

    try:
        # Та же сервисная функция, что и у JSON-ручки, — логика не дублируется.
        user = await authenticate_user(form_data, db)
    except HTTPException:
        # Для HTML отдаём страницу с ошибкой, а не JSON 401.
        return templates.TemplateResponse(
            "auth/login.html",
            {
                "request": request,
                "error": "Неверное имя пользователя или пароль",
                "next_url": safe_next,  # сохраняем цель при повторной попытке
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    # authenticate_user не смотрит is_active — дублируем проверку здесь,
    # чтобы отключённый сотрудник не получил cookie.
    if not user.is_active:
        return templates.TemplateResponse(
            "auth/login.html",
            {
                "request": request,
                "error": "Учётная запись отключена",
                "next_url": safe_next,
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )

    access_token = create_access_token(data={"sub": user.username})

    # Возврат на исходную страницу, если next прошёл валидацию, иначе на главную.
    target = safe_next or "/"

    # 303 See Other: после POST браузер делает GET по новому адресу.
    response = RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key="access_token",          # имя ДОЛЖНО совпадать с чтением в get_optional_user
        value=access_token,
        httponly=True,               # JS не имеет доступа к cookie — защита от XSS
        secure=False,                # локально по http — False; на проде с HTTPS → True
        samesite="lax",              # на кросс-сайтовом POST cookie не уйдёт — гасит CSRF
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    return response


@router.post("/logout")
async def logout():
    """Удаляет cookie с токеном и возвращает на форму входа.

    Только POST: GET-logout уводился бы через next в петлю и был бы
    уязвим к CSRF через <img src="/logout">. POST это закрывает.
    """
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    # Атрибуты path/samesite должны совпадать с set_cookie, иначе часть
    # браузеров не сматчит cookie и не удалит её.
    response.delete_cookie(key="access_token", path="/", samesite="lax")
    return response