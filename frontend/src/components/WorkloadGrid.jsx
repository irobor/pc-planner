// app/frontend/src/components/WorkloadGrid.jsx
// Сетка загрузки бюро: инженеры, их шифры и календарные дни.
//
// Данные приходят плоскими слоями (employees / rows / days / cells) и
// собираются здесь в индексы: во вложенном дереве дни инженера пришлось
// бы дублировать в каждой строке шифра.
//
// Два правила редактирования, и они несимметричны:
//   - ПОСТАВИТЬ день может только текущий исполнитель шифра, и только
//     если эта дата у шифра ещё никем не занята;
//   - СНЯТЬ день может кто угодно, в том числе прежний исполнитель.
// Асимметрия не произвольная: добавление занимает ресурс, удаление
// освобождает. Без второго правила план, оставшийся за прежним
// исполнителем, стало бы нечем убрать — его строка не кликабельна.
//
// canEdit отражает право plan:manage. Страницу открывает plan:view,
// поэтому инженер сетку видит, но не правит. Настоящую защиту держит
// API — здесь только интерфейс.
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { applyCells, fetchWorkload } from '../api/planning';

// Ширина окна в днях. Отчётные периоды в организации идут с 25-го по
// 25-е, и в любой день начальнику нужны два соседних периода целиком:
// сданный план вперёд и закрываемый факт назад. 90 дней покрывают оба
// с запасом на обоих концах. Серверный предел — 120 (MAX_INTERVAL_DAYS).
const WINDOW_DAYS = 90;

// Насколько окно отступает назад от текущей даты.
const WINDOW_BACK_DAYS = 45;

// Шаг стрелок. Месяц, а не неделя: внутри окна перемещаются прокруткой,
// стрелки нужны только для дальних прыжков за его пределы.
const STEP_DAYS = 30;
// Сколько дней прошлого остаётся видно слева от текущей даты при
// открытии. Ноль прижал бы сегодня вплотную к липким колонкам, и
// вчерашний день пришлось бы искать прокруткой.
const ANCHOR_DAYS = 4;

const WEEKDAYS = ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб'];

const MONTHS = [
  'янв', 'фев', 'мар', 'апр', 'май', 'июн',
  'июл', 'авг', 'сен', 'окт', 'ноя', 'дек',
];

// Состояния дня, при которых план не ставится. Сервер проверит их сам,
// но ждать отказа ради того, что уже известно из ответа, незачем.
const BLOCKED_STATES = new Set([
  'out_of_horizon',
  'not_assigned',
  'nonwork',
  'absence',
  'has_fact',
]);

// --- работа с датами -------------------------------------------------
// Везде ISO-строки (ГГГГ-ММ-ДД), а не Date: сервер принимает и отдаёт
// именно их, а пересчёт в объект и обратно на каждой ячейке добавил бы
// риск уехать на день из-за часового пояса.

/**
 * Собирает ISO-дату из МЕСТНЫХ компонентов.
 *
 * Через toISOString() делать нельзя: строка "2026-09-14T00:00:00"
 * читается как местное время, а toISOString отдаёт UTC — при
 * положительном смещении зоны дата уезжает на сутки назад, и
 * прибавление одного дня ровно компенсирует этот сдвиг.
 */
