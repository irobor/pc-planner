# app/__init__.py
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware  # ← Добавили импорт
from starlette.exceptions import HTTPException as StarletteHTTPException
from app.api.v1.router import api_router
from app.web.router import web_router
from app.core.startup import lifespan
from pathlib import Path
from app.core.guards import RedirectToLogin, redirect_to_login_handler, no_store_middleware
from app.core.errors import (http_exception_handler, unhandled_exception_handler,)

BASE_DIR = Path(__file__).parent  # это app/


def create_app() -> FastAPI:
    # lifespan передаётся в конструктор: обработчики жизненного цикла
    # нельзя навесить на уже созданный объект приложения.
    app = FastAPI(title="My Project API", lifespan=lifespan)

    # --- НАСТРОЙКА CORS ---
    # Список адресов (доменов), которым разрешено делать запросы к твоему API
    # Мы разрешаем внешним сайтам (фронтендам) делать запросы к нашему бэкенду
    origins = [
        "http://localhost:3000",      # Популярный порт для React
        "http://127.0.0.1:3000",
        "http://localhost:5173",      # Популярный порт для Vite (Vue/React)
        "http://127.0.0.1:5173",
        # "https://your-production-frontend.com" # Сюда потом впишешь боевой адрес фронта
    ]
    
    
    
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,            # Разрешаем запросы только с этих адресов
        allow_credentials=True,           # Разрешаем отправку кук и JWT в заголовках
        allow_methods=["*"],              # Разрешаем любые методы (GET, POST, POST, DELETE)
        allow_headers=["*"],              # Разрешаем любые HTTP-заголовки
    )
    # -----------------------
    #----------вставка no-store для вошедшего
    # Гард защищённых HTML-страниц: редирект гостя + no-store вошедшему.
    app.add_exception_handler(RedirectToLogin, redirect_to_login_handler)
    app.middleware("http")(no_store_middleware)
    # --------------------------------------------------------

        # --- Слой обработки ошибок (403/404/500), ветвление HTML/API ---
    # StarletteHTTPException, а не fastapi.HTTPException: только так
    # перехватывается 404 от несовпавшего роута.
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)

    # Монтируем статику и роутеры
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
    
    app.include_router(web_router)               # ← HTML страницы
    app.include_router(api_router, prefix="/api")

    

    return app

# app = create_app()