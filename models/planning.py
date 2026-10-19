# app/models/planning.py
"""ORM-модели раздела «Планирование».

Модели построены строго по DDL базы (правило 26 — схема как источник
истины), сверенному по дампу от 02.09.2026. Семь таблиц образуют
логический модуль «план и факт занятости»:

    DayType            — справочник типов дня (рабочий/выходной/сокращённый)
    ProductionCalendar — производственный календарь: одна строка = один день
    AbsenceType        — справочник причин неявки (отпуск, больничный, …)
    AbsencePeriod      — период неявки конкретного работника
    WorkPlan           — шапка плана: шифр + исполнитель + границы
    PlanCalendarCell   — плановая ячейка: один инженеро-день под шифр
    FactCalendarCell   — фактическая ячейка: отметка работника о работе

Ключевая особенность модуля: владелец истины — ЯЧЕЙКИ, а не шапка.
Начальник раскладывает дни вручную в календарной сетке, а start_date,
end_date и work_days вычисляются из положенного. Обратный вывод
невозможен: раскладка дырявая (пропускает выходные, отсутствия и дни,
занятые другими шифрами), и повторный прогон алгоритма в другой момент
времени даст другой набор дат при тех же границах.

Отличие от absence_periods, где хранится именно диапазон: неявка
непрерывна, поэтому её проекция на дни вычисляется однозначно и таблица
ячеек ей не нужна. План так не умеет — отсюда plan_calendar_cells.

Все FK и ondelete зеркалят БД буквально: где в схеме RESTRICT, там
RESTRICT и в модели. Расхождение здесь особенно опасно — ложный CASCADE
создал бы уверенность, что удаление плана уносит ячейки, и при
autogenerate откатил бы защиту в самой БД.
"""
from datetime import date, datetime
from typing import TYPE_CHECKING, List, Optional

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
    from app.models.composition import ProjectMarkSet
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


class WorkPlan(Base):
    """Шапка плана: связка «шифр + исполнитель» и границы раскладки.

    ВАЖНО: это производная запись, а не задание. Владелец истины —
    plan_calendar_cells. Начальник раскладывает дни в сетке, после чего
    start_date / end_date / work_days пересчитываются из ячеек: первый
    день, последний день, количество. Обратный вывод невозможен —
    раскладка дырявая.

    Отсюда же правило записи: сначала пишем ячейки, затем одним UPDATE
    ставим границы и updated_at. Порядок обязателен, пока жив триггер
    trg_update_end_date на plan_calendar_cells (он пересчитывает
    end_date построчно и затрёт значение, записанное до вставки).
    Триггер снимается в шаге 6 вместе с fill_plan_cells.

    Статус: draft в схеме есть, но не используется — сохранение сразу
    создаёт confirmed. archived оставлен под перепланирование.
    """

    __tablename__ = "work_plan"

    id: Mapped[int] = mapped_column(primary_key=True)

    # CASCADE: удаление шифра уносит его план целиком — плана без шифра
    # не бывает.
    mark_set_id: Mapped[int] = mapped_column(
        ForeignKey("project_mark_sets.id", ondelete="CASCADE"),
        nullable=False,
    )

    # RESTRICT: работника нельзя удалить, пока на нём висят планы.
    # NOT NULL — плана без человека не существует, поэтому шифр без
    # назначенного исполнителя спланировать нельзя.
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # Границы. start_date и work_days объявлены NOT NULL в БД, поэтому
    # заполняются в том же запросе, что создаёт план. end_date nullable:
    # исторически его ставил триггер после вставки первой ячейки.
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    work_days: Mapped[int] = mapped_column(nullable=False)
    end_date: Mapped[Optional[date]] = mapped_column(Date)

    status: Mapped[str] = mapped_column(
        String(20),
        server_default=text("'draft'::character varying"),
        nullable=False,
    )

    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # onupdate НЕ ставим: штамп проставляет пишущая сторона явно, тем же
    # UPDATE, что пересчитывает границы. Так поле переживёт снятие
    # триггера без осиротения.
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )

    # passive_deletes=True: удаление плана каскадит на ячейки силами БД,
    # ORM не должна вычитывать их ради построчного DELETE.
    cells: Mapped[List["PlanCalendarCell"]] = relationship(
        back_populates="work_plan",
        lazy="raise_on_sql",
        passive_deletes=True,
    )

    mark_set: Mapped["ProjectMarkSet"] = relationship(lazy="raise_on_sql")
    employee: Mapped["Employee"] = relationship(lazy="raise_on_sql")

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'confirmed', 'archived')",
            name="work_plan_status_check",
        ),
        CheckConstraint("work_days > 0", name="work_plan_work_days_check"),
        # Один активный план на шифр. Частичный уникальный индекс:
        # archived-планы в него не попадают, поэтому история
        # перепланирований накапливается без конфликтов.
        # postgresql_where зеркалит WHERE из DDL — без него autogenerate
        # увидит расхождение и предложит пересоздать индекс.
        Index(
            "idx_one_active_plan",
            "mark_set_id",
            unique=True,
            postgresql_where=text("status IN ('draft', 'confirmed')"),
        ),
        Index("idx_work_plan_employee", "employee_id"),
    )

    def __repr__(self) -> str:
        return (
            f"WorkPlan(id={self.id!r}, mark_set_id={self.mark_set_id!r}, "
            f"employee_id={self.employee_id!r}, status={self.status!r})"
        )


