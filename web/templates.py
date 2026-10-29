# app/web/templates.py
from pathlib import Path

from starlette.requests import Request
from fastapi.templating import Jinja2Templates
from jinja2 import StrictUndefined


# Каталог шаблонов — app/templates.
# (этот файл лежит в app/web/, поэтому .parent.parent = app/)
APP_DIR = Path(__file__).resolve().parent.parent


def _deny_all(permission_name: str) -> bool:
    """Заглушка can() для гостя: у неавторизованного прав нет ни одного.

    Отдельная функция, а не lambda: имя видно в трейсбеке и подпись
    совпадает с User.can, поэтому шаблон не различает эти два случая.
    """
    return False


def inject_user_context(request: Request) -> dict:
    """Context-processor: добавляет user, can и user_card в каждый шаблон.

    В контекст уходит функция проверки, а не плоский список прав:
    список нельзя было бы согласовать с обходом для роли admin, не
    подменяя семантику оператора in. Шаблон вызывает can("право") —
    ровно та же логика, что на сервере.
    """
    user = getattr(request.state, "user", None)

    # Bound-метод конкретного пользователя; для гостя — заглушка,
    # чтобы шаблон не проверял user на None перед каждым вызовом.
    can = user.can if user is not None else _deny_all

    # Карточку (должность, подразделение) кладёт require_user_html. Ключ
    # отдаём всегда, хотя бы None: шаблоны в режиме StrictUndefined, и
    # страница без гарда иначе падала бы на обращении к user_card.
    user_card = getattr(request.state, "user_card", None)

    return {"user": user, "can": can, "user_card": user_card}


# context_processors принимает список синхронных Callable[[Request], dict].
# Наш процессор примешивает user/user_perms в контекст каждого шаблона.
templates = Jinja2Templates(
    directory=APP_DIR / "templates",
    context_processors=[inject_user_context],
)
# Строгий режим: обращение к неопределённой переменной — ошибка, а не
# тихое False. Без него забытая проверка вида "x" in user_perms просто
# скрывает элемент интерфейса, ничем себя не выдавая.
templates.env.undefined = StrictUndefined