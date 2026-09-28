# app/api/v1/composition/service.py
"""Сервисный слой раздела «Состав проекта».

Слой 2.1 (read-only):
    get_composition_refs      — справочник марок для формы редактора
    get_project_composition   — текущий состав проекта для превью

Слой 2.2 (синхронизация, Решение X) — раздел 2.2.2 (чтение + разница):
    _resolve_marka_keys       — id марок → бизнес-ключи (отдел+марка+стадия)
    _read_current_composition — текущее состояние как ключ → строка вьюхи
    _compute_diff             — разница желаемого и текущего (чистая функция)
    _ensure_project_exists    — проверка существования проекта (→ 404)

Функции чтения не знают о pydantic-схемах API: разворачивание в схемы
делает слой routes (разделение слоёв, вариант «а»). Запись/Защита A/
транзакция — раздел 2.2.3 (отдельно).
"""
import logging
from typing import Any, NamedTuple

from sqlalchemy import delete as sa_delete, exists as sa_exists, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.v1.composition.schemas import CompositionItemIn, CompositionRowOut
from app.models.composition import MarkaType, ProjectComposition
from app.models.project import Project
from app.models.views import VProjectComposition

# Логгер модуля: точные причины нерезолвируемых марок пишутся сюда,
# наружу (в HTTP detail) уходит только обобщённый список id (правило 18).
logger = logging.getLogger(__name__)


async def get_composition_refs(db: AsyncSession) -> list[dict[str, Any]]:
    """Справочник марок для формы редактора: марка × отдел + её стадия.

    Возвращает плоский список словарей под схему MarkaRefOut. Форма элемента:

        {
            "marka_type_id": int,
            "code": str,
            "name": str,
            "department": <ORM Department>,   # ОДИН из отделов марки
            "stage": <ORM ProjectStage>,      # ровно одна
        }

    Разворот по отделам: марка с несколькими отделами (department_markas —
    M:M) даёт НЕСКОЛЬКО строк справочника — по одной на отдел. ГИП выбирает
    конкретную пару марка×отдел (напр. ТР·СТР и ТР·ГЕНП — два разных пункта).
    Одноотдельная марка даёт одну строку, как раньше.

    Фильтр остался только по стадии: в справочник попадают марки с РОВНО
    одной стадией (marka_to_stage). Марка без стадии — поставить её на стадию
    нельзя, пропускаем. Марка без отделов (0 записей в department_markas) —
    предлагать нечего, тоже пропускаем. Неоднозначность стадии/отсутствие
    отдела — забота администратора справочников, не формы ГИПа.
    """
    # Грузим марки с ОБЕИМИ связями через selectinload. В async-контексте
    # это обязательно: relationship'ы MarkaType.departments/allowed_stages
    # ленивые (lazy="select"), а ленивая подгрузка внутри await бросает
    # MissingGreenlet. selectinload тянет связи заранее батч-запросами.
    stmt = select(MarkaType).options(
        selectinload(MarkaType.departments),
        selectinload(MarkaType.allowed_stages),
    )
    result = await db.execute(stmt)
    markas = result.scalars().all()

    refs: list[dict[str, Any]] = []
    for marka in markas:
        # Стадия по-прежнему должна быть определена однозначно (одна).
        # ≠1 (нет стадии или, теоретически, 2+) → марку не предлагаем.
        if len(marka.allowed_stages) != 1:
            continue

        # Марка без отделов — ни один отдел не вправе её выпускать, в форме
        # выбрать нечего. Пропускаем (в отличие от многоотдельности, это
        # не разворот, а отсутствие вариантов).
        if not marka.departments:
            continue

        # Стадия одна — безопасно берём по индексу [0].
        stage = marka.allowed_stages[0]

        # Разворот: одна строка справочника на КАЖДЫЙ отдел марки.
        for department in marka.departments:
            refs.append(
                {
                    "marka_type_id": marka.id,
                    "code": marka.code,
                    "name": marka.name,
                    "department": department,
                    "stage": stage,
                }
            )

    return refs


