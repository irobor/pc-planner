# app/core/startup.py
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from sqlalchemy import select

from app.core.dependencies import async_session
from app.core.security import ADMIN_ROLE
from app.models.user import Permission, Role, role_permissions

logger = logging.getLogger(__name__)


class RbacConfigurationError(RuntimeError):
    """Справочник ролей в БД не соответствует константам кода."""


async def check_admin_role_exists() -> int:
    """Убеждается, что роль ADMIN_ROLE заведена в базе, и возвращает её id.

    Без этой проверки переименование роли в БД не вызвало бы ошибок:
    обход просто перестал бы срабатывать, и суперпользователь начал бы
    получать 403 там, где раньше проходил. Диагностировать это по
    симптому «внезапно нет прав» долго, поэтому рассогласование ловим
    при запуске.

    id возвращается, чтобы следующая проверка не искала роль повторно.
    """
    # Открываем собственную сессию: зависимость get_db работает только
    # внутри HTTP-запроса, а на старте запроса ещё нет.
    async with async_session() as session:
        # Выбираем только id: у Role объявлен lazy="selectin" на
        # permissions, и загрузка сущности потянула бы лишний запрос.
        result = await session.execute(select(Role.id).where(Role.name == ADMIN_ROLE))
        role_id: int | None = result.scalars().first()

    if role_id is None:
        raise RbacConfigurationError(
            f"Роль '{ADMIN_ROLE}' отсутствует в таблице roles. "
            "Обход прав для суперпользователя работать не будет."
        )

    logger.info("Роль суперпользователя '%s' найдена (id=%s).", ADMIN_ROLE, role_id)
    return role_id


async def check_admin_role_has_no_permissions(role_id: int) -> None:
    """Убеждается, что у роли ADMIN_ROLE нет записей в role_permissions.

    Это диагностическое условие обхода прав. User.can() пропускает роль
    admin, не заглядывая в её права. Пока прав у роли нет, любой
    успешный проход суперпользователя доказывает, что обход работает.
    Если роли выдать обычные права, она начнёт проходить проверки ещё и
    обычным путём — и поломку обхода (например, проверку мимо can())
    никто не заметит.

    Нарушение НЕ останавливает запуск, а пишется в лог уровня ERROR:
    в отличие от пропавшей роли, лишнее право ничего не ломает сейчас —
    оно лишь лишает нас способа заметить будущую поломку. Останавливать
    из-за этого всё приложение (например, после ночной перезагрузки)
    несоразмерно. Чтобы сделать проверку жёсткой, замените logger.error
    на raise RbacConfigurationError(...).
    """
    async with async_session() as session:
        result = await session.execute(
            select(Permission.name)
            .join(role_permissions, role_permissions.c.permission_id == Permission.id)
            .where(role_permissions.c.role_id == role_id)
            .order_by(Permission.name)
        )
        names = list(result.scalars().all())

    if names:
        logger.error(
            "У роли '%s' есть права в role_permissions: %s. Обход прав "
            "работает, но перестал быть проверяемым: уберите права у роли "
            "в админке.",
            ADMIN_ROLE,
            ", ".join(names),
        )
        return

    logger.info("У роли '%s' нет собственных прав — как и должно быть.", ADMIN_ROLE)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Жизненный цикл приложения: проверки конфигурации до приёма запросов.

    Пропавшая роль admin останавливает запуск (fail-fast): не
    поднявшийся сервис заметен сразу, а сервис с молча отключённым
    обходом прав — нет. Лишние права у роли admin — только ошибка в
    логе (см. check_admin_role_has_no_permissions).
    """
    role_id = await check_admin_role_exists()
    await check_admin_role_has_no_permissions(role_id)
    yield