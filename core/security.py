# app/core/security.py
from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import jwt
from werkzeug.security import generate_password_hash, check_password_hash

from app.core.config import settings

from urllib.parse import urlparse

# Пути, на которые редиректить после логина бессмысленно или вредно:
# /login — петля на форму, /logout — стёр бы только что выданную cookie.
_FORBIDDEN_NEXT: frozenset[str] = frozenset({"/login", "/logout"})

# Имя роли-суперпользователя. Обход прав завязан на ИМЯ роли, а не на её
# набор прав: роль admin может вообще не иметь записей в role_permissions.
#
# Значение обязано совпадать с литералом, зашитым в модели контура Flask.
# Поэтому константа живёт в коде, а НЕ в Settings/.env: конфигурация двух
# контуров может разъехаться незаметно, и суперпользователь молча
# перестанет проходить проверки в одном из них.
ADMIN_ROLE: str = "admin"


def is_safe_next(next_path: str | None) -> bool:
    """Проверяет, что next_path — безопасный локальный путь для редиректа.

    Защищает от open-redirect: внешние и протокол-относительные URL
    (//evil.com, /\\evil.com, https://evil.com) отсекаются, чтобы
    злоумышленник не увёл пользователя на чужой сайт после входа.
    """
    # Пустое значение — нечего проверять, вызывающий код подставит "/".
    if not next_path:
        return False

    # Бэкслеши некоторые браузеры нормализуют в "/", превращая
    # "/\evil.com" в "//evil.com". Приводим заранее, чтобы поймать это.
    normalized = next_path.replace("\\", "/")

    # Должен быть одиночный "/" в начале. "//evil.com" — это
    # протокол-относительный абсолютный URL, а не локальный путь.
    if not normalized.startswith("/") or normalized.startswith("//"):
        return False

    # Контрольная проверка через парсер: у локального пути не должно
    # быть ни схемы (http), ни хоста (netloc).
    parsed = urlparse(normalized)
    if parsed.scheme or parsed.netloc:
        return False

    # Явный денлист вырожденных случаев (петли).
    if normalized in _FORBIDDEN_NEXT:
        return False

    return True

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Проверяет пароль против werkzeug-хеша.

    Формат хеша совместим с Flask-приложением, поэтому пользователи,
    заведённые во Flask, проходят аутентификацию здесь без изменений.
    """
    # ВАЖНО: у werkzeug порядок аргументов обратный passlib —
    # сначала хеш, потом открытый пароль.
    return check_password_hash(hashed_password, plain_password)


def get_password_hash(password: str) -> str:
    """Возвращает werkzeug-хеш пароля для записи в БД."""
    # method задаём явно, чтобы FastAPI и Flask создавали хеши одного
    # формата независимо от версии werkzeug. Подставь сюда тот же method,
    # что используется в вашем Flask (старый дефолт — pbkdf2:sha256,
    # новый — scrypt).
    return generate_password_hash(password, method="pbkdf2:sha256")


def create_access_token(
    data: dict,
    expires_delta: Optional[timedelta] = None,
) -> str:
    """Создаёт JWT с полем exp (время истечения)."""
    to_encode = data.copy()

    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(
            minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
        )

    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)