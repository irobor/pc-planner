# app/models/planning.py
"""ORM-модели раздела «Планирование».

Модели построены строго по DDL базы (правило 26 — схема как источник
истины). Шесть таблиц образуют логический модуль «план и факт занятости»:

    DayType            — справочник типов дня (рабочий/выходной/сокращённый)
    ProductionCalendar — производственный календарь: одна строка = один день
    AbsenceType        — справочник причин неявки (отпуск, больничный, …)
    AbsencePeriod      — период неявки конкретного работника
    PlanCalendarCell   — плановая ячейка: один инженеро-день на пакет работ
    FactCalendarCell   — фактическая ячейка: отметка работника о работе

План и факт симметричны: обе ячейки ссылаются на пакет работ
(WorkPackage, app/models/composition.py) напрямую. Шапки плана нет —
план это только ячейки. Первый день, последний день и число дней
пакета вычисляются запросом из положенного. Обратный вывод невозможен:
раскладка дырявая (пропускает выходные, отсутствия и дни, занятые
другими пакетами).

Отличие от absence_periods, где хранится именно диапазон: неявка
непрерывна, поэтому её проекция на дни вычисляется однозначно и таблица
ячеек ей не нужна. План так не умеет — отсюда plan_calendar_cells.

Все FK и ondelete зеркалят БД буквально: где в схеме RESTRICT, там
RESTRICT и в модели. Расхождение здесь особенно опасно — ложный CASCADE
создал бы уверенность, что удаление уносит зависимые строки, и при
autogenerate изменил бы поведение самой БД.
"""
from datetime import date, datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    FetchedValue,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,    
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    # Только для аннотаций: реальный импорт создал бы цикл, а строковые
    # аннотации SQLAlchemy разрешает по имени класса в общем реестре.
    from app.models.composition import WorkPackage
    from app.models.employee import Employee


class DayType(Base):
    """Тип календарного дня: рабочий, выходной, праздник, сокращённый.

    Справочник вынесен отдельно, потому что production_calendar
    нормализован: признака «рабочий» в нём нет, он лежит здесь в
    is_working. Любая проверка «можно ли планировать на этот день»
    обязана дойти до этой таблицы через day_type_id.

    Код SHORT (сокращённый предпраздничный) на планирование не влияет:
    день остаётся рабочим, разница только в часах табеля.
    """

    __tablename__ = "day_types"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Мнемокод: WORK, WEEKEND, HOLIDAY, SHORT. UNIQUE в БД.
    code: Mapped[str] = mapped_column(String(10), unique=True, nullable=False)

    name: Mapped[str] = mapped_column(String(50), nullable=False)

    # Ядро справочника: false запрещает и планирование, и факт.
    is_working: Mapped[bool] = mapped_column(Boolean, nullable=False)

    def __repr__(self) -> str:
        return f"DayType(id={self.id!r}, code={self.code!r})"


