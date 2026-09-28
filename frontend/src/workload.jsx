// app/frontend/src/workload.jsx
// Точка входа React-острова «Загрузка бюро» (аналог composition.jsx).
// Собирается Vite в workload.bundle.js (vite.config.js, вход workload).
import React from 'react';
import ReactDOM from 'react-dom/client';

import WorkloadGrid from './components/WorkloadGrid';

// Монтажный узел задан в шаблоне planner/workload.html:
// <div id="workload-root">.
const rootNode = document.getElementById('workload-root');

// Монтируемся только если узел на странице есть: бандл может случайно
// подключиться к странице без нужного div, и createRoot(null) упал бы.
if (rootNode) {
  // Серверные данные приезжают через data-атрибуты. dataset всегда
  // отдаёт СТРОКУ ("7", не 7), поэтому id сразу приводим к числу: при
  // отсутствии атрибута получим NaN — заметный сигнал ошибки настройки,
  // а не тихий мусор.
  const departmentId = Number(rootNode.dataset.departmentId);

  // Дату оставляем строкой ISO: в таком виде она уходит в API и
  // сравнивается с датами ответа. Преобразование в Date и обратно
  // добавило бы риск уехать на день из-за часового пояса.
  //
  // Считает её сервер (date.today()), а не браузер: пояс машины
  // пользователя может отличаться от рабочего.
  const today = rootNode.dataset.today;
    // dataset отдаёт строку, поэтому сравниваем с "true", а не приводим
  // к булеву: непустая строка "false" стала бы истиной.
  const canEdit = rootNode.dataset.canEdit === 'true';

  ReactDOM.createRoot(rootNode).render(
    <React.StrictMode>
      <WorkloadGrid
        departmentId={departmentId}
        today={today}
        canEdit={canEdit}
      />
    </React.StrictMode>
    
  );
}
