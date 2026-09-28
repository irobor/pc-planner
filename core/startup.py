# app/core/startup.py
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from sqlalchemy import select

from app.core.dependencies import async_session
from app.core.security import ADMIN_ROLE
from app.models.user import Role

logger = logging.getLogger(__name__)


class RbacConfigurationError(RuntimeError):
    """Справочник ролей в БД не соответствует константам кода."""


async def check_admin_role_exists() -> None:
    """Убеждается, что роль ADMIN_ROLE заведена в базе.

    Без этой проверки переименование роли в БД не вызвало бы ошибок:
    обход просто перестал бы срабатывать, и суперпользователь начал бы
    получать 403 там, где раньше проходил. Диагностировать это по
    симптому «внезапно нет прав» долго, поэтому рассогласование ловим
    при запуске.
    """
    # Открываем собственную сессию: зависимость get_db работает только
    # внутри HTTP-запроса, а на старте запроса ещё нет.
    async with async_session() as session:
        # Выбираем только id: у Role объявлен lazy="selectin" на
        # permissions, и загрузка сущности потянула бы лишний запрос.
        result = await session.execute(
            select(Role.id).where(Role.name == ADMIN_ROLE)
        )
        role_id: int | None = result.scalars().first()

    if role_id is None:
        raise RbacConfigurationError(
            f"Роль '{ADMIN_ROLE}' отсутствует в таблице roles. "
            "Обход прав для суперпользователя работать не будет."
        )

    logger.info("Роль суперпользователя '%s' найдена (id=%s).", ADMIN_ROLE, role_id)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Жизненный цикл приложения: проверки конфигурации до приёма запросов.

    Исключение здесь останавливает запуск (fail-fast): не поднявшийся
    сервис заметен сразу, а сервис с молча отключённым обходом прав —
    нет.
    """
    await check_admin_role_exists()
    yield