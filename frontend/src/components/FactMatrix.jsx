// app/frontend/src/components/FactMatrix.jsx
// Лента факта инженера — раздел «Мой факт» (/fact).
//
// Строки — шифры, столбцы — дни ленты: 14 дней до якорного дня и 28
// после (по умолчанию якорь — сегодня). Лента, а не календарный месяц:
// в любой день, и 1-го числа тоже, на экране сегодня и предыдущий
// рабочий день. Стрелки сдвигают ленту на неделю.
// В клетке две позиции: сверху
// плановая полоса, снизу отметка факта. Отметка ставится кликом по
// клетке и сохраняется СРАЗУ: одно нажатие — один запрос, без кнопки
// «Сохранить», которую легко забыть.
//
// Все правила — на сервере. Кликабельность дня берётся из
// days[].state === 'open', допустимость шифра — из rows[].allows_fact.
// Компонент ничего не вычисляет сам: иначе правила оказались бы
// записаны дважды, на двух языках, и однажды разошлись бы.
//
// Даты — строки ISO от сервера, и все вычисления с ними делает сервер:
// якоря соседних положений ленты (prev_anchor / next_anchor) приходят
// готовыми. В Date превращаем только ради дня недели и подписи, и
// только из местных компонентов (new Date(y, m, d)): toISOString() при
// UTC+3 сдвигает дату на сутки назад.
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { fetchFactWindow, postMark } from '../api/fact';
import MarkSetSearch from './MarkSetSearch';

// Родительный падеж — для строки «Понедельник, 12 октября».
const MONTHS_GENITIVE = [
  'января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
  'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря',
];
// Короткие названия — для заголовка столбца первого числа месяца.
const MONTHS_SHORT = [
  'янв', 'фев', 'мар', 'апр', 'мая', 'июн',
  'июл', 'авг', 'сен', 'окт', 'ноя', 'дек',
];
const WEEKDAYS_SHORT = ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб'];
const WEEKDAYS_LONG = [
  'Воскресенье', 'Понедельник', 'Вторник', 'Среда',
  'Четверг', 'Пятница', 'Суббота',
];

// Подсказки при наведении на недоступный день. Это подсказка, а не
// правило: если сервер откажет, покажем его текст.
const STATE_HINTS = {
  open: 'Отметить работу по этому шифру',
  closed: 'Срок отметки истёк',
  future: 'День ещё не наступил',
  nonwork: 'Нерабочий день',
  not_assigned: 'Вы не числитесь в штате на эту дату',
  out_of_horizon: 'Производственный календарь не заполнен',
};

/** Части ISO-даты «ГГГГ-ММ-ДД» числами — без участия часового пояса. */
function splitIso(iso) {
  const [year, month, day] = iso.split('-').map(Number);
  return { year, month, day };
}

/** День недели (0 — воскресенье) по ISO-дате, из местных компонентов. */
function weekdayOf(iso) {
  const { year, month, day } = splitIso(iso);
  return new Date(year, month - 1, day).getDay();
}

/** «Понедельник, 12 октября» — строка над лентой. */
function formatToday(iso) {
  const { month, day } = splitIso(iso);
  return `${WEEKDAYS_LONG[weekdayOf(iso)]}, ${day} ${MONTHS_GENITIVE[month - 1]}`;
}

/**
 * Порядок строк — посимвольно, как sort() на сервере (Python сравнивает
 * коды символов). localeCompare дал бы другой порядок, и после
 * перезагрузки строки переставились бы.
 */
function compareMarka(a, b) {
  if (a.marka_full === b.marka_full) {
    return 0;
  }
  return a.marka_full < b.marka_full ? -1 : 1;
}

/**
 * Подпись диапазона ленты: «01.10 – 12.11.2026». Если лента пересекает
 * Новый год — год у обеих дат: «28.12.2026 – 08.02.2027».
 */
