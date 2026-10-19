// app/frontend/src/components/ChangesetJournal.jsx
// Журнал правок администратора по одному сотруднику — под сеткой.
//
// Устроен как история коммитов: правка — сообщение (кто, когда,
// комментарий), раскрывается в diff — строки «было → стало». Только
// чтение: журнал объясняет данные, а не меняет их.
//
// Хранится diff по дням, а показывается сжато: одинаковые изменения
// подряд идущих дней сворачиваются в диапазон — «план 12–14.10: пусто →
// 053-ЭМ#1» вместо трёх строк. Это вопрос отображения, не хранения.
import React, { useEffect, useState } from 'react';

import { fetchChangesets } from '../api/admin';

const LAYER_NAMES = { plan: 'план', fact: 'факт' };

/** «16.10.2026 16:12» — местное время из момента UTC. */
function formatMoment(isoMoment) {
  // Для МОМЕНТА времени перевод в местный пояс — то, что нужно:
  // created_at приходит в UTC («…Z»), браузер покажет время по UTC+3.
  // Ловушка toISOString касается только дат без времени.
  return new Date(isoMoment).toLocaleString('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

/** «12.10» из «2026-10-12». */
const dayMonth = (iso) => `${iso.slice(8, 10)}.${iso.slice(5, 7)}`;

/** Следующий календарный день ISO-даты — из местных компонентов. */
function nextIso(iso) {
  const [year, month, day] = iso.split('-').map(Number);
  const value = new Date(year, month - 1, day + 1);
  return [
    value.getFullYear(),
    String(value.getMonth() + 1).padStart(2, '0'),
    String(value.getDate()).padStart(2, '0'),
  ].join('-');
}

/**
 * Дни в компактную строку: подряд идущие — диапазоном.
 * ['2026-10-12','2026-10-13','2026-10-14','2026-10-16'] → «12–14.10, 16.10».
 * Выходной разрывает диапазон: «12–16.10» не должно скрывать, что 14-го
 * изменения не было.
 */
function formatDays(dates) {
  const sorted = [...dates].sort();
  const parts = [];
  let start = sorted[0];
  let end = sorted[0];
  for (const iso of sorted.slice(1)) {
    if (iso === nextIso(end)) {
      end = iso;
      continue;
    }
    parts.push(start === end ? dayMonth(start) : `${dayMonth(start)}–${dayMonth(end)}`);
    start = iso;
    end = iso;
  }
  parts.push(start === end ? dayMonth(start) : `${dayMonth(start)}–${dayMonth(end)}`);
  return parts.join(', ');
}

/**
 * Сворачивает строки diff: одинаковые (полоса, было, стало) — в одну
 * строку со списком дней. Порядок — по первому появлению, то есть по
 * дате, план раньше факта (так их отдаёт сервер).
 */
function groupItems(items) {
  const groups = new Map();
  for (const item of items) {
    const key = `${item.layer}|${item.old_marka ?? ''}|${item.new_marka ?? ''}`;
    if (!groups.has(key)) {
      groups.set(key, { ...item, dates: [] });
    }
    groups.get(key).dates.push(item.work_date);
  }
  return [...groups.values()];
}

/** Шифр или «пусто» — пустая ячейка в diff должна быть видна словом. */
const markaOrEmpty = (marka) =>
  marka ? <code>{marka}</code> : <span className="text-muted">пусто</span>;

/**
 * Props:
 *   employeeId — чей журнал показывать;
 *   reloadKey  — меняется после сохранения правки: журнал перечитывается.
 */
export default function ChangesetJournal({ employeeId, reloadKey }) {
  const [changesets, setChangesets] = useState(null);
  const [error, setError] = useState('');
  // Раскрытые правки. Свежая правка раскрыта сразу после сохранения —
  // видно, что именно записалось.
  const [expanded, setExpanded] = useState(() => new Set());

  useEffect(() => {
    let cancelled = false;
    setError('');
    fetchChangesets(employeeId)
      .then((data) => {
        if (cancelled) {
          return;
        }
        setChangesets(data);
        // После сохранения (reloadKey > 0) раскрываем самую свежую.
        setExpanded(reloadKey > 0 && data.length > 0 ? new Set([data[0].id]) : new Set());
      })
      .catch((exc) => {
        if (!cancelled) {
          setError(exc.message);
        }
      });
    // Отмена: при быстрой смене сотрудника ответ по прежнему не должен
    // перезаписать журнал нового.
    return () => {
      cancelled = true;
    };
  }, [employeeId, reloadKey]);

  function toggle(id) {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }

  return (
    <section className="mt-4">
      <h2 className="h6 mb-2">Журнал правок</h2>

      {error && <div className="alert alert-danger py-2">{error}</div>}
      {!error && changesets === null && <div className="text-muted small">Загрузка…</div>}
      {changesets?.length === 0 && (
        <div className="text-muted small">Правок по этому сотруднику пока нет.</div>
      )}

      {changesets?.length > 0 && (
        <ul className="list-group">
          {changesets.map((cs) => {
            const isOpen = expanded.has(cs.id);
            return (
              <li key={cs.id} className="list-group-item">
                {/* Заголовок правки — кнопка: раскрыть / свернуть diff. */}
                <button
                  type="button"
                  className="btn btn-link p-0 text-start text-decoration-none text-body w-100"
                  onClick={() => toggle(cs.id)}
                  aria-expanded={isOpen}
                >
                  <span className="text-muted small me-2">{isOpen ? '▾' : '▸'}</span>
                  <span className="fw-semibold me-2">№{cs.id}</span>
                  <span className="text-muted small me-2">
                    {formatMoment(cs.created_at)} · {cs.changed_by}
                  </span>
                  <span>«{cs.comment}»</span>
                  <span className="text-muted small ms-2">
                    изменений: {cs.items.length}
                  </span>
                </button>

                {isOpen && (
                  <ul className="list-unstyled small mt-2 mb-0 ms-4">
                    {groupItems(cs.items).map((group) => (
                      <li key={`${group.layer}|${group.dates[0]}|${group.new_marka}`}>
                        <span className="text-muted">
                          {LAYER_NAMES[group.layer]} {formatDays(group.dates)}:
                        </span>{' '}
                        {markaOrEmpty(group.old_marka)} → {markaOrEmpty(group.new_marka)}
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
