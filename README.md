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
5. Запусти бота:
   - `set -a && source .env && set +a && python3 bot.py`

## Переменные окружения

- `BOT_TOKEN` — токен Telegram-бота
- `SPREADSHEET_ID` — ID Google-таблицы
- `PHOTO_CHAT_ID` — chat id для отправки фото
- `CREDENTIALS_FILE` — путь к JSON сервисного аккаунта Google
- `ALLOWED_USER_IDS` — список разрешённых Telegram user id через запятую
- `ALLOWED_GROUP_CHAT_IDS` — список разрешённых group/supergroup chat id через запятую
- `GROUP_REPORT_DRAFT_TTL_SECONDS` — время жизни черновиков из групповых сообщений (в секундах)
- `CARD_MESSAGES_AUTO_CLEANUP_SECONDS` — автоочистка карточек точек в чате (в секундах, `0` выключает)

## Проверки

- Проверка синтаксиса: `python3 -m py_compile bot.py`
- Линтер: `ruff check .`

## Безопасность

- Не коммить `.env` и `credentials.json`.
- Если токены или ключи попадали в публичный доступ, обязательно ротируй их.
