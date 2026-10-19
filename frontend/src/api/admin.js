// app/frontend/src/api/admin.js
// Обращения к JSON-API администратора данных (app/api/v1/admin/routes.py).
// Вынесены из компонентов, чтобы те занимались отрисовкой: URL и форма
// запросов — единственное, что придётся менять при правках контракта.
//
// Все ручки требуют права plan:override и fact:override; токен лежит в
// cookie access_token, которую ставит форма входа.
import { parseResponse } from './http';

const BASE = '/api/admin';

const JSON_HEADERS = { Accept: 'application/json' };

/** GET с параметрами — общий вид для всех читающих ручек. */
async function getJson(path, params = {}, signal = undefined) {
  const query = new URLSearchParams(params).toString();
  const response = await fetch(`${BASE}${path}${query ? `?${query}` : ''}`, {
    credentials: 'same-origin',
    headers: JSON_HEADERS,
    signal,
  });
  return parseResponse(response);
}

/**
 * Список сотрудников для выбора: [{ id, full_name, department_code }].
 *
 * Только имена — ни шифров, ни ячеек. Полный список: фильтр по вводу
 * делается на клиенте.
 */
export function fetchEmployees() {
  return getJson('/employees');
}

/**
 * Сетка одного сотрудника на интервал.
 *
 * dateFrom / dateTo — строки ISO (ГГГГ-ММ-ДД), границы включительные,
 * интервал не шире 120 дней. Ответ: { employee_id, full_name, date_from,
 * date_to, today, horizon_from, horizon_to, rows, days, cells }.
 *
 * days[].plan_allowed / fact_allowed — можно ли ПОСТАВИТЬ значение в
 * полосу. Очистить ячейку можно всегда.
 */
export function fetchAdminGrid(employeeId, dateFrom, dateTo) {
  return getJson('/grid', {
    employee_id: String(employeeId),
    date_from: dateFrom,
    date_to: dateTo,
  });
}

/**
 * Поиск шифра ЛЮБОГО статуса (включая выпущенные) для «+ добавить шифр».
 *
 * signal — от AbortController: при быстром наборе устаревший запрос
 * отменяется, иначе медленный ответ затёр бы свежую выдачу. Отменённый
 * запрос бросает AbortError — вызывающий код его игнорирует.
 */
export function searchAdminMarkSets(query, signal) {
  return getJson('/mark-sets', { q: query }, signal);
}

/**
 * Сохраняет пачку правок одного сотрудника.
 *
 * cells — ЖЕЛАЕМОЕ ИТОГОВОЕ СОСТОЯНИЕ затронутых ячеек:
 *   [{ layer: 'plan' | 'fact', work_date: 'ГГГГ-ММ-ДД', mark_set_id | null }]
 * null — очистить ячейку. «Было» сервер берёт из базы сам.
 *
 * comment — обязательный комментарий к правке (как сообщение коммита).
 *
 * Ответ: { changeset_id, applied }. changeset_id = null — в итоге ничего
 * не изменилось. При 409 Error несёт rejected — причины по ячейкам.
 */
export async function saveAdminChanges(employeeId, comment, cells) {
  const response = await fetch(`${BASE}/changes`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { ...JSON_HEADERS, 'Content-Type': 'application/json' },
    body: JSON.stringify({ employee_id: employeeId, comment, cells }),
  });
  return parseResponse(response);
}

/**
 * Журнал правок сотрудника, свежие сверху:
 *   [{ id, created_at, changed_by, comment, items: [...] }]
 *
 * created_at — момент времени в UTC (ISO с «Z»). Перевод в местное время
 * делает браузер (toLocaleString) — для МОМЕНТОВ это правильно; ловушка
 * toISOString касается только дат без времени.
 */
export function fetchChangesets(employeeId) {
  return getJson('/changesets', { employee_id: String(employeeId) });
}