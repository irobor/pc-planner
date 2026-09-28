// app/frontend/src/api/planning.js
// Обращения к JSON-API планирования. Вынесены из компонента, чтобы он
// занимался отрисовкой, а не формой запросов: URL и разбор ошибок —
// единственное, что придётся менять при правках контракта.

// Базовый префикс. Версии в адресе нет — так же, как у состава
// (/api/composition): api_router подключается с prefix="/api", а сам
// v1-роутер версию не добавляет.
const BASE = '/api/planning';

/**
 * Разбирает ответ сервера, приводя ошибку к единому виду.
 *
 * Тело ошибки у FastAPI лежит в detail, но его форма разная: строка для
 * 403/404/422 и объект {message, rejected} для 409. Компоненту удобнее
 * получать одинаковый Error с полем rejected — пустым, когда отказов
 * по операциям нет.
 */
async function parseResponse(response) {
  if (response.ok) {
    return response.json();
  }

  let detail = null;
  try {
    const body = await response.json();
    detail = body.detail;
  } catch {
    // Тело не JSON (например, HTML-страница 500). Оставляем detail = null
    // и покажем общий текст: разбирать вёрстку ради сообщения незачем.
  }

  const error = new Error(
    typeof detail === 'string'
      ? detail
      : detail?.message || 'Не удалось выполнить запрос'
  );
  error.status = response.status;
  // Список непрошедших операций — только у 409. Всегда массив, чтобы
  // вызывающий код не проверял наличие поля.
  error.rejected = Array.isArray(detail?.rejected) ? detail.rejected : [];
  throw error;
}

/**
 * Читает сетку загрузки подразделения на интервал.
 *
 * Даты — строки ISO (ГГГГ-ММ-ДД). Границы включительные с обеих сторон,
 * как и везде в планировании.
 */
export async function fetchWorkload(departmentId, dateFrom, dateTo) {
  const params = new URLSearchParams({
    department_id: String(departmentId),
    date_from: dateFrom,
    date_to: dateTo,
  });

  // credentials: токен лежит в cookie access_token, которую ставит форма
  // входа. same-origin достаточно — фронт и API на одном домене.
  const response = await fetch(`${BASE}/workload?${params}`, {
    credentials: 'same-origin',
    headers: { Accept: 'application/json' },
  });
  return parseResponse(response);
}

/**
 * Применяет пачку изменений плановых ячеек.
 *
 * operations — массив { op: 'add' | 'remove', mark_set_id, work_date }.
 * Исполнитель не передаётся: сервер выводит его из шифра.
 *
 * Правило «всё или ничего»: при отказе не применяется ничего, и Error
 * несёт rejected со списком причин.
 */
export async function applyCells(operations) {
  const response = await fetch(`${BASE}/cells`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'application/json',
    },
    body: JSON.stringify({ operations }),
  });
  return parseResponse(response);
}
