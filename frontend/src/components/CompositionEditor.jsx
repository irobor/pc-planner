// app/frontend/src/components/CompositionEditor.jsx
// Компонент-«корзина» редактора состава проекта.
// Часть 2.3.3.e — удаление (крестик + Защита A на UI) и сохранение (POST).
// После этой части корзина работает сквозь: загрузка → правка → синхронизация.
import React, { useState, useEffect } from 'react';

const API_BASE = '/api/composition';

// Составной ключ строки состава: марка + отдел. Одна марка может идти под
// несколькими отделами (многоотдельность, напр. ТР·ГЕНП и ТР·СТР), поэтому
// идентичность строки — ПАРА, а не marka_type_id. Две формы данных требуют
// двух хелперов: /refs отдаёт отдел вложенно (m.department.id), строки
// корзины — плоско (row.department_id).
const refKey = (m) => `${m.marka_type_id}:${m.department.id}`;
const rowKey = (row) => `${row.marka_type_id}:${row.department_id}`;

function CompositionEditor({ projectId }) {
  // --- Состояние ---
  const [refs, setRefs] = useState([]);
  const [selected, setSelected] = useState([]);
  const [query, setQuery] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // Флаг «идёт сохранение»: блокирует кнопку и показывает «Сохранение…»,
  // чтобы двойной клик не отправил POST дважды.
  const [saving, setSaving] = useState(false);

  // Сообщение о результате сохранения (успех или ошибка POST). null —
  // ничего не показываем. Отделено от error (ошибка ЗАГРУЗКИ), т.к. это
  // разные стадии с разной судьбой UI.
  const [saveMessage, setSaveMessage] = useState(null);

  // --- Загрузка данных ---
  useEffect(() => {
    async function loadData() {
      setError(null);
      setLoading(true);
      try {
        const [refsResp, compResp] = await Promise.all([
          fetch(`${API_BASE}/refs`, { credentials: 'include' }),
          fetch(`${API_BASE}/${projectId}`, { credentials: 'include' }),
        ]);
        if (!refsResp.ok || !compResp.ok) {
          throw new Error(
            `Ошибка загрузки: refs=${refsResp.status}, ` +
            `composition=${compResp.status}`
          );
        }
        const refsData = await refsResp.json();
        const compData = await compResp.json();
        setRefs(refsData);
        setSelected(compData);
      } catch (err) {
        console.error('Загрузка данных редактора состава:', err);
        setError('Не удалось загрузить данные. Попробуйте обновить страницу.');
      } finally {
        setLoading(false);
      }
    }
    loadData();
  }, [projectId]);
  
  // --- Производные данные ---
  // Множество ключей ПАР уже в корзине — по нему dropdown исключает
  // выбранное. Ключ составной: одна марка под разными отделами = разные
  // строки, поэтому ТР·ГЕНП в корзине НЕ прячет ТР·СТР из dropdown.
  const selectedKeys = new Set(selected.map(rowKey));
  const q = query.trim().toLowerCase();
  const dropdownItems = q === ''
    ? []
    : refs.filter(
        (m) =>
          m.code.toLowerCase().startsWith(q) &&
          !selectedKeys.has(refKey(m))
      );

  // --- Добавление марки ---
  function handleAdd(marka) {
    const newRow = {
      marka_type_id: marka.marka_type_id,
      marka_code: marka.code,
      marka_name: marka.name,
      stage_id: marka.stage.id,
      stage_name: marka.stage.stage_name,
      department_id: marka.department.id,
      department_name: marka.department.name,
      has_mark_sets: false,
      composition_id: null,
    };
    setSelected([...selected, newRow]);
    setQuery('');
  }

  // --- Удаление марки ---
    // Иммутабельно через filter: новый массив без строки с ЭТОЙ парой
    // (марка+отдел). Фильтр по паре, а не по marka_type_id: иначе удаление
    // ТР·ГЕНП снесло бы и ТР·СТР (та же марка, другой отдел). Защита A
    // (has_mark_sets) проверяется на кнопке (disabled) — сюда для таких строк
    // клик не дойдёт.
    function handleRemove(markaTypeId, departmentId) {
      setSelected(
        selected.filter(
          (row) =>
            !(
              row.marka_type_id === markaTypeId &&
              row.department_id === departmentId
            )
        )
      );
    }

  // --- Сохранение (POST-синхронизация) ---
  async function handleSave() {
    // Confirm перед записью (E3). Отказ — выходим, ничего не делаем.
    if (!window.confirm('Сохранить состав?')) {
      return;
    }

    setSaving(true);
    setSaveMessage(null);

    // Payload-минимум: пары (марка, отдел) из корзины. Стадию сервер выведет
    // сам. Дубли пар уберёт схема (их и не должно быть — dropdown исключает
    // уже выбранные пары).
    const payload = {
      items: selected.map((row) => ({
        marka_type_id: row.marka_type_id,
        department_id: row.department_id,
      })),
    };

    try {
      const resp = await fetch(`${API_BASE}/${projectId}`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });

      // fetch не бросает на HTTP-ошибках — читаем тело и ветвим по статусу.
      const data = await resp.json();

      if (!resp.ok) {
        // Сервер вернул 4xx/5xx. detail нейтрален и на русском (заложено
        // в бэкенде, правило 18) — показываем его пользователю (E2).
        // Пример: 409 → «Нельзя удалить марки с комплектами».
        setSaveMessage({
          type: 'error',
          text: data.detail || 'Ошибка сохранения состава.',
        });
        return;
      }

      // Успех: сервер вернул обновлённый состав. Заменяем им selected
      // (E1-а) — корзина синхронизируется с БД: новые марки получают
      // реальные composition_id, has_mark_sets становится актуальным.
      setSelected(data);
      setSaveMessage({ type: 'success', text: 'Состав сохранён.' });
    } catch (err) {
      // Сетевой сбой (не HTTP-ошибка — её поймали выше через resp.ok).
      console.error('Сохранение состава:', err);
      setSaveMessage({
        type: 'error',
        text: 'Сетевая ошибка. Проверьте соединение и повторите.',
      });
    } finally {
      setSaving(false);
    }
  }

  // --- Рендер ---
  if (loading) {
    return <div className="text-muted py-4">Загрузка данных…</div>;
  }
  if (error) {
    return <div className="alert alert-danger">{error}</div>;
  }

  return (
    <div>
      <h2 className="h5 mb-3">Состав проекта</h2>

      {/* --- Поле ввода + dropdown --- */}
      <div className="mb-4 position-relative">
        <label className="form-label">Добавить марку</label>
        <input
          type="text"
          className="form-control"
          placeholder="Введите код марки (например, АР)"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        {dropdownItems.length > 0 && (
          <ul className="list-group position-absolute w-100 composition-dropdown">
            {dropdownItems.map((m) => (
              <li
                key={refKey(m)}
                className="list-group-item list-group-item-action"
                role="button"
                onClick={() => handleAdd(m)}
              >
                <strong>{m.code}</strong>{' '}
                <span className="text-muted">{m.name}</span>{' '}
                <span className="badge bg-secondary">{m.stage.stage_name}</span>{' '}
                <span className="text-muted small">· {m.department.name}</span>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* --- Превью выбранного состава --- */}
      {selected.length === 0 ? (
        <p className="text-muted">Состав пуст. Добавьте марки через поле выше.</p>
      ) : (
        <ul className="list-group mb-3">
          {selected.map((row) => (
            <li
              key={rowKey(row)}
              className="list-group-item d-flex justify-content-between align-items-center"
            >
              <span>
                <strong>{row.marka_code}</strong>{' '}
                <span className="text-muted">{row.marka_name}</span>{' '}
                <span className="badge bg-secondary">{row.stage_name}</span>{' '}
                <span className="text-muted small">· {row.department_name}</span>
                {row.has_mark_sets && (
                  <span className="badge bg-warning text-dark ms-2">
                    есть комплекты
                  </span>
                )}
              </span>

              {/* Крестик удаления. Защита A на UI: у строки с комплектами
                  кнопка disabled — удалить нельзя (каскад снёс бы работу
                  бюро). title поясняет, почему заблокировано. */}
              <button
                type="button"
                className="btn btn-sm btn-outline-danger"
                onClick={() => handleRemove(row.marka_type_id, row.department_id)}
                disabled={row.has_mark_sets}
                title={
                  row.has_mark_sets
                    ? 'Нельзя удалить: под маркой есть комплекты'
                    : 'Удалить марку из состава'
                }
              >
                &times;
              </button>
            </li>
          ))}
        </ul>
      )}

      {/* --- Сообщение о результате сохранения --- */}
      {saveMessage && (
        <div
          className={
            saveMessage.type === 'success'
              ? 'alert alert-success'
              : 'alert alert-danger'
          }
        >
          {saveMessage.text}
        </div>
      )}

      {/* --- Кнопка сохранения --- */}
      {/* disabled во время сохранения — защита от двойного POST. */}
      <button
        type="button"
        className="btn btn-primary"
        onClick={handleSave}
        disabled={saving}
      >
        {saving ? 'Сохранение…' : 'Сохранить'}
      </button>
    </div>
  );
}

export default CompositionEditor;