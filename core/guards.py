# app/core/guards.py
from typing import Annotated, Optional
from urllib.parse import urlencode

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse

from app.core.dependencies import get_optional_user
from app.core.security import is_safe_next
from app.models.user import User


class RedirectToLogin(Exception):
    """Сигнал «гость на защищённой HTML-странице → отправить на форму входа».

    Зависимость не может вернуть редирект вместо значения, поэтому она
    бросает это исключение, а зарегистрированный хендлер превращает его
    в RedirectResponse. Несёт путь, на который вернуть после логина.
    """

    def __init__(self, next_path: str) -> None:
        self.next_path = next_path


async def require_user_html(
    request: Request,
    user: Annotated[Optional[User], Depends(get_optional_user)],
) -> User:
    """Гард защищённых HTML-страниц: проверка входа + пометка под no-store.

    Гость (user is None) → RedirectToLogin с текущим путём.
    Вошедший → ставим флаг no_store (его прочитает middleware) и
    возвращаем user, чтобы роут получил его для шаблона.
    """
    if user is None:
        # Путь страницы станет ?next=, чтобы вернуть сюда после входа.
        raise RedirectToLogin(request.url.path)

    # Флаг читается no_store_middleware уже на этапе ответа.
    # Ставится ТОЛЬКО для защищённых страниц — статика сюда не заходит.
    request.state.no_store = True
    return user


async def redirect_to_login_handler(
    request: Request,
    exc: RedirectToLogin,
) -> RedirectResponse:
    """Хендлер RedirectToLogin: собирает /login?next=… с проверкой next."""
    # is_safe_next отсекает open-redirect и петли (/login, /logout).
    target = exc.next_path if is_safe_next(exc.next_path) else None
    if target:
        url = "/login?" + urlencode({"next": target})
    else:
        url = "/login"
    return RedirectResponse(url=url, status_code=303)


async def no_store_middleware(request: Request, call_next):
    """Вешает Cache-Control: no-store только на помеченные ответы.

    Решение принимает не middleware по URL, а сам роут через гард
    (request.state.no_store). Поэтому /static и /api сюда не попадают —
    они флаг не ставят, и их кэширование не ломается.
    """
    response = await call_next(request)
    if getattr(request.state, "no_store", False):
        response.headers["Cache-Control"] = "no-store"
    return response

class RequirePermissionHtml:
    """Гард HTML-страницы, защищённой конкретным правом (RBAC).

    HTML-аналог PermissionChecker. Разница — в обработке трёх исходов:
    гость уходит на /login (через require_user_html), авторизованный без
    права получает HTML-страницу 403, обладатель права допускается.

    Каскад исходов обеспечивается зависимостью от require_user_html:
    она сама отсеивает гостя (RedirectToLogin → /login) и ставит
    no_store, поэтому сюда доходит только авторизованный User (не None),
    и остаётся выполнить единственную проверку — наличие права.

    Используется как фабрика в Depends:
        user = Depends(RequirePermissionHtml("registry:project_composition"))
    """

    def __init__(self, required_permission: str) -> None:
        # Имя требуемого права (как в таблице permissions), напр.
        # "registry:project_composition". Сохраняется на момент создания
        # зависимости в роуте.
        self.required_permission = required_permission

    async def __call__(
        self,
        user: Annotated[User, Depends(require_user_html)],
    ) -> User:
        """Проверяет право у уже авторизованного пользователя.

        Гость и no_store обработаны в require_user_html до входа сюда.
        Здесь — только право: нет → 403-страница, есть → пропускаем.
        """
        # То же правило, что и в API-контуре: User.can() учитывает роль
        # суперпользователя до опроса role_permissions.
        if not user.can(self.required_permission):
            # fastapi.HTTPException — подкласс starlette-исключения, на
            # которое в errors.py зарегистрирован обработчик. Путь страницы
            # не из /api, поэтому отрисуется HTML-403, а не JSON.
            # detail на 403-странице не виден (текст берётся из _ERROR_CONTENT),
            # но осмысленный текст полезен для логов и консистентности.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Недостаточно прав. Требуется: {self.required_permission}",
            )
        return user