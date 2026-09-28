// app/frontend/src/components/CompositionCreate.jsx
// Родительский компонент страницы создания состава (вход B).
// Часть B.3.c — выбор проекта + защита от рассинхрона (Стратегия 1) +
// монтирование корзины CompositionEditor после выбора (композиция).
//
// Идея композиции: этот компонент выбирает ПРОЕКТ (по шифру через
// автодополнение), а затем рендерит внутри себя готовый CompositionEditor
// с выбранным projectId. CompositionEditor не переписан — ему всё равно,
// откуда пришёл projectId (из URL во входе A или отсюда во входе B).
import React, { useState, useEffect } from 'react';
import CompositionEditor from './CompositionEditor';

const API_BASE = '/api/composition';
const MIN_QUERY_LENGTH = 3;
const DEBOUNCE_MS = 300;

function CompositionCreate() {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState([]);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState(null);

  // Выбранный проект: null → режим выбора (поле+dropdown); объект
  // {project_id, code, title, has_composition} → режим редактирования
  // (корзина под полем). Это переключатель режима компонента.
  const [selectedProject, setSelectedProject] = useState(null);

  // --- Автодополнение с debounce ---
  // Не запускаем поиск, когда проект УЖЕ выбран (B3c-Q1): dropdown в этом
  // режиме скрыт, гонять запросы незачем.
  useEffect(() => {
    const normalized = query.trim();

    // Проект выбран ИЛИ запрос короткий → не ищем, чистим результаты.
    if (selectedProject !== null || normalized.length < MIN_QUERY_LENGTH) {
      setResults([]);
      setSearchError(null);
      setSearching(false);
      return;
    }

    const timer = setTimeout(async () => {
      setSearching(true);
      setSearchError(null);
      try {
        const resp = await fetch(
          `${API_BASE}/project-search?q=${encodeURIComponent(normalized)}`,
          { credentials: 'include' }
        );
        if (!resp.ok) {
          throw new Error(`Поиск вернул ${resp.status}`);
        }
        const data = await resp.json();
        setResults(data);
      } catch (err) {
        console.error('Автодополнение шифра проекта:', err);
        setSearchError('Ошибка поиска');
        setResults([]);
      } finally {
        setSearching(false);
      }
    }, DEBOUNCE_MS);

    return () => clearTimeout(timer);
    // selectedProject в зависимостях: при сбросе выбора (правка текста)
    // эффект перезапустится и поиск снова заработает.
  }, [query, selectedProject]);

  // --- Изменение текста в поле (управляемый инпут + Стратегия 1) ---
  // Любая правка текста после выбора СБРАСЫВАЕТ выбранный проект: иначе
  // поле показывало бы один шифр, а корзина грузила состав другого
  // (рассинхрон). Сброс → корзина закрывается, режим выбора возвращается.
  function handleQueryChange(e) {
    setQuery(e.target.value);
    if (selectedProject !== null) {
      setSelectedProject(null);
    }
  }

  // --- Выбор проекта из dropdown (B3c-Q4) ---
  // Клик по пункту: фиксируем проект, ставим его шифр в поле, скрываем
  // dropdown (он не показывается, пока selectedProject !== null).
  function handleSelect(project) {
    setSelectedProject(project);
    setQuery(project.code); // поле показывает выбранный шифр (B3-Q2)
    setResults([]);
  }

  // Dropdown виден, только когда проект НЕ выбран, запрос длинный, нет
  // загрузки/ошибки.
  const showDropdown =
    selectedProject === null &&
    query.trim().length >= MIN_QUERY_LENGTH &&
    !searching &&
    !searchError;

  return (
    <div>
      {/* --- Поле ввода шифра --- */}
      <div className="mb-3 position-relative">
        <label className="form-label">Шифр проекта</label>
        <input
          type="text"
          className="form-control"
          placeholder="Введите шифр проекта (например, 053)"
          value={query}
          onChange={handleQueryChange}
        />

        {searching && <small className="text-muted">Поиск…</small>}
        {searchError && <small className="text-danger">{searchError}</small>}

        {showDropdown && (
          <ul className="list-group position-absolute w-100 composition-dropdown">
            {results.length === 0 ? (
              <li className="list-group-item text-muted">Проекты не найдены</li>
            ) : (
              results.map((p) => (
                // Кликабельный пункт: выбор проекта.
                <li
                  key={p.project_id}
                  className="list-group-item list-group-item-action"
                  role="button"
                  onClick={() => handleSelect(p)}
                >
                  <strong>{p.code}</strong>{' '}
                  <span className="text-muted">{p.title}</span>{' '}
                  {p.has_composition && (
                    <span className="badge bg-info text-dark">состав есть</span>
                  )}
                </li>
              ))
            )}
          </ul>
        )}
      </div>

      {/* --- Корзина выбранного проекта (композиция) --- */}
      {/* Рендерится, только когда проект выбран. key={project_id}
          заставляет React ПЕРЕСОЗДАТЬ CompositionEditor при смене проекта
          (новый key = новый экземпляр = свежая загрузка состава), а не
          переиспользовать старый со старыми данными (B3c-Q3). */}
      {selectedProject !== null && (
        <div>
          <div className="alert alert-secondary py-2">
            Состав проекта <strong>{selectedProject.code}</strong>
            {' — '}
            {selectedProject.title}
          </div>
          <CompositionEditor
            key={selectedProject.project_id}
            projectId={selectedProject.project_id}
          />
        </div>
      )}
    </div>
  );
}

export default CompositionCreate;