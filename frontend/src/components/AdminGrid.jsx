// app/frontend/src/components/AdminGrid.jsx
// «План и факт сотрудников» — ремонтный режим администратора данных.
//
// Сетка ОДНОГО сотрудника: строки — шифры, в каждой клетке две полосы,
// сверху план, снизу факт. Обе редактируются, без процессных
// ограничений (окно отметки, блокировка плана фактом, статус шифра):
// что можно поставить в день, сервер отдаёт флагами days[].plan_allowed
// и fact_allowed. Очистить ячейку можно всегда.
//
// Правки копятся как несохранённые (пунктир) и уходят пачкой с
// обязательным комментарием — как коммит. Приёмы взяты из сетки
// начальника (WorkloadGrid): несохранённые изменения, сохранение пачкой,
// разбор отказов 409, прокрутка к сегодня один раз на окно. Сама сетка
// начальника не тронута: режим администратора внутри неё превратил бы
// каждое условие в «если администратор — иначе».
//
// Несохранённая правка хранит ЖЕЛАЕМОЕ состояние ячейки «полоса + день»:
// шифр или null (очистить). Ровно в этом виде она уходит на сервер, а
// «было» сервер берёт из базы сам.
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import {
  fetchAdminGrid,
  fetchEmployees,
  saveAdminChanges,
  searchAdminMarkSets,
} from '../api/admin';
import ChangesetJournal from './ChangesetJournal';
import MarkSetSearch from './MarkSetSearch';

// Окно ленты: администратор чаще исправляет прошлое, поэтому назад
// смотрим дальше, чем вперёд (у инженера — наоборот). 6 недель.
const WINDOW_BACK_DAYS = 28;
const WINDOW_FORWARD_DAYS = 14;
// Шаг стрелок — неделя.
const STEP_DAYS = 7;
// Сколько дней до сегодняшнего оставлять видимыми при автопрокрутке.
const ANCHOR_DAYS = 7;

const WEEKDAYS = ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб'];
const MONTHS = [
  'янв', 'фев', 'мар', 'апр', 'мая', 'июн',
  'июл', 'авг', 'сен', 'окт', 'ноя', 'дек',
];

// Подсказки к запрету — по причине из days[].plan_reason / fact_reason.
const REASON_HINTS = {
  out_of_horizon: 'Производственный календарь не заполнен',
  not_assigned: 'Сотрудник не числится в штате на эту дату',
  nonwork: 'Нерабочий день',
  absence: 'День закрыт табелем',
  future: 'День ещё не наступил',
};

const LAYER_NAMES = { plan: 'план', fact: 'факт' };

// --- даты ------------------------------------------------------------
// Везде ISO-строки (ГГГГ-ММ-ДД). Date создаётся ТОЛЬКО из местных
// компонентов и обратно собирается из них же: toISOString() отдаёт UTC
// и при UTC+3 сдвигает дату на сутки назад (ловушка из брифа).

