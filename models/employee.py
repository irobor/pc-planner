# app/models/employee.py
"""ORM-модель кадровой личности работника.

Модель построена строго по DDL базы test_timesheet (правило 26 — схема
как источник истины), сверенному через pgAdmin CREATE Script.

Ключевое разделение идентичности в проекте:

    employees — кадровая личность: ФИО, табельный номер, рабочее место.
                Хаб для табеля, планирования, назначений и неявок.
    users     — только аутентификация и права; ФИО там нет.

Связь необязательна в обе стороны:
  * работник без учётной записи (архивариус, рабочий) — user_id IS NULL;
  * учётная запись без работника (sys_admin, reader) — строки в
    employees просто нет.

Отдела, должности и периода работы здесь нет намеренно: они темпоральны
и живут в employee_assignments (приём = открытие назначения, перевод =
закрытие + открытие, увольнение = закрытие последнего).

Табельный номер обязателен и содержит только цифры. Работник без номера
не опознаётся в бухгалтерском табеле — такая запись бессмысленна. Тип
varchar(20), а не integer, выбран потому, что ведущий ноль значим: 007
и 7 — разные номера. Сортировка по числовому значению выполняется на
стороне приложения.
"""
from datetime import date, datetime
from typing import Optional

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Employee(Base):
    """Работник как личность — стабильная запись, не удаляемая при увольнении."""

    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Ссылка на учётную запись необязательна: работник может не иметь
    # доступа в систему. ON DELETE SET NULL — удаление учётки обнуляет
    # ссылку, но сохраняет кадровую карточку и всю историю табеля.
    # UNIQUE гарантирует кардинальность 0..1 и допускает множественные
    # NULL, поэтому работников без учёток может быть сколько угодно.
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        unique=True,
    )

    # Единственный надёжный различитель работников: ФИО не уникально
    # (в бюро есть однофамильцы, полные тёзки принципиально допустимы).
    personnel_number: Mapped[str] = mapped_column(
        String(20), unique=True, nullable=False
    )

    full_name: Mapped[str] = mapped_column(String(100), nullable=False)

    # Справочные атрибуты рабочего места: нужны только чтобы найти
    # человека и позвонить. В расчётах табеля и планирования не
    # участвуют, история переездов не ведётся. Телефон намеренно без
    # UNIQUE — он закреплён за рабочим местом, может быть общим для
    # нескольких работников или передан другому при увольнении.
    room: Mapped[Optional[str]] = mapped_column(String(10))
    phone: Mapped[Optional[str]] = mapped_column(String(20))

    __table_args__ = (
        # NOT NULL в одиночку недостаточен: пустая строка для PostgreSQL —
        # законное значение, а HTML-форма присылает именно её при
        # незаполненном поле. Хуже того, UNIQUE считает '' обычным
        # значением, поэтому второй работник с пустым полем упал бы с
        # сообщением «номер уже занят», хотя проблема совсем в другом.
        # Регулярка закрывает пустую строку, пробелы и буквы одним
        # правилом — отдельный CHECK на непустоту избыточен.
        #
        # Имя констрейнта задано явно и менять его нельзя: Flask-контур
        # опознаёт ошибку по diag.constraint_name, потому что текст
        # сообщения PostgreSQL приходит в локали сервера (cp1251) и валит
        # psycopg2 на UnicodeDecodeError. Имена констрейнтов — ASCII, и
        # это единственный надёжный способ разобрать ошибку.
        #
        # Ограничение подхода: буквенные и дефисные номера не проходят.
        # Если у подразделения СУБ (субподрядчики) нумерация окажется
        # иной, потребуется смягчение до '^[0-9A-Za-z-]+$' — с правкой
        # и схемы, и этой модели, иначе они разъедутся.
        CheckConstraint(
            "personnel_number ~ '^[0-9]+$'",
            name="employees_personnel_number_digits",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"Employee(id={self.id!r}, "
            f"personnel_number={self.personnel_number!r}, "
            f"full_name={self.full_name!r})"
        )

class EmployeeAssignment(Base):
    """Назначение работника: где и кем он числится в заданный период.

    Темпоральная таблица. Приём на работу — открытие назначения
    (valid_to IS NULL), перевод — закрытие текущего и открытие нового,
    увольнение — закрытие последнего. История не удаляется: по ней
    восстанавливается, в каком подразделении работник числился в любой
    прошедший день.

    На этой таблице стоит область управления (app/core/scope.py):
    поддерево подразделений начальника отсчитывается от department_id
    его АКТУАЛЬНОГО назначения.

    ┌─────────────────────────────────────────────────────────────────┐
    │ ВНИМАНИЕ. В БД есть ограничение, которого НЕТ в этой модели:    │
    │                                                                 │
    │   employee_assignments_no_overlap                               │
    │   EXCLUDE USING gist (                                          │
    │       employee_id WITH =,                                       │
    │       daterange(valid_from, valid_to, '[]') WITH &&             │
    │   )                                                             │
    │                                                                 │
    │ Оно запрещает пересечение периодов у одного работника и тем     │
    │ гарантирует, что на любую дату назначение ровно одно (или ноль).│
    │ Весь расчёт области опирается на эту гарантию.                  │
    │                                                                 │
    │ SQLAlchemy не умеет выражать EXCLUDE штатными средствами,       │
    │ поэтому Alembic его НЕ ВИДИТ. При пересоздании таблицы          │
    │ миграцией ограничение исчезнет молча — и у работника появятся   │
    │ пересекающиеся назначения, а область станет неопределённой.     │
    │ Восстанавливать вручную (см. DDL выше).                         │
    └─────────────────────────────────────────────────────────────────┘

    Границы периода ЗАМКНУТЫ с обеих сторон — daterange создан с '[]'.
    День valid_to ещё ВХОДИТ в назначение, это не «действует до».
    Отсюда условие актуальности: valid_to IS NULL OR valid_to >= today.
    """

    __tablename__ = "employee_assignments"

    id: Mapped[int] = mapped_column(primary_key=True)

    # CASCADE: удаление работника уносит его назначения. Расходится с
    # остальными ключами (RESTRICT) намеренно — назначение не имеет
    # смысла без работника, это его атрибут во времени, а не сущность.
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
    )

    # RESTRICT: подразделение со штатом удалить нельзя. Иначе сотрудники
    # повисли бы вне оргструктуры и выпали из любой области управления.
    department_id: Mapped[int] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # RESTRICT по той же причине. Должность на доступ не влияет
    # (positions.rank — только для workflow согласования), но нужна
    # кадровому контуру Flask.
    position_id: Mapped[int] = mapped_column(
        ForeignKey("positions.id", ondelete="RESTRICT"),
        nullable=False,
    )

    valid_from: Mapped[date] = mapped_column(Date, nullable=False)

    # NULL = назначение открыто, работник числится здесь сейчас.
    valid_to: Mapped[Optional[date]] = mapped_column(Date)

    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )

    __table_args__ = (
        # Воспроизводит ограничение БД с тем же именем, чтобы
        # autogenerate не счёл его лишним и не предложил удалить.
        CheckConstraint(
            "valid_to IS NULL OR valid_to >= valid_from",
            name="employee_assignments_range_check",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"EmployeeAssignment(employee_id={self.employee_id!r}, "
            f"department_id={self.department_id!r}, "
            f"valid_from={self.valid_from!r}, valid_to={self.valid_to!r})"
        )



class Position(Base):
    """Справочник должностей. Таблица кадрового контура Flask.

    Наш контур только читает её: название должности показывается в меню
    пользователя (app/core/assignments.py). Поля — строго по DDL;
    rank нужен Flask для порядка согласования, здесь не используется.
    Связь (relationship) с EmployeeAssignment не заведена: единственный
    запрос делает явный JOIN, а связь, которой никто не пользуется, —
    лишний код.
    """

    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    rank: Mapped[int] = mapped_column(nullable=False)

    def __repr__(self) -> str:
        return f"Position(id={self.id!r}, name={self.name!r})"