class PlanCalendarCell(Base):
    """Плановая ячейка: один инженеро-день, отданный под один шифр.

    Атом планирования. Дробных долей дня нет: день либо занят шифром
    целиком, либо свободен.

    unique_plan_cell (employee_id, work_date) — главное ограничение
    модуля. Оно физически запрещает положить два шифра на один день
    одного человека, независимо от того, что нарисовано на экранах.
    Именно на него опирается стратегия разрешения конфликтов при
    одновременной работе двух начальников: конкурент получает
    IntegrityError на сохранении, а не тихую перезапись.

    employee_id продублирован здесь при живом work_plan.employee_id
    намеренно — иначе ограничение выше пришлось бы проверять join'ом,
    а UNIQUE так не умеет. Следствие: при смене исполнителя обе таблицы
    надо править согласованно.

    work_date ссылается на production_calendar — ячейку нельзя создать
    на дату вне заполненного календаря.
    """

    __tablename__ = "plan_calendar_cells"

    id: Mapped[int] = mapped_column(primary_key=True)

    # CASCADE: удаление плана уносит его ячейки силами БД.
    work_plan_id: Mapped[int] = mapped_column(
        ForeignKey("work_plan.id", ondelete="CASCADE"),
        nullable=False,
    )

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

    work_plan: Mapped["WorkPlan"] = relationship(
        back_populates="cells", lazy="raise_on_sql"
    )

    __table_args__ = (
        UniqueConstraint(
            "employee_id", "work_date", name="unique_plan_cell"
        ),
        # Индекс дублирует уникальное ограничение по составу колонок, но
        # он есть в БД — объявляем faithful, иначе autogenerate предложит
        # его удалить.
        Index("idx_plan_cells_employee_date", "employee_id", "work_date"),
        Index("idx_plan_cells_work_plan", "work_plan_id"),
    )

    def __repr__(self) -> str:
        return (
            f"PlanCalendarCell(id={self.id!r}, "
            f"employee_id={self.employee_id!r}, "
            f"work_date={self.work_date!r})"
        )


class FactCalendarCell(Base):
    """Фактическая ячейка: отметка работника о работе за день.

    Симметрична плановой, но ссылается на шифр напрямую (mark_set_id),
    а не на план. Так и должно быть: факт возможен там, где плана не
    было вовсе — «факт вне плана» на макете сетки.

    is_auto — отметка проставлена системой, а не человеком.
    override_reason — пояснение к ручному исправлению администратором.
    is_retro — отметка задним числом; вычисляется триггером БД.

    unique_fact_cell (employee_id, work_date): за день можно отчитаться
    ровно один раз, по одному шифру.
    """

    __tablename__ = "fact_calendar_cells"

    id: Mapped[int] = mapped_column(primary_key=True)

    # CASCADE: удаление шифра уносит отметки по нему.
    mark_set_id: Mapped[int] = mapped_column(
        ForeignKey("project_mark_sets.id", ondelete="CASCADE"),
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

    mark_set: Mapped["ProjectMarkSet"] = relationship(lazy="raise_on_sql")

    __table_args__ = (
        UniqueConstraint(
            "employee_id", "work_date", name="unique_fact_cell"
        ),
        Index("idx_fact_cells_employee_date", "employee_id", "work_date"),
        Index("idx_fact_cells_mark_set", "mark_set_id"),
    )

    def __repr__(self) -> str:
        return (
            f"FactCalendarCell(id={self.id!r}, "
            f"mark_set_id={self.mark_set_id!r}, "
            f"work_date={self.work_date!r})"
        )