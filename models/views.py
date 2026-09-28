# app/models/views.py
"""Read-only ORM-модели над представлениями (VIEW) БД.

Отделены от таблиц (app/models/composition.py и др.) сознательно: это
проекции только для чтения, а не сущности, в которые пишет приложение.
Отдельный модуль честно сигналит потребителю: «здесь SELECT, не INSERT».

ВАЖНО (Alembic): при инициализации миграций autogenerate увидит эти
классы в Base.metadata как таблицы и попытается сгенерировать CREATE
TABLE. VIEW создаётся отдельным DDL (см. tablev4.sql), поэтому такие
модели нужно исключать из autogenerate через include_object в env.py —
тем же механизмом, что и боевую projects. Здесь это не решается, лишь
фиксируется как известное следствие мапинга VIEW на ORM.
"""
from typing import Optional

from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class VProjectComposition(Base):
    """Строка представления v_project_composition (состав проекта).

    Плоская проекция задания ГИПа с разрешёнными человекочитаемыми
    полями (коды/имена отдела, марки, стадии) и счётчиком физических
    документов. Используется API-редактором состава: наполняет превью
    существующими строками (existing) и даёт сырые id для сборки payload
    на фронте и последующей POST-синхронизации.

    Контур записи — ТОЛЬКО ЧТЕНИЕ. Во VIEW нельзя вставлять/обновлять
    без INSTEAD OF-триггеров (их нет). Поэтому у модели нет relationship,
    server_default и прочего, что провоцировало бы запись: любые попытки
    session.add/flush по этому классу — ошибка проектирования.

    Псевдо-PK: у VIEW нет физического первичного ключа, а ORM требует
    его для identity map. Роль PK играет composition_id — он приходит из
    pc.id (PK таблицы project_composition), а GROUP BY pc.id во вьюхе
    гарантирует ровно одну строку выборки на каждый composition_id.
    Уникальность — по построению запроса, а не по ограничению в БД.
    """

    __tablename__ = "v_project_composition"

    # Псевдо-PK. Не создаёт ограничения в БД — только объявляет ORM,
    # какую колонку считать идентити-ключом строки представления.
    composition_id: Mapped[int] = mapped_column(primary_key=True)

    # id проекта — по нему фильтруется состав в GET /{project_id}.
    # (Во вьюху добавлен на Слое 2.1: фильтровать по id надёжнее и
    # дешевле, чем по строковому project_code.)
    project_id: Mapped[int] = mapped_column()

    # Шифр проекта — для отображения/логов. Тип varchar(100) наследуется
    # от боевой projects.code (после перевода джойна вьюхи на projects).
    project_code: Mapped[str] = mapped_column()

    # Отдел строки состава: id + код + имя. id — часть бизнес-ключа и
    # опора для сверки при синхронизации; код/имя — для превью.
    department_id: Mapped[int] = mapped_column()
    department_code: Mapped[str] = mapped_column()
    department_name: Mapped[str] = mapped_column()

    # Имя вышестоящего отдела. Единственное NULLABLE-поле вьюхи: приходит
    # из LEFT JOIN на departments d_parent, у корневого отдела родителя
    # нет → NULL. Optional обязателен, иначе mypy/ORM ждут NOT NULL.
    parent_department_name: Mapped[Optional[str]] = mapped_column()

    # Марка строки: id + код + имя. marka_type_id — ядро бизнес-ключа
    # (React add/remove оперируют им), код/имя — для превью.
    marka_type_id: Mapped[int] = mapped_column()
    marka_code: Mapped[str] = mapped_column()
    marka_name: Mapped[str] = mapped_column()

    # Стадия строки: id + имя. stage_id — часть бизнес-ключа
    # (unique_composition = project+department+marka+stage).
    stage_id: Mapped[int] = mapped_column()
    stage_name: Mapped[str] = mapped_column()

    # Число физических документов (project_mark_sets) под этой строкой.
    # count() в PostgreSQL возвращает bigint, но реальные значения малы —
    # int достаточно. На чтении из него выводится has_mark_sets (>0):
    # флаг «есть комплекты» → крестик удаления в корзине неактивен
    # (Защита A: удаление строки с комплектами каскадом снесло бы работу
    # бюро, поэтому запрещено).
    documents_count: Mapped[int] = mapped_column()

    def __repr__(self) -> str:
        return (
            f"VProjectComposition(composition_id={self.composition_id!r}, "
            f"project_id={self.project_id!r}, "
            f"marka_code={self.marka_code!r})"
        )