async def get_project_composition(
    db: AsyncSession,
    project_id: int,
) -> list[VProjectComposition]:
    """Текущий состав проекта из представления v_project_composition.

    Читает строки состава для одного проекта из read-only проекции
    VProjectComposition. Возвращает ORM-объекты как есть — схема
    CompositionRowOut в роуте выберет нужные поля и выведет has_mark_sets
    из documents_count.

    Пустой список — валидный результат (у проекта ещё нет состава).
    Решение «показывать/скрывать пустой состав» принимает роут, а не
    сервис: сервис лишь достаёт данные.
    """
    # Простой SELECT по project_id (колонка добавлена во вьюху на Слое
    # 2.1). Сортировка — для стабильного порядка в превью: по коду отдела,
    # затем по коду марки, затем по стадии. Иначе порядок строк из VIEW
    # недетерминирован (GROUP BY не гарантирует упорядочивание вывода).
    stmt = (
        select(VProjectComposition)
        .where(VProjectComposition.project_id == project_id)
        .order_by(
            VProjectComposition.department_code,
            VProjectComposition.marka_code,
            VProjectComposition.stage_name,
        )
    )
    result = await db.execute(stmt)
    # scalars().all() → список объектов VProjectComposition (не кортежей).
    return list(result.scalars().all())


# ===========================================================================
# Слой 2.2 — синхронизация состава (Решение X).
#
# Раздел 2.2.2: резолв payload в бизнес-ключи, чтение текущего состояния,
# вычисление разницы. ТОЛЬКО чтение и чистая логика — без записи, без
# Защиты A, без транзакции (это раздел 2.2.3).
# ===========================================================================


class CompositionKey(NamedTuple):
    """Бизнес-ключ строки состава ВНУТРИ одного проекта.

    Тройка (department_id, marka_type_id, stage_id) — та же, что в
    unique_composition, но БЕЗ project_id: project_id общий для всей
    операции синхронизации (приходит из URL), внутри одного проекта он
    не различает строки, поэтому в ключ сравнения не входит. Хешируемость
    NamedTuple позволяет складывать ключи в set для вычисления разницы.
    """

    department_id: int
    marka_type_id: int
    stage_id: int


class SyncDiff(NamedTuple):
    """Результат сравнения желаемого состава с текущим (Решение X).

    to_insert — ключи, которых нет в БД (добавить);
    to_delete — строки VProjectComposition, которых нет в payload (удалить);
                это ORM-объекты вьюхи (несут composition_id и documents_count
                для Защиты A и физического удаления в 2.2.3);
    to_keep   — ключи, совпавшие с БД (не трогаем; для сводки/лога).
    """

    to_insert: list[CompositionKey]
    to_delete: list[VProjectComposition]
    to_keep: list[CompositionKey]


class MarkaResolveError(Exception):
    """Не удалось однозначно резолвить одну или несколько пар payload.

    Несёт список «плохих» пар (marka_type_id, department_id): марки нет в
    marka_types, ЛИБО отдел не вправе выпускать марку (нет в department_markas),
    ЛИБО стадия марки неоднозначна (≠1). Роут превращает в HTTP 422 с обобщённым
    detail; точные причины по каждой паре пишутся в лог, наружу не утекают
    (правило 18). Собираем ВСЕ плохие пары разом (решение Q5-б), чтобы ГИП
    увидел все проблемы за одну попытку, а не по одной.
    """

    def __init__(self, bad_items: list[tuple[int, int]]) -> None:
        self.bad_items = bad_items
        super().__init__(f"Недопустимые пары (марка, отдел): {bad_items}")