function formatRange(fromIso, untilIso) {
  const from = splitIso(fromIso);
  const to = splitIso(untilIso);
  const dm = (part) =>
    `${String(part.day).padStart(2, '0')}.${String(part.month).padStart(2, '0')}`;
  return from.year === to.year
    ? `${dm(from)} – ${dm(to)}.${to.year}`
    : `${dm(from)}.${from.year} – ${dm(to)}.${to.year}`;
}

// Сколько дней до сегодняшнего оставлять видимыми при автопрокрутке,
// когда лента шире экрана: неделя контекста — видно, отмечены ли
// прошлые дни.
const SCROLL_CONTEXT_DAYS = 7;

/** Ключ клетки. Инженер один, поэтому достаточно пары (шифр, дата). */
function cellKey(markSetId, workDate) {
  return `${markSetId}|${workDate}`;
}

export default function FactMatrix() {
  // Ответ сервера на текущее положение ленты. null — ещё не загружено.
  const [data, setData] = useState(null);
  // Строки, добавленные через поиск: живут только в браузере до первой
  // отметки. После отметки шифр приходит с сервера и отсюда убирается.
  const [addedRows, setAddedRows] = useState([]);
  // Якорный день ленты. null — сегодня по мнению сервера: браузеру не
  // нужно знать дату при первой загрузке.
  const [anchor, setAnchor] = useState(null);

  const [loading, setLoading] = useState(true);
  // Ошибка загрузки — матрицу показать нельзя (403, сеть).
  const [loadError, setLoadError] = useState('');
  // Отказ отметки — матрица остаётся, сверху закрываемое сообщение.
  const [notice, setNotice] = useState('');
  // Клетка, отметка которой сейчас сохраняется. Одна на всю матрицу:
  // пока запрос в пути, другие клики игнорируются, иначе два быстрых
  // клика по разным шифрам одного дня дали бы гонку переносов.
  const [savingKey, setSavingKey] = useState(null);
  const [searchOpen, setSearchOpen] = useState(false);

  // Номер последнего запроса ленты. При быстром листании ответы могут
  // прийти не по порядку; показываем только ответ на последний запрос.
  const requestRef = useRef(0);

  // Прокрутка к сегодня — один раз на положение ленты. Перечитывание
  // после отказа отметки меняет data, но не якорь: по этой пометке
  // эффект понимает, что ленту двигать не нужно (тот же приём, что в
  // сетке загрузки).
  const scrollRef = useRef(null);
  const todayRef = useRef(null);
  const scrolledAnchorRef = useRef(null);

  const load = useCallback(async (target) => {
    const requestId = requestRef.current + 1;
    requestRef.current = requestId;
    setLoading(true);
    try {
      const result = await fetchFactWindow(target);
      if (requestRef.current !== requestId) {
        return;  // пока ждали, запросили другое положение ленты
      }
      setData(result);
      setLoadError('');
    } catch (error) {
      if (requestRef.current !== requestId) {
        return;
      }
      setLoadError(error.message);
    } finally {
      if (requestRef.current === requestId) {
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    load(anchor);
  }, [load, anchor]);

  // Сдвиг ленты. Добавленные вручную строки НЕ сбрасываются: при сдвиге
  // на неделю открытые дни могут остаться на экране, и строка ещё
  // нужна. target — готовый якорь от сервера или null (сегодня).
  function goTo(target) {
    setNotice('');
    setSearchOpen(false);
    setAnchor(target);
  }

  // Подводка к сегодняшнему дню, если лента шире экрана (ноутбук).
  // requestAnimationFrame: эффект выполняется до раскладки таблицы, и
  // offsetLeft у заголовка ещё не готов; кадром позже размеры посчитаны.
  useEffect(() => {
    if (!data || scrolledAnchorRef.current === data.anchor) {
      return undefined;
    }
    const frame = requestAnimationFrame(() => {
      const container = scrollRef.current;
      if (!container) {
        return;
      }
      const marker = todayRef.current;
      if (!marker) {
        // Сегодня вне ленты (сдвинули стрелками) — показываем начало.
        container.scrollLeft = 0;
      } else {
        // Липкая колонка «Шифр» закрывает левый край, поэтому её ширина
        // вычитается; SCROLL_CONTEXT_DAYS дней слева — контекст.
        const sticky = container.querySelector('.fact-matrix__code');
        const stickyWidth = sticky ? sticky.offsetWidth : 0;
        const dayWidth = marker.offsetWidth || 36;
        container.scrollLeft = Math.max(
          0,
          marker.offsetLeft - stickyWidth - SCROLL_CONTEXT_DAYS * dayWidth
        );
      }
      // Пометка — только после фактической подводки: если кадр
      // отменили, следующий запуск эффекта подведёт заново.
      scrolledAnchorRef.current = data.anchor;
    });
    return () => cancelAnimationFrame(frame);
  }, [data]);

  // Производные структуры для быстрого доступа при отрисовке.
  const view = useMemo(() => {
    if (!data) {
      return null;
    }
    const cells = new Map();
    for (const cell of data.cells) {
      cells.set(cellKey(cell.mark_set_id, cell.work_date), cell);
    }
    const serverIds = new Set(data.rows.map((row) => row.mark_set_id));
    // Серверные строки (уже отсортированы по шифру), затем добавленные
    // вручную — внизу, в порядке добавления.
    const rows = [
      ...data.rows,
      ...addedRows.filter((row) => !serverIds.has(row.mark_set_id)),
    ];
    const hasAbsence = data.days.some((day) => day.state === 'absence');
    return {
      cells,
      rows,
      ids: new Set(rows.map((row) => row.mark_set_id)),
      hasAbsence,
    };
  }, [data, addedRows]);

  // Клик по клетке (строка, день).
  async function handleCellClick(row, day) {
    const key = cellKey(row.mark_set_id, day.work_date);
    const cell = view.cells.get(key);

    // Ничего не отправляем, если день или шифр закрыт, если уже идёт
    // сохранение, или если это собственная отметка (сервер ответил бы
    // noop — снять отметку нельзя, только перенести).
    if (day.state !== 'open' || !row.allows_fact || savingKey || cell?.has_fact) {
      return;
    }

    setSavingKey(key);
    setNotice('');
    try {
      const result = await postMark(row.mark_set_id, day.work_date);

      // Заменяем столбец дня целиком: при переносе меняются две клетки
      // (старая теряет факт, новая получает), сервер отдал обе.
      setData((current) => ({
        ...current,
        days: current.days.map((item) =>
          item.work_date === result.day.work_date ? result.day : item
        ),
        cells: [
          ...current.cells.filter((item) => item.work_date !== day.work_date),
          ...result.cells,
        ],
        // Строка шифра теперь серверная: заменяем или добавляем.
        rows: current.rows.some((item) => item.mark_set_id === result.row.mark_set_id)
          ? current.rows.map((item) =>
              item.mark_set_id === result.row.mark_set_id ? result.row : item
            )
          : [...current.rows, result.row].sort(compareMarka),
      }));
      setAddedRows((current) =>
        current.filter((item) => item.mark_set_id !== result.row.mark_set_id)
      );
    } catch (error) {
      setNotice(error.message);
      // 409 и 404 означают, что картина на экране устарела (неявка,
      // выпущенный шифр, вторая вкладка). Перечитываем месяц: заодно
      // подтянется причина отказа.
      if (error.status === 409 || error.status === 404) {
        load(anchor);
      }
    } finally {
      setSavingKey(null);
    }
  }

  // Шифр выбран в поиске — добавляем строку на месте.
  function handleAdd(item) {
    setAddedRows((current) => [
      ...current,
      {
        mark_set_id: item.id,
        marka_full: item.marka_full,
        status_id: null,
        is_executor: false,
        // Поиск отдаёт только шифры, по которым факт ставится.
        allows_fact: true,
        is_added: true,
      },
    ]);
    setSearchOpen(false);
  }

  // --- Отрисовка ---------------------------------------------------------

  if (!data) {
    return (
      <div className="text-muted py-5 text-center">
        {loadError ? (
          <span className="text-danger">{loadError}</span>
        ) : (
          'Загрузка матрицы…'
        )}
      </div>
    );
  }

  // Лента сдвинута стрелками — показываем кнопку возврата к сегодня.
  const shifted = data.anchor !== data.today;

  return (
    <div className="fact-board">
      {/* Над лентой: дата слева, переключатель справа — в одну строку. */}
      <div className="d-flex flex-wrap align-items-center justify-content-between gap-3 mb-2">
        <div className="text-muted">{formatToday(data.today)}</div>

        <div className="d-flex align-items-center gap-2">
          {shifted && (
            <button
              type="button"
              className="btn btn-sm btn-outline-secondary"
              onClick={() => goTo(null)}
            >
              Сегодня
            </button>
          )}
          {/* Стрелки берут готовые якоря от сервера: браузер даты не
              складывает. null — лента упёрлась в край календаря. */}
          <div className="btn-group" role="group" aria-label="Сдвиг ленты">
            <button
              type="button"
              className="btn btn-outline-secondary"
              disabled={!data.prev_anchor || loading}
              onClick={() => goTo(data.prev_anchor)}
              aria-label="На неделю назад"
              title="На неделю назад"
            >
              ‹
            </button>
            <span className="btn btn-outline-secondary disabled fact-range-label">
              {formatRange(data.date_from, data.date_to)}
            </span>
            <button
              type="button"
              className="btn btn-outline-secondary"
              disabled={!data.next_anchor || loading}
              onClick={() => goTo(data.next_anchor)}
              aria-label="На неделю вперёд"
              title="На неделю вперёд"
            >
              ›
            </button>
          </div>
        </div>
      </div>

      {notice && (
        <div className="alert alert-warning d-flex align-items-start justify-content-between gap-3" role="alert">
          <span>{notice}</span>
          <button
            type="button"
            className="btn-close"
            aria-label="Закрыть"
            onClick={() => setNotice('')}
          />
        </div>
      )}
      {loadError && (
        <div className="alert alert-danger" role="alert">
          {loadError}
        </div>
      )}

      {/* Лента в карточке. Ширина карточки — по содержимому (.fact-board
          в style.css): на широком мониторе справа остаётся поле, на узком
          появляется нижний скролл внутри карточки. */}
      <div className="card">
        <div
          ref={scrollRef}
          className={`fact-matrix-wrap${loading ? ' fact-matrix-wrap--loading' : ''}`}
        >
          <table className="fact-matrix">
            <thead>
              <tr>
                <th className="fact-matrix__code" scope="col">Шифр</th>
                {data.days.map((day) => {
                  const weekday = weekdayOf(day.work_date);
                  const { month, day: dayNumber } = splitIso(day.work_date);
                  const isToday = day.work_date === data.today;
                  const classes = ['fact-matrix__day', `fact-matrix__day--${day.state}`];
                  if (isToday) {
                    classes.push('fact-matrix__day--today');
                  }
                  return (
                    <th
                      key={day.work_date}
                      ref={isToday ? todayRef : undefined}
                      className={classes.join(' ')}
                      scope="col"
                    >
                      {/* Лента пересекает месяцы, и «01» без подсказки
                          неоднозначно. Первого числа вместо дня недели —
                          месяц, как в сетке начальника. */}
                      {dayNumber === 1 ? (
                        <div className="fact-matrix__weekday fw-semibold text-body">
                          {MONTHS_SHORT[month - 1]}
                        </div>
                      ) : (
                        <div className="fact-matrix__weekday">{WEEKDAYS_SHORT[weekday]}</div>
                      )}
                      <div>{splitIso(day.work_date).day.toString().padStart(2, '0')}</div>
                    </th>
                  );
                })}
              </tr>

              {/* Строка «Табель» — только если в ленте есть неявки. Буква
                  стоит ОДИН раз на день: неявка принадлежит человеку, а не
                  работе по шифру. */}
              {view.hasAbsence && (
                <tr className="fact-matrix__tab-row">
                  <th className="fact-matrix__code text-muted small" scope="row">Табель</th>
                  {data.days.map((day) => (
                    <td
                      key={day.work_date}
                      className={`fact-matrix__tab fact-matrix__day--${day.state}`}
                    >
                      {day.state === 'absence' ? day.tab_letter : ''}
                    </td>
                  ))}
                </tr>
              )}
            </thead>

            <tbody>
              {view.rows.length === 0 && (
                <tr>
                  <td className="text-muted py-4 text-center" colSpan={data.days.length + 1}>
                    В этом периоде у вас нет шифров. Если работали над шифром —
                    добавьте его кнопкой ниже.
                  </td>
                </tr>
              )}

              {view.rows.map((row) => (
                <tr key={row.mark_set_id}>
                  <th className="fact-matrix__code" scope="row">
                    <span className="font-monospace">{row.marka_full}</span>
                    {!row.allows_fact && (
                      <span className="text-muted small ms-2">закрыт</span>
                    )}
                    {row.is_added && (
                      <span className="text-primary small ms-2">добавлен</span>
                    )}
                    {!row.is_executor && !row.is_added && row.allows_fact && (
                      <span className="text-muted small ms-2">не мой</span>
                    )}
                  </th>

                  {data.days.map((day) => {
                    const key = cellKey(row.mark_set_id, day.work_date);
                    const cell = view.cells.get(key);
                    const clickable =
                      day.state === 'open' && row.allows_fact && !cell?.has_fact && !savingKey;
                    const classes = ['fact-matrix__cell', `fact-matrix__day--${day.state}`];
                    if (day.work_date === data.today) {
                      classes.push('fact-matrix__day--today');
                    }
                    if (clickable) {
                      classes.push('fact-matrix__cell--clickable');
                    }
                    if (savingKey === key) {
                      classes.push('fact-matrix__cell--saving');
                    }
                    const hint =
                      day.state === 'absence'
                        ? `Табель: ${day.tab_letter || 'неявка'}`
                        : !row.allows_fact
                          ? 'Шифр выпущен'
                          : cell?.has_fact
                            ? 'Отмечено'
                            : STATE_HINTS[day.state];

                    return (
                      <td
                        key={day.work_date}
                        className={classes.join(' ')}
                        title={hint}
                        onClick={clickable ? () => handleCellClick(row, day) : undefined}
                      >
                        {/* Верхняя позиция — план, нижняя — факт. Цвет
                            факта тот же, что в сетке начальника: зелёный
                            по плану, янтарный вне плана. */}
                        <div
                          className={`workload-bar${cell?.has_plan ? ' workload-bar--plan' : ' fact-matrix__slot'}`}
                        />
                        <div
                          className={`workload-bar${
                            savingKey === key
                              ? ' workload-bar--pending'
                              : cell?.has_fact
                                ? cell.has_plan
                                  ? ' workload-bar--fact'
                                  : ' workload-bar--offplan'
                                : ' fact-matrix__slot fact-matrix__slot--fact'
                          }`}
                        />
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="d-flex flex-wrap align-items-center justify-content-between gap-3 mt-3">
        {!searchOpen && (
          <button
            type="button"
            className="btn btn-sm btn-outline-primary"
            onClick={() => setSearchOpen(true)}
          >
            + добавить шифр
          </button>
        )}

        {/* ms-auto: легенда остаётся справа, даже когда кнопка скрыта
            (открыт поиск). flex-wrap: на узком экране переносится. */}
        <div className="fact-legend d-flex flex-wrap align-items-center gap-3 small text-muted ms-auto">
          <span className="d-inline-flex align-items-center gap-1">
            <span className="workload-bar workload-bar--plan fact-legend__bar" /> план
          </span>
          <span className="d-inline-flex align-items-center gap-1">
            <span className="workload-bar workload-bar--fact fact-legend__bar" /> факт по плану
          </span>
          <span className="d-inline-flex align-items-center gap-1">
            <span className="workload-bar workload-bar--offplan fact-legend__bar" /> факт вне плана
          </span>
          <span>Отметить можно сегодня и в предыдущий рабочий день</span>
        </div>
      </div>

      {searchOpen && (
        <MarkSetSearch
          existingIds={view.ids}
          onAdd={handleAdd}
          onClose={() => setSearchOpen(false)}
        />
      )}
    </div>
  );
}
