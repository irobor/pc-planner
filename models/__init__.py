# app/models/__init__.py
"""Единая точка сборки ORM-моделей проекта.

Импортирует все модели и связующие Table, чтобы один `import app.models`
затягивал полную Base.metadata и конфигурировал все мапперы разом —
включая кросс-модульные relationship (ProjectComposition.project → Project)
и secondary-связки. Без этого порядок импортов в рантайме и в Alembic
env.py становится хрупким: часть таблиц может не попасть в metadata, и
autogenerate предложит их удалить.

ВАЖНО: сами модули моделей импортируют Base из app.models.base напрямую,
а НЕ отсюда — иначе возникнет циклический импорт (этот __init__ ещё не
достроен в момент импорта подмодулей). Реэкспорт Base ниже — только для
внешних потребителей (Alembic, прикладной код): from app.models import Base.

При добавлении нового модуля моделей дописывать его И сюда, И в __all__:
модуль-сирота останется вне metadata, и autogenerate сочтёт его таблицы
лишними.
"""

# Base — реэкспорт для внешних потребителей (Alembic target_metadata и т.п.).
from app.models.base import Base  # noqa: F401

# Модели RBAC и связки ролей/прав (объявлены как Table в user.py).
from app.models.user import (  # noqa: F401
    Permission,
    Role,
    User,
    role_permissions,
    user_roles,
)

# Кадровая личность работника и его назначения во времени.
# Employee: импорт обязателен здесь — User.employee ссылается на "Employee"
# строкой, и без регистрации класса в реестре связь упала бы при первом
# запросе, а не при старте.
# EmployeeAssignment: темпоральная таблица, на которой стоит область
# управления (app/core/scope.py). В БД у неё есть EXCLUDE-ограничение
# employee_assignments_no_overlap, невидимое для SQLAlchemy и Alembic.
# Position: справочник должностей (таблица Flask, читаем для меню пользователя).
from app.models.employee import Employee, EmployeeAssignment, Position  # noqa: F401

# Модель боевой таблицы projects (read-only для нашего контура).
from app.models.project import Project  # noqa: F401

# Модели раздела «Состав проекта» + связующие Table.
# SetStatus живёт здесь, а не в project.py: статус описывает жизненный
# цикл КОМПЛЕКТА (project_mark_sets), а не проекта в журнале.
from app.models.composition import (  # noqa: F401
    Department,
    MarkaType,
    ProjectComposition,
    ProjectMarkSet,
    ProjectStage,
    SetStatus,
    department_markas,
    marka_to_stage,
)

# Модели раздела «Планирование»: календарь, неявки, план и факт.
from app.models.planning import (  # noqa: F401
    AbsencePeriod,
    AbsenceType,
    DayType,
    FactCalendarCell,
    PlanCalendarCell,
    ProductionCalendar,
    WorkPlan,
)

# Read-only проекция состава проекта (VIEW v_project_composition).
# Отдельный модуль: это SELECT, а не сущность, в которую пишут.
from app.models.views import VProjectComposition  # noqa: F401


# Публичный API пакета: что видно при from app.models import *.
# Table-объекты (user_roles, role_permissions, department_markas,
# marka_to_stage) тоже включены — они часть metadata и могут
# понадобиться в прямых Core-запросах.
__all__ = [
    "Base",
    "User",
    "Role",
    "Permission",
    "Employee",
    "EmployeeAssignment",
    "Position",
    "user_roles",
    "role_permissions",
    "Project",
    "Department",
    "MarkaType",
    "ProjectStage",
    "ProjectMarkSet",
    "ProjectComposition",
    "SetStatus",
    "department_markas",
    "marka_to_stage",
    "VProjectComposition",
    "DayType",
    "ProductionCalendar",
    "AbsenceType",
    "AbsencePeriod",
    "WorkPlan",
    "PlanCalendarCell",
    "FactCalendarCell",
]