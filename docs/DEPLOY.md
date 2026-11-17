# Развёртывание (всё на бесплатных тарифах)

Платные услуги не нужны. Если сервис просит карту, остановитесь и проверьте тариф.

## 1. Neon (база данных)
1. neon.tech → создайте проект.
2. Connect → включите **Pooled connection** → скопируйте строку `postgresql://...`. Это `DATABASE_URL`.

## 2. Cloudflare R2 (файлы)
1. Cloudflare → R2 → создайте бакет (например `gamedev-ai`).
2. R2 → Manage API tokens → создайте токен с правом Object Read & Write на этот бакет.
3. Запишите Account ID, Access Key ID, Secret Access Key, имя бакета.
   (R2 может потребовать привязать карту, но в бесплатный лимит деньги не списываются. Если не хотите, файлы будут на временном диске.)

## 3. Render
1. render.com → New → Blueprint → выберите репозиторий (файл `render.yaml`).
2. Заполните переменные: `DATABASE_URL`, `R2_*`. `API_TOKEN` сгенерируется сам.
3. После деплоя откройте Environment и скопируйте `API_TOKEN`.
4. Проверка: `python scripts/smoke_test.py https://ВАШ.onrender.com ТОКЕН`

Free-сервис засыпает через 15 минут и просыпается около минуты. Данные при этом не теряются: они в Neon и R2.

### Агент кода (по желанию)
Чтобы ИИ мог искать и менять код в GitHub, добавьте `GITHUB_TOKEN` (инструкция: [GITHUB.md](GITHUB.md)).

## 4. GPU-узел (отдельно, по желанию)
Любой сервер с OpenAI-совместимым API (Ollama, vLLM, llama.cpp). Откройте его через Cloudflare Tunnel и задайте в Render:
`GPU_BASE_URL` (например `https://xxx.trycloudflare.com/v1`), `GPU_MODEL`, при необходимости `GPU_API_KEY`.
Когда GPU выключен, запросы идут на резервных провайдеров, а фоновые задачи ждут и продолжаются сами.

## 5. Фронтенд
В настройках интерфейса выберите режим backend, укажите адрес Render и `API_TOKEN`.