async def _resolve_marka_keys(
    db: AsyncSession,
    items: list[CompositionItemIn],
) -> dict[tuple[int, int], CompositionKey]:
    """Проверяет пары (марка, отдел) и достраивает бизнес-ключи.

    Для каждой пары payload:
      * марка существует в marka_types;
      * стадия марки однозначна (ровно одна в marka_to_stage) — ВЫВОДИТСЯ;
      * отдел из payload ВПРАВЕ выпускать марку (есть в marka.departments,
        т.е. в department_markas) — ПРОВЕРЯЕТСЯ, а не выводится.

    В этом суть перехода на пары: отдел приходит от клиента (ГИП его выбрал
    в форме из развёрнутого /refs), сервер лишь валидирует компетенцию.
    Стадия остаётся выводимой — марка задаёт её однозначно.

    Ключ результата — пара (marka_type_id, department_id); значение —
    CompositionKey(department_id, marka_type_id, stage_id). project_id в ключ
    не кладём (он общий, добавится на вставке из URL).

    Собирает ВСЕ нерезолвируемые пары и, если такие есть, бросает
    MarkaResolveError со списком (Q5-б). Причину по каждой пишет в лог,
    наружу отдаёт только пары (правило 18). Пустой список → пустой результат
    (валидно: очистка состава).
    """
    # Пустой payload — резолвить нечего (синхронизация к пустому составу).
    if not items:
        return {}

    # Уникальные id марок из пар — для ОДНОГО IN-запроса (одна марка может
    # встречаться в нескольких парах с разными отделами).
    marka_ids = {item.marka_type_id for item in items}

    # selectinload обязателен в async (как в /refs). IN-фильтр — один запрос
    # на все марки payload.
    stmt = (
        select(MarkaType)
        .where(MarkaType.id.in_(marka_ids))
        .options(
            selectinload(MarkaType.departments),
            selectinload(MarkaType.allowed_stages),
        )
    )
    result = await db.execute(stmt)
    # Индексируем найденные марки по id — и для доступа, и чтобы определить,
    # каких марок из payload вообще нет в справочнике.
    found: dict[int, MarkaType] = {m.id: m for m in result.scalars().all()}

    keys: dict[tuple[int, int], CompositionKey] = {}
    bad_items: list[tuple[int, int]] = []

    # Идём по ИСХОДНЫМ парам (дубли уже сняты схемой). Каждую валидируем
    # независимо, плохие копим — атомарность обеспечит оркестратор (частично
    # не синхронизируем).
    for item in items:
        pair = (item.marka_type_id, item.department_id)
        marka = found.get(item.marka_type_id)

        # Марки нет в справочнике.
        if marka is None:
            bad_items.append(pair)
            logger.warning(
                "Синхронизация состава: марка id=%s не найдена в marka_types",
                item.marka_type_id,
            )
            continue

        # Стадия должна быть однозначна. Схемой НЕ гарантируется (PK
        # marka_to_stage допускает 2+ стадии на марку) — держится только
        # данными, поэтому проверяем в коде, а не полагаемся на инвариант.
        if len(marka.allowed_stages) != 1:
            bad_items.append(pair)
            logger.warning(
                "Синхронизация состава: марка id=%s нерезолвируема по стадии "
                "(стадий=%s)",
                item.marka_type_id,
                len(marka.allowed_stages),
            )
            continue

        # ПРОВЕРКА отдела: department_id из payload обязан быть среди отделов,
        # компетентных для марки (department_markas). Это второй рубеж перед
        # триггером trg_composition_validity — ловим до записи, понятным 422.
        allowed_dept_ids = {d.id for d in marka.departments}
        if item.department_id not in allowed_dept_ids:
            bad_items.append(pair)
            logger.warning(
                "Синхронизация состава: отдел id=%s не вправе выпускать "
                "марку id=%s (нет в department_markas)",
                item.department_id,
                item.marka_type_id,
            )
            continue

        # Пара валидна: отдел — из payload (проверен), стадия — выведена.
        keys[pair] = CompositionKey(
            department_id=item.department_id,
            marka_type_id=item.marka_type_id,
            stage_id=marka.allowed_stages[0].id,
        )

    # Хоть одна плохая пара → отказ всей операции (Q5-б: все разом).
    if bad_items:
        raise MarkaResolveError(bad_items)

    return keys

async def _read_current_composition(
    db: AsyncSession,
    project_id: int,
) -> dict[CompositionKey, VProjectComposition]:
    """Читает текущий состав проекта как отображение ключ → строка вьюхи.

    Источник — VProjectComposition (Q4): в ней уже есть composition_id
    (для физического удаления в 2.2.3), documents_count (для Защиты A) и
    все три поля бизнес-ключа. Отдельный запрос к project_composition и
    ручной подсчёт комплектов не нужны — вьюха это агрегирует.
    """
    stmt = select(VProjectComposition).where(
        VProjectComposition.project_id == project_id
    )
    result = await db.execute(stmt)
    rows = result.scalars().all()

    # Индексируем по бизнес-ключу: так to_delete/to_keep вычисляются
    # пересечением множеств ключей, а по ключу достаём строку (с id).
    return {
        CompositionKey(
            department_id=row.department_id,
            marka_type_id=row.marka_type_id,
            stage_id=row.stage_id,
        ): row
        for row in rows
    }


