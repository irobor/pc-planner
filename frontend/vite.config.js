// app\frontend\vite.config.js
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { resolve } from 'path'

export default defineConfig({
  plugins: [react()],
  build: {
    // 1. Указываем, куда складывать готовый билд (выходим из frontend и идем в static)
    outDir: resolve(__dirname, '../static'),

    // 2. Очищаем папку static перед сборкой (будь аккуратен, Vite удалит старые js-файлы в этой папке)
    //    false: не чистим static — иначе сборка снесла бы чужие файлы
    //    (второй бандл, style.css). С несколькими входами это критично.
    emptyOutDir: false,

    rollupOptions: {
      // 3. Точки входа: КАЖДАЯ — корень независимого дерева импортов,
      
      //    composition — новая «корзина» состава проекта. Ключи объекта
      
      //    Файл src/composition.jsx создадим в 2.3.3, а
      //    src/composition-create.jsx — в B.3 (страница создания состава,
      //    вход B). До появления файла НЕ собирать (vite build упадёт
      //    «вход не найден»).
      input: { 
        workload: resolve(__dirname, 'src/workload.jsx'),       
        composition: resolve(__dirname, 'src/composition.jsx'),
        'composition-create': resolve(__dirname, 'src/composition-create.jsx'),
      },

      output: {
        // 4. Имя выходного файла — шаблон с [name] (ключ входа), а не
       
        //    планировщик не ломается), composition → composition.bundle.js.
        //    Хэшей нет намеренно — имена стабильны для подключения в Jinja
        //    (ценой ручного hard-refresh браузера после пересборки).
        
        entryFileNames: 'js/[name].bundle.js',
        chunkFileNames: 'js/[name].js',

        // 5. Если React потянет за собой CSS, сохраняем его в общую папку стилей.
        
        //    отложено: по минимуму корзина на Bootstrap-классах без своего CSS.
        assetFileNames: (assetInfo) => {
          if (assetInfo.name && assetInfo.name.endsWith('.css')) {
            return 'css/planner-react.css';
          }
          return 'assets/[name].[ext]';
        },
      },
    },
  },
})