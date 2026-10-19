// app/frontend/src/api/fact.js
// Обращения к JSON-API факта (app/api/v1/fact/routes.py). Вынесены из
// компонентов, чтобы те занимались отрисовкой: URL и форма запросов —
// единственное, что придётся менять при правках контракта.
//
// Инженер нигде не передаётся: сервер берёт его из cookie входа.
import { parseResponse } from './http';

const BASE = '/api/fact';

// Общие параметры fetch. credentials: токен лежит в cookie access_token,
// которую ставит форма входа; same-origin достаточно — фронт и API на
// одном адресе.
const JSON_HEADERS = { Accept: 'application/json' };

/**
 * Лента инженера от якорного дня: 14 дней до и 28 после.
 *
 * anchor — строка ISO (ГГГГ-ММ-ДД) ровно в том виде, в каком её отдал
 * сервер (prev_anchor / next_anchor прошлого ответа). Без неё сервер
 * строит ленту от сегодня: острову при первой загрузке не нужно знать
 * дату, а браузер вообще не складывает даты (ловушка toISOString при
 * UTC+3).
 */
export async function fetchFactWindow(anchor) {
  const params = new URLSearchParams();
  if (anchor) {
    params.set('anchor', anchor);
  }
  const query = params.toString();
  const response = await fetch(`${BASE}/window${query ? `?${query}` : ''}`, {
    credentials: 'same-origin',
    headers: JSON_HEADERS,
  });
  return parseResponse(response);
}

/**
 * Поиск шифра для «+ добавить шифр».
 *
 * signal — от AbortController: при быстром наборе предыдущий запрос
 * отменяется, иначе медленный ответ на «053-2» мог бы прийти ПОСЛЕ
 * ответа на «053-22» и затереть правильную выдачу. Отменённый запрос
 * бросает AbortError — вызывающий код его просто игнорирует.
 */
export async function searchMarkSets(query, signal) {
  const params = new URLSearchParams({ q: query });
  const response = await fetch(`${BASE}/mark-sets?${params}`, {
    credentials: 'same-origin',
    headers: JSON_HEADERS,
    signal,
  });
  return parseResponse(response);
}

/**
 * Отметка одного дня: создать, перенести или ничего (свой же шифр).
 *
 * workDate — строка ISO (ГГГГ-ММ-ДД) ровно в том виде, в каком пришла
 * от сервера в days[].work_date. В Date не превращаем: при UTC+3
 * toISOString() сдвинул бы дату на сутки назад.
 *
 * Ответ — свежее состояние дня: { action, day, cells, row }.
 */
export async function postMark(markSetId, workDate) {
  const response = await fetch(`${BASE}/mark`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { ...JSON_HEADERS, 'Content-Type': 'application/json' },
    body: JSON.stringify({ mark_set_id: markSetId, work_date: workDate }),
  });
  return parseResponse(response);
}
