# Coffee Bot

Telegram-бот для учета обслуживания точек, закупок, ревизий и проезда с хранением данных в Google Sheets.

## Быстрый старт

1. Создай и активируй виртуальное окружение:
   - `python3 -m venv .venv`
   - `source .venv/bin/activate`
2. Установи зависимости:
   - `pip install -r requirements.txt`
3. Подготовь переменные окружения:
   - `cp .env.example .env`
   - заполни значения в `.env`
4. Проверь, что сервисный ключ Google доступен по пути из `CREDENTIALS_FILE`.
   - локально можно использовать `credentials.json` в папке проекта
   - в проде используй абсолютный путь вне репозитория, например `/etc/coffee-bot/credentials.json`
5. Запусти бота:
   - `set -a && source .env && set +a && python3 bot.py`

## Переменные окружения

- `BOT_TOKEN` — токен Telegram-бота
- `SPREADSHEET_ID` — ID Google-таблицы
- `PHOTO_CHAT_ID` — обязательный chat id для отправки фото
- `CREDENTIALS_FILE` — путь к JSON сервисного аккаунта Google; в проде держи файл вне папки проекта
- `USERS_JSON` — необязательный JSON-словарь вида `{"1395822345":"Матвей"}`; если переменная не задана, бот читает сотрудников из листа `Пользователи`
- `ALLOWED_USER_IDS` — список разрешённых Telegram user id через запятую; если переменная не задана, бот берёт id из `USERS_JSON` или листа `Пользователи`
- `ALLOWED_GROUP_CHAT_IDS` — список разрешённых group/supergroup chat id через запятую
- `GROUP_REPORT_DRAFT_TTL_SECONDS` — время жизни черновиков из групповых сообщений (в секундах)
- `CARD_MESSAGES_AUTO_CLEANUP_SECONDS` — автоочистка карточек точек в чате (в секундах, `0` выключает)

## Проверки

- Проверка синтаксиса: `python3 -m py_compile bot.py`
- Линтер: `ruff check .`

## Безопасность

- Не коммить `.env` и `credentials.json`.
- В проде хранить ключ сервисного аккаунта вне репозитория, например в `/etc/coffee-bot/credentials.json`.
- Если токены или ключи попадали в публичный доступ, обязательно ротируй их.