def _compute_diff(
    m_new: dict[tuple[int, int], CompositionKey],
    m_old: dict[CompositionKey, VProjectComposition],
) -> SyncDiff:
    """Вычисляет разницу между желаемым и текущим составом (чистая функция).

    Без БД и await — тестируется изолированно. Сравнение по бизнес-ключу
    (не по composition_id): у новых марок composition_id ещё нет, ключ —
    единственный способ сопоставить желаемое с существующим.

    to_insert = ключи payload, которых нет в БД;
    to_delete = строки БД, ключей которых нет в payload;
    to_keep   = совпавшие (не трогаем).

    Защита A здесь НЕ проверяется — это раздел 2.2.3 (применение). Тут
    только «что изменилось», а не «можно ли применить».
    """
    new_keys: set[CompositionKey] = set(m_new.values())
    old_keys: set[CompositionKey] = set(m_old.keys())

    # Разница на множествах ключей.
    insert_keys = new_keys - old_keys      # добавить: есть в payload, нет в БД
    delete_keys = old_keys - new_keys      # удалить: есть в БД, нет в payload
    keep_keys = new_keys & old_keys        # оставить: пересечение

    return SyncDiff(
        to_insert=list(insert_keys),
        # По ключу достаём ORM-строку вьюхи из m_old — она несёт
        # composition_id и documents_count, нужные для 2.2.3.
        to_delete=[m_old[key] for key in delete_keys],
        to_keep=list(keep_keys),
    )


async def _ensure_project_exists(
    db: AsyncSession,
    project_id: int,
) -> None:
    """Проверяет существование проекта; нет → сигнал для HTTP 404.

    Q6: проверяется ТОЛЬКО факт существования (без status_id). В отличие
    от GET-превью (Слой 2.1, мягко отдаёт []), запись требует валидного
    проекта: нельзя синхронизировать состав несуществующего проекта (FK
    project_composition.project_id → projects всё равно отверг бы вставку,
    но явная проверка даёт понятный 404 вместо ошибки БД).

    Бросает ProjectNotFoundError — роут превратит в 404. Существование
    проверяем через EXISTS: не тянем строку целиком, только факт наличия.
    """
    stmt = select(sa_exists().where(Project.id == project_id))
    project_found = await db.scalar(stmt)

    if not project_found:
        raise ProjectNotFoundError(project_id)


class ProjectNotFoundError(Exception):
    """Проект с указанным id не существует (сигнал для HTTP 404).

    Роут ловит и превращает в 404 с нейтральным detail. Отдельный тип
    (не общий Exception), чтобы роут отличал «нет проекта» (404) от
    «плохие марки» (MarkaResolveError → 422) и прочих ошибок (500).
    """

    def __init__(self, project_id: int) -> None:
        self.project_id = project_id
        super().__init__(f"Проект id={project_id} не найден")


# ===========================================================================
# Раздел 2.2.3 — Защита A + применение синхронизации в транзакции.
#
# Здесь появляется ЗАПИСЬ. Оркестратор sync_project_composition владеет
# границей транзакции: вся операция (чтение + DELETE + INSERT) атомарна —
# один commit в конце, rollback при любой ошибке (паттерн задаётся впервые,
# станет образцом для будущих write-эндпоинтов).
# ===========================================================================


class CompositionProtectedError(Exception):
    """Защита A: попытка удалить строку состава, под которой есть комплекты.

    Удаление такой строки каскадом (project_mark_sets.composition_id →
    project_composition.id, ON DELETE CASCADE) снесло бы физические
    документы и всю работу бюро. Поэтому операция отвергается целиком.
    Роут превращает в HTTP 409 (конфликт состояния: запрос валиден, но
    противоречит текущему состоянию БД). Список конкретных марок наружу
    не отдаём — сообщения «нельзя удалить марки с комплектами» достаточно
    (решение A1), детали — в лог.
    """


class CompositionSaveError(Exception):
    """Ошибка БД при применении синхронизации (триггер/unique/прочее).

    Широкая обёртка над IntegrityError/DBAPIError (решение A5): триггер
    trg_composition_validity (недопустимая пара марка↔стадия или отдел↔
    марка) и unique_composition (дубль) прилетают сюда. Наружу — единый
    нейтральный detail без SQL/текста триггера (правило 18), полный
    трейсбек — в лог. Роут превращает в HTTP 422.
    """


