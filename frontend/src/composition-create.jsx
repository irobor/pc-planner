// app/frontend/src/composition-create.jsx
// Точка входа React-острова «Создание состава проекта» (вход B).
// Собирается Vite в composition-create.bundle.js (vite.config.js,
// вход composition-create).
//
// Отличие от composition.jsx (редактор): project_id НЕ читаем — на
// странице создания проект ещё не выбран, id появится в React после
// ввода шифра и выбора из автодополнения.
import React from 'react';
import ReactDOM from 'react-dom/client';
import CompositionCreate from './components/CompositionCreate';

// Монтажный узел из create.html: <div id="composition-create-root">.
// Свой id (не composition-root) — отличает точку монтирования создания
// от редактора.
const rootNode = document.getElementById('composition-create-root');

if (rootNode) {
  ReactDOM.createRoot(rootNode).render(
    <React.StrictMode>
      <CompositionCreate />
    </React.StrictMode>
  );
}