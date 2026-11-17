# Подключение агента к GitHub (с iPad, ~5 минут)

Агенту нужен **токен** — специальный пароль, который разрешает работать только с одним репозиторием. Он хранится в Render, не в интерфейсе и не в GitHub.

## 1. Создать токен
1. В Safari открой github.com и войди в свой аккаунт `dobroe345-cyber`.
2. Фото профиля → **Settings** → внизу слева **Developer settings** → **Personal access tokens** → **Fine-grained tokens** → **Generate new token**.
3. Заполни:
   - **Token name**: `gamedev-ai-agent`
   - **Expiration**: 90 days (потом создашь новый)
   - **Resource owner**: `dobroe345-cyber`
   - **Repository access**: **Only select repositories** → выбери `GameDev-AI`
   - **Permissions → Repository permissions**: **Contents** → *Read and write*, **Pull requests** → *Read and write*. Остальное не трогай (Metadata: Read-only включится сам).
4. **Generate token** и сразу скопируй строку `github_pat_…`: потом её уже не покажут.

## 2. Вставить токен в Render
1. dashboard.render.com → сервис `GameDev-AI-1` → **Environment** → **Add Environment Variable**.
2. Key: `GITHUB_TOKEN`, Value: вставь токен. Сохрани (**Save, rebuild and deploy**). Подожди 1–2 минуты.
3. Необязательно: `GITHUB_REPOS` = `dobroe345-cyber/GameDev-AI` (по умолчанию уже так; если репозиториев несколько, перечисли через запятую).

## 3. Проверить
Интерфейс → Настройки → Подключение → **Проверить связь**. В конце должно быть: `GitHub для агента: подключён (dobroe345-cyber/GameDev-AI)`.
Потом включи кнопку **Агент** возле поля ввода и дай маленькую задачу, например: «Добавь комментарий в начало README».

## Если что-то не так
| Сообщение | Причина |
|---|---|
| «GitHub не подключён: задайте GITHUB_TOKEN» | переменная не сохранена или сервер не перезапустился |
| 401 / «токен не принят» | токен скопирован не полностью или истёк |
| 404 / «нет доступа к репозиторию» | в токене не выбран этот репозиторий |
| 403 / «не хватает прав» | не включены Contents или Pull requests: Read and write |

## Безопасность
- Никому не присылай токен (в том числе в чат с ИИ) и не вставляй его в интерфейс или в файлы репозитория.
- Если токен случайно показался: GitHub → тот же список токенов → **Delete**, и создай новый.
- Агент не пишет в `main` и не нажимает Merge. Подробно: [BODY.md](BODY.md).
- Дополнительно можно включить защиту `main`: репозиторий → Settings → Branches → Add rule → `main` → *Require a pull request before merging*.