async def sync_project_composition(
    db: AsyncSession,
    project_id: int,
    items: list[CompositionItemIn],
) -> list[CompositionRowOut]:
    """Синхронизирует состав проекта с присланным списком марок (Решение X).

    Оркестратор всего Слоя 2.2: приводит строки project_composition данного
    проекта к желаемому состоянию (payload). Владеет транзакцией — вся
    работа атомарна (всё или ничего).

    Порядок (любой шаг может прервать операцию соответствующим исключением):
      1. проект существует?           → нет: ProjectNotFoundError (404)
      2. резолв марок в ключи         → плохие: MarkaResolveError (422)
      3. чтение текущего состояния M_old
      4. вычисление разницы SyncDiff
      5. Защита A на to_delete        → комплекты: CompositionProtectedError (409)
      6. применение: DELETE + INSERT  → ошибка БД: CompositionSaveError (422)
      7. commit, возврат обновлённого состава

    Возвращает актуальный состав после синхронизации (решение A6): фронт
    сразу перерисует превью без второго GET.
    """
    try:
        # --- Шаги 1-4: чтение и подготовка (раздел 2.2.2) ---
        # 1. Проект должен существовать (иначе FK всё равно отвергнет, но
        #    явная проверка даёт понятный 404 вместо ошибки БД).
        await _ensure_project_exists(db, project_id)

        # 2. Резолв каждой марки payload в бизнес-ключ (отдел+марка+стадия).
        #    Плохие марки → MarkaResolveError (все разом, решение Q5-б).
        m_new = await _resolve_marka_keys(db, items)

        # 3. Текущее состояние состава как ключ → строка вьюхи (с
        #    composition_id и documents_count).
        m_old = await _read_current_composition(db, project_id)

        # 4. Разница по бизнес-ключу (чистая функция).
        diff = _compute_diff(m_new, m_old)

        # --- Шаг 5: Защита A ---
        # Среди строк на удаление ищем те, под которыми есть комплекты
        # (documents_count > 0). Хоть одна такая → отказ всей операции:
        # каскад снёс бы работу бюро. Проверяем ДО записи, чтобы вообще
        # ничего не трогать при нарушении.
        protected = [
            row for row in diff.to_delete if row.documents_count > 0
        ]
        if protected:
            # В лог — какие именно марки защищены (для диагностики).
            # Наружу detail пойдёт обобщённый (решение A1), список не светим.
            logger.warning(
                "Защита A: отказ удаления строк с комплектами "
                "(project_id=%s, composition_id=%s)",
                project_id,
                [row.composition_id for row in protected],
            )
            raise CompositionProtectedError(
                "Нельзя удалить марки с комплектами"
            )

        # --- Шаг 6: применение (DELETE перед INSERT) ---
        # Порядок важен: удаляем раньше вставки, освобождая бизнес-ключи —
        # инвариант против ложного unique_composition (хотя to_delete и
        # to_insert по построению не пересекаются по ключу).
        if diff.to_delete:
            # Bulk-delete по composition_id (решение A3). Удаляем реальную
            # таблицу project_composition, НЕ вьюху (вьюха read-only).
            delete_ids = [row.composition_id for row in diff.to_delete]
            await db.execute(
                sa_delete(ProjectComposition).where(
                    ProjectComposition.id.in_(delete_ids)
                )
            )

        if diff.to_insert:
            # ORM add_all (решение A4): достраиваем project_id из URL к
            # каждому ключу. При flush/commit сработает триггер
            # trg_composition_validity — его исключение ловим ниже.
            new_rows = [
                ProjectComposition(
                    project_id=project_id,
                    department_id=key.department_id,
                    marka_type_id=key.marka_type_id,
                    stage_id=key.stage_id,
                )
                for key in diff.to_insert
            ]
            db.add_all(new_rows)

        # --- Шаг 7: коммит всей операции (чтение+DELETE+INSERT атомарно) ---
        # Именно здесь БД исполнит триггеры и проверит ограничения. Любая
        # их ошибка прилетит как IntegrityError/DBAPIError — ловим ниже.
        await db.commit()

    except (ProjectNotFoundError, MarkaResolveError, CompositionProtectedError):
        # Доменные исключения (404/422/409) — откатываем и пробрасываем
        # роуту как есть, он выберет HTTP-код. rollback обязателен: часть
        # изменений могла осесть в сессии до ошибки.
        await db.rollback()
        raise

    except (IntegrityError, DBAPIError) as exc:
        # Ошибка БД на commit (триггер валидности / unique / прочее).
        # Откатываем, логируем с трейсбеком, наружу — нейтральный detail
        # (решение A5, правило 18: ни SQL, ни текста триггера клиенту).
        await db.rollback()
        logger.exception(
            "Ошибка БД при синхронизации состава (project_id=%s)", project_id
        )
        raise CompositionSaveError(
            "Ошибка сохранения состава, проверьте корректность марок"
        ) from exc

    # --- Возврат обновлённого состава (решение A6) ---
    # Отдельным запросом после commit читаем актуальное состояние и
    # разворачиваем в схемы. Транзакция уже закрыта — это чистое чтение.
    fresh = await get_project_composition(db, project_id)
    return [CompositionRowOut.model_validate(row) for row in fresh]


