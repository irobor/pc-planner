// app/frontend/src/api/http.js
// Общий разбор ответов JSON-API для всех островков.
//
// Вынесен из planning.js, когда появился второй клиент (fact.js): разбор
// ошибок FastAPI должен быть записан в одном месте, иначе две копии
// однажды разойдутся, и одна страница покажет текст сервера, а другая —
// «Не удалось выполнить запрос».

/**
 * Разбирает ответ сервера, приводя ошибку к единому виду.
 *
 * Тело ошибки у FastAPI лежит в detail, но его форма разная: строка для
 * 403/404/409/422 из наших роутов, объект {message, rejected} для 409
 * планирования, массив для 422 валидации FastAPI. Вызывающему коду удобнее
 * получать одинаковый Error с полями status и rejected.
 */
export async function parseResponse(response) {
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
  // Список непрошедших операций — только у 409 планирования. Всегда
  // массив, чтобы вызывающий код не проверял наличие поля.
  error.rejected = Array.isArray(detail?.rejected) ? detail.rejected : [];
  throw error;
}