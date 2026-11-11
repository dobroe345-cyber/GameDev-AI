# GameDev AI

ИИ-помощник для разработки игр: чат-интерфейс (работает на любом устройстве), Python-бэкенд с ИИ-ядром, памятью и файловым менеджером, и заготовка C++ движка.

```
GameDev-AI/
├── frontend/   index.html, style.css, script.js   — интерфейс (GitHub Pages)
├── backend/    main.py, ai_core.py, memory.py, file_manager.py
├── cpp/        engine.cpp, engine.h               — ядро движка (ctypes)
├── models/     локальные модели (пусто)
├── assets/     ресурсы (пусто)
├── tests/      pytest
├── config/     config.json, system_prompt.txt
├── requirements.txt
└── README.md
```

## 1. Фронтенд на GitHub Pages

1. Создай репозиторий на GitHub и залей туда все файлы.
2. **Settings → Pages → Source: GitHub Actions** (workflow уже лежит в `.github/workflows/pages.yml`, публикует папку `frontend`).
3. После пуша в `main` сайт откроется по адресу `https://<имя>.github.io/<репозиторий>/`.

Без сервера интерфейс работает в **демо-режиме** (бейдж «демо» сверху): чаты, поиск, экспорт, файлы, темы — всё локально в браузере.

Что умеет интерфейс: несколько чатов (создать / переименовать / удалить / искать), потоковый вывод ответа, кнопка «стоп» и «заново», Markdown и блоки кода с кнопкой «Копировать», прикрепление текстовых файлов (в т.ч. перетаскиванием, до 200 КБ), экспорт чата в `.md`, светлая / тёмная / системная тема, настройки (адрес сервера, модель, температура, системная инструкция), горячая клавиша `Ctrl/Cmd+K` — новый чат. Адаптивная вёрстка под телефон, планшет и ПК.

## 2. Бэкенд

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cd backend
uvicorn main:app --reload --port 8000
```

Затем в интерфейсе: **Настройки → Адрес бэкенда** → `http://localhost:8000` → «Проверить связь».

Если фронтенд открыт с GitHub Pages, добавь свой адрес `https://<имя>.github.io` в `cors_origins` в `config/config.json`. Браузер на HTTPS-странице не пускает запросы на `http://` — для боевого использования нужен сервер с HTTPS.

### API

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/api/health` | проверка связи |
| POST | `/api/chat` | потоковый ответ (текст) |
| GET/DELETE | `/api/chats[/{id}]` | история из памяти |
| GET/PUT/DELETE | `/api/files[/{path}]` | файлы рабочей папки |
| POST | `/api/files/upload` | загрузка файла |

### ИИ-ядро

`backend/ai_core.py` — провайдеры подключаются через `@register_provider("имя")`. Готовые: `stub` (по умолчанию) и `openai_compatible` (OpenAI, Ollama, llama.cpp server, LM Studio). Переключение — `ai.provider` в `config/config.json`; ключ берётся из переменной окружения `AI_API_KEY`. Системный промпт — `config/system_prompt.txt` (сюда вставим твой промпт).

## 3. C++ движок

```bash
cd cpp
g++ -std=c++17 -O2 -shared -fPIC -o libengine.so engine.cpp     # Linux
# macOS: -o libengine.dylib   Windows (MinGW): -o engine.dll
```

Экспортируются функции `gdai_create / gdai_spawn / gdai_step / gdai_get_pos / gdai_count / gdai_kill / gdai_destroy` (см. `engine.h`) — их можно вызывать из Python через `ctypes`.

## 4. Тесты

```bash
pytest tests
```