# ===========================================================================
# Вход B (под-слой 2.1-B) — автодополнение шифра проекта.
# ===========================================================================

# id статуса «В работе» из справочника project_status. ДУБЛИРУЕТ константу
# в app/web/routers/composition.py (web-контур). ТЕХДОЛГ: вынести в общий
# модуль (app/core/) и импортировать в оба контура — единый источник истины.
STATUS_IN_PROGRESS = 1

# Минимальная длина запроса автодополнения: короче — не ищем (не мусолим
# базу и не показываем dropdown на 1-2 символа). Решение B1-Q1.
MIN_SEARCH_LENGTH = 3

# Максимум результатов автодополнения (решение B1-Q2): dropdown длиннее
# неудобен, ГИП уточнит ввод.
SEARCH_LIMIT = 20


async def search_projects(
    db: AsyncSession,
    query: str,
) -> list[dict[str, Any]]:
    """Автодополнение шифра проекта для страницы создания состава (вход B).

    Возвращает список проектов «в работе», чей шифр начинается на query
    (префиксный ILIKE, регистронезависимо), с флагом has_composition
    (есть ли уже состав). Форма элемента под схему ProjectSearchOut:
        {project_id, code, title, has_composition}

    Короткий запрос (< MIN_SEARCH_LENGTH) → пустой список: не ищем по 1-2
    символам. Пустой результат — валиден (ничего не нашлось).

    Только проекты status_id = «В работе» (B1-Q6): консистентно с
    редактором, который открывается лишь для таких. Так ГИП не выберет
    архивный проект, на котором редактор потом откажет.
    """
    # Короткий запрос — не ищем (B1-Q1). strip: пробелы не считаем за длину.
    normalized = query.strip()
    if len(normalized) < MIN_SEARCH_LENGTH:
        return []

    # Коррелированный EXISTS: есть ли хоть одна строка состава у проекта
    # (B1-Q5). Как отдельное скалярное выражение с меткой has_composition —
    # войдёт колонкой в SELECT. correlate(Project) связывает подзапрос с
    # внешней строкой projects по project_id.
    has_composition_expr = (
        sa_exists()
        .where(ProjectComposition.project_id == Project.id)
        .correlate(Project)
        .label("has_composition")
    )

    # Явный select нужных колонок + флаг (способ «б»): результат — строки
    # с полями id/code/title/has_composition, ровно под схему. ILIKE 'q%'
    # (B1-Q3/Q4): префиксный, регистронезависимый. Фильтр по статусу
    # (B1-Q6). LIMIT (B1-Q2).
    stmt = (
        select(
            Project.id.label("project_id"),
            Project.code,
            Project.title,
            has_composition_expr,
        )
        .where(
            Project.code.ilike(f"{normalized}%"),
            Project.status_id == STATUS_IN_PROGRESS,
        )
        .order_by(Project.code)  # стабильный порядок в dropdown
        .limit(SEARCH_LIMIT)
    )

    result = await db.execute(stmt)
    # .mappings() отдаёт строки как dict-подобные объекты с ключами-метками
    # (project_id, code, title, has_composition) — сразу под ProjectSearchOut.
    return [dict(row) for row in result.mappings().all()]