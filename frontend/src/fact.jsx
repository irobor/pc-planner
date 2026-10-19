// app/frontend/src/fact.jsx
// Точка входа React-острова «Отметка факта» на главной (аналог
// workload.jsx). Собирается Vite в fact.bundle.js (vite.config.js,
// вход fact).
import React from 'react';
import ReactDOM from 'react-dom/client';

import FactMatrix from './components/FactMatrix';

// Монтажный узел задан в шаблоне index.html: <div id="fact-root">.
const rootNode = document.getElementById('fact-root');

// Монтируемся только если узел на странице есть. Шаблон выводит его не
// всем: без права fact:own или без кадровой карточки блока нет, а
// createRoot(null) упал бы.
//
// data-атрибутов нет намеренно: остров не нуждается в начальных
// параметрах. Инженера сервер берёт из входа, текущий месяц и
// «сегодня» отдаёт сам в первом ответе GET /api/fact/month.
if (rootNode) {
  ReactDOM.createRoot(rootNode).render(
    <React.StrictMode>
      <FactMatrix />
    </React.StrictMode>
  );
}
