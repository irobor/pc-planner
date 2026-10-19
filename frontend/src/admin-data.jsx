// app/frontend/src/admin-data.jsx
// Точка входа React-острова «План и факт сотрудников» в разделе
// «Администрирование» (/admin/data). Собирается Vite в
// admin-data.bundle.js (vite.config.js, вход admin-data).
import React from 'react';
import ReactDOM from 'react-dom/client';

import AdminGrid from './components/AdminGrid';

// Монтажный узел задан в шаблоне admin/data.html.
const rootNode = document.getElementById('admin-data-root');

// Монтируемся только если узел на странице есть: createRoot(null) упал
// бы. data-атрибутов нет: сотрудника выбирают на экране, а всё прочее
// остров берёт сам через /api/admin/*.
if (rootNode) {
  ReactDOM.createRoot(rootNode).render(
    <React.StrictMode>
      <AdminGrid />
    </React.StrictMode>
  );
}
