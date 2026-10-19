// app/frontend/src/components/MarkSetSearch.jsx
// Панель «+ добавить шифр»: поиск шифра, которого нет в матрице.
//
// Случай из жизни: инженер работал над чужим комплектом (подмена,
// помощь соседнему бюро), а в его матрице такой строки нет. Найти шифр
// должно быть проще, чем отметить «ближайший свой» — иначе факт соврёт.
//
// Панель только НАХОДИТ шифр и отдаёт его наружу через onAdd. Строка,
// добавленная так, живёт в браузере до первой отметки: сервер о ней не
// знает, и хранить её незачем — пустая добавленная строка ничего не
// значит. После отметки она приходит с сервера как обычная.
//
// Порядок выдачи, предел 20 и запрет выпущенных шифров решает сервер.
// Здесь — только удобство набора: задержка, отмена устаревших запросов.
import React, { useEffect, useRef, useState } from 'react';

import { searchMarkSets } from '../api/fact';

// Минимальная длина строки поиска. Дублирует SEARCH_MIN_LENGTH сервиса
// ради удобства: короткую строку незачем отправлять, сервер ответил бы
// 422. Решает всё равно сервер — если константы разойдутся, отказ
// придёт оттуда и покажется текстом.
const MIN_LENGTH = 5;

// Пауза после последнего нажатия клавиши, прежде чем искать. Без неё
// каждая буква уходила бы отдельным запросом.
const DEBOUNCE_MS = 300;

/**
 * Панель поиска шифра.
 *
 * Props:
 *   existingIds — Set id шифров, уже стоящих в матрице: для них вместо
 *                 кнопки «Добавить» пишем «Уже в матрице»;
 *   onAdd(item) — выбран шифр (элемент выдачи MarkSetFound);
 *   onClose()   — панель закрыть (кнопка «×» или Esc);
 *   search      — функция поиска (query, signal) → Promise<MarkSetFound[]>.
 *                 По умолчанию — поиск инженера (без выпущенных шифров);
 *                 экран администратора передаёт свой (любой статус);
 *   emptyText   — подпись при пустой выдаче;
 *   hintText    — подсказка под списком.
 */
export default function MarkSetSearch({
  existingIds,
  onAdd,
  onClose,
  search = searchMarkSets,
  emptyText = 'Ничего не найдено. Выпущенные шифры не показываются.',
  hintText = 'Сначала шифры вашего бюро, затем отдела, затем остальной организации.',
}) {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState([]);
  // idle — ещё не искали или строка короткая; loading — запрос в пути;
  // done — ответ получен (в том числе пустой); error — отказ сервера.
  const [status, setStatus] = useState('idle');
  const [errorText, setErrorText] = useState('');

  const inputRef = useRef(null);

  // Фокус в поле сразу при открытии: инженер нажал «+ добавить шифр»
  // ради того, чтобы печатать, лишний клик в поле ни к чему.
  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  // Поиск с задержкой и отменой устаревшего запроса.
  //
  // Каждый запуск эффекта (новая буква) создаёт свой AbortController, а
  // очистка эффекта (следующая буква или закрытие панели) отменяет и
  // таймер, и запрос. Так медленный ответ на «053-2» не придёт поверх
  // уже показанного ответа на «053-22».
  useEffect(() => {
    const text = query.trim();
    if (text.length < MIN_LENGTH) {
      setResults([]);
      setStatus('idle');
      return undefined;
    }

    const controller = new AbortController();
    const timer = setTimeout(async () => {
      setStatus('loading');
      try {
        const found = await search(text, controller.signal);
        setResults(found);
        setStatus('done');
      } catch (error) {
        // Отменённый запрос — не ошибка: его заменил более свежий.
        if (error.name === 'AbortError') {
          return;
        }
        setResults([]);
        setErrorText(error.message);
        setStatus('error');
      }
    }, DEBOUNCE_MS);

    return () => {
      clearTimeout(timer);
      controller.abort();
    };
    // search в зависимостях: функцию передаёт родитель, и при её смене
    // поиск должен перезапуститься. На практике она постоянна.
  }, [query, search]);

  // Esc закрывает панель — привычное поведение для всплывающих панелей.
  function handleKeyDown(event) {
    if (event.key === 'Escape') {
      onClose();
    }
  }

  const tooShort = query.trim().length < MIN_LENGTH;

  return (
    <div className="card border-primary mt-3">
      <div className="card-body">
        <div className="d-flex align-items-center justify-content-between mb-2">
          <label htmlFor="fact-search-input" className="form-label mb-0 text-muted">
            Шифр, которого нет в матрице
          </label>
          <button
            type="button"
            className="btn-close"
            aria-label="Закрыть поиск"
            onClick={onClose}
          />
        </div>

        <input
          id="fact-search-input"
          ref={inputRef}
          type="search"
          className="form-control font-monospace fact-search__input"
          placeholder="Например, 053-22-14"
          autoComplete="off"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={handleKeyDown}
        />

        {/* Подсказка под полем меняется по состоянию: так пользователь
            всегда понимает, почему список пуст. */}
        <div className="form-text">
          {tooShort && `Введите не меньше ${MIN_LENGTH} символов шифра.`}
          {!tooShort && status === 'loading' && 'Поиск…'}
          {!tooShort && status === 'done' && results.length === 0 && emptyText}
          {!tooShort && status === 'error' && (
            <span className="text-danger">{errorText}</span>
          )}
        </div>

        {results.length > 0 && (
          <ul className="list-group mt-2">
            {results.map((item) => {
              const inMatrix = existingIds.has(item.id);
              return (
                <li
                  key={item.id}
                  className="list-group-item d-flex align-items-center justify-content-between gap-3"
                >
                  <div className="text-truncate">
                    <span className="font-monospace fw-semibold">
                      {item.marka_full}
                    </span>
                    <span className="text-muted small ms-2">
                      {item.department_code} · {item.department_name}
                    </span>
                  </div>
                  {inMatrix ? (
                    <span className="text-muted small text-nowrap">
                      Уже в матрице
                    </span>
                  ) : (
                    <button
                      type="button"
                      className="btn btn-sm btn-outline-primary text-nowrap"
                      onClick={() => onAdd(item)}
                    >
                      Добавить строкой
                    </button>
                  )}
                </li>
              );
            })}
          </ul>
        )}

        <div className="form-text mt-2">{hintText}</div>
      </div>
    </div>
  );
}