function toIso(value) {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, '0');
  const day = String(value.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function shiftIso(iso, days) {
  const value = new Date(`${iso}T00:00:00`);
  value.setDate(value.getDate() + days);
  return toIso(value);
}

function listDates(fromIso, untilIso) {
  const result = [];
  let current = fromIso;
  // Ограничитель на случай, если сдвиг даты перестанет двигать дату:
  // зависшая вкладка отлаживается куда хуже, чем короткий список.
  while (current <= untilIso && result.length < 400) {
    result.push(current);
    current = shiftIso(current, 1);
  }
  return result;
}

function formatDay(iso) {
  const value = new Date(`${iso}T00:00:00`);
  return {
    weekday: WEEKDAYS[value.getDay()],
    day: String(value.getDate()).padStart(2, '0'),
  };
}

function formatRange(fromIso, untilIso) {
  const label = (iso) =>
    `${iso.slice(8, 10)}.${iso.slice(5, 7)}.${iso.slice(2, 4)}`;
  return `${label(fromIso)} – ${label(untilIso)}`;
}

/** Первое число месяца — по нему ставится разделитель в сетке. */
const isMonthStart = (iso) => iso.slice(8, 10) === '01';

/** Короткое имя месяца: «сен», «окт». */
const monthLabel = (iso) => MONTHS[Number(iso.slice(5, 7)) - 1];

// --- ключи -----------------------------------------------------------
// Собираются строками через «|»: составной ключ из массива требовал бы
// одинаковой ссылки, а строка сравнивается по значению.

// День инженера — для вердикта и сводной строки.
const dayKey = (employeeId, iso) => `${employeeId}|${iso}`;

// Дата ШИФРА — опора правила «дата шифра планируется один раз».
// Инженера здесь нет намеренно: занятость принадлежит шифру.
const planKey = (markSetId, iso) => `${markSetId}|${iso}`;

// Несохранённое изменение. Инженер В КЛЮЧЕ ОБЯЗАТЕЛЕН: снять день у
// одного и отдать его другому — две разные операции на одну пару
// «шифр + дата». Без инженера они схлопывались бы в одну, и второй
// клик отменял бы первый вместо того, чтобы добавиться к нему.
const cellId = (markSetId, employeeId, iso) =>
  `${markSetId}|${employeeId}|${iso}`;

// --- индексация ------------------------------------------------------

function buildIndex(grid) {
  const dayByKey = new Map();
  for (const day of grid.days) {
    dayByKey.set(dayKey(day.employee_id, day.work_date), day);
  }

  // Кто занимает дату ШИФРА: ключ «шифр|дата» → id инженера. Владелец
  // может быть любым, включая прежнего исполнителя.
  const ownerBySetDay = new Map();
  const factByKey = new Map();
  for (const cell of grid.cells) {
    if (cell.has_plan) {
      ownerBySetDay.set(
        planKey(cell.mark_set_id, cell.work_date),
        cell.employee_id
      );
    }
    if (cell.has_fact) {
      factByKey.set(
        `${cell.employee_id}|${cell.mark_set_id}|${cell.work_date}`,
        true
      );
    }
  }

  const rowsByEmployee = new Map();
  for (const row of grid.rows) {
    const list = rowsByEmployee.get(row.employee_id) || [];
    list.push(row);
    rowsByEmployee.set(row.employee_id, list);
  }

  return { dayByKey, ownerBySetDay, factByKey, rowsByEmployee };
}

/**
 * Накладывает несохранённые изменения на занятость дат шифров.
 *
 * Нужна, потому что доступность меняется по ходу правки: снял день у
 * Иванова — он в тот же миг доступен Комарову, ещё до сохранения.
 *
 * Удаления обрабатываются ПЕРВЫМ проходом, добавления вторым — так же,
 * как на сервере. Иначе порядок кликов влиял бы на результат: перенос
 * дня, начатый с добавления, стёр бы собственную вставку.
 */
function applyPending(ownerBySetDay, pending) {
  const result = new Map(ownerBySetDay);

  for (const [key, operation] of pending) {
    if (operation !== 'remove') {
      continue;
    }
    const [markSetId, , iso] = key.split('|');
    result.delete(planKey(Number(markSetId), iso));
  }

  for (const [key, operation] of pending) {
    if (operation !== 'add') {
      continue;
    }
    const [markSetId, employeeId, iso] = key.split('|');
    result.set(planKey(Number(markSetId), iso), Number(employeeId));
  }

  return result;
}

/** Классы колонки дня: разделитель месяца и отметка сегодня. */
function dayColumnClasses(base, iso, todayIso) {
  const classes = [base];
  if (isMonthStart(iso)) {
    classes.push(`${base}--month-start`);
  }
  if (iso === todayIso) {
    classes.push(`${base}--today`);
  }
  return classes;
}

// --- ячейки ----------------------------------------------------------

/**
 * Ячейка на пересечении строки шифра и дня.
 *
 * В ячейке ВСЕГДА две строки: верхняя под план, нижняя под факт. Пустая
 * строка остаётся прозрачной, но место занимает — иначе одиночная
 * полоса вставала бы по центру, и «запланировано, но не отработано»
 * выглядело бы неотличимо от «отработано без плана».
 */
function MarkSetCell({
  hasPlan,
  hasFact,
  pending,
  dayState,
  rejected,
  clickable,
  title,
  columnClasses,
  onClick,
}) {
  const classes = [...columnClasses];
  if (dayState === 'nonwork') {
    classes.push('workload-grid__cell--nonwork');
  }
  if (clickable) {
    classes.push('workload-grid__cell--clickable');
  }
  if (rejected) {
    classes.push('workload-grid__cell--rejected');
  }

  // Несохранённое показываем пунктиром: пока пачка не ушла на сервер,
  // это намерение, а не факт. Добавление — пунктирная рамка без заливки,
  // удаление — гаснущая полоса на прежнем месте.
  let planClass = '';
  if (pending === 'add') {
    planClass = ' workload-bar--pending';
  } else if (pending === 'remove') {
    planClass = ' workload-bar--removing';
  } else if (hasPlan) {
    planClass = ' workload-bar--plan';
  }

  let factClass = '';
  if (hasFact) {
    factClass = hasPlan ? ' workload-bar--fact' : ' workload-bar--offplan';
  }

  return (
    <td
      className={classes.join(' ')}
      title={title}
      onClick={clickable ? onClick : undefined}
    >
      <span className={`workload-bar${planClass}`} />
      <span className={`workload-bar${factClass}`} />
    </td>
  );
}

/**
 * Ячейка сводной строки инженера: занят день чем-нибудь или нет.
 *
 * Буква табеля приходит даже когда отсутствие не блокирует
 * планирование (командировка): день планируемый, но отличать его от
 * обычного всё равно нужно.
 */
function EmployeeCell({ day, busy, columnClasses }) {
  const classes = [...columnClasses];
  if (day?.state === 'nonwork') {
    classes.push('workload-grid__cell--nonwork');
  }

  if (day?.tab_letter) {
    return (
      <td className={classes.join(' ')}>
        <span className="workload-absence">{day.tab_letter}</span>
      </td>
    );
  }

  return (
    <td className={classes.join(' ')}>
      {busy && <span className="workload-bar workload-bar--busy" />}
    </td>
  );
}

/**
 * Статусы, встречающиеся в строках окна, — для легенды.
 *
 * Берём из ответа сервера, а не из списка в коде: названия живут в
 * справочнике set_statuses, и дублировать их здесь незачем.
 * Возвращает пары [id, название] по возрастанию id.
 */
function statusLegendOf(rows) {
  const byId = new Map();
  for (const row of rows) {
    if (row.status_id != null && !byId.has(row.status_id)) {
      byId.set(row.status_id, row.status_name);
    }
  }
  return [...byId.entries()].sort((a, b) => a[0] - b[0]);
}

// --- компонент -------------------------------------------------------

export default function WorkloadGrid({ departmentId, today, canEdit }) {
  // Левый край окна. Отступаем назад от сегодня, чтобы закрываемый
  // период факта был доступен без листания стрелками.
  const [windowStart, setWindowStart] = useState(() =>
    shiftIso(today, -WINDOW_BACK_DAYS)
  );
  const [grid, setGrid] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  // Свёрнутые инженеры: новый человек в ответе появится раскрытым.
  const [collapsed, setCollapsed] = useState(() => new Set());
  // Несохранённые изменения: ключ «шифр|инженер|дата» → 'add'|'remove'.
  const [pending, setPending] = useState(() => new Map());
  // Отказы последнего сохранения: тексты для показа и ключи для
  // подсветки конкретных ячеек.
  const [rejections, setRejections] = useState([]);

  // Обёртка с прокруткой и заголовок сегодняшней колонки — нужны, чтобы
  // после загрузки подвести ленту к текущей дате.
  const scrollRef = useRef(null);
  const todayRef = useRef(null);
  // Окно, к которому лента уже подведена. Перечитывание после
  // сохранения меняет grid, но не окно, — по этой пометке эффект
  // понимает, что двигать ленту не нужно.
  const anchoredWindowRef = useRef(null);

  const windowEnd = useMemo(
    () => shiftIso(windowStart, WINDOW_DAYS - 1),
    [windowStart]
  );

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchWorkload(departmentId, windowStart, windowEnd);
      setGrid(data);
    } catch (err) {
      setError(err.message);
      setGrid(null);
    } finally {
      setLoading(false);
    }
  }, [departmentId, windowStart, windowEnd]);

  useEffect(() => {
    load();
  }, [load]);

  const dates = useMemo(
    () => listDates(windowStart, windowEnd),
    [windowStart, windowEnd]
  );

  const index = useMemo(() => (grid ? buildIndex(grid) : null), [grid]);

  // Подводка к текущей дате — один раз на каждое окно. В зависимостях
  // есть grid (подводить можно только по разложенной таблице), поэтому
  // эффект запускается и после сохранения; пометка anchoredWindowRef
  // не даёт ему в этом случае сдвинуть ленту из-под руки.
  //
  // requestAnimationFrame обязателен: эффект выполняется до того, как
  // браузер разложил таблицу, и offsetLeft у заголовка ещё не имеет
  // осмысленного значения. Кадром позже размеры уже посчитаны.
  useEffect(() => {
    if (!grid || anchoredWindowRef.current === windowStart) {
      return undefined;
    }
    const frame = requestAnimationFrame(() => {
      const container = scrollRef.current;
      const marker = todayRef.current;
      if (!container) {
        return;
      }
      if (!marker) {
        // Сегодня вне окна — уехали стрелками. Показываем начало.
        container.scrollLeft = 0;
      } else {
        // Липкие колонки закрывают левый край ленты, поэтому к отступу
        // добавляется их ширина: иначе текущая дата оказалась бы под
        // колонкой «Шифр».
        const sticky = container.querySelector('.workload-grid__days');
        const stickyRight = sticky
          ? sticky.offsetLeft + sticky.offsetWidth
          : 0;
        // ANCHOR_DAYS дней контекста слева, чтобы недавнее прошлое было
        // видно без прокрутки.
        const dayWidth = marker.offsetWidth || 48;
        container.scrollLeft = Math.max(
          0,
          marker.offsetLeft - stickyRight - ANCHOR_DAYS * dayWidth
        );
      }
      // Пометка ставится только после фактической подводки. Если кадр
      // отменили раньше (быстрая смена окна, StrictMode в разработке),
      // следующий запуск эффекта подведёт ленту заново.
      anchoredWindowRef.current = windowStart;
    });
    return () => cancelAnimationFrame(frame);
  }, [windowStart, grid]);

  const effectiveOwner = useMemo(() => {
    if (!index) {
      return new Map();
    }
    return applyPending(index.ownerBySetDay, pending);
  }, [index, pending]);

  // Занятость дня самого инженера — для сводной строки и для проверки
  // «человек уже занят другим шифром». Пересобирается из занятости дат
  // шифров, поэтому несохранённые изменения учтены здесь тоже.
  const busyDays = useMemo(() => {
    const result = new Map();
    for (const [key, employeeId] of effectiveOwner) {
      const [markSetId, iso] = key.split('|');
      result.set(dayKey(employeeId, iso), Number(markSetId));
    }
    return result;
  }, [effectiveOwner]);

  const employeeById = useMemo(() => {
    const result = new Map();
    for (const employee of grid?.employees || []) {
      result.set(employee.id, employee.full_name);
    }
    return result;
  }, [grid]);

  // Отказ приходит без инженера — только шифр и дата. Подсвечиваем обе
  // строки такого шифра: какая из них виновата, сервер не сообщает, а
  // догадываться на клиенте не за чем.
  const rejectedKeys = useMemo(
    () =>
      new Set(
        rejections.map((item) => planKey(item.mark_set_id, item.work_date))
      ),
    [rejections]
  );

  const shiftWindow = (days) => {
    const next = shiftIso(windowStart, days);
    if (grid && next < grid.horizon_from) {
      setWindowStart(grid.horizon_from);
      return;
    }
    if (grid && shiftIso(next, WINDOW_DAYS - 1) > grid.horizon_to) {
      return;
    }
    setWindowStart(next);
  };

  const toggleEmployee = (employeeId) => {
    setCollapsed((previous) => {
      const next = new Set(previous);
      if (next.has(employeeId)) {
        next.delete(employeeId);
      } else {
        next.add(employeeId);
      }
      return next;
    });
  };

  /**
   * Клик по ячейке шифра: ставит день в план или снимает его.
   *
   * Повторный клик по несохранённому изменению отменяет само изменение,
   * а не добавляет обратное: нажал по ошибке — нажал ещё раз, и в пачке
   * не останется следа. Ключ включает инженера, поэтому снятие у одного
   * и постановка другому на ту же дату друг друга не гасят.
   */
  const toggleCell = (markSetId, employeeId, iso, occupied) => {
    const key = cellId(markSetId, employeeId, iso);
    setPending((previous) => {
      const next = new Map(previous);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.set(key, occupied ? 'remove' : 'add');
      }
      return next;
    });
    // Прежние отказы к новому состоянию не относятся.
    setRejections([]);
  };

  const discard = () => {
    setPending(new Map());
    setRejections([]);
  };

  const save = async () => {
    const operations = [];
    for (const [key, operation] of pending) {
      // Инженер в операцию не уходит: для add сервер берёт его из
      // id_executor шифра, для remove — из владельца ячейки. В ключе
      // он нужен только клиенту, чтобы различать строки.
      const [markSetId, , iso] = key.split('|');
      operations.push({
        op: operation,
        mark_set_id: Number(markSetId),
        work_date: iso,
      });
    }
    if (operations.length === 0) {
      return;
    }

    setSaving(true);
    setError(null);
    setRejections([]);
    try {
      await applyCells(operations);
      setPending(new Map());
      // Перечитываем окно целиком, а не сливаем ответ по ключам.
      // Ответ покрывает только затронутые даты, а сохранение меняет и
      // то, что за их пределами: границы плана, число дней, иногда сам
      // факт существования строки. Один лишний GET дешевле сложного
      // слияния, в котором легко потерять состояние.
      await load();
    } catch (err) {
      if (err.rejected?.length) {
        // Пачка отклонена целиком: изменения оставляем на месте, чтобы
        // было что править, и подсвечиваем непрошедшие ячейки.
        setRejections(err.rejected);
      } else {
        setError(err.message);
      }
    } finally {
      setSaving(false);
    }
  };

  if (error) {
    return (
      <div className="alert alert-danger" role="alert">
        {error}
      </div>
    );
  }

  if (!grid || !index) {
    return <div className="text-muted py-5 text-center">Загрузка сетки…</div>;
  }

  return (
    <div className="workload">
      <div className="d-flex align-items-center justify-content-between mb-2">
        <div className="btn-group btn-group-sm" role="group">
          <button
            type="button"
            className="btn btn-outline-secondary"
            onClick={() => shiftWindow(-STEP_DAYS)}
            title="Назад на месяц"
          >
            ‹
          </button>
          <span className="btn btn-outline-secondary disabled">
            {formatRange(windowStart, windowEnd)}
          </span>
          <button
            type="button"
            className="btn btn-outline-secondary"
            onClick={() => shiftWindow(STEP_DAYS)}
            title="Вперёд на месяц"
          >
            ›
          </button>
        </div>

        <div className="d-flex align-items-center gap-2">
          {/* Панель сохранения только при праве записи. Инженер не
              должен разложить себе неделю и обнаружить, что сохранить
              её нечем. */}
          {canEdit && (
            <>
              {pending.size > 0 && (
                <span className="text-muted small">
                  Изменений: {pending.size}
                </span>
              )}
              <button
                type="button"
                className="btn btn-sm btn-outline-secondary"
                onClick={discard}
                disabled={pending.size === 0 || saving}
              >
                Отменить
              </button>
              <button
                type="button"
                className="btn btn-sm btn-primary"
                onClick={save}
                disabled={pending.size === 0 || saving}
              >
                {saving ? 'Сохранение…' : 'Сохранить'}
              </button>
            </>
          )}
          <button
            type="button"
            className="btn btn-sm btn-outline-secondary"
            onClick={load}
            disabled={loading || saving}
          >
            Обновить
          </button>
        </div>
      </div>

      {rejections.length > 0 && (
        <div className="alert alert-warning py-2" role="alert">
          <div className="fw-semibold">Изменения не применены</div>
          <ul className="mb-0 small">
            {rejections.map((item) => (
              <li key={planKey(item.mark_set_id, item.work_date)}>
                {item.message}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Горизонтальная прокрутка живёт на обёртке, а липкие колонки —
          на самих ячейках: так заголовок и левая часть остаются на
          месте, пока таблица едет под ними. */}
      <div className="workload-scroll" ref={scrollRef}>
        <table className="table table-sm workload-grid mb-0">
          <thead>
            <tr>
              <th className="workload-grid__name">Шифр</th>
              <th className="workload-grid__days">дн.</th>
              {dates.map((iso) => {
                const { weekday, day } = formatDay(iso);
                const monthStart = isMonthStart(iso);
                return (
                  <th
                    key={iso}
                    ref={iso === today ? todayRef : undefined}
                    className={dayColumnClasses(
                      'workload-grid__date',
                      iso,
                      today
                    ).join(' ')}
                  >
                    {/* В первый день месяца вместо дня недели стоит имя
                        месяца: на ленте в три месяца это единственный
                        ориентир, по которому дата находится взглядом. */}
                    <span
                      className={
                        monthStart
                          ? 'workload-grid__month'
                          : 'workload-grid__weekday'
                      }
                    >
                      {monthStart ? monthLabel(iso) : weekday}
                    </span>
                    <span className="workload-grid__daynum">{day}</span>
                  </th>
                );
              })}
            </tr>
          </thead>

          <tbody>
            {grid.employees.map((employee) => {
              const rows = index.rowsByEmployee.get(employee.id) || [];
              const isCollapsed = collapsed.has(employee.id);

              // Сводка считается по эффективной занятости, поэтому
              // число меняется сразу при клике, не дожидаясь записи.
              const plannedDays = dates.filter((iso) => {
                const key = dayKey(employee.id, iso);
                return (
                  busyDays.has(key) ||
                  index.dayByKey.get(key)?.state === 'has_fact'
                );
              }).length;

              return (
                <React.Fragment key={employee.id}>
                  <tr className="workload-grid__employee">
                    <th className="workload-grid__name">
                      <button
                        type="button"
                        className="btn btn-link btn-sm p-0 text-decoration-none"
                        onClick={() => toggleEmployee(employee.id)}
                      >
                        {isCollapsed ? '▸' : '▾'} {employee.full_name}
                      </button>
                    </th>
                    <td className="workload-grid__days">{plannedDays}</td>
                    {dates.map((iso) => {
                      const key = dayKey(employee.id, iso);
                      const day = index.dayByKey.get(key);
                      return (
                        <EmployeeCell
                          key={iso}
                          day={day}
                          busy={busyDays.has(key) || day?.state === 'has_fact'}
                          columnClasses={dayColumnClasses(
                            'workload-grid__cell',
                            iso,
                            today
                          )}
                        />
                      );
                    })}
                  </tr>

                  {!isCollapsed &&
                    rows.map((row) => (
                      <tr key={`${row.employee_id}-${row.mark_set_id}`}>
                        <td className="workload-grid__name workload-grid__name--set">
                          <code className="workload-marka">
                            {row.marka_full}
                          </code>
                          
                          {!row.is_executor && (
                            <span
                              className="badge text-bg-secondary ms-2"
                              title="Инженер не исполнитель этого шифра: план можно только снять"
                            >
                              не исполнитель
                            </span>
                          )}
                        </td>
                                                <td className="workload-grid__days">
                          {/* Слева кружок статуса, справа дни. Кружок не
                              занимает ширины колонки «Шифр»: длинный шифр
                              не вытесняет статус. Название — по наведению. */}
                          <div className="d-flex align-items-center justify-content-between">
                            {row.status_name ? (
                              <span
                                className={`set-status-dot set-status--${row.status_id}`}
                                title={`Статус: ${row.status_name}`}
                                role="img"
                                aria-label={`Статус: ${row.status_name}`}
                              />
                            ) : (
                              <span />
                            )}
                            <span>{row.work_days ?? '—'}</span>
                          </div>
                        </td>
                        {dates.map((iso) => {
                          const setKey = planKey(row.mark_set_id, iso);
                          const day = index.dayByKey.get(
                            dayKey(employee.id, iso)
                          );
                          const owner = effectiveOwner.get(setKey);
                          const hasPlan = owner === employee.id;
                          // Ключ с инженером — изменение принадлежит
                          // конкретной строке, и соседняя строка того
                          // же шифра его не подхватит.
                          const pendingOp = pending.get(
                            cellId(row.mark_set_id, employee.id, iso)
                          );

                          // Снять можно всегда: операция ничего не
                          // создаёт и никого не занимает. Именно это
                          // делает выполнимым правило «освободите дату
                          // и ставьте заново» — план мог остаться за
                          // прежним исполнителем.
                          let clickable = canEdit && hasPlan && !saving;
                          let title;

                          if (!hasPlan) {
                            // Поставить может только текущий
                            // исполнитель: сервер берёт человека из
                            // шифра, и клик по чужой строке завёл бы
                            // план не тому.
                          if (!row.allows_plan) {
                              // Шифр на проверке или выпущен: сервер
                              // откажет (SET_CLOSED). Снятие плана эта
                              // ветка не трогает — она только для
                              // пустых клеток.
                              title = `Шифр в статусе «${row.status_name}»: план можно только снять`;
                            } else if (!row.is_executor) {
                              title = 'Планировать может только исполнитель';
                            } else if (owner !== undefined) {
                              // Дата шифра уже занята — возможно, за
                              // другим человеком. Две записи об одном
                              // дне работы означали бы двойной счёт
                              // трудозатрат.
                              title = `Дата занята планом: ${
                                employeeById.get(owner) || 'другой инженер'
                              }`;
                            } else if (BLOCKED_STATES.has(day?.state)) {
                              title = undefined;
                            } else if (busyDays.has(dayKey(employee.id, iso))) {
                              title = 'Инженер занят другим шифром';
                            } else {
                              clickable = canEdit && !saving;
                            }
                          }

                          return (
                            <MarkSetCell
                              key={iso}
                              hasPlan={hasPlan}
                              hasFact={index.factByKey.has(
                                `${employee.id}|${row.mark_set_id}|${iso}`
                              )}
                              pending={pendingOp}
                              dayState={day?.state}
                              rejected={rejectedKeys.has(setKey)}
                              clickable={clickable}
                              title={title}
                              columnClasses={dayColumnClasses(
                                'workload-grid__cell',
                                iso,
                                today
                              )}
                              onClick={() =>
                                toggleCell(
                                  row.mark_set_id,
                                  employee.id,
                                  iso,
                                  hasPlan
                                )
                              }
                            />
                          );
                        })}
                      </tr>
                    ))}

                  {!isCollapsed && rows.length === 0 && (
                    <tr>
                      <td
                        className="text-muted small workload-grid__name"
                        colSpan={dates.length + 2}
                      >
                        Работ нет
                      </td>
                    </tr>
                  )}
                </React.Fragment>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="workload-legend small text-muted mt-2">
        <span className="workload-bar workload-bar--plan" /> план
        <span className="workload-bar workload-bar--fact ms-3" /> факт по плану
        <span className="workload-bar workload-bar--offplan ms-3" /> факт вне
        плана
        {canEdit && (
          <>
            <span className="workload-bar workload-bar--pending ms-3" /> не
            сохранено
          </>
        )}
        <span className="ms-3">
          верхняя строка пары — план, нижняя — факт
        </span>
        {/* Статусы шифров — только те, что есть в окне. */}
        {statusLegendOf(grid.rows).map(([statusId, statusName]) => (
          <span key={statusId} className="ms-3 d-inline-flex align-items-center gap-1">
            <span className={`set-status-dot set-status--${statusId}`} />
            {statusName}
          </span>
        ))}
      </div>
    </div>
  );
}
