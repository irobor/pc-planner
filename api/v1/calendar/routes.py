# app\api\v1\calendar\routes.py
from typing import List
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_db
from app.core.dependencies import get_db, PermissionChecker  # Импортируем наш чекер прав
from app.api.v1.calendar.schemas import TestCalendarResponse
from app.api.v1.calendar.service import CalendarService  # Импортируем сервис

router = APIRouter(tags=["Тест календаря"])

@router.get("/test-january", 
            response_model=List[TestCalendarResponse], 
            summary='Тест календаря')
async def get_test_january(
    db: AsyncSession = Depends(get_db),
    current_user = Depends(PermissionChecker("registry:view_all"))
    ):
    """
    Тестовый эндпоинт: возвращает дни Января 2025 года.
    Доступен только авторизованным пользователям с правом 'registry:view_all'.
    """
    # Вызываем логику из сервисного слоя
    calendar_days = await CalendarService.get_january_2025(db)
    
    return calendar_days