class ProductionCalendar(Base):
    """Производственный календарь: одна строка на каждый календарный день.

    PK — сама дата, а не суррогатный id. Это не стилистика: на work_date
    ссылаются обе таблицы ячеек, и такой FK физически запрещает создать
    ячейку на дату, которой нет в календаре.

    Практическое следствие: горизонт планирования ограничен заполненным
    календарём (сейчас 2025-01-01 … 2026-12-31). Выход за край даёт
    ошибку уровня БД (23503), поэтому раскладка обязана проверять
    границу заранее и отвечать понятным текстом, а не 500-й.

    Заполняется вручную функцией fill_calendar_year(p_year) — автоматики
    нет намеренно: расстановка праздников требует сверки с
    производственным календарём РФ.
    """

    __tablename__ = "production_calendar"

    work_date: Mapped[date] = mapped_column(Date, primary_key=True)

    # RESTRICT: справочник типов нельзя чистить, пока на него ссылаются
    # дни календаря.
    day_type_id: Mapped[int] = mapped_column(
        ForeignKey("day_types.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # Название праздника или причина переноса. Свободный текст.
    description: Mapped[Optional[str]] = mapped_column(Text)

    # lazy="raise_on_sql" — в async-контуре ленивая подгрузка даёт
    # MissingGreenlet далеко от места, где формировался запрос. Явная
    # ошибка при разработке лучше падения на рендеринге; нужные связи
    # тянем через selectinload.
    day_type: Mapped["DayType"] = relationship(lazy="raise_on_sql")

    def __repr__(self) -> str:
        return f"ProductionCalendar(work_date={self.work_date!r})"


class AbsenceType(Base):
    """Справочник причин неявки: отпуск, больничный, командировка и т. д.

    blocks_planning — ключевое поле для раскладки. Не всякая неявка
    закрывает день: командировка может считаться рабочей. Проверять надо
    именно этот флаг, а не факт наличия периода.

    tab_letter — буква табеля (Б, ОТ, К). Та самая, что выводится в
    ячейке сетки поверх дня отсутствия.
    """

    __tablename__ = "absence_types"

    id: Mapped[int] = mapped_column(primary_key=True)

    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(50), nullable=False)

    # Буква табеля. UNIQUE — две причины не могут делить обозначение.
    tab_letter: Mapped[str] = mapped_column(
        String(5), unique=True, nullable=False
    )

    # true — на такой день план ставить нельзя.
    blocks_planning: Mapped[bool] = mapped_column(
        Boolean, server_default=text("true"), nullable=False
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean, server_default=text("true"), nullable=False
    )

    # Код по унифицированной форме Т-13. Nullable и UNIQUE одновременно:
    # в PostgreSQL NULL не конфликтует с NULL, поэтому у нескольких
    # причин код может отсутствовать.
    t13_code: Mapped[Optional[str]] = mapped_column(String(2), unique=True)

    def __repr__(self) -> str:
        return f"AbsenceType(id={self.id!r}, code={self.code!r})"


class AbsencePeriod(Base):
    """Период неявки работника — хранится диапазоном, а не по дням.

    Причина, по которой здесь нет таблицы ячеек: неявка непрерывна.
    Из пары дат любой день диапазона восстанавливается однозначно и
    навсегда, поэтому проекция на сетку вычисляется на лету (так же
    устроен табель Flask через v_timesheet).

    Границы включительные: daterange(date_from, date_to, '[]') в
    EXCLUDE-ограничении. Последний день отсутствия входит в период.

    На раскладку влияет не сам факт периода, а absence_types.blocks_planning.
    """

    __tablename__ = "absence_periods"

    id: Mapped[int] = mapped_column(primary_key=True)

    # RESTRICT: работника нельзя удалить, пока за ним числятся неявки.
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="RESTRICT"),
        nullable=False,
    )

    absence_type_id: Mapped[int] = mapped_column(
        ForeignKey("absence_types.id", ondelete="RESTRICT"),
        nullable=False,
    )

    date_from: Mapped[date] = mapped_column(Date, nullable=False)
    date_to: Mapped[date] = mapped_column(Date, nullable=False)

    # Кто оформил. SET NULL: удаление учётной записи не должно уносить
    # кадровую историю.
    created_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    # В БД DEFAULT now() без NOT NULL — отсюда Optional.
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    absence_type: Mapped["AbsenceType"] = relationship(lazy="raise_on_sql")
    employee: Mapped["Employee"] = relationship(lazy="raise_on_sql")

    __table_args__ = (
        # Границы диапазона: конец не раньше начала.
        CheckConstraint(
            "date_to >= date_from",
            name="absence_periods_range_check",
        ),
        # Запрет пересечения периодов одного работника. Реализован
        # EXCLUDE USING gist: у человека не может быть одновременно
        # отпуска и больничного. Требует расширения btree_gist (в БД
        # уже установлено) — оно даёт gist-оператор `=` для integer.
        ExcludeConstraint(
            ("employee_id", "="),
            (text("daterange(date_from, date_to, '[]')"), "&&"),
            name="absence_periods_no_overlap",
            using="gist",
        ),
        Index("idx_absence_periods_employee", "employee_id", "date_from"),
    )

    def __repr__(self) -> str:
        return (
            f"AbsencePeriod(id={self.id!r}, "
            f"employee_id={self.employee_id!r}, "
            f"date_from={self.date_from!r}, date_to={self.date_to!r})"
        )



