# app\api\v1\router.py
from fastapi import APIRouter
from fastapi.staticfiles import StaticFiles
from app.api.v1.projects.routes import router as projects_router
from app.api.v1.auth.routes import router as auth_router
from app.api.v1.composition.routes import router as composition_router
from app.api.v1.planning.routes import router as planning_router

# Сюда будешь добавлять остальные: sets, timesheet и т.д.

api_router = APIRouter()

# Объединяем модули

api_router.include_router(projects_router, prefix="/projects", tags=["projects"])
api_router.include_router(auth_router, prefix="/auth")
api_router.include_router(composition_router, prefix="/composition")
api_router.include_router(planning_router, prefix="/planning")


