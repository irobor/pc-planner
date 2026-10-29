# app/models/composition.py
"""ORM-модели раздела «Состав проекта».

Модели построены строго по DDL тестовой БД (правило 26 — схема как
источник истины), сверенному через pgAdmin CREATE Script. Пять таблиц
и две связующие Table образуют логический модуль «состав проекта»:

    Department     — оргструктура (самоссылка parent_id, дерево отделов)
    MarkaType      — справочник типов марок/разделов
    ProjectStage   — справочник стадий (ПД/РД/ПД+РД)
    department_markas — компетенции: какой отдел выпускает какую марку
    marka_to_stage    — допустимость: какая марка разрешена на какой стадии
    ProjectComposition — целевая таблица: задание ГИПа (проект+отдел+марка+стадия)

Связки department_markas и marka_to_stage объявлены как Table (Core),
консистентно с user_roles/role_permissions в user.py: собственных полей
у них нет, это чистые матрицы many-to-many.
"""
from datetime import datetime
from typing import List, Optional, TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Table,
    Column,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.project import Project
    from app.models.employee import Employee


# Связка «отдел ↔ допустимая марка» (компетенции бюро).
# PK составной (department_id, marka_type_id), оба FK с ON DELETE CASCADE:
# удаление отдела или марки автоматически чистит строки компетенций.
department_markas = Table(
    "department_markas",
    Base.metadata,
    Column(
        "department_id",
        ForeignKey("departments.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "marka_type_id",
        ForeignKey("marka_types.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


# Связка «марка ↔ допустимая стадия» (матрица допустимости).
# Именно её читает триггер trg_check_composition_validity при вставке
# строки состава — проверяет, что пара (марка, стадия) разрешена.
marka_to_stage = Table(
    "marka_to_stage",
    Base.metadata,
    Column(
        "marka_type_id",
        ForeignKey("marka_types.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "stage_id",
        ForeignKey("project_stages.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class Department(Base):
    """Отдел/бюро — самоссылающийся справочник оргструктуры.

    Иерархия «Отдел → Бюро → …» реализована через parent_id (adjacency
    list). Глубина произвольная: parent_id = NULL у корневого узла.
    Механизм чтения дерева (рекурсивный CTE / сборка в Python) выбирается
    на этапе чтения — модель к нему нейтральна.
    """

    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)

    # Самоссылка: NULL у корня, иначе id вышестоящего подразделения.
    # ON DELETE SET NULL — при удалении родителя дети становятся корнями,
    # а не удаляются каскадно (faithful DDL: departments_parent_id_fkey).
    parent_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("departments.id", ondelete="SET NULL"),
    )

    # parent/children — навигация по дереву. remote_side указывает
    # SQLAlchemy, что "одна" сторона самоссылки — это id (родитель),
    # иначе он не сможет различить направления в пределах одной таблицы.
    # lazy по умолчанию (select): детей грузим явно там, где строим
    # дерево, чтобы не плодить запросы в списках, где иерархия не нужна.
    parent: Mapped[Optional["Department"]] = relationship(
        back_populates="children",
        remote_side=[id],
    )
    children: Mapped[List["Department"]] = relationship(
        back_populates="parent",
    )

    # Компетенции отдела: какие марки он вправе выпускать. Через связку
    # department_markas. Читается формой состава (какие марки предлагать).
    allowed_markas: Mapped[List["MarkaType"]] = relationship(
        secondary=department_markas,
        back_populates="departments",
    )

    __table_args__ = (
        # Не-уникальный индекс на parent_id для иерархических запросов —
        # faithful DDL (idx_departments_parent), чтобы autogenerate его
        # не счёл лишним и не предложил удалить.
        Index("idx_departments_parent", "parent_id"),
    )

    def __repr__(self) -> str:
        return f"Department(id={self.id!r}, code={self.code!r})"


class MarkaType(Base):
    """Тип марки/раздела (ТХ, КЖ, ОВ…). Справочник-классификатор.

    code НЕ уникален сознательно: одна марка может присутствовать и в РД,
    и в ПД (подтверждено пользователем). Уникальность держит не схема,
    а данные/код — согласовано, как и для department_markas.
    """

    __tablename__ = "marka_types"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Без unique=True — faithful DDL (в БД только индекс, не constraint).
    code: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)

    # Обратные стороны связок. departments — отделы, выпускающие эту марку;
    # allowed_stages — стадии, на которых марка допустима (для формы).
    departments: Mapped[List["Department"]] = relationship(
        secondary=department_markas,
        back_populates="allowed_markas",
    )
    allowed_stages: Mapped[List["ProjectStage"]] = relationship(
        secondary=marka_to_stage,
        back_populates="allowed_markas",
    )

    __table_args__ = (
        # Не-уникальный индекс на code — faithful DDL (idx_marka_types_code).
        Index("idx_marka_types_code", "code"),
    )

    def __repr__(self) -> str:
        return f"MarkaType(id={self.id!r}, code={self.code!r})"


class ProjectStage(Base):
    """Стадия документации (ПД, РД, ПД/РД). Справочник."""

    __tablename__ = "project_stages"

    id: Mapped[int] = mapped_column(primary_key=True)
    stage_name: Mapped[str] = mapped_column(
        String(50), unique=True, nullable=False
    )

    # Марки, допустимые на этой стадии (обратная сторона marka_to_stage).
    allowed_markas: Mapped[List["MarkaType"]] = relationship(
        secondary=marka_to_stage,
        back_populates="allowed_stages",
    )

    def __repr__(self) -> str:
        return f"ProjectStage(id={self.id!r}, stage_name={self.stage_name!r})"


class ProjectComposition(Base):
    """Состав проекта — задание ГИПа (уровень 1).

    Одна строка = «этот отдел выпускает эту марку на этой стадии в рамках
    этого проекта». Валидность пар (марка,стадия) и (отдел,марка) на
    вставке проверяет триггер trg_check_composition_validity на стороне
    БД — код формы обязан ловить его исключение и отдавать понятный detail
    без утечки внутренних деталей (правило 18).
    """

    __tablename__ = "project_composition"

    id: Mapped[int] = mapped_column(primary_key=True)

    # project_id → projects (перецеплено со стаба project на боевую копию).
    # CASCADE: удаление проекта чистит его состав целиком.
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Три справочных FK — RESTRICT: нельзя удалить отдел/марку/стадию,
    # пока на них ссылается строка состава (защита справочников).
    department_id: Mapped[int] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT"),
        nullable=False,
    )
    marka_type_id: Mapped[int] = mapped_column(
        ForeignKey("marka_types.id", ondelete="RESTRICT"),
        nullable=False,
    )
    stage_id: Mapped[int] = mapped_column(
        ForeignKey("project_stages.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # Навигационные связи для отображения дерева/списка состава.
    # relationship к Project использует строковую ссылку "Project" —
    # класс живёт в другом модуле (project.py), резолвится лениво при
    # конфигурации мапперов. lazy по умолчанию; конкретную стратегию
    # загрузки (selectin/joinedload) выберем на этапе чтения дерева.
    project: Mapped["Project"] = relationship()  
    department: Mapped["Department"] = relationship()
    marka_type: Mapped["MarkaType"] = relationship()
    stage: Mapped["ProjectStage"] = relationship()
    # Комплекты (физические документы), заведённые начальником бюро под
    # этой строкой состава. Обратная сторона ProjectMarkSet.composition.
    # Уровень 2 дерева: под маркой показываются её шифры. lazy по
    # умолчанию (select) — комплекты грузим явно там, где строим дерево.
    mark_sets: Mapped[List["ProjectMarkSet"]] = relationship(
        back_populates="composition",
        # Явный ключ обязателен: после появления recipient_composition_id
        # таблицы связаны двумя FK и автовывод условия соединения
        # невозможен (AmbiguousForeignKeysError при конфигурации мапперов).
        # Строкой, а не объектом: ProjectMarkSet объявлен ниже по файлу.
        foreign_keys="ProjectMarkSet.composition_id",
    )

    __table_args__ = (
        # Бизнес-ключ строки состава — та же четвёрка, что unique_composition
        # в DDL. Имя constraint совпадает с БД ради чистого autogenerate.
        UniqueConstraint(
            "project_id",
            "department_id",
            "marka_type_id",
            "stage_id",
            name="unique_composition",
        ),
        # Индекс поиска состава по отделу+стадии — faithful DDL.
        Index("idx_composition_dept_stage", "department_id", "stage_id"),
    )

    def __repr__(self) -> str:
        return (
            f"ProjectComposition(id={self.id!r}, "
            f"project_id={self.project_id!r})"
        )
    
class ProjectMarkSet(Base):
    """Комплект/документ — физический шифр (уровень 2 состава).

    Создаётся начальником бюро в рамках строки состава (composition_id):
    ГИП назначил марку на стадии → бюро заводит под неё конкретный шифр
    (marka_full, напр. 053-22-14а-ТХ1). Несколько комплектов могут
    ссылаться на одну строку состава.

    Задание смежному отделу — тот же комплект, у которого заполнен
    recipient_composition_id. Отдельной сущности и отдельного флага нет:
    признак задания выводится из наличия адресата, поэтому разойтись
    ему не с чем.

    Запись ведут оба контура: Flask (реестр) и FastAPI (кабинет
    начальника бюро). last_update без onupdate на стороне ORM —
    штамп ставит пишущая сторона явно.
    """

    __tablename__ = "project_mark_sets"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Родительская строка состава — источник работы.
    # RESTRICT (Защита A): удаление строки состава физически заблокировано,
    # пока под ней есть комплекты. Модель раньше объявляла CASCADE и
    # разошлась с БД — при autogenerate это дало бы откат защиты.
    composition_id: Mapped[int] = mapped_column(
        ForeignKey("project_composition.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # Строка состава получателя задания.
    # NULL — обычный комплект; заполнено — задание смежному отделу.
    # RESTRICT по той же причине, что и у composition_id: нельзя удалить
    # строку состава, на которую выдано задание.
    # relationship намеренно не объявлен: имя адресата достаётся явным
    # JOIN в запросе дерева, а экран входящих отложен на v2.
    recipient_composition_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("project_composition.id", ondelete="RESTRICT"),
    )

    # Бизнес-ключ комплекта — полный шифр, вводится вручную начальником
    # бюро. UNIQUE на уровне БД гарантирует отсутствие дублей шифров.
    marka_full: Mapped[str] = mapped_column(Text, unique=True, nullable=False)

    # Исполнитель. FK на employees без relationship (минимум для дерева —
    # решение согласовано): в дереве состава ФИО не показываем, только
    # шифр. SET NULL: увольнение сотрудника обнуляет ссылку, комплект
    # остаётся. Nullable — комплект может быть ещё не назначен.
    id_executor: Mapped[Optional[int]] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"),
    )

    # Статус документа. FK на set_statuses без relationship (минимум).
    # RESTRICT: нельзя удалить статус, пока на него ссылается комплект.
    status_id: Mapped[int] = mapped_column(
        ForeignKey("set_statuses.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # Метки времени — timestamptz (WITH time zone), в отличие от projects
    # (там without). server_default=now() ставит БД. onupdate НЕ ставим —
    # см. docstring класса (запись ведёт Flask, у нас только чтение).
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    last_update: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
    )

    # Обратная навигация к строке состава-источнику (уровень 2 → 1 дерева).
    # foreign_keys обязателен — см. комментарий у mark_sets.
    composition: Mapped["ProjectComposition"] = relationship(
        back_populates="mark_sets",
        foreign_keys="ProjectMarkSet.composition_id",
    )

        # Исполнитель шифра. lazy="raise_on_sql": в async-контуре ленивая
    # подгрузка при обращении из шаблона даёт MissingGreenlet уже после
    # закрытия сессии — далеко от места, где формировался запрос.
    # Явная ошибка при разработке лучше падения на рендеринге.
    executor: Mapped[Optional["Employee"]] = relationship(
        foreign_keys="ProjectMarkSet.id_executor",
        lazy="raise_on_sql",
    )

    __table_args__ = (
        # Индексы — faithful DDL (idx_sets_composition/executor/status),
        # чтобы autogenerate не счёл их лишними и не предложил удалить.
        Index("idx_sets_composition", "composition_id"),
        Index("idx_sets_executor", "id_executor"),
        Index("idx_sets_status", "status_id"),
        # Частичный индекс по заданиям: строки с NULL в него не попадают.
        # postgresql_where зеркалит WHERE из DDL — без него autogenerate
        # увидит расхождение и предложит пересоздать индекс.
        Index(
            "idx_sets_recipient",
            "recipient_composition_id",
            postgresql_where=text("recipient_composition_id IS NOT NULL"),
        ),
        # Запрет самоадресации: источник и получатель — разные строки
        # состава. Имя совпадает с БД ради чистого autogenerate.
        CheckConstraint(
            "recipient_composition_id IS NULL "
            "OR recipient_composition_id <> composition_id",
            name="project_mark_sets_recipient_not_self",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"ProjectMarkSet(id={self.id!r}, "
            f"marka_full={self.marka_full!r})"
        )


class SetStatus(Base):
    """Статус комплекта: жизненный цикл документа.

    Значения: 1 «В плане», 2 «В работе», 3 «На проверке», 4 «Выпущен».
    Новый шифр заводится с «В плане»; «Выпущен» ставит начальник бюро,
    подтверждая сдачу.

    Модель нужна не только для чтения названий: без неё SQLAlchemy не
    может разрешить внешний ключ project_mark_sets.status_id и падает
    на INSERT (NoReferencedTableError) — при сбросе в базу движок
    выстраивает порядок вставки по зависимостям и разыменовывает
    каждый FK.
    """

    __tablename__ = "set_statuses"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)

    # Колонка есть в DDL, но не используется ни FastAPI, ни Flask: цвет
    # статуса задаётся в static/css/style.css (--status-N), потому что
    # подставить его из базы можно только запрещённым inline-стилем.
    # Хранит прежнюю палитру — ориентироваться на неё не нужно.
    color: Mapped[Optional[str]] = mapped_column(String(20))

    # Порядок отображения в списках. server_default, а не default:
    # значение проставляет БД, и оно едино для обоих контуров.
    sort_order: Mapped[Optional[int]] = mapped_column(server_default="0")

    def __repr__(self) -> str:
        return f"SetStatus(id={self.id!r}, name={self.name!r})"