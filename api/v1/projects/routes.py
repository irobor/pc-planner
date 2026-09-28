from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.dependencies import get_db  # Твоя функция получения сессии
from app.api.v1.projects.schemas import ProjectRead
from app.api.v1.projects.service import project_service

router = APIRouter()

@router.get("/", response_model=list[ProjectRead])
async def get_projects(db: AsyncSession = Depends(get_db)):
    """
    Маршрут: Принял запрос -> Взял сессию БД -> Отдал Сервису
    """
    return await project_service.get_projects_with_time(db)