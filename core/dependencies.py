# app\core\dependencies.py
from typing import Annotated, AsyncGenerator, Optional
from fastapi import Depends, HTTPException, status, Request 
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker


from app.core.config import settings
from app.models.user import User


# 1. База данных (твоё)
engine = create_async_engine(settings.DATABASE_URL, echo=True)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        yield session

# 2. Настройка OAuth2
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)

# 3. Получение текущего юзера (Обновлено!)
async def get_optional_user(
    request: Request,
    token_from_header: Annotated[Optional[str], Depends(oauth2_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Optional[User]:
    """Возвращает пользователя по токену из заголовка/cookie ИЛИ None.

    Не бросает исключений — подходит для HTML-страниц, где при отсутствии
    авторизации нужен редирект на /login, а не ответ 401.
    """
    token = token_from_header or request.cookies.get("access_token")
    if token is None:
        return None

    try:
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
        username: Optional[str] = payload.get("sub")
        if username is None:
            return None
    except JWTError:
        return None

    result = await db.execute(select(User).where(User.username == username))
    user = result.scalars().first()

    # Несуществующий или отключённый пользователь = не авторизован.
    if user is None or not user.is_active:
        return None

        # Кладём найденного пользователя в request.state, чтобы синхронный
    # context_processor шаблонов мог прочитать его без повторного похода
    # в БД (см. инъектор user / user_perms). Пишем только при успехе —
    # для гостя ключ не ставим, процессор трактует отсутствие как None.
    request.state.user = user

    return user


async def get_current_user(
    user: Annotated[Optional[User], Depends(get_optional_user)],
) -> User:
    """Строгая версия для API: при отсутствии авторизации бросает 401."""
    # PermissionChecker и JSON-ручки зависят именно от этой функции.
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Не удалось валидировать токен",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user

# 4. Тот самый "Чекер" (Аналог декоратора Flask)
class PermissionChecker:
    def __init__(self, required_permission: str):
        self.required_permission = required_permission

    # Мы явно прописываем Depends в аргументах __call__
    async def __call__(self, user: User = Depends(get_current_user)) -> User:
        # Проверка делегирована модели: там же живёт обход для роли admin,
        # поэтому API, HTML-гард и шаблоны судят по одному правилу.
        if not user.can(self.required_permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Недостаточно прав. Требуется: {self.required_permission}",
            )
        return user