class PlanCalendarCell(Base):
    """Плановая ячейка: один инженеро-день, отданный под один пакет работ.

    Атом планирования. Дробных долей дня нет: день либо занят пакетом
    целиком, либо свободен.

    unique_plan_cell (employee_id, work_date) — главное ограничение
    модуля. Оно физически запрещает положить два пакета на один день
    одного человека, независимо от того, что нарисовано на экранах.
    Именно на него опирается стратегия разрешения конфликтов при
    одновременной работе двух начальников: конкурент получает
    IntegrityError на сохранении, а не тихую перезапись.

    employee_id хранится в ячейке, а не берётся из пакета: ограничение
    выше join'ом не проверить — UNIQUE так не умеет. И ячейка помнит,
    кто должен был работать в тот день: при смене исполнителя пакета
    прошлые ячейки остаются за прежним человеком.

    work_date ссылается на production_calendar — ячейку нельзя создать
    на дату вне заполненного календаря.
    """

    __tablename__ = "plan_calendar_cells"

    id: Mapped[int] = mapped_column(primary_key=True)

    # CASCADE: удаление пакета уносит его план силами БД.
    package_id: Mapped[int] = mapped_column(
        ForeignKey("work_packages.id", ondelete="CASCADE"),
        nullable=False,
    )

    # RESTRICT: работника нельзя удалить, пока на нём есть план.
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # Без ondelete: в БД NO ACTION. День календаря нельзя удалить, пока
    # на него что-то запланировано.
    work_date: Mapped[date] = mapped_column(
        ForeignKey("production_calendar.work_date"),
        nullable=False,
    )

    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    package: Mapped["WorkPackage"] = relationship(lazy="raise_on_sql")

    __table_args__ = (
        UniqueConstraint(
            "employee_id", "work_date", name="unique_plan_cell"
        ),
        # Индекс дублирует уникальное ограничение по составу колонок, но
        # он есть в БД — объявляем faithful, иначе autogenerate предложит
        # его удалить.
        Index("idx_plan_cells_employee_date", "employee_id", "work_date"),
        # Ячейки пакета: сводка «Дни / Срок», готовность, удаление шифра.
        Index("idx_plan_cells_package", "package_id"),
    )

    def __repr__(self) -> str:
        return (
            f"PlanCalendarCell(id={self.id!r}, "
            f"package_id={self.package_id!r}, "
            f"employee_id={self.employee_id!r}, "
            f"work_date={self.work_date!r})"
        )


class FactCalendarCell(Base):
    """Фактическая ячейка: отметка работника о работе за день.

    Симметрична плановой: тоже ссылается на пакет работ. Факт возможен
    там, где плана не было вовсе — «факт вне плана» на сетке, — и не
    обязательно от исполнителя пакета: подмена живёт только в факте.

    is_auto — отметка проставлена системой, а не человеком.
    override_reason — пояснение к ручному исправлению администратором.
    is_retro — отметка задним числом; вычисляется триггером БД.

    unique_fact_cell (employee_id, work_date): за день можно отчитаться
    ровно один раз, по одному пакету.
    """

    __tablename__ = "fact_calendar_cells"

    id: Mapped[int] = mapped_column(primary_key=True)

    # CASCADE: удаление пакета уносит отметки по нему.
    package_id: Mapped[int] = mapped_column(
        ForeignKey("work_packages.id", ondelete="CASCADE"),
        nullable=False,
    )

    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="RESTRICT"),
        nullable=False,
    )

    work_date: Mapped[date] = mapped_column(
        ForeignKey("production_calendar.work_date"),
        nullable=False,
    )

    is_auto: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), nullable=False
    )

    override_reason: Mapped[Optional[str]] = mapped_column(Text)

    # Ретроввод: отметка поставлена или перенесена после окончания
    # work_date. Значение вычисляет триггер trg_fact_check_absence, а не
    # приложение — поэтому server_default на вставку и FetchedValue на
    # обновление: SQLAlchemy знает, что база меняет поле сама, и не
    # держит в памяти устаревшее значение.
    is_retro: Mapped[bool] = mapped_column(
        Boolean,
        server_default=text("false"),
        server_onupdate=FetchedValue(),
        nullable=False,
    )

    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    package: Mapped["WorkPackage"] = relationship(lazy="raise_on_sql")

    __table_args__ = (
        UniqueConstraint(
            "employee_id", "work_date", name="unique_fact_cell"
        ),
        Index("idx_fact_cells_employee_date", "employee_id", "work_date"),
        # Отметки пакета: готовность, остаток, удаление шифра.
        Index("idx_fact_cells_package", "package_id"),
    )

    def __repr__(self) -> str:
        return (
            f"FactCalendarCell(id={self.id!r}, "
            f"package_id={self.package_id!r}, "
            f"work_date={self.work_date!r})"
        )