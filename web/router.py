# app/web/router.py
from fastapi import APIRouter

from app.web.routers.main import router as main_router  
from app.web.routers.auth import router as auth_router          # +
from app.web.routers.planner import router as planner_router
from app.web.routers.composition import router as composition_router

web_router = APIRouter()

# Подключаем наши HTML-страницы
web_router.include_router(main_router)                         # GET /
web_router.include_router(auth_router)                          # /login, /logout
web_router.include_router(planner_router, tags=["Web Планировщик"]) 
# Для планировщика префикс не ставим, чтобы адрес остался /planner

# Состав проекта. Префикс не ставим — путь /project-composition задан
# прямо в роуте, как и у планировщика.
web_router.include_router(composition_router, tags=["Web Состав проекта"])