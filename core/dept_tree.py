# app/core/dept_tree.py
"""Дерево подразделений: структуры узлов и чистые функции обхода.

Модуль выделен из app/web/routers/composition.py, когда у сборки дерева
появился второй потребитель — экран «Работы бюро» в разделе планировщика.
Импорт функции из одного веб-роутера в другой означал бы, что роутеры
знают друг о друге; общее место для этого — core, где уже лежит вся
сквозная инфраструктура (guards, scope, errors).

Всё здесь — чистые функции без AsyncSession и HTTP: данные приносит
вызывающий роут, модуль только преобразует их в структуру для шаблона.
Отсюда же и тестируемость: узлы собираются из обычных объектов, БД не
нужна.

Механизм — сборка в памяти, а НЕ рекурсивный CTE. Справочник отделов
мал (десятки строк), а обход в Python прозрачнее и не требует диалекта
БД. То же решение принято в app/core/scope.py для обхода вниз — двух
разных способов ходить по одному дереву в проекте нет.
"""

from dataclasses import dataclass, field

from app.models.composition import (
    Department,
    MarkaType,
    ProjectComposition,
    ProjectMarkSet,
    ProjectStage,
)

@dataclass
class MarkaNode:
    """Узел марки в дереве: марка + стадия + её комплекты (уровень 2).

    marka_type и stage — из строки project_composition (задание ГИПа).
    sets — комплекты (project_mark_sets), заведённые начальником бюро под
    этой строкой; может быть пустым (шифр ещё не заведён).
    """

    # id строки project_composition. Нужен потребителям, которые
    # обращаются к строке состава: планировщик заводит под ней шифр.
    # Тождество строки, а не служебные данные сборки — поэтому в узле,
    # а не отдельным множеством, как manageable_ids.
    composition_id: int
    marka_type: MarkaType
    stage: ProjectStage
    sets: list[ProjectMarkSet] = field(default_factory=list)


@dataclass
class DeptNode:
    """Узел отдела в дереве состава (уровень 1 и промежуточные родители).

    children — дочерние отделы (следующий уровень иерархии departments).
    markas — марки, назначенные ГИПом непосредственно этому отделу.
    Промежуточный отдел-родитель без собственных марок имеет пустой
    markas, но непустой children (он на пути к корню).
    """

    department: Department
    children: list["DeptNode"] = field(default_factory=list)
    markas: list[MarkaNode] = field(default_factory=list)


