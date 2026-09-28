# app/api/v1/auth/routes.py
# В шапке файла должны присутствовать:
from typing import Annotated, Optional
from fastapi import APIRouter, Depends
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

# --- ВОЗВРАЩАЕМ ТВОИ ОРИГИНАЛЬНЫЕ ИМПОРТЫ ---
# Замени пути ниже на те, которые у тебя РЕАЛЬНО работают в проекте:
from app.core.dependencies import get_db,  get_current_user 

# Из безопасности берем функцию создания токена
from app.core.security import create_access_token, is_safe_next


from app.models.user import User
from app.api.v1.auth.schemas import Token, UserOut
from app.api.v1.auth.service import authenticate_user  # Наш новый сервис
router = APIRouter(tags=["auth"])


# --- 2. Вход (получение токена) ---
@router.post("/login", response_model=Token)
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(), 
    db: AsyncSession = Depends(get_db)
):
    # Вызываем логику аутентификации из сервиса
    user = await authenticate_user(form_data, db)
    
    # Создаем токен
    access_token = create_access_token(data={"sub": user.username})
    
    return {"access_token": access_token, "token_type": "bearer"}


@router.get("/me", response_model=UserOut)
async def read_users_me(current_user: User = Depends(get_current_user)):
    return current_user