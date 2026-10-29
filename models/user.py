# app\models\user.py
from datetime import datetime
from typing import List, Optional, TYPE_CHECKING
from sqlalchemy import String, Integer, ForeignKey, Table, Column, DateTime, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from app.models.base import Base
from app.core.security import ADMIN_ROLE

if TYPE_CHECKING:
    from app.models.employee import Employee

# Таблица связи: Пользователь <-> Роль
user_roles = Table(
    "user_roles",
    Base.metadata,
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("role_id", Integer, ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
)

# Таблица связи: Роль <-> Право (Permission)
role_permissions = Table(
    "role_permissions",
    Base.metadata,
    Column("role_id", Integer, ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
    Column("permission_id", Integer, ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True),
)
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)   
    # ФИО работника здесь больше нет: идентичность разделена, кадровое имя
    # живёт в employees.full_name. Учётка может вообще не иметь работника. 
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Связь с ролями
    roles: Mapped[List["Role"]] = relationship(
        secondary=user_roles, 
        back_populates="users",
        lazy="selectin" # Автоматически подгружаем роли при запросе пользователя
    )
    # Кадровая карточка работника. Связь односторонняя и необязательная:
    # у технической учётки (sys_admin, reader) работника нет вовсе.
    #
    # lazy="joined", а не "selectin" как у roles: связь скалярная 0..1
    # (гарантия — UNIQUE (user_id) в employees), поэтому LEFT OUTER JOIN
    # не размножает строки и подтягивает работника тем же запросом.
    # ФИО достаётся без дополнительного обращения к БД на каждый
    # HTTP-запрос — а get_optional_user вызывается на всех роутах.
    #
    # innerjoin=False указан явно, хотя это и значение по умолчанию:
    # INNER JOIN отсёк бы все учётки без кадровой карточки, и они
    # перестали бы проходить авторизацию вовсе.
    employee: Mapped[Optional["Employee"]] = relationship(
        lazy="joined",
        innerjoin=False,
    )

    @property
    def permission_names(self) -> list[str]:
        """Плоский список имён всех прав пользователя по всем его ролям.

        Единый источник истины о правах: на это свойство опираются и
        серверная проверка (PermissionChecker), и инъектор шаблонов
        (user_perms), чтобы интерфейс и бэкенд считали права одинаково.

        Безопасно вызывается синхронно: связи User.roles и Role.permissions
        загружаются через lazy="selectin", поэтому коллекции уже в памяти
        и обращение к ним не порождает запросов в БД.
        """
        # Двойной проход: по ролям пользователя, затем по правам каждой роли.
        # Дубликаты возможны (пересекающиеся права у разных ролей) — для
        # проверки "право in списка" это несущественно.
        return [p.name for role in self.roles for p in role.permissions]

    @property
    def is_superuser(self) -> bool:
        """Признак учётной записи-суперпользователя (роль ADMIN_ROLE).

        Вычисляется из ролей, а не хранится колонкой в users: признак
        должен читаться из тех же данных, что и во Flask, иначе один и
        тот же пользователь оказался бы администратором в одном контуре
        и обычным — в другом.

        Безопасно вызывается синхронно: User.roles загружается через
        lazy="selectin", коллекция уже в памяти.
        """
        return any(role.name == ADMIN_ROLE for role in self.roles)

    def can(self, permission_name: str) -> bool:
        """Единственная точка проверки права. Аналог User.can() во Flask.

        Порядок проверок повторяет эталон: сначала короткое замыкание по
        роли суперпользователя, и только затем — опрос набора прав. Роли
        admin можно не назначать ни одного права: она обходит
        role_permissions целиком.

        На этот метод опираются все три контрольные точки — API
        (PermissionChecker), HTML (RequirePermissionHtml) и шаблоны
        (инъектор can). Иначе интерфейс и бэкенд считали бы по-разному:
        админ проходил бы ручки, но не видел кнопок.
        """
        # Короткое замыкание: permission_names не вычисляется вовсе.
        if self.is_superuser:
            return True

        return permission_name in self.permission_names

    
    @property
    def display_name(self) -> str:
        """Имя пользователя для отображения в интерфейсе.

        Фасад над разделённой идентичностью: шаблонам не нужно знать,
        что ФИО хранится в employees и что у части учётных записей
        работника нет вовсе. Правило записано здесь и только здесь —
        поэтому шаблоны не придётся править при изменении схемы.

        Безопасно вызывается синхронно: связь employee загружается
        через lazy="joined" вместе с самим пользователем, поэтому
        обращение к ней не порождает запроса в БД (отложенная загрузка
        в async-контуре дала бы MissingGreenlet).
        """
        # Технические учётки кадровой карточки не имеют — для них
        # показываем логин. Это не деградация, а корректное отображение
        # служебного пользователя: ФИО у него отсутствует по смыслу.
        if self.employee is not None:
            return self.employee.full_name
        return self.username

    @property
    def short_name(self) -> str:
        """Короткое имя для шапки: «Иванов Иван Иванович» → «Иванов И. И.».

        Фамилия целиком (двойная через дефис — одно слово), из следующих
        двух слов — первая буква с точкой. Слова дальше третьего
        отбрасываются («Мамедов Рашид Али оглы» → «Мамедов Р. А.»).
        Правило рассчитано на полную запись ФИО, как в боевой БД.

        Служебная учётка без карточки — логин, как в display_name.
        """
        parts = self.employee.full_name.split() if self.employee else []
        # Пустое ФИО — ошибка данных, но шапка из-за неё падать не должна:
        # такую учётку показываем логином, как служебную.
        if not parts:
            return self.username
        surname, *names = parts
        initials = [f"{name[0]}." for name in names[:2]]
        return " ".join([surname, *initials])


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)

    users: Mapped[List["User"]] = relationship(secondary=user_roles, back_populates="roles")
    permissions: Mapped[List["Permission"]] = relationship(
        secondary=role_permissions, 
        back_populates="roles",
        lazy="selectin" # Автоматически подгружаем права для роли
    )


class Permission(Base):
    __tablename__ = "permissions"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)

    roles: Mapped[List["Role"]] = relationship(secondary=role_permissions, back_populates="permissions")
    