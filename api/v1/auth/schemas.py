# app\api\v1\auth\schemas.py
from pydantic import BaseModel, ConfigDict
from typing import Optional, List

# --- СХЕМЫ ДЛЯ ТОКЕНОВ ---
class Token(BaseModel):
    access_token: str
    token_type: str

class TokenData(BaseModel):
    username: Optional[str] = None


# --- СХЕМЫ ДЛЯ РОЛЕЙ ---
class RoleOut(BaseModel):
    id: int  # Добавим ID, раз он есть в БД
    name: str
    
    model_config = ConfigDict(from_attributes=True)


# --- СХЕМЫ ДЛЯ ПОЛЬЗОВАТЕЛЯ ---

# 1. ТО ЧТО ПРИХОДИТ (Регистрация)
class UserCreate(BaseModel):
    username: str
    password: str
    full_name: str

# 2. ТО ЧТО УХОДИТ (Отображение)
class UserOut(BaseModel):
    id: int
    username: str
    full_name: str
    is_active: bool
    roles: List[RoleOut] = [] # Список ролей, если они есть

    model_config = ConfigDict(from_attributes=True)