# app/core/errors.py
import logging

from fastapi import Request, status
from fastapi.exception_handlers import (
    http_exception_handler as fastapi_http_exception_handler,
)
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.web.templates import templates

# Логгер модуля. Реальные трейсбеки 500 пишутся сюда, а не клиенту.
logger = logging.getLogger(__name__)


# Тексты страниц ошибок: код → (заголовок, сообщение).
# Нейтральные формулировки без внутренних деталей (правило №18:
# ни трейсбеков, ни SQL, ни системных путей наружу).
_ERROR_CONTENT: dict[int, tuple[str, str]] = {
    status.HTTP_403_FORBIDDEN: (
        "Доступ запрещён",
        "У вас недостаточно прав для просмотра этой страницы.",
    ),
    status.HTTP_404_NOT_FOUND: (
        "Страница не найдена",
        "Запрашиваемая страница не существует или была перемещена.",
    ),
    status.HTTP_500_INTERNAL_SERVER_ERROR: (
        "Внутренняя ошибка сервера",
        "Произошла ошибка на стороне сервера. Попробуйте позже.",
    ),
}

# Фолбэк для кодов вне карты (например, 405 Method Not Allowed).
_FALLBACK_CONTENT: tuple[str, str] = (
    "Ошибка",
    "При обработке запроса произошла ошибка.",
)


def _is_api_request(request: Request) -> bool:
    """True, если запрос относится к JSON-контуру (префикс /api).

    Граница контуров — префикс пути, а не заголовок Accept: это чёткая
    граница и она устойчива к будущему /api/v2 (тоже начинается с /api).
    """
    return request.url.path.startswith("/api")


def _render_error_page(request: Request, code: int) -> Response:
    """Рендерит HTML-страницу ошибки с кодом, заголовком и сообщением."""
    title, message = _ERROR_CONTENT.get(code, _FALLBACK_CONTENT)
    return templates.TemplateResponse(
        "errors/error.html",
        {
            "request": request,
            "code": code,
            "title": title,
            "message": message,
        },
        # Статус ответа = коду ошибки, а не 200. Иначе 404-страница
        # отдавалась бы с кодом 200 и вводила бы клиентов в заблуждение.
        status_code=code,
    )


async def http_exception_handler(
    request: Request,
    exc: StarletteHTTPException,
) -> Response:
    """Единый обработчик HTTP-ошибок: ветвление HTML / API по префиксу пути.

    Зарегистрирован на starlette.exceptions.HTTPException — это покрывает
    и ошибки, брошенные в коде (raise HTTPException(...)), и 404, который
    порождает сам роутинг при несовпадении пути. На fastapi.HTTPException
    вешать нельзя: тогда 404 от несовпавшего роута не перехватится.
    """
    # API-контур: отдаём родной JSON FastAPI ({"detail": ...}) как есть,
    # чтобы React и Swagger получали привычную форму ответа.
    if _is_api_request(request):
        return await fastapi_http_exception_handler(request, exc)

    # HTML-контур: страница ошибки по её коду.
    return _render_error_page(request, exc.status_code)


async def unhandled_exception_handler(
    request: Request,
    exc: Exception,
) -> Response:
    """Перехват необработанных исключений → безопасная 500.

    Настоящая ошибка с трейсбеком уходит ТОЛЬКО в лог сервера. Наружу —
    чистое сообщение без внутренних деталей (правило №18).
    """
    # logger.exception сам приложит трейсбек текущего исключения в лог.
    logger.exception("Необработанное исключение при обработке запроса")

    if _is_api_request(request):
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Внутренняя ошибка сервера"},
        )

    return _render_error_page(
        request, status.HTTP_500_INTERNAL_SERVER_ERROR
    )