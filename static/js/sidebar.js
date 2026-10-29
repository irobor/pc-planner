// app/static/js/sidebar.js
// Сворачивание бокового меню раздела (layouts/section.html).
//
// Начальное состояние рисует сервер по cookie sidebar_collapsed, поэтому
// здесь только реакция на клик: переключить класс (меню сворачивается
// сразу, без перезагрузки — несохранённые клики в сетке не теряются),
// обновить подпись и aria-expanded кнопки, запомнить выбор в cookie.
(function () {
  const layout = document.querySelector('.section-layout');
  const button = document.querySelector('.section-sidebar-toggle');
  if (!layout || !button) {
    return;
  }

  button.addEventListener('click', () => {
    const collapsed = layout.classList.toggle('section-layout--collapsed');
    const label = collapsed
      ? button.dataset.labelExpand
      : button.dataset.labelCollapse;

    button.setAttribute('aria-expanded', String(!collapsed));
    button.dataset.label = label;
    button.querySelector('.section-sidebar-toggle__label').textContent = label;

    // Год: свернул один раз — и забыл. path=/ — одно состояние на все
    // разделы с боковым меню. Lax — cookie не уходит с чужих сайтов.
    document.cookie =
      'sidebar_collapsed=' + (collapsed ? '1' : '0') +
      '; path=/; max-age=31536000; SameSite=Lax';
  });
})();