def build_composition_tree(
    compositions: list[ProjectComposition],
    departments: list[Department],
) -> list[DeptNode]:
    """Собирает дерево состава проекта из плоских строк (Python-сборка).

    На входе:
      compositions — строки состава проекта (с подгруженными department,
                     marka_type, stage, mark_sets);
      departments  — ВСЕ отделы справочника (нужны, чтобы восстановить
                     промежуточных родителей на пути к корню).

    На выходе: список корневых DeptNode. В дерево попадают только отделы,
    участвующие в проекте (несущие марки), и их предки — кроме корня
    организации (узла с parent_id IS NULL), который уровнем отображения
    не является. Прочие отделы справочника отбрасываются.

    Механизм — сборка в памяти (не рекурсивный CTE): справочник отделов
    мал, а логика прозрачнее и тестируется как чистая функция.
    """
    # Индекс отделов по id — для подъёма по parent_id к предкам.
    dept_by_id: dict[int, Department] = {d.id: d for d in departments}

    # Узел на каждый отдел справочника. Пока «пустые» — марки и связи
    # родитель-ребёнок проставим ниже. Держим все, отфильтруем на выходе.
    node_by_id: dict[int, DeptNode] = {
        d.id: DeptNode(department=d) for d in departments
    }

    # Множество id отделов, реально участвующих в дереве: сами носители
    # марок + все их предки. Служебные данные алгоритма — в DeptNode не
    # кладём (решение: узел чистый, релевантность отдельно).
    relevant: set[int] = set()

    # 1. Раскладываем строки состава по узлам-отделам.
    #    Каждая строка состава = одна марка на стадии у своего отдела.
    for comp in compositions:
        node = node_by_id[comp.department_id]
        node.markas.append(
            MarkaNode(
                composition_id=comp.id, 
                marka_type=comp.marka_type,
                stage=comp.stage,
                # list() — материализуем коллекцию mark_sets (она уже
                # подгружена selectinload, запросов в БД не будет).
                sets=list(comp.mark_sets),
            )
        )
        relevant.add(comp.department_id)

    # 2. Поднимаем релевантность к предкам: каждый отдел-носитель марок
    #    тянет за собой цепочку родителей до корня (чтобы показать
    #    промежуточные узлы «Технологический отдел → ТЕХН»).
    #    seen — защита от цикла в parent_id (A→B→A зациклил бы while).
    for dept_id in list(relevant):
        current: Department | None = dept_by_id.get(dept_id)
        seen: set[int] = set()
        while (
            current is not None
            and current.parent_id is not None
            and current.id not in seen
        ):
            seen.add(current.id)
            # Родителя достаём объектом, а не берём голый id: нужен его
            # собственный parent_id — по нему отличаем корень организации
            # от обычного вышестоящего отдела.
            parent = dept_by_id.get(current.parent_id)

            # Висячая ссылка: parent_id указывает на отсутствующий отдел.
            # Подъём прекращаем — иначе несуществующий id попал бы в
            # relevant и уронил шаг 3 обращением к node_by_id.
            if parent is None:
                break

            # Корень организации (parent_id IS NULL) — не уровень
            # отображения: принадлежность проекта ПЦ подразумевается, а
            # узел добавил бы лишний уровень над всеми отделами.
            if parent.parent_id is None:
                break

            relevant.add(parent.id)
            current = parent

    # 3. Связываем родитель-дети, но ТОЛЬКО среди релевантных узлов.
    #    Нерелевантные отделы (не участвуют в проекте) в дерево не идут.
    roots: list[DeptNode] = []
    for dept_id in relevant:
        node = node_by_id[dept_id]
        parent_id = node.department.parent_id
        # Узел — корень, если у него нет родителя ИЛИ родитель сам
        # нерелевантен (например, отдел назначен напрямую, минуя иерархию).
        if parent_id is not None and parent_id in relevant:
            node_by_id[parent_id].children.append(node)
        else:
            roots.append(node)

    # 4. Сортировка для стабильного отображения (иначе порядок из БД
    #    недетерминирован). Отделы — по имени (как в ТЗ), марки — по коду
    #    и стадии, комплекты — по шифру.
    def sort_node(node: DeptNode) -> None:
        node.children.sort(key=lambda n: n.department.name)
        node.markas.sort(
            key=lambda m: (m.marka_type.code, m.stage.stage_name)
        )
        for marka in node.markas:
            marka.sets.sort(key=lambda s: s.marka_full)
        # Рекурсивно упорядочиваем поддеревья.
        for child in node.children:
            sort_node(child)

    roots.sort(key=lambda n: n.department.name)
    for root in roots:
        sort_node(root)

    return roots


def collect_expanded_ids(
    nodes: list[DeptNode],
    manageable_ids: set[int],
) -> set[int]:
    """Отделы, чьи ветки должны быть раскрыты при загрузке страницы.

    Раскрыт узел, у которого САМ отдел или любой из потомков входит в
    область управления. Иначе начальник бюро увидел бы свёрнутый экран:
    его бюро лежит внутри чужого отдела, и сворачивание родителя спрятало
    бы собственную ветку.

    Возвращает id узлов на путях к управляемым отделам, включая сами
    управляемые. Всё остальное остаётся свёрнутым — чужие ветки видны
    заголовком, но не разворачиваются, пока их не откроют вручную.
    """
    expanded: set[int] = set()

    def visit(node: DeptNode) -> bool:
        """Обходит поддерево, возвращает «есть ли здесь что-то своё»."""
        found = node.department.id in manageable_ids

        # Обходим ВСЕХ детей без короткого замыкания: нужно не только
        # узнать факт, но и пометить каждый узел на пути вниз.
        for child in node.children:
            if visit(child):
                found = True

        if found:
            expanded.add(node.department.id)
        return found

    for root in nodes:
        visit(root)

    return expanded