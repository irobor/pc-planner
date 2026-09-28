# app/api/v1/auth/service.py
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from fastapi import HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from app.models.user import User
from werkzeug.security import check_password_hash  # Используем Werkzeug для совместимости с Flask

async def authenticate_user(form_data: OAuth2PasswordRequestForm, db: AsyncSession) -> User:
    """
    Ищет пользователя в БД и проверяет его пароль по стандарту Werkzeug.
    Возвращает объект User, если всё ок, иначе выбрасывает HTTPException.
    """
    # 1. Ищем пользователя по username
    result = await db.execute(select(User).where(User.username == form_data.username))
    user = result.scalars().first()
    
    # 2. Проверяем существование пользователя и хэш его пароля (Werkzeug)
    if not user or not check_password_hash(user.password_hash, form_data.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверное имя пользователя или пароль",
            headers={"WWW-Authenticate": "Bearer"},
        )
        
    return user