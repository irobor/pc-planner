# app\api\v1\router.py
from fastapi import APIRouter
from app.api.v1.auth.routes import router as auth_router
from app.api.v1.composition.routes import router as composition_router
from app.api.v1.planning.routes import router as planning_router
from app.api.v1.fact.routes import router as fact_router
from app.api.v1.admin.routes import router as admin_router


api_router = APIRouter()

# Объединяем модули


api_router.include_router(auth_router, prefix="/auth")
api_router.include_router(composition_router, prefix="/composition")
api_router.include_router(planning_router, prefix="/planning")
api_router.include_router(fact_router, prefix="/fact")
api_router.include_router(admin_router, prefix="/admin")