/** ISO-дата из местных компонентов Date. */
function toIso(value) {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, '0');
  const day = String(value.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

/** Сдвиг ISO-даты на days дней — через местную полночь. */
function shiftIso(iso, days) {
  const [year, month, day] = iso.split('-').map(Number);
  return toIso(new Date(year, month - 1, day + days));
}

/** Сегодня по часам браузера (UTC+3, как и сервер). */
function todayIso() {
  return toIso(new Date());
}

/** «пн» и «12» для заголовка; первого числа — месяц вместо дня недели. */
function dayLabel(iso) {
  const [year, month, day] = iso.split('-').map(Number);
  const weekday = WEEKDAYS[new Date(year, month - 1, day).getDay()];
  return {
    top: day === 1 ? MONTHS[month - 1] : weekday,
    isMonthStart: day === 1,
    day: String(day).padStart(2, '0'),
  };
}

/** «15.09 – 27.10.2026»; через Новый год — год у обеих дат. */
function formatRange(fromIso, untilIso) {
  const dm = (iso) => `${iso.slice(8, 10)}.${iso.slice(5, 7)}`;
  return fromIso.slice(0, 4) === untilIso.slice(0, 4)
    ? `${dm(fromIso)} – ${dm(untilIso)}.${untilIso.slice(0, 4)}`
    : `${dm(fromIso)}.${fromIso.slice(0, 4)} – ${dm(untilIso)}.${untilIso.slice(0, 4)}`;
}

// --- ключи и несохранённые правки ------------------------------------

// Ячейка сотрудника — «полоса|день». Сотрудник один на сетку.
const cellKey = (layer, iso) => `${layer}|${iso}`;

/**
 * Значение ячейки с учётом несохранённой правки.
 * pending хранит желаемое состояние: шифр или null (очистить).
 */
function effectiveValue(base, pending, layer, iso) {
  const key = cellKey(layer, iso);
  return pending.has(key) ? pending.get(key) : base[layer].get(iso) ?? null;
}

/** Строки шифров в порядке сервера (посимвольно, как sort() в Python). */
function compareMarka(a, b) {
  if (a.marka_full === b.marka_full) {
    return 0;
  }
  return a.marka_full < b.marka_full ? -1 : 1;
}

// --- полоса ----------------------------------------------------------

/**
 * Одна полоса клетки (план или факт) одного шифра в один день.
 *
 * Состояния: стоит и не менялась — цвет; поставлена правкой — пунктир;
 * снята правкой — гаснущая полоса; пусто — прозрачное место (держит
 * высоту, чтобы план всегда был сверху, а факт снизу).
 */
function Slot({ layer, here, wasHere, factOnPlan, clickable, rejected, title, onClick }) {
  let bar = '';
  if (here && !wasHere) {
    bar = ' workload-bar--pending';
  } else if (!here && wasHere) {
    bar = ' workload-bar--removing';
  } else if (here && layer === 'plan') {
    bar = ' workload-bar--plan';
  } else if (here) {
    // Факт по плану — зелёный, вне плана — янтарный, как у начальника.
    bar = factOnPlan ? ' workload-bar--fact' : ' workload-bar--offplan';
  }

  const classes = ['admin-slot'];
  if (clickable) {
    classes.push('admin-slot--clickable');
  }
  if (rejected) {
    classes.push('admin-slot--rejected');
  }

  return (
    <div
      className={classes.join(' ')}
      title={title}
      onClick={clickable ? onClick : undefined}
      role={clickable ? 'button' : undefined}
    >
      <span className={`workload-bar${bar}`} />
    </div>
  );
}

// --- компонент -------------------------------------------------------

export default function AdminGrid({ onSaved }) {
  const [employees, setEmployees] = useState([]);
  const [employeeId, setEmployeeId] = useState(null);

  // Начало окна. Конец — WINDOW_BACK + WINDOW_FORWARD дней спустя.
  const [windowStart, setWindowStart] = useState(() =>
    shiftIso(todayIso(), -WINDOW_BACK_DAYS)
  );
  const windowEnd = shiftIso(windowStart, WINDOW_BACK_DAYS + WINDOW_FORWARD_DAYS);

  const [grid, setGrid] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  // Сообщение об успехе («Правка №N сохранена»).
  const [notice, setNotice] = useState('');

  // Несохранённые правки: «полоса|день» → шифр или null.
  const [pending, setPending] = useState(() => new Map());
  // Строки, добавленные поиском: живут до сохранения или смены сотрудника.
  const [addedRows, setAddedRows] = useState([]);
  // Отказы 409 по ячейкам — подсветка и список причин.
  const [rejections, setRejections] = useState([]);

  const [searchOpen, setSearchOpen] = useState(false);
  const [commentOpen, setCommentOpen] = useState(false);
  const [comment, setComment] = useState('');
  const [saving, setSaving] = useState(false);
  // Счётчик сохранений: журнал перечитывается, когда он меняется. Ноль —
  // сотрудник только что выбран, свежую правку раскрывать не нужно.
  const [journalKey, setJournalKey] = useState(0);

  const requestRef = useRef(0);
  const scrollRef = useRef(null);
  const todayRef = useRef(null);
  // Окно, к которому лента уже подведена (см. эффект прокрутки).
  const anchoredRef = useRef(null);

  // Список имён — один раз при открытии страницы.
  useEffect(() => {
    fetchEmployees()
      .then(setEmployees)
      .catch((exc) => setError(exc.message));
  }, []);

  const load = useCallback(async () => {
    if (employeeId == null) {
      return;
    }
    // Ответы на быстрые переключения могут прийти не по порядку —
    // показываем только ответ на последний запрос.
    const requestId = requestRef.current + 1;
    requestRef.current = requestId;
    setLoading(true);
    try {
      const data = await fetchAdminGrid(employeeId, windowStart, windowEnd);
      if (requestRef.current === requestId) {
        setGrid(data);
        setError('');
      }
    } catch (exc) {
      if (requestRef.current === requestId) {
        setError(exc.message);
      }
    } finally {
      if (requestRef.current === requestId) {
        setLoading(false);
      }
    }
  }, [employeeId, windowStart, windowEnd]);

  useEffect(() => {
    load();
  }, [load]);

  // Подводка к сегодня — один раз на окно (как в WorkloadGrid после
  // исправления «прыжка ленты»): перечитывание после сохранения меняет
  // grid, но не окно, и ленту из-под руки не дёргает.
  useEffect(() => {
    const anchorKey = `${employeeId}|${windowStart}`;
    if (!grid || anchoredRef.current === anchorKey) {
      return undefined;
    }
    const frame = requestAnimationFrame(() => {
      const container = scrollRef.current;
      if (!container) {
        return;
      }
      const marker = todayRef.current;
      if (!marker) {
        container.scrollLeft = 0;
      } else {
        const sticky = container.querySelector('.workload-grid__name');
        const stickyWidth = sticky ? sticky.offsetWidth : 0;
        const dayWidth = marker.offsetWidth || 36;
        container.scrollLeft = Math.max(
          0,
          marker.offsetLeft - stickyWidth - ANCHOR_DAYS * dayWidth
        );
      }
      anchoredRef.current = anchorKey;
    });
    return () => cancelAnimationFrame(frame);
  }, [grid, employeeId, windowStart]);

  // Текущее состояние из базы: день → шифр, отдельно для плана и факта.
  const base = useMemo(() => {
    const plan = new Map();
    const fact = new Map();
    for (const cell of grid?.cells || []) {
      if (cell.has_plan) {
        plan.set(cell.work_date, cell.mark_set_id);
      }
      if (cell.has_fact) {
        fact.set(cell.work_date, cell.mark_set_id);
      }
    }
    return { plan, fact };
  }, [grid]);

  const dayByDate = useMemo(() => {
    const result = new Map();
    for (const day of grid?.days || []) {
      result.set(day.work_date, day);
    }
    return result;
  }, [grid]);

  // Серверные строки + добавленные поиском (без повторов), внизу.
  const rows = useMemo(() => {
    const serverRows = grid?.rows || [];
    const serverIds = new Set(serverRows.map((row) => row.mark_set_id));
    return [
      ...serverRows,
      ...addedRows.filter((row) => !serverIds.has(row.mark_set_id)),
    ];
  }, [grid, addedRows]);

  const rejectedKeys = useMemo(
    () => new Set(rejections.map((item) => cellKey(item.layer, item.work_date))),
    [rejections]
  );

  const dates = useMemo(() => (grid ? grid.days.map((day) => day.work_date) : []), [grid]);

  const hasTabRow = grid?.days.some((day) => day.tab_letter);

  // --- действия ------------------------------------------------------

  /** Есть несохранённое — спросить, прежде чем потерять. */
  function confirmDiscard() {
    return (
      pending.size === 0 ||
      window.confirm('Есть несохранённые изменения. Сбросить их?')
    );
  }

  function resetEdits() {
    setPending(new Map());
    setAddedRows([]);
    setRejections([]);
    setComment('');
  }

  function selectEmployee(value) {
    if (!confirmDiscard()) {
      return;
    }
    resetEdits();
    setNotice('');
    setSearchOpen(false);
    setGrid(null);
    setJournalKey(0);
    setEmployeeId(value ? Number(value) : null);
  }

  function shiftWindow(days) {
    // Несохранённые правки при сдвиге НЕ сбрасываются: они привязаны к
    // датам и уйдут при сохранении, даже если окно уехало. Счётчик
    // «Изменений: N» показывает, что они есть.
    setWindowStart((current) => shiftIso(current, days));
  }

  function goToday() {
    setWindowStart(shiftIso(todayIso(), -WINDOW_BACK_DAYS));
  }

  /**
   * Клик по полосе шифра в день — переключатель в столбце:
   * в каждом дне у полосы один шифр. Клик по шифру X ставит X (если там
   * был другой — он «переезжает»); клик по уже стоящему X — очищает.
   *
   * Если желаемое совпало с базой, правка убирается из пачки: «туда и
   * обратно» не оставляет следа, как и на сервере.
   */
  function toggleSlot(layer, markSetId, iso) {
    setPending((previous) => {
      const next = new Map(previous);
      const current = effectiveValue(base, previous, layer, iso);
      const desired = current === markSetId ? null : markSetId;
      const original = base[layer].get(iso) ?? null;
      const key = cellKey(layer, iso);
      if (desired === original) {
        next.delete(key);
      } else {
        next.set(key, desired);
      }
      return next;
    });
    // Отказ по этой ячейке устарел — пользователь её поправил.
    setRejections((list) =>
      list.filter((item) => cellKey(item.layer, item.work_date) !== cellKey(layer, iso))
    );
  }

  function handleAdd(item) {
    setAddedRows((current) => [
      ...current,
      {
        mark_set_id: item.id,
        marka_full: item.marka_full,
        status_id: null,
        status_name: null,
        is_executor: false,
        is_added: true,
      },
    ]);
    setSearchOpen(false);
  }

  async function handleSave() {
    const text = comment.trim();
    if (!text || pending.size === 0) {
      return;
    }
    const cells = [...pending].map(([key, markSetId]) => {
      const [layer, workDate] = key.split('|');
      return { layer, work_date: workDate, mark_set_id: markSetId };
    });

    setSaving(true);
    setError('');
    try {
      const result = await saveAdminChanges(employeeId, text, cells);
      setCommentOpen(false);
      resetEdits();
      setNotice(
        result.changeset_id == null
          ? 'Изменений нет: итоговое состояние совпало с базой.'
          : `Правка №${result.changeset_id} сохранена: изменено ячеек — ${result.applied}.`
      );
      await load();
      if (result.changeset_id != null) {
        setJournalKey((key) => key + 1);
      }
      onSaved?.(result);
    } catch (exc) {
      setCommentOpen(false);
      if (exc.rejected.length > 0) {
        // Правки остаются на месте — их можно поправить и сохранить снова.
        setRejections(exc.rejected);
      } else {
        setError(exc.message);
        // «Данные изменились» — картина устарела, перечитываем.
        if (exc.status === 409) {
          await load();
        }
      }
    } finally {
      setSaving(false);
    }
  }

  // --- отрисовка -----------------------------------------------------

  const today = grid?.today || todayIso();

  return (
    <div>
      {/* Выбор сотрудника. Нативный select: набор первых букв ФИО сразу
          переходит к нужной строке, списка в сотню имён хватает. */}
      <div className="d-flex flex-wrap align-items-center gap-2 mb-3">
        <label htmlFor="admin-employee" className="form-label mb-0">
          Сотрудник
        </label>
        <select
          id="admin-employee"
          className="form-select admin-employee-select"
          value={employeeId ?? ''}
          onChange={(event) => selectEmployee(event.target.value)}
        >
          <option value="">— выберите сотрудника —</option>
          {employees.map((item) => (
            <option key={item.id} value={item.id}>
              {item.full_name}
              {item.department_code ? ` · ${item.department_code}` : ''}
            </option>
          ))}
        </select>
      </div>

      {error && (
        <div className="alert alert-danger" role="alert">
          {error}
        </div>
      )}
      {notice && (
        <div className="alert alert-success d-flex justify-content-between gap-3" role="status">
          <span>{notice}</span>
          <button
            type="button"
            className="btn-close"
            aria-label="Закрыть"
            onClick={() => setNotice('')}
          />
        </div>
      )}

      {employeeId != null && !grid && !error && (
        <div className="text-muted py-4">Загрузка сетки…</div>
      )}

      {grid && (
        <>
          {/* Окно ленты. */}
          <div className="d-flex flex-wrap align-items-center gap-2 mb-2">
            <div className="btn-group" role="group" aria-label="Сдвиг ленты">
              <button
                type="button"
                className="btn btn-outline-secondary"
                disabled={loading || grid.date_from <= grid.horizon_from}
                onClick={() => shiftWindow(-STEP_DAYS)}
                aria-label="На неделю назад"
                title="На неделю назад"
              >
                ‹
              </button>
              <span className="btn btn-outline-secondary disabled admin-range-label">
                {formatRange(grid.date_from, grid.date_to)}
              </span>
              <button
                type="button"
                className="btn btn-outline-secondary"
                disabled={loading || grid.date_to >= grid.horizon_to}
                onClick={() => shiftWindow(STEP_DAYS)}
                aria-label="На неделю вперёд"
                title="На неделю вперёд"
              >
                ›
              </button>
            </div>
            {!dates.includes(today) && (
              <button
                type="button"
                className="btn btn-sm btn-outline-secondary"
                onClick={goToday}
              >
                Сегодня
              </button>
            )}
          </div>

          {rejections.length > 0 && (
            <div className="alert alert-warning py-2" role="alert">
              <div className="fw-semibold">Изменения не применены</div>
              <ul className="mb-0 small">
                {rejections.map((item) => (
                  <li key={cellKey(item.layer, item.work_date)}>{item.message}</li>
                ))}
              </ul>
            </div>
          )}

          <div
            ref={scrollRef}
            className={`workload-scroll${loading ? ' admin-grid--loading' : ''}`}
          >
            <table className="table table-sm workload-grid mb-0">
              <thead>
                <tr>
                  <th className="workload-grid__name">Шифр</th>
                  {dates.map((iso) => {
                    const label = dayLabel(iso);
                    const day = dayByDate.get(iso);
                    const classes = ['workload-grid__date'];
                    if (label.isMonthStart) {
                      classes.push('workload-grid__date--month-start');
                    }
                    if (iso === today) {
                      classes.push('workload-grid__date--today');
                    }
                    if (day?.plan_reason === 'nonwork') {
                      classes.push('workload-grid__cell--nonwork');
                    }
                    return (
                      <th
                        key={iso}
                        ref={iso === today ? todayRef : undefined}
                        className={classes.join(' ')}
                      >
                        <span
                          className={
                            label.isMonthStart
                              ? 'workload-grid__month'
                              : 'workload-grid__weekday'
                          }
                        >
                          {label.top}
                        </span>
                        <span className="workload-grid__daynum">{label.day}</span>
                      </th>
                    );
                  })}
                </tr>
                {/* Табель — буква неявки один раз на день: неявка
                    принадлежит человеку, а не шифру. */}
                {hasTabRow && (
                  <tr>
                    <th className="workload-grid__name text-muted small">Табель</th>
                    {dates.map((iso) => (
                      <td key={iso} className="workload-grid__cell text-center small text-muted">
                        {dayByDate.get(iso)?.tab_letter || ''}
                      </td>
                    ))}
                  </tr>
                )}
              </thead>

              <tbody>
                {rows.length === 0 && (
                  <tr>
                    <td className="text-muted small workload-grid__name" colSpan={dates.length + 1}>
                      В этом периоде у сотрудника нет шифров. Добавьте шифр кнопкой ниже.
                    </td>
                  </tr>
                )}

                {rows.map((row) => (
                  <tr key={row.mark_set_id}>
                    <th className="workload-grid__name workload-grid__name--set">
                      <code className="workload-marka">{row.marka_full}</code>
                      {row.is_added && (
                        <span className="badge text-bg-primary ms-2">добавлен</span>
                      )}
                      {row.status_name && (
                        <span
                          className={`set-status-dot set-status--${row.status_id} ms-2`}
                          title={`Статус: ${row.status_name}`}
                          role="img"
                          aria-label={`Статус: ${row.status_name}`}
                        />
                      )}
                    </th>

                    {dates.map((iso) => {
                      const day = dayByDate.get(iso);
                      const classes = ['workload-grid__cell'];
                      if (iso === today) {
                        classes.push('workload-grid__cell--today');
                      }
                      if (dayLabel(iso).isMonthStart) {
                        classes.push('workload-grid__cell--month-start');
                      }
                      if (day?.plan_reason === 'nonwork') {
                        classes.push('workload-grid__cell--nonwork');
                      }

                      const effPlan = effectiveValue(base, pending, 'plan', iso);

                      const slots = ['plan', 'fact'].map((layer) => {
                        const here = effectiveValue(base, pending, layer, iso) === row.mark_set_id;
                        const wasHere = (base[layer].get(iso) ?? null) === row.mark_set_id;
                        const allowed = day?.[`${layer}_allowed`];
                        // Очистить можно всегда; поставить — если день
                        // разрешает эту полосу.
                        const clickable = !saving && (here || wasHere || allowed);
                        let title;
                        if (here) {
                          title = `${LAYER_NAMES[layer]}: снять`;
                        } else if (allowed) {
                          title = `${LAYER_NAMES[layer]}: поставить ${row.marka_full}`;
                        } else {
                          title = REASON_HINTS[day?.[`${layer}_reason`]] || undefined;
                        }
                        return (
                          <Slot
                            key={layer}
                            layer={layer}
                            here={here}
                            wasHere={wasHere}
                            factOnPlan={effPlan === row.mark_set_id}
                            clickable={clickable}
                            rejected={rejectedKeys.has(cellKey(layer, iso)) && (here || wasHere)}
                            title={title}
                            onClick={() => toggleSlot(layer, row.mark_set_id, iso)}
                          />
                        );
                      });

                      return (
                        <td key={iso} className={classes.join(' ')}>
                          {slots}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Под сеткой: добавить шифр, счётчик, отмена, сохранение. */}
          <div className="d-flex flex-wrap align-items-center gap-2 mt-3">
            {!searchOpen && (
              <button
                type="button"
                className="btn btn-sm btn-outline-primary"
                onClick={() => setSearchOpen(true)}
              >
                + добавить шифр
              </button>
            )}
            <div className="ms-auto d-flex align-items-center gap-2">
              <span className="text-muted small">Изменений: {pending.size}</span>
              <button
                type="button"
                className="btn btn-sm btn-outline-secondary"
                disabled={pending.size === 0 || saving}
                onClick={() => {
                  if (window.confirm('Отменить все несохранённые изменения?')) {
                    resetEdits();
                  }
                }}
              >
                Отменить
              </button>
              <button
                type="button"
                className="btn btn-sm btn-primary"
                disabled={pending.size === 0 || saving}
                onClick={() => setCommentOpen(true)}
              >
                Сохранить…
              </button>
            </div>
          </div>

          <div className="admin-legend small text-muted mt-2">
            <span className="workload-bar workload-bar--plan" /> план
            <span className="workload-bar workload-bar--fact ms-3" /> факт по плану
            <span className="workload-bar workload-bar--offplan ms-3" /> факт вне плана
            <span className="workload-bar workload-bar--pending ms-3" /> не сохранено
            <span className="ms-3">верхняя полоса — план, нижняя — факт</span>
          </div>

          {searchOpen && (
            <MarkSetSearch
              existingIds={new Set(rows.map((row) => row.mark_set_id))}
              onAdd={handleAdd}
              onClose={() => setSearchOpen(false)}
              search={searchAdminMarkSets}
              emptyText="Ничего не найдено."
              hintText="Ищутся шифры любого статуса, включая выпущенные."
            />
          )}
        </>
      )}

      {/* Журнал правок — под сеткой, только чтение. */}
      {employeeId != null && (
        <ChangesetJournal employeeId={employeeId} reloadKey={journalKey} />
      )}

      {/* Окно комментария — на состоянии React, без JS Bootstrap: одна
          библиотека отвечает за элемент. Классы modal/d-block дают вид
          стандартного окна Bootstrap. */}
      {commentOpen && (
        <div className="modal d-block admin-modal" role="dialog" aria-modal="true">
          <div className="modal-dialog">
            <div className="modal-content">
              <div className="modal-header">
                <h2 className="modal-title h5">Комментарий к правке</h2>
                <button
                  type="button"
                  className="btn-close"
                  aria-label="Закрыть"
                  onClick={() => setCommentOpen(false)}
                  disabled={saving}
                />
              </div>
              <div className="modal-body">
                <p className="small text-muted">
                  Изменений: {pending.size}. Комментарий обязателен — он
                  объясняет правку в журнале.
                </p>
                <textarea
                  className="form-control"
                  rows={3}
                  maxLength={2000}
                  value={comment}
                  onChange={(event) => setComment(event.target.value)}
                  placeholder="Например: отмечал не тот шифр 12–16.10, перенёс на ЭМ#1"
                  autoFocus
                />
              </div>
              <div className="modal-footer">
                <button
                  type="button"
                  className="btn btn-outline-secondary"
                  onClick={() => setCommentOpen(false)}
                  disabled={saving}
                >
                  Отмена
                </button>
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={handleSave}
                  disabled={saving || !comment.trim()}
                >
                  {saving ? 'Сохранение…' : 'Сохранить'}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
