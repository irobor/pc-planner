// app/frontend/src/composition.jsx
// Точка входа React-острова «Состав проекта» (аналог planner.jsx).
// Собирается Vite в composition.bundle.js (vite.config.js, вход composition).
import React from 'react';
import ReactDOM from 'react-dom/client';
import CompositionEditor from './components/CompositionEditor';

// Монтажный узел задан в шаблоне edit.html: <div id="composition-root">.
const rootNode = document.getElementById('composition-root');

// Защита: монтируемся только если узел на странице есть. Тот же приём,
// что в planner.jsx — бандл может случайно подключиться к странице без
// нужного div, и тогда getElementById вернёт null. Без проверки
// createRoot(null) упал бы с ошибкой.
if (rootNode) {
  // Читаем project_id, который сервер положил в data-атрибут
  // (edit.html: data-project-id="{{ project.id }}"). Это способ
  // прокинуть серверные данные в React-остров без глобальных переменных.
  //
  // ВАЖНО: dataset всегда отдаёт СТРОКУ ("3", не 3). Для API-запросов
  // (URL /api/composition/3) строка сгодится, но чтобы избежать сюрпризов
  // (например, сравнений с числами), сразу приводим к числу через Number.
  // Если атрибута нет — dataset.projectId === undefined → Number → NaN,
  // это заметный сигнал ошибки конфигурации (лучше явного мусора).
  const projectId = Number(rootNode.dataset.projectId);

  // Передаём projectId в компонент как prop. Prop — это «входной параметр»
  // компонента, аналог аргумента функции: CompositionEditor(projectId=3).
  // Компонент получит его и по нему будет грузить состав.
  ReactDOM.createRoot(rootNode).render(
    <React.StrictMode>
      <CompositionEditor projectId={projectId} />
    </React.StrictMode>
  );
}
