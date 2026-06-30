import asyncio
import calendar
import hashlib
import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from functools import lru_cache
from html import escape as escape_html
from pathlib import Path
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError, WorksheetNotFound
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    PicklePersistence,
    filters,
)
from telegram.helpers import escape_markdown


# ============ НАСТРОЙКИ ============
def parse_env_id_set(name, default_values=None):
    raw = os.getenv(name, "").strip()
    if not raw:
        return {int(value) for value in (default_values or [])}

    values = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            values.add(int(chunk))
        except ValueError as exc:
            raise RuntimeError(f"{name} must contain comma-separated integer IDs") from exc
    return values


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "").strip()
PHOTO_CHAT_ID_RAW = os.getenv("PHOTO_CHAT_ID", "").strip()
CREDENTIALS_FILE = os.getenv("CREDENTIALS_FILE", "credentials.json").strip()
USERS_JSON = os.getenv("USERS_JSON", "").strip()
GROUP_REPORT_DRAFT_TTL_SECONDS = int(os.getenv("GROUP_REPORT_DRAFT_TTL_SECONDS", "86400"))
GROUP_REPORT_FEEDBACK_AUTO_DELETE_SECONDS = int(
    os.getenv("GROUP_REPORT_FEEDBACK_AUTO_DELETE_SECONDS", "300")
)
GROUP_REPORT_ACTION_WINDOW_SECONDS = int(os.getenv("GROUP_REPORT_ACTION_WINDOW_SECONDS", "60"))
SERVICE_DUPLICATE_REVIEW_TTL_SECONDS = int(os.getenv("SERVICE_DUPLICATE_REVIEW_TTL_SECONDS", "3600"))
CARD_MESSAGES_AUTO_CLEANUP_SECONDS = int(os.getenv("CARD_MESSAGES_AUTO_CLEANUP_SECONDS", "600"))
SHEETS_BOOK_CACHE_TTL_SECONDS = int(os.getenv("SHEETS_BOOK_CACHE_TTL_SECONDS", "60"))
PAYOUT_SCREEN_LOAD_TIMEOUT_SECONDS = int(os.getenv("PAYOUT_SCREEN_LOAD_TIMEOUT_SECONDS", "25"))
BOT_TIMEZONE = ZoneInfo("Europe/Moscow")
REMINDER_STATE_FILE = os.getenv("REMINDER_STATE_FILE", "reminder_state.json").strip()
PERSISTENCE_FILE = os.getenv("PERSISTENCE_FILE", "bot_state.pickle").strip()
SERVICE_TODAY_GROUP_POST_HOUR = int(os.getenv("SERVICE_TODAY_GROUP_POST_HOUR", "9"))
SERVICE_TODAY_GROUP_DELETE_HOUR = int(os.getenv("SERVICE_TODAY_GROUP_DELETE_HOUR", "2"))
HOME_REVISION_REMINDER_HOUR = int(os.getenv("HOME_REVISION_REMINDER_HOUR", "12"))
MONTH_CLOSE_REVISION_REMINDER_HOUR = int(os.getenv("MONTH_CLOSE_REVISION_REMINDER_HOUR", "12"))

if PHOTO_CHAT_ID_RAW:
    try:
        PHOTO_CHAT_ID = int(PHOTO_CHAT_ID_RAW)
    except ValueError as exc:
        raise RuntimeError("PHOTO_CHAT_ID must be an integer") from exc
else:
    PHOTO_CHAT_ID = None

USER_DIRECTORY_SHEET = "Пользователи"
USER_DIRECTORY_HEADERS = ["telegram_id", "имя", "роль"]

USER_ROLE_WORKER = "worker"
USER_ROLE_PAID_WORKER = "paid_worker"
USER_ROLE_PAYOUT_VIEWER = "payout_viewer"
USER_ROLE_PAYOUT_EDITOR = "payout_editor"


def normalize_role_token(value):
    return re.sub(r"[^0-9a-zа-яё]+", "_", str(value or "").strip().lower()).strip("_")


USER_ROLE_ALIAS_MAP = {
    normalize_role_token(alias): role
    for role, aliases in {
        USER_ROLE_WORKER: {
            USER_ROLE_WORKER,
            "сотрудник",
            "работник",
            "worker",
            "staff",
            "employee",
        },
        USER_ROLE_PAID_WORKER: {
            USER_ROLE_PAID_WORKER,
            "зп",
            "зарплата",
            "выплаты",
            "salary",
            "salary_worker",
            "paid",
            "paid_worker",
        },
        USER_ROLE_PAYOUT_VIEWER: {
            USER_ROLE_PAYOUT_VIEWER,
            "зп_просмотр",
            "выплаты_просмотр",
            "salary_viewer",
            "payout_viewer",
            "viewer",
        },
        USER_ROLE_PAYOUT_EDITOR: {
            USER_ROLE_PAYOUT_EDITOR,
            "зп_редактор",
            "выплаты_редактор",
            "salary_editor",
            "payout_editor",
            "editor",
        },
    }.items()
    for alias in aliases
}

DEPRECATED_USER_DIRECTORY = {
    # DEPRECATED: перенести в Sheets.
    1395822345: {
        "name": "Матвей",
        "roles": [USER_ROLE_WORKER, USER_ROLE_PAYOUT_VIEWER, USER_ROLE_PAYOUT_EDITOR],
    },
    611556433: {
        "name": "Владислав",
        "roles": [USER_ROLE_WORKER],
    },
    5075547917: {
        "name": "Начальник",
        "roles": [USER_ROLE_WORKER],
    },
    874403512: {
        "name": "Кирилл",
        "roles": [USER_ROLE_WORKER, USER_ROLE_PAID_WORKER, USER_ROLE_PAYOUT_VIEWER],
    },
    8370154716: {
        "name": "Александр",
        "roles": [USER_ROLE_WORKER, USER_ROLE_PAID_WORKER, USER_ROLE_PAYOUT_VIEWER],
    },
}
ALLOWED_USER_IDS = parse_env_id_set("ALLOWED_USER_IDS")
ALLOWED_GROUP_CHAT_IDS = parse_env_id_set("ALLOWED_GROUP_CHAT_IDS")

SERVICE_PRICE = 250
DEFAULT_FARE = 48
PAYOUT_SHEET = "Выплаты"
PAYOUT_STATUS_PENDING = "ожидает"
PAYOUT_STATUS_PAID = "переведено"
POINTS = ["Беломорский", "Гагарина", "Гиппо", "Южный", "Сити", "Макси", "Бел2"]
REVISION_LOCATIONS = POINTS + ["Дома", "Гараж"]
POINT_SHORT_LABELS = {
    "Беломорский": "Беломор",
}

_BOOK_CACHE = {
    "book": None,
    "expires_at": None,
    "worksheets": {},
}
_ENSURED_WORKSHEET_GROUPS = set()

SUPPLIES = [
    "Стаканы", "Кофе", "Шоколад", "Раф", "Молоко", "Сиропы",
    "Трубочки", "Палочки", "Сахар", "Крышки бел",
    "Крышки чёрн", "Манжеты", "Мус.пакеты", "Влажные салф"
]

PURCHASE_ITEMS = ["Вода 19л", "Влажные салфетки", "Мусорные пакеты", "Другое"]

SERVICE_REPORT_POINT_ALIASES = {
    "беломорский": "Беломорский",
    "бел": "Беломорский",
    "гагарина": "Гагарина",
    "гагар": "Гагарина",
    "гиппо": "Гиппо",
    "южный": "Южный",
    "юж": "Южный",
    "сити": "Сити",
    "макси": "Макси",
    "бел2": "Бел2",
    "бел 2": "Бел2",
    "б2": "Бел2",
}

SUPPLY_UNITS = {
    "Стаканы": "туб", "Кофе": "пачек", "Шоколад": "пачек",
    "Раф": "пачек", "Молоко": "пачек", "Сиропы": "бутылок", "Трубочки": "пачек",
    "Палочки": "пачек", "Сахар": "стиков", "Крышки бел": "туб",
    "Крышки чёрн": "туб", "Манжеты": "пачек",
    "Мус.пакеты": "рулонов", "Влажные салф": "пачек",
}

DEFAULT_SHORTAGE_OPTIONS = ["0", "0.5", "1", "1.5", "2", "3"]
SHORTAGE_ITEM_OPTIONS = {
    "Стаканы": ["0", "0.5", "1", "1.5", "2", "3"],
    "Крышки бел": ["0", "0.5", "1", "1.5", "2", "3"],
    "Крышки чёрн": ["0", "0.5", "1", "1.5", "2", "3"],
    "Сиропы": ["0", "0.5", "1", "1.5", "2", "3"],
    "Сахар": ["0", "50", "100", "200", "300", "400"],
    "Манжеты": ["0", "0.4", "0.8", "1.2", "1.6", "2"],
    "Палочки": ["0", "0.4", "0.8", "1.2", "1.6", "2"],
    "Трубочки": ["0", "0.4", "0.8", "1.2", "1.6", "2"],
}

SHORTAGE_NEXT_VISIT_LABELS = {
    "enough": "хватит до следующего приезда",
    "not_enough": "не хватит до следующего приезда",
}

REVISION_ITEMS = [
    "Кофе", "Молоко", "Мока", "Шоколад", "Стаканы",
    "Сахар", "Сиропы", "Крышки бел", "Крышки чёрн",
    "Трубочки", "Палочки", "Манжеты", "Вода",
    "Влажные салф", "Мус.пакеты", "Салфетки сухие",
]

REVISION_UNITS = {
    "Кофе": "пачек",
    "Молоко": "пачек",
    "Мока": "пачек",
    "Шоколад": "пачек",
    "Стаканы": "шт",
    "Сахар": "стиков",
    "Сиропы": "бутылок",
    "Крышки бел": "шт",
    "Крышки чёрн": "шт",
    "Трубочки": "шт",
    "Палочки": "шт",
    "Манжеты": "шт",
    "Вода": "бут",
    "Влажные салф": "пачек",
    "Мус.пакеты": "рулонов",
    "Салфетки сухие": "шт",
}

PROCUREMENT_UNIT_SHORT = {
    "пачек": "пач",
    "бутылок": "бут",
    "стиков": "стик",
    "рулонов": "рул",
    "шт": "шт",
    "бут": "бут",
}

REVISION_ITEM_OPTIONS = {
    "Кофе": ["0", "0.5", "1", "2", "3", "5"],
    "Молоко": ["0", "0.5", "1", "2", "3", "5"],
    "Мока": ["0", "0.5", "1", "2", "3", "5"],
    "Шоколад": ["0", "0.5", "1", "2", "3", "5"],
    "Стаканы": ["0", "50", "100", "200", "300", "500"],
    "Сахар": ["0", "50", "100", "200", "300", "400"],
    "Сиропы": ["0", "1", "2", "3", "4", "5"],
    "Крышки бел": ["0", "50", "100", "200", "300", "500"],
    "Крышки чёрн": ["0", "50", "100", "200", "300", "500"],
    "Трубочки": ["0", "50", "100", "200", "300", "500"],
    "Палочки": ["0", "50", "100", "200", "300", "500"],
    "Манжеты": ["0", "50", "100", "200", "300", "500"],
    "Вода": ["0", "1", "2", "3", "4", "5"],
    "Влажные салф": ["0", "0.5", "1", "2", "3", "5"],
    "Мус.пакеты": ["0", "0.5", "1", "2", "3", "5"],
    "Салфетки сухие": ["0", "50", "100", "200", "300", "500"],
}

REVISION_STOCK_THRESHOLDS = {
    "Кофе": {"point_critical": 1.5, "point_warning": 2.5, "network_critical": 35, "network_warning": 56},
    "Молоко": {"point_critical": 1.5, "point_warning": 2.5, "network_critical": 35, "network_warning": 56},
    "Мока": {"point_critical": 1, "point_warning": 2, "network_critical": 28, "network_warning": 49},
    "Шоколад": {"point_critical": 1, "point_warning": 2, "network_critical": 28, "network_warning": 49},
    "Стаканы": {"point_critical": 150, "point_warning": 250, "network_critical": 1500, "network_warning": 2500},
    "Сахар": {"point_critical": 100, "point_warning": 180, "network_critical": 1000, "network_warning": 1800},
    "Сиропы": {"point_critical": 2, "point_warning": 4, "network_critical": 14, "network_warning": 20},
    "Крышки бел": {"point_critical": 120, "point_warning": 200, "network_critical": 1000, "network_warning": 1700},
    "Крышки чёрн": {"point_critical": 250, "point_warning": 400, "network_critical": 2200, "network_warning": 3500},
    "Трубочки": {"point_critical": 120, "point_warning": 200, "network_critical": 900, "network_warning": 1500},
    "Палочки": {"point_critical": 250, "point_warning": 400, "network_critical": 2200, "network_warning": 3500},
    "Манжеты": {"point_critical": 25, "point_warning": 40, "network_critical": 220, "network_warning": 350},
    "Влажные салф": {"point_critical": 0.5, "point_warning": 1, "network_critical": 6, "network_warning": 10},
    "Мус.пакеты": {"point_critical": 0.5, "point_warning": 1, "network_critical": 6, "network_warning": 10},
    "Салфетки сухие": {"point_critical": 150, "point_warning": 250, "network_critical": 1200, "network_warning": 2000},
}

REPAIR_REASONS = [
    "Не включается",
    "Течет",
    "Ошибка",
    "Кофемолка",
]

REPAIR_DOCUMENT_TYPES = [
    "Фото поломки",
    "Счёт",
    "Платёжка",
    "Акт",
]

REPAIR_UNKNOWN_MACHINE_MODEL = "Не указано"
REPAIR_MANUAL_SERVICE_PREFIX = "manual:"

REPAIR_EXPENSE_TYPES = [
    "Перевозка",
    "Диагностика",
    "Запчасти",
    "Работа",
    "Прочее",
]

REPAIR_MACHINE_WORKING = "Работает"
REPAIR_MACHINE_REPAIR = "В ремонте"
REPAIR_MACHINE_DISCARDED = "Списан"

REPAIR_STATUS_FIXED = "Зафиксирована"
REPAIR_STATUS_WAITING_TRANSPORT = "Ожидает перевозки"
REPAIR_STATUS_ON_THE_WAY = "В пути на сервис"
REPAIR_STATUS_IN_REPAIR = "В ремонте"
REPAIR_STATUS_WAITING_PARTS = "Ожидает запчасти"
REPAIR_STATUS_WAITING_INVOICE = "Ожидает счёт"
REPAIR_STATUS_INVOICE_PAID = "Счёт оплачен"
REPAIR_STATUS_READY = "Готов на сервисе"
REPAIR_STATUS_RETURNING = "В пути обратно"
REPAIR_STATUS_INSTALLED = "Установлен"
REPAIR_STATUS_DISCARDED = "Списан"

REPAIR_ACTIVE_STATUSES = {
    REPAIR_STATUS_FIXED,
    REPAIR_STATUS_WAITING_TRANSPORT,
    REPAIR_STATUS_ON_THE_WAY,
    REPAIR_STATUS_IN_REPAIR,
    REPAIR_STATUS_WAITING_PARTS,
    REPAIR_STATUS_WAITING_INVOICE,
    REPAIR_STATUS_INVOICE_PAID,
    REPAIR_STATUS_READY,
    REPAIR_STATUS_RETURNING,
}

REPAIR_STATUS_FLOW = {
    REPAIR_STATUS_FIXED: [REPAIR_STATUS_WAITING_TRANSPORT, REPAIR_STATUS_IN_REPAIR, REPAIR_STATUS_DISCARDED],
    REPAIR_STATUS_WAITING_TRANSPORT: [REPAIR_STATUS_ON_THE_WAY, REPAIR_STATUS_DISCARDED],
    REPAIR_STATUS_ON_THE_WAY: [REPAIR_STATUS_IN_REPAIR],
    REPAIR_STATUS_IN_REPAIR: [REPAIR_STATUS_WAITING_PARTS, REPAIR_STATUS_WAITING_INVOICE, REPAIR_STATUS_READY, REPAIR_STATUS_DISCARDED],
    REPAIR_STATUS_WAITING_PARTS: [REPAIR_STATUS_IN_REPAIR, REPAIR_STATUS_DISCARDED],
    REPAIR_STATUS_WAITING_INVOICE: [REPAIR_STATUS_INVOICE_PAID, REPAIR_STATUS_DISCARDED],
    REPAIR_STATUS_INVOICE_PAID: [REPAIR_STATUS_IN_REPAIR, REPAIR_STATUS_READY, REPAIR_STATUS_DISCARDED],
    REPAIR_STATUS_READY: [REPAIR_STATUS_RETURNING, REPAIR_STATUS_INSTALLED],
    REPAIR_STATUS_RETURNING: [REPAIR_STATUS_INSTALLED],
    REPAIR_STATUS_INSTALLED: [],
    REPAIR_STATUS_DISCARDED: [],
}

REPAIR_STATUS_ICONS = {
    REPAIR_STATUS_FIXED: "⚪",
    REPAIR_STATUS_WAITING_TRANSPORT: "🔵",
    REPAIR_STATUS_ON_THE_WAY: "🚚",
    REPAIR_STATUS_IN_REPAIR: "🟡",
    REPAIR_STATUS_WAITING_PARTS: "🟡",
    REPAIR_STATUS_WAITING_INVOICE: "📄",
    REPAIR_STATUS_INVOICE_PAID: "💳",
    REPAIR_STATUS_READY: "🟢",
    REPAIR_STATUS_RETURNING: "🚚",
    REPAIR_STATUS_INSTALLED: "✅",
    REPAIR_STATUS_DISCARDED: "⚫",
}

# States
(MAIN_MENU, SERVICE_MENU_SECTION, REPORT_MENU_SECTION, RENT_MENU_SECTION, REPAIR_MENU_SECTION, INFO_MENU, INFO_POINT,
 SERVICE_WHO, SERVICE_DATE, SERVICE_DATE_CUSTOM,
 SERVICE_POINT, SERVICE_PHOTO,
 SERVICE_WATER, SERVICE_WATER_CUSTOM,
 SERVICE_PURCHASE, SERVICE_PURCHASE_SELECT,
 SERVICE_PURCHASE_QTY, SERVICE_PURCHASE_OTHER_NAME,
 SERVICE_PURCHASE_SUM,
 SERVICE_SHORTAGE, SERVICE_SHORTAGE_SELECT,
 SERVICE_SHORTAGE_QTY, SERVICE_SHORTAGE_QTY_CUSTOM,
 SERVICE_SHORTAGE_NEXT_VISIT,
 SERVICE_CONFIRM,
 TRAVEL_WHO, TRAVEL_DATE, TRAVEL_DATE_CUSTOM,
 TRAVEL_ACTION, TRAVEL_CUSTOM_SUM, TRAVEL_TRIPS_CUSTOM,
 RENT_PAYMENT_RECEIPT,
 REPAIR_NEW_POINT, REPAIR_NEW_MACHINE, REPAIR_NEW_MACHINE_QUICK,
 REPAIR_NEW_REASON, REPAIR_NEW_REASON_CUSTOM, REPAIR_NEW_DESCRIPTION,
 REPAIR_NEW_SERVICE_CENTER, REPAIR_NEW_DATE, REPAIR_NEW_DATE_CUSTOM, REPAIR_NEW_CONFIRM,
 REPAIR_STATUS_UPDATE,
 REPAIR_EXPENSE_TYPE, REPAIR_EXPENSE_AMOUNT, REPAIR_EXPENSE_DESCRIPTION, REPAIR_EXPENSE_PAID,
 REPAIR_HISTORY_POINT, REPAIR_HISTORY_MACHINE,
 REPAIR_NEW_PHOTO, REPAIR_SET_SERVICE, REPAIR_SET_SERVICE_MANUAL,
 REPAIR_SET_DATE_BROKEN, REPAIR_SET_DATE_BROKEN_CUSTOM,
 REPAIR_SET_DATE_SENT, REPAIR_SET_DATE_SENT_CUSTOM,
 REPAIR_SET_DATE_PLAN, REPAIR_SET_DATE_PLAN_CUSTOM,
 REPAIR_DOC_UPLOAD,
 REPORT_DAY, REPORT_PERIOD, REPORT_PERIOD_CUSTOM,
 DELETE_DATE, DELETE_DATE_CUSTOM, DELETE_POINT,
 DELETE_ENTRY, DELETE_CONFIRM,
 REVISION_MENU, REVISION_PERIOD, REVISION_LOCATION,
 REVISION_EXISTING, REVISION_ITEM, REVISION_ITEM_CUSTOM,
 REVISION_CONFIRM, REVISION_EDIT_ACTION, REVISION_EDIT_ITEM_SELECT,
 REVISION_VIEW_MODE, REVISION_VIEW_LOCATION, REVISION_VIEW_ITEM,
 REVISION_COMPARE_LOCATION, REVISION_DELETE_CONFIRM,
 REVISION_IMPORT_TEXT, REVISION_IMPORT_CONFIRM,
 REVISION_PROCUREMENT_REPORT,
 TRAVEL_MENU, TRAVEL_HISTORY_PERIOD,
 SALARY_TASK_WORKER, SALARY_TASK_DATE, SALARY_TASK_DATE_CUSTOM,
 SALARY_TASK_DESCRIPTION, SALARY_TASK_AMOUNT, SALARY_TASK_CONFIRM,
 PAYOUT_SCREEN, PAYOUT_CORRECTION_AMOUNT, PAYOUT_CORRECTION_NOTE,
 PAYOUT_TRAVEL_EDIT_AMOUNT, PAYOUT_TRAVEL_EDIT_DATE,
 PAYOUT_TASK_EDIT_DESCRIPTION, PAYOUT_TASK_EDIT_AMOUNT,
 PAYOUT_TASK_EDIT_DATE) = range(100)

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)
GROUP_REPORT_SAVE_LOCK = asyncio.Lock()


async def run_blocking(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


async def global_error_handler(update: object, context):
    logger.exception("Unhandled exception while processing update", exc_info=context.error)


def parse_users_json(raw_value):
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise RuntimeError("USERS_JSON must be valid JSON") from exc

    if not isinstance(payload, dict):
        raise RuntimeError("USERS_JSON must be a JSON object mapping Telegram IDs to names or user objects")

    entries = {}
    for raw_id, raw_user in payload.items():
        try:
            telegram_id = int(str(raw_id).strip())
        except ValueError as exc:
            raise RuntimeError("USERS_JSON keys must be Telegram integer IDs") from exc

        if isinstance(raw_user, dict):
            name = str(raw_user.get("name", "")).strip()
            raw_roles = raw_user.get("roles", "")
        else:
            name = str(raw_user).strip()
            raw_roles = ""
        if not name:
            raise RuntimeError("USERS_JSON values must include non-empty user names")
        entries[telegram_id] = {
            "telegram_id": telegram_id,
            "name": name,
            "roles": get_bootstrap_user_roles(telegram_id, name, raw_roles),
        }
    return entries


def normalize_user_roles(raw_roles):
    if isinstance(raw_roles, str):
        parts = [part.strip() for part in re.split(r"[,;/|\n]+", raw_roles)]
    elif isinstance(raw_roles, (list, tuple, set)):
        parts = [str(part).strip() for part in raw_roles]
    elif raw_roles in (None, ""):
        parts = []
    else:
        parts = [str(raw_roles).strip()]

    normalized = []
    seen = set()
    for part in parts:
        token = normalize_role_token(part)
        if not token:
            continue
        role = USER_ROLE_ALIAS_MAP.get(token)
        if not role or role in seen:
            continue
        seen.add(role)
        normalized.append(role)

    if not normalized:
        normalized.append(USER_ROLE_WORKER)
    if USER_ROLE_PAYOUT_EDITOR in normalized and USER_ROLE_PAYOUT_VIEWER not in normalized:
        normalized.append(USER_ROLE_PAYOUT_VIEWER)
    if USER_ROLE_PAID_WORKER in normalized and USER_ROLE_WORKER not in normalized:
        normalized.append(USER_ROLE_WORKER)
    return normalized


def build_user_directory_entries_from_records(records):
    entries = {}
    for record in records:
        raw_id = str(record.get("telegram_id", "")).strip()
        name = str(record.get("имя", "")).strip()
        if not raw_id and not name:
            continue
        if not raw_id or not name:
            logger.warning(
                "Skipping incomplete user directory row: telegram_id=%r name=%r",
                raw_id,
                name,
            )
            continue
        try:
            telegram_id = int(raw_id)
        except ValueError:
            logger.warning("Skipping invalid telegram_id in %s: %r", USER_DIRECTORY_SHEET, raw_id)
            continue
        entries[telegram_id] = {
            "telegram_id": telegram_id,
            "name": name,
            "roles": get_bootstrap_user_roles(telegram_id, name, record.get("роль", "")),
        }
    return entries


def build_user_directory_entries_from_fallback():
    return {
        telegram_id: {
            "telegram_id": telegram_id,
            "name": str(payload.get("name", "")).strip(),
            "roles": normalize_user_roles(payload.get("roles", [])),
        }
        for telegram_id, payload in DEPRECATED_USER_DIRECTORY.items()
        if str(payload.get("name", "")).strip()
    }


def get_bootstrap_user_roles(telegram_id, name, raw_roles):
    if str(raw_roles or "").strip():
        return normalize_user_roles(raw_roles)

    payload = DEPRECATED_USER_DIRECTORY.get(telegram_id)
    if payload:
        return normalize_user_roles(payload.get("roles", []))

    normalized_name = normalize_text_key(name)
    for deprecated_payload in DEPRECATED_USER_DIRECTORY.values():
        deprecated_name = str(deprecated_payload.get("name", "")).strip()
        if deprecated_name and normalize_text_key(deprecated_name) == normalized_name:
            return normalize_user_roles(deprecated_payload.get("roles", []))

    return normalize_user_roles([])


@lru_cache(maxsize=1)
def get_user_directory_entries():
    if USERS_JSON:
        return parse_users_json(USERS_JSON)

    try:
        get_or_create_worksheet(USER_DIRECTORY_SHEET, USER_DIRECTORY_HEADERS)
        entries = build_user_directory_entries_from_records(get_records(USER_DIRECTORY_SHEET, USER_DIRECTORY_HEADERS))
        if entries:
            return entries
        logger.warning(
            "%s is empty, using deprecated user directory fallback",
            USER_DIRECTORY_SHEET,
        )
    except Exception:
        logger.exception(
            "Failed to load users from %s, using deprecated fallback",
            USER_DIRECTORY_SHEET,
        )

    return build_user_directory_entries_from_fallback()


@lru_cache(maxsize=1)
def get_user_directory():
    return {
        telegram_id: payload["name"]
        for telegram_id, payload in get_user_directory_entries().items()
    }


def get_users_with_role(role):
    return [
        payload
        for payload in get_user_directory_entries().values()
        if role in payload.get("roles", [])
    ]


@lru_cache(maxsize=1)
def get_worker_names():
    return [
        payload["name"]
        for payload in sorted(get_user_directory_entries().values(), key=lambda item: item["telegram_id"])
        if payload.get("name")
    ]


@lru_cache(maxsize=1)
def get_paid_workers():
    return [
        payload["name"]
        for payload in sorted(get_users_with_role(USER_ROLE_PAID_WORKER), key=lambda item: item["telegram_id"])
        if payload.get("name")
    ]


@lru_cache(maxsize=1)
def get_payout_viewer_ids():
    return {
        payload["telegram_id"]
        for payload in get_user_directory_entries().values()
        if USER_ROLE_PAYOUT_VIEWER in payload.get("roles", [])
        or USER_ROLE_PAYOUT_EDITOR in payload.get("roles", [])
    }


@lru_cache(maxsize=1)
def get_payout_editor_ids():
    return {
        payload["telegram_id"]
        for payload in get_user_directory_entries().values()
        if USER_ROLE_PAYOUT_EDITOR in payload.get("roles", [])
    }


def get_allowed_user_ids():
    directory_ids = set(get_user_directory().keys())
    if ALLOWED_USER_IDS:
        return directory_ids | ALLOWED_USER_IDS
    return directory_ids


def get_configured_user_name(user_id):
    if user_id is None:
        return None
    return get_user_directory().get(user_id)


def is_allowed_user(update):
    user = update.effective_user
    return bool(user and user.id in get_allowed_user_ids())


def is_payout_viewer(update):
    user = getattr(update, "effective_user", None)
    return bool(user and user.id in get_payout_viewer_ids())


def is_payout_editor(update):
    user = getattr(update, "effective_user", None)
    return bool(user and user.id in get_payout_editor_ids())


def is_allowed_group_chat(update):
    chat = update.effective_chat
    return bool(
        chat
        and chat.type in {"group", "supergroup"}
        and ALLOWED_GROUP_CHAT_IDS
        and chat.id in ALLOWED_GROUP_CHAT_IDS
    )


def is_private_chat(update):
    chat = update.effective_chat
    return bool(chat and chat.type == "private")


def is_allowed_group_report_chat(update):
    return is_private_chat(update) or is_allowed_group_chat(update)


async def deny_private_access(update):
    message = update.effective_message
    if message:
        await message.reply_text("⛔ Нет доступа.")
    return ConversationHandler.END


async def deny_callback_access(query):
    await query.answer("⛔ Нет доступа.", show_alert=True)

# ============ GOOGLE SHEETS ============
SERVICE_HEADERS = [
    "Дата", "Кто", "Точка", "Вода(бут)", "Нехватка",
    "Остатки", "Закупки", "Сумма закупок", "Сумма обслуж", "В ЗП",
]
TRAVEL_HEADERS = ["Дата", "Кто", "Сумма"]
PHOTO_HEADERS = ["Дата", "Точка", "Кто", "File_ID"]
SALARY_TASK_HEADERS = ["Дата", "Кто", "Описание", "Сумма", "Кто добавил"]
PAYOUT_HEADERS = [
    "Период", "Кому",
    "Сумма обсл", "Кол-во обсл",
    "Сумма закупок",
    "Сумма проезда", "Кол-во записей проезда",
    "Сумма доплат", "Кол-во доплат",
    "Корректировка", "Комм. корректировки",
    "Итого",
    "Статус",
    "Дата перевода",
    "Кто отметил",
]
REVISION_HEADERS = ["Период", "Локация", "Кто", "Дата заполнения"] + REVISION_ITEMS
GROUP_REPORT_LOG_HEADERS = [
    "Chat_ID", "Source_Key", "Source_Message_ID", "Media_Group_ID",
    "Кто", "Точка", "Дата", "Fingerprint", "Service_Row", "Photo_Rows",
    "Revision_Row", "Revision_Period", "Revision_Location", "Revision_Mode",
    "Revision_Backup", "Статус", "Создано",
]
RENT_LANDLORD_HEADERS = [
    "id", "Имя / Название", "Телефон", "Email", "ИНН",
    "Р/счёт", "Банк", "БИК", "К/с", "Заметки",
]
RENT_LEASE_HEADERS = [
    "id", "Точка", "Арендодатель ID", "Номер договора", "Дата заключения",
    "Дата начала", "Дата окончания", "Базовая ставка", "Текущая ставка",
    "Дедлайн (число)", "Договор file_id", "Статус", "Заметки",
]
RENT_PAYMENT_HEADERS = [
    "id", "Договор ID", "Точка", "Период", "Сумма",
    "Дата оплаты", "Кто отметил", "Статус", "Чек file_id", "Заметки",
]
RENT_INDEXATION_HEADERS = [
    "id", "Договор ID", "Точка", "Дата применения",
    "Процент", "Было", "Стало", "Применена",
]
RENT_DOCUMENT_HEADERS = [
    "id", "Точка", "Тип", "Название", "Связанный тип",
    "Связанный ID", "file_id", "Дата загрузки", "Кто загрузил",
]
RENT_SERVICE_HEADERS = [
    "Период", "Chat_ID", "Message_ID", "Тип", "Статус", "Уровень", "Дата обновления",
]
REPAIR_CENTER_HEADERS = [
    "id", "Название", "Город", "Контактное лицо",
    "Телефон", "Email", "Адрес", "Специализация", "Заметки",
]
REPAIR_MACHINE_HEADERS = [
    "id", "Точка", "Бренд", "Модель", "Серийный номер",
    "Дата покупки", "Гарантия до", "Статус", "Заметки",
]
REPAIR_HEADERS = [
    "id", "Аппарат ID", "Точка", "Сервис ID", "Причина",
    "Описание поломки", "Статус", "Дата поломки", "Дата отправки",
    "Дата готовности (план)", "Дата готовности (факт)", "Дата возврата",
    "Перевозка откуда", "Перевозка куда", "На гарантии",
    "Итого расходов", "Кто создал", "Заметки",
]
REPAIR_EXPENSE_HEADERS = [
    "id", "Ремонт ID", "Тип расхода", "Описание", "Сумма",
    "Дата", "Оплачено", "Кто отметил", "Документ file_id", "Заметки",
]
REPAIR_DOCUMENT_HEADERS = [
    "id", "Ремонт ID", "Точка", "Тип",
    "Название", "file_id", "Дата загрузки", "Кто загрузил",
]
REPAIR_SERVICE_HEADERS = [
    "Ремонт ID", "Chat_ID", "Message_ID", "Последний уровень",
    "Дней напоминаем", "Дата обновления",
]

RENT_SHEET_LANDLORDS = "Арендодатели"
RENT_SHEET_LEASES = "Договоры"
RENT_SHEET_PAYMENTS = "Оплаты аренды"
RENT_SHEET_INDEXATIONS = "Индексации"
RENT_SHEET_DOCUMENTS = "Документы"
RENT_SHEET_SERVICE = "Аренда_служебное"
REPAIR_SHEET_CENTERS = "Сервисные центры"
REPAIR_SHEET_MACHINES = "Аппараты"
REPAIR_SHEET_REPAIRS = "Ремонты"
REPAIR_SHEET_EXPENSES = "Расходы на ремонт"
REPAIR_SHEET_DOCUMENTS = "Документы ремонта"
REPAIR_SHEET_SERVICE = "Ремонт_служебное"
SALARY_TASK_SHEET = "ЗП задачи"


def get_sheet():
    if not SPREADSHEET_ID:
        raise RuntimeError("SPREADSHEET_ID is not set")
    now = datetime.now()
    cached_book = _BOOK_CACHE.get("book")
    expires_at = _BOOK_CACHE.get("expires_at")
    if cached_book and expires_at and now < expires_at:
        return cached_book

    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]
    credentials_json = os.getenv("CREDENTIALS_JSON", "").strip()
    if credentials_json:
        creds = Credentials.from_service_account_info(json.loads(credentials_json), scopes=scopes)
    else:
        creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    client = gspread.authorize(creds)
    book = client.open_by_key(SPREADSHEET_ID)
    _BOOK_CACHE["book"] = book
    _BOOK_CACHE["expires_at"] = now + timedelta(seconds=max(SHEETS_BOOK_CACHE_TTL_SECONDS, 5))
    _BOOK_CACHE["worksheets"] = {}
    return book


def get_cached_worksheet(title, headers=None):
    worksheets = _BOOK_CACHE.setdefault("worksheets", {})
    cached_sheet = worksheets.get(title)
    if cached_sheet is not None:
        return cached_sheet

    book = get_sheet()
    try:
        sheet = book.worksheet(title)
    except WorksheetNotFound:
        if headers is None:
            raise
        sheet = book.add_worksheet(title=title, rows=200, cols=max(len(headers), 1))
        worksheets[title] = sheet
        return sheet

    worksheets[title] = sheet
    return sheet


def ensure_worksheet_group(group_key, worksheets):
    if group_key in _ENSURED_WORKSHEET_GROUPS:
        return
    for title, headers in worksheets:
        get_or_create_worksheet(title, headers)
    _ENSURED_WORKSHEET_GROUPS.add(group_key)


def get_records(worksheet_name, headers):
    records = get_records_with_rows(worksheet_name, headers)
    for record in records:
        record.pop("__row", None)
    return records


def get_or_create_worksheet(title, headers):
    sheet = get_cached_worksheet(title, headers)
    current_headers = sheet.row_values(1)
    if not current_headers:
        sheet.append_row(headers)
        return sheet

    if len(current_headers) < len(headers):
        sheet.add_cols(len(headers) - len(current_headers))
        current_headers = sheet.row_values(1)

    if current_headers[:len(headers)] != headers:
        end_column = chr(ord("A") + len(headers) - 1)
        sheet.update(f"A1:{end_column}1", [headers])
    return sheet


def append_row_and_get_index(sheet, values):
    sheet.append_row(values)
    return len(sheet.get_all_values())


def default_service_salary_workers(who):
    worker = str(who or "").strip()
    if worker in get_paid_workers():
        return [worker]
    return []


def normalize_salary_workers(value):
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple, set)):
        parts = [str(part).strip() for part in value]
    elif value in (None, ""):
        parts = []
    else:
        parts = [str(value).strip()]

    workers = []
    seen = set()
    for part in parts:
        if not part:
            continue
        if part in seen:
            continue
        seen.add(part)
        workers.append(part)
    return order_workers(workers)


def serialize_salary_workers(value):
    return ", ".join(normalize_salary_workers(value))


def get_service_salary_workers(entry):
    raw_value = str(entry.get("В ЗП", "")).strip().lower()
    if raw_value in {"нет", "-", "—", "none"}:
        return []
    workers = normalize_salary_workers(entry.get("В ЗП", ""))
    if workers:
        return workers
    return default_service_salary_workers(entry.get("Кто", ""))


def get_service_salary_workers_from_context(svc):
    if "salary_workers" in svc:
        return normalize_salary_workers(svc.get("salary_workers"))
    return default_service_salary_workers(svc.get("who", ""))


def calculate_service_sum_for_workers(workers):
    paid_workers = set(get_paid_workers())
    paid_count = sum(1 for worker in normalize_salary_workers(workers) if worker in paid_workers)
    return SERVICE_PRICE * paid_count


def build_service_row_values(data):
    salary_workers = normalize_salary_workers(data.get("salary_workers"))
    salary_workers_raw = serialize_salary_workers(salary_workers)
    if not salary_workers_raw and "salary_workers" in data and str(data.get("who", "")).strip() in set(get_paid_workers()):
        salary_workers_raw = "нет"
    return [
        data["date"], data["who"], data["point"], data["water"],
        data.get("shortage", ""), data.get("shortage_qty", ""),
        data.get("purchases", ""), data.get("purchase_sum", 0),
        data.get("service_sum", 0), salary_workers_raw,
    ]


def add_service_row(data):
    sheet = get_or_create_worksheet("Обслуживание", SERVICE_HEADERS)
    return append_row_and_get_index(sheet, build_service_row_values(data))


def update_service_row(row_num, data):
    sheet = get_or_create_worksheet("Обслуживание", SERVICE_HEADERS)
    sheet.update(f"A{row_num}:J{row_num}", [build_service_row_values(data)])


def add_travel_row(date, who, amount):
    return append_row_and_get_index(get_or_create_worksheet("Проезд", TRAVEL_HEADERS), [date, who, amount])


def update_travel_row(row_num, date, who, amount):
    get_or_create_worksheet("Проезд", TRAVEL_HEADERS).update(
        f"A{row_num}:C{row_num}",
        [[date, who, amount]],
    )


def delete_travel_row(row_num):
    get_or_create_worksheet("Проезд", TRAVEL_HEADERS).delete_rows(row_num)


def add_photo_row(date, point, who, file_id):
    return append_row_and_get_index(get_or_create_worksheet("Фото", PHOTO_HEADERS), [date, point, who, file_id])


def update_photo_row(row_num, date, point, who, file_id):
    get_or_create_worksheet("Фото", PHOTO_HEADERS).update(
        f"A{row_num}:D{row_num}",
        [[date, point, who, file_id]],
    )

def get_all_services():
    get_or_create_worksheet("Обслуживание", SERVICE_HEADERS)
    return get_records("Обслуживание", SERVICE_HEADERS)

def get_all_travels():
    get_or_create_worksheet("Проезд", TRAVEL_HEADERS)
    return get_records("Проезд", TRAVEL_HEADERS)


def get_all_travels_with_rows():
    get_or_create_worksheet("Проезд", TRAVEL_HEADERS)
    return get_records_with_rows("Проезд", TRAVEL_HEADERS)


def get_all_photos():
    get_or_create_worksheet("Фото", PHOTO_HEADERS)
    return get_records("Фото", PHOTO_HEADERS)


def get_sheet1_records():
    return get_sheet().sheet1.get_all_records()


def get_records_with_rows(worksheet_name, headers):
    sheet = get_cached_worksheet(worksheet_name, headers)
    values = sheet.get_all_values()
    records = []

    for row_num, row in enumerate(values[1:], start=2):
        if not any(str(cell).strip() for cell in row):
            continue
        record = {header: (row[i] if i < len(row) else "") for i, header in enumerate(headers)}
        record["__row"] = row_num
        records.append(record)

    return records


def get_all_services_with_rows():
    get_or_create_worksheet("Обслуживание", SERVICE_HEADERS)
    return get_records_with_rows("Обслуживание", SERVICE_HEADERS)


def get_all_photos_with_rows():
    get_or_create_worksheet("Фото", PHOTO_HEADERS)
    return get_records_with_rows("Фото", PHOTO_HEADERS)


def get_all_salary_tasks():
    get_or_create_worksheet(SALARY_TASK_SHEET, SALARY_TASK_HEADERS)
    return get_records(SALARY_TASK_SHEET, SALARY_TASK_HEADERS)


def get_all_salary_tasks_with_rows():
    get_or_create_worksheet(SALARY_TASK_SHEET, SALARY_TASK_HEADERS)
    return get_records_with_rows(SALARY_TASK_SHEET, SALARY_TASK_HEADERS)


def add_salary_task_row(entry):
    sheet = get_or_create_worksheet(SALARY_TASK_SHEET, SALARY_TASK_HEADERS)
    return append_row_and_get_index(
        sheet,
        [
            entry.get("date", ""),
            entry.get("who", ""),
            entry.get("description", ""),
            entry.get("amount", ""),
            entry.get("added_by", ""),
        ],
    )


def update_salary_task_row(row_num, entry):
    get_or_create_worksheet(SALARY_TASK_SHEET, SALARY_TASK_HEADERS).update(
        f"A{row_num}:E{row_num}",
        [[
            entry.get("date", ""),
            entry.get("who", ""),
            entry.get("description", ""),
            entry.get("amount", ""),
            entry.get("added_by", ""),
        ]],
    )


def delete_salary_task_row(row_num):
    get_or_create_worksheet(SALARY_TASK_SHEET, SALARY_TASK_HEADERS).delete_rows(row_num)


def ensure_payouts_worksheet():
    ensure_worksheet_group("payouts", [(PAYOUT_SHEET, PAYOUT_HEADERS)])


def build_payout_row_values(entry):
    return [
        entry.get("period", ""),
        entry.get("who", ""),
        entry.get("service_sum", 0),
        entry.get("service_count", 0),
        entry.get("purchase_sum", 0),
        entry.get("travel_sum", 0),
        entry.get("travel_count", 0),
        entry.get("salary_task_sum", 0),
        entry.get("salary_task_count", 0),
        entry.get("correction", 0),
        entry.get("correction_note", ""),
        entry.get("total", 0),
        entry.get("status", PAYOUT_STATUS_PENDING),
        entry.get("paid_date", ""),
        entry.get("paid_by", ""),
    ]


def get_all_payouts():
    ensure_payouts_worksheet()
    return get_records(PAYOUT_SHEET, PAYOUT_HEADERS)


def get_all_payouts_with_rows():
    ensure_payouts_worksheet()
    return get_records_with_rows(PAYOUT_SHEET, PAYOUT_HEADERS)


def find_payout_record(records, period_key, who):
    period_key = str(period_key).strip()
    who = str(who).strip()
    return next(
        (
            record
            for record in records
            if str(record.get("Период", "")).strip() == period_key
            and str(record.get("Кому", "")).strip() == who
        ),
        None,
    )


def get_payout(period_key, who):
    return find_payout_record(get_all_payouts_with_rows(), period_key, who)


def add_payout_row(entry):
    sheet = get_or_create_worksheet(PAYOUT_SHEET, PAYOUT_HEADERS)
    return append_row_and_get_index(sheet, build_payout_row_values(entry))


def update_payout_row(row_num, entry):
    sheet = get_or_create_worksheet(PAYOUT_SHEET, PAYOUT_HEADERS)
    sheet.update(
        f"A{row_num}:O{row_num}",
        [build_payout_row_values(entry)],
    )


def build_payout_entry_from_record(record):
    if not record:
        return {
            "period": "",
            "who": "",
            "service_sum": 0,
            "service_count": 0,
            "purchase_sum": 0,
            "travel_sum": 0,
            "travel_count": 0,
            "salary_task_sum": 0,
            "salary_task_count": 0,
            "correction": 0,
            "correction_note": "",
            "total": 0,
            "status": PAYOUT_STATUS_PENDING,
            "paid_date": "",
            "paid_by": "",
        }

    return {
        "period": str(record.get("Период", "")).strip(),
        "who": str(record.get("Кому", "")).strip(),
        "service_sum": parse_numeric_value(record.get("Сумма обсл", "")) or 0,
        "service_count": int(parse_numeric_value(record.get("Кол-во обсл", "")) or 0),
        "purchase_sum": parse_numeric_value(record.get("Сумма закупок", "")) or 0,
        "travel_sum": parse_numeric_value(record.get("Сумма проезда", "")) or 0,
        "travel_count": int(parse_numeric_value(record.get("Кол-во записей проезда", "")) or 0),
        "salary_task_sum": parse_numeric_value(record.get("Сумма доплат", "")) or 0,
        "salary_task_count": int(parse_numeric_value(record.get("Кол-во доплат", "")) or 0),
        "correction": parse_numeric_value(record.get("Корректировка", "")) or 0,
        "correction_note": str(record.get("Комм. корректировки", "")).strip(),
        "total": parse_numeric_value(record.get("Итого", "")) or 0,
        "status": str(record.get("Статус", "")).strip() or PAYOUT_STATUS_PENDING,
        "paid_date": str(record.get("Дата перевода", "")).strip(),
        "paid_by": str(record.get("Кто отметил", "")).strip(),
    }


def upsert_payout(period_key, who, payload):
    period_key = str(period_key).strip()
    who = str(who).strip()
    payouts = get_all_payouts_with_rows()
    existing = find_payout_record(payouts, period_key, who)
    entry = build_payout_entry_from_record(existing)
    entry.update(payload)
    entry["period"] = period_key
    entry["who"] = who
    if existing and existing.get("__row"):
        update_payout_row(existing["__row"], entry)
        entry["__row"] = existing["__row"]
        return entry

    row_num = add_payout_row(entry)
    entry["__row"] = row_num
    return entry


def mark_payout_paid(period_key, who, paid_by, snapshot):
    return upsert_payout(
        period_key,
        who,
        {
            "service_sum": snapshot.get("service_sum", 0),
            "service_count": snapshot.get("service_count", 0),
            "purchase_sum": snapshot.get("purchase_sum", 0),
            "travel_sum": snapshot.get("travel_sum", 0),
            "travel_count": snapshot.get("travel_count", 0),
            "salary_task_sum": snapshot.get("salary_task_sum", 0),
            "salary_task_count": snapshot.get("salary_task_count", 0),
            "correction": snapshot.get("correction", 0),
            "correction_note": snapshot.get("correction_note", ""),
            "total": snapshot.get("total", 0),
            "status": PAYOUT_STATUS_PAID,
            "paid_date": today(),
            "paid_by": paid_by,
        },
    )


def unmark_payout_paid(period_key, who):
    return upsert_payout(
        period_key,
        who,
        {
            "status": PAYOUT_STATUS_PENDING,
            "paid_date": "",
            "paid_by": "",
        },
    )


def get_all_revisions():
    sheet = get_or_create_worksheet("Ревизия", REVISION_HEADERS)
    values = sheet.get_all_values()
    records = []
    for row in values[1:]:
        if not any(str(cell).strip() for cell in row):
            continue
        record = {header: (row[i] if i < len(row) else "") for i, header in enumerate(REVISION_HEADERS)}
        records.append(record)
    return records


def ensure_rent_worksheets():
    worksheets = [
        (RENT_SHEET_LANDLORDS, RENT_LANDLORD_HEADERS),
        (RENT_SHEET_LEASES, RENT_LEASE_HEADERS),
        (RENT_SHEET_PAYMENTS, RENT_PAYMENT_HEADERS),
        (RENT_SHEET_INDEXATIONS, RENT_INDEXATION_HEADERS),
        (RENT_SHEET_DOCUMENTS, RENT_DOCUMENT_HEADERS),
        (RENT_SHEET_SERVICE, RENT_SERVICE_HEADERS),
    ]
    ensure_worksheet_group("rent", worksheets)


def get_all_rent_landlords():
    get_or_create_worksheet(RENT_SHEET_LANDLORDS, RENT_LANDLORD_HEADERS)
    return get_records(RENT_SHEET_LANDLORDS, RENT_LANDLORD_HEADERS)


def get_all_rent_leases():
    get_or_create_worksheet(RENT_SHEET_LEASES, RENT_LEASE_HEADERS)
    return get_records(RENT_SHEET_LEASES, RENT_LEASE_HEADERS)


def get_all_rent_leases_with_rows():
    get_or_create_worksheet(RENT_SHEET_LEASES, RENT_LEASE_HEADERS)
    return get_records_with_rows(RENT_SHEET_LEASES, RENT_LEASE_HEADERS)


def get_all_rent_payments():
    get_or_create_worksheet(RENT_SHEET_PAYMENTS, RENT_PAYMENT_HEADERS)
    return get_records(RENT_SHEET_PAYMENTS, RENT_PAYMENT_HEADERS)


def get_all_rent_payments_with_rows():
    get_or_create_worksheet(RENT_SHEET_PAYMENTS, RENT_PAYMENT_HEADERS)
    return get_records_with_rows(RENT_SHEET_PAYMENTS, RENT_PAYMENT_HEADERS)


def get_all_rent_indexations():
    get_or_create_worksheet(RENT_SHEET_INDEXATIONS, RENT_INDEXATION_HEADERS)
    return get_records(RENT_SHEET_INDEXATIONS, RENT_INDEXATION_HEADERS)


def get_all_rent_documents():
    get_or_create_worksheet(RENT_SHEET_DOCUMENTS, RENT_DOCUMENT_HEADERS)
    return get_records(RENT_SHEET_DOCUMENTS, RENT_DOCUMENT_HEADERS)


def add_rent_payment_row(entry):
    sheet = get_or_create_worksheet(RENT_SHEET_PAYMENTS, RENT_PAYMENT_HEADERS)
    return append_row_and_get_index(sheet, [
        entry.get("id", ""),
        entry.get("lease_id", ""),
        entry.get("point", ""),
        entry.get("period", ""),
        entry.get("amount", ""),
        entry.get("paid_date", ""),
        entry.get("paid_by", ""),
        entry.get("status", ""),
        entry.get("receipt_file_id", ""),
        entry.get("notes", ""),
    ])


def add_rent_document_row(entry):
    sheet = get_or_create_worksheet(RENT_SHEET_DOCUMENTS, RENT_DOCUMENT_HEADERS)
    return append_row_and_get_index(sheet, [
        entry.get("id", ""),
        entry.get("point", ""),
        entry.get("doc_type", ""),
        entry.get("title", ""),
        entry.get("related_type", ""),
        entry.get("related_id", ""),
        entry.get("file_id", ""),
        entry.get("uploaded_at", ""),
        entry.get("uploaded_by", ""),
    ])


def ensure_repair_worksheets():
    worksheets = [
        (REPAIR_SHEET_CENTERS, REPAIR_CENTER_HEADERS),
        (REPAIR_SHEET_MACHINES, REPAIR_MACHINE_HEADERS),
        (REPAIR_SHEET_REPAIRS, REPAIR_HEADERS),
        (REPAIR_SHEET_EXPENSES, REPAIR_EXPENSE_HEADERS),
        (REPAIR_SHEET_DOCUMENTS, REPAIR_DOCUMENT_HEADERS),
        (REPAIR_SHEET_SERVICE, REPAIR_SERVICE_HEADERS),
    ]
    ensure_worksheet_group("repair", worksheets)


def get_all_repair_centers():
    ensure_repair_worksheets()
    return get_records(REPAIR_SHEET_CENTERS, REPAIR_CENTER_HEADERS)


def add_repair_center_row(entry):
    ensure_repair_worksheets()
    sheet = get_or_create_worksheet(REPAIR_SHEET_CENTERS, REPAIR_CENTER_HEADERS)
    return append_row_and_get_index(sheet, [
        entry.get("id", ""),
        entry.get("Название", ""),
        entry.get("Город", ""),
        entry.get("Контактное лицо", ""),
        entry.get("Телефон", ""),
        entry.get("Email", ""),
        entry.get("Адрес", ""),
        entry.get("Специализация", ""),
        entry.get("Заметки", ""),
    ])


def get_all_repair_machines():
    ensure_repair_worksheets()
    return get_records(REPAIR_SHEET_MACHINES, REPAIR_MACHINE_HEADERS)


def get_all_repair_machines_with_rows():
    ensure_repair_worksheets()
    return get_records_with_rows(REPAIR_SHEET_MACHINES, REPAIR_MACHINE_HEADERS)


def get_all_repairs():
    ensure_repair_worksheets()
    return get_records(REPAIR_SHEET_REPAIRS, REPAIR_HEADERS)


def get_all_repairs_with_rows():
    ensure_repair_worksheets()
    return get_records_with_rows(REPAIR_SHEET_REPAIRS, REPAIR_HEADERS)


def get_all_repair_expenses():
    ensure_repair_worksheets()
    return get_records(REPAIR_SHEET_EXPENSES, REPAIR_EXPENSE_HEADERS)


def get_all_repair_documents():
    ensure_repair_worksheets()
    return get_records(REPAIR_SHEET_DOCUMENTS, REPAIR_DOCUMENT_HEADERS)


def build_repair_machine_row_values(entry):
    return [
        entry.get("id", ""),
        entry.get("Точка", ""),
        entry.get("Бренд", ""),
        entry.get("Модель", ""),
        entry.get("Серийный номер", ""),
        entry.get("Дата покупки", ""),
        entry.get("Гарантия до", ""),
        entry.get("Статус", ""),
        entry.get("Заметки", ""),
    ]


def add_repair_machine_row(entry):
    sheet = get_or_create_worksheet(REPAIR_SHEET_MACHINES, REPAIR_MACHINE_HEADERS)
    return append_row_and_get_index(sheet, build_repair_machine_row_values(entry))


def update_repair_machine_row(row_num, entry):
    sheet = get_or_create_worksheet(REPAIR_SHEET_MACHINES, REPAIR_MACHINE_HEADERS)
    sheet.update(f"A{row_num}:I{row_num}", [build_repair_machine_row_values(entry)])


def build_repair_row_values(entry):
    return [
        entry.get("id", ""),
        entry.get("Аппарат ID", ""),
        entry.get("Точка", ""),
        entry.get("Сервис ID", ""),
        entry.get("Причина", ""),
        entry.get("Описание поломки", ""),
        entry.get("Статус", ""),
        entry.get("Дата поломки", ""),
        entry.get("Дата отправки", ""),
        entry.get("Дата готовности (план)", ""),
        entry.get("Дата готовности (факт)", ""),
        entry.get("Дата возврата", ""),
        entry.get("Перевозка откуда", ""),
        entry.get("Перевозка куда", ""),
        entry.get("На гарантии", ""),
        entry.get("Итого расходов", ""),
        entry.get("Кто создал", ""),
        entry.get("Заметки", ""),
    ]


def add_repair_row(entry):
    sheet = get_or_create_worksheet(REPAIR_SHEET_REPAIRS, REPAIR_HEADERS)
    return append_row_and_get_index(sheet, build_repair_row_values(entry))


def update_repair_row(row_num, entry):
    sheet = get_or_create_worksheet(REPAIR_SHEET_REPAIRS, REPAIR_HEADERS)
    sheet.update(f"A{row_num}:R{row_num}", [build_repair_row_values(entry)])


def add_repair_expense_row(entry):
    sheet = get_or_create_worksheet(REPAIR_SHEET_EXPENSES, REPAIR_EXPENSE_HEADERS)
    return append_row_and_get_index(sheet, [
        entry.get("id", ""),
        entry.get("Ремонт ID", ""),
        entry.get("Тип расхода", ""),
        entry.get("Описание", ""),
        entry.get("Сумма", ""),
        entry.get("Дата", ""),
        entry.get("Оплачено", ""),
        entry.get("Кто отметил", ""),
        entry.get("Документ file_id", ""),
        entry.get("Заметки", ""),
    ])


def add_repair_document_row(entry):
    sheet = get_or_create_worksheet(REPAIR_SHEET_DOCUMENTS, REPAIR_DOCUMENT_HEADERS)
    return append_row_and_get_index(sheet, [
        entry.get("id", ""),
        entry.get("Ремонт ID", ""),
        entry.get("Точка", ""),
        entry.get("Тип", ""),
        entry.get("Название", ""),
        entry.get("file_id", ""),
        entry.get("Дата загрузки", ""),
        entry.get("Кто загрузил", ""),
    ])


def get_group_report_logs_with_rows():
    sheet = get_or_create_worksheet("Импорт группы", GROUP_REPORT_LOG_HEADERS)
    values = sheet.get_all_values()
    records = []
    for row_num, row in enumerate(values[1:], start=2):
        if not any(str(cell).strip() for cell in row):
            continue
        record = {header: (row[i] if i < len(row) else "") for i, header in enumerate(GROUP_REPORT_LOG_HEADERS)}
        record["__row"] = row_num
        records.append(record)
    return records


def append_group_report_log(entry):
    sheet = get_or_create_worksheet("Импорт группы", GROUP_REPORT_LOG_HEADERS)
    values = [
        entry.get("chat_id", ""),
        entry.get("source_key", ""),
        entry.get("source_message_id", ""),
        entry.get("media_group_id", ""),
        entry.get("who", ""),
        entry.get("point", ""),
        entry.get("date", ""),
        entry.get("fingerprint", ""),
        entry.get("service_row", ""),
        entry.get("photo_rows", ""),
        entry.get("revision_row", ""),
        entry.get("revision_period", ""),
        entry.get("revision_location", ""),
        entry.get("revision_mode", ""),
        entry.get("revision_backup", ""),
        entry.get("status", ""),
        entry.get("created_at", ""),
    ]
    return append_row_and_get_index(sheet, values)


def update_group_report_log(row_num, entry):
    sheet = get_or_create_worksheet("Импорт группы", GROUP_REPORT_LOG_HEADERS)
    sheet.update(
        f"A{row_num}:Q{row_num}",
        [[
            entry.get("chat_id", ""),
            entry.get("source_key", ""),
            entry.get("source_message_id", ""),
            entry.get("media_group_id", ""),
            entry.get("who", ""),
            entry.get("point", ""),
            entry.get("date", ""),
            entry.get("fingerprint", ""),
            entry.get("service_row", ""),
            entry.get("photo_rows", ""),
            entry.get("revision_row", ""),
            entry.get("revision_period", ""),
            entry.get("revision_location", ""),
            entry.get("revision_mode", ""),
            entry.get("revision_backup", ""),
            entry.get("status", ""),
            entry.get("created_at", ""),
        ]],
    )


def get_all_revisions_with_rows():
    sheet = get_or_create_worksheet("Ревизия", REVISION_HEADERS)
    values = sheet.get_all_values()
    records = []
    for row_num, row in enumerate(values[1:], start=2):
        if not any(str(cell).strip() for cell in row):
            continue
        record = {header: (row[i] if i < len(row) else "") for i, header in enumerate(REVISION_HEADERS)}
        record["__row"] = row_num
        records.append(record)
    return records


def build_revision_row_values(data):
    values = data.get("values", {})
    return [
        data["period"],
        data["location"],
        data["who"],
        data["filled_at"],
        *[values.get(item, "") for item in REVISION_ITEMS],
    ]


def add_revision_row(data):
    sheet = get_or_create_worksheet("Ревизия", REVISION_HEADERS)
    return append_row_and_get_index(sheet, build_revision_row_values(data))


def update_revision_row(row_num, data):
    sheet = get_or_create_worksheet("Ревизия", REVISION_HEADERS)
    sheet.update(
        f"A{row_num}:{chr(ord('A') + len(REVISION_HEADERS) - 1)}{row_num}",
        [build_revision_row_values(data)],
    )


def delete_revision_row(row_num):
    get_or_create_worksheet("Ревизия", REVISION_HEADERS).delete_rows(row_num)


def delete_service_entry(service_row_num, photo_row_num=None):
    book = get_sheet()
    book.worksheet("Обслуживание").delete_rows(service_row_num)
    if photo_row_num:
        book.worksheet("Фото").delete_rows(photo_row_num)


def delete_service_entries_for_date(date_str):
    service_rows = sorted(
        [entry["__row"] for entry in get_all_services_with_rows() if entry.get("Дата") == date_str],
        reverse=True,
    )
    photo_rows = sorted(
        [entry["__row"] for entry in get_all_photos_with_rows() if entry.get("Дата") == date_str],
        reverse=True,
    )

    book = get_sheet()
    service_sheet = book.worksheet("Обслуживание")
    photo_sheet = book.worksheet("Фото")

    for row_num in service_rows:
        service_sheet.delete_rows(row_num)

    for row_num in photo_rows:
        photo_sheet.delete_rows(row_num)

    return len(service_rows), len(photo_rows)

def today():
    return now_local().strftime("%d.%m.%Y")


def format_date(value):
    return value.strftime("%d.%m.%Y")


def yesterday():
    return format_date(now_local() - timedelta(days=1))


def day_before_yesterday():
    return format_date(now_local() - timedelta(days=2))


def now_local():
    return datetime.now(BOT_TIMEZONE)


def parse_date(date_str):
    try:
        return datetime.strptime(str(date_str).strip(), "%d.%m.%Y")
    except (TypeError, ValueError):
        return None


def format_group_report_created_at(dt=None):
    return (dt or now_local()).strftime("%Y-%m-%d %H:%M:%S")


def parse_group_report_created_at(value):
    raw = str(value or "").strip()
    if not raw:
        return None

    for fmt in ("%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            parsed = datetime.strptime(raw, fmt)
            return parsed.replace(tzinfo=BOT_TIMEZONE)
        except ValueError:
            continue
    return None


def is_group_report_action_window_open(record):
    created_at = parse_group_report_created_at(record.get("Создано", ""))
    if not created_at:
        return False
    return now_local() - created_at <= timedelta(seconds=max(GROUP_REPORT_ACTION_WINDOW_SECONDS, 1))


def get_period_key_for_date(date_str):
    parsed = parse_date(date_str)
    if not parsed:
        return None
    return build_period_key(parsed.year, parsed.month)


def is_date_in_period_key(date_str, period_key):
    return get_period_key_for_date(date_str) == str(period_key).strip()


def get_period_restriction_error(date_str, period_key):
    if period_key and not is_date_in_period_key(date_str, period_key):
        return f"❌ Нужна дата внутри {format_period_label(period_key)}."
    return None


def validate_manual_date_input(date_str, allow_future=False, max_future_days=365):
    raw = str(date_str).strip()
    parsed = parse_date(raw)

    if not parsed:
        parts = raw.split(".")
        if len(parts) == 2:
            try:
                day = int(parts[0])
                month = int(parts[1])
            except ValueError:
                return None, "❌ Введите дату в формате дд.мм, например 05.04."

            today_date = now_local().date()
            try:
                current_year_candidate = datetime(today_date.year, month, day).date()
            except ValueError:
                current_year_candidate = None

            try:
                previous_year_candidate = datetime(today_date.year - 1, month, day).date()
            except ValueError:
                previous_year_candidate = None

            if current_year_candidate and current_year_candidate <= today_date:
                parsed = datetime.combine(current_year_candidate, datetime.min.time())
            elif current_year_candidate and month > today_date.month and previous_year_candidate:
                parsed = datetime.combine(previous_year_candidate, datetime.min.time())
            elif current_year_candidate and allow_future:
                parsed = datetime.combine(current_year_candidate, datetime.min.time())
            elif current_year_candidate:
                return None, f"❌ Нельзя указывать дату позже {format_date(today_date)}."
            else:
                return None, "❌ Введите дату в формате дд.мм, например 05.04."

    if not parsed:
        return None, "❌ Введите дату в формате дд.мм, например 05.04."

    today_date = now_local().date()
    min_date = today_date - timedelta(days=365)
    parsed_date = parsed.date()

    if parsed_date > today_date and not allow_future:
        return None, f"❌ Нельзя указывать дату позже {format_date(today_date)}."
    if parsed_date > today_date and allow_future:
        max_date = today_date + timedelta(days=max_future_days)
        if parsed_date > max_date:
            return None, f"❌ Можно указать дату не позже {format_date(max_date)}."
    if parsed_date < min_date:
        return None, f"❌ Можно указать дату не старше {format_date(min_date)}."

    return parsed, None


def normalize_number_text(value):
    raw = str(value).strip().replace(",", ".")
    if not raw:
        raise ValueError("empty")

    number = float(raw)
    if number < 0:
        raise ValueError("negative")

    if number.is_integer():
        return str(int(number))
    return f"{number:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def format_number(value):
    if value in (None, ""):
        return ""
    try:
        return normalize_number_text(value)
    except (TypeError, ValueError):
        return str(value)


def format_money(value):
    formatted = format_number(value)
    return f"{formatted}₽" if formatted else "0₽"


def normalize_service_report_point_name(value):
    return SERVICE_REPORT_POINT_ALIASES.get(normalize_text_key(value))


def contains_normalized_alias(text, alias):
    pattern = rf"(^|[^\w]){re.escape(alias)}([^\w]|$)"
    return re.search(pattern, text) is not None


def extract_service_report_point(text):
    candidates = []
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if lines:
        candidates.append(lines[0])
    candidates.append(str(text or ""))

    alias_items = sorted(SERVICE_REPORT_POINT_ALIASES.items(), key=lambda item: len(item[0]), reverse=True)
    for candidate in candidates:
        normalized = normalize_text_key(candidate)
        for alias, point in alias_items:
            if contains_normalized_alias(normalized, alias):
                return point
    return None


def extract_service_report_date(text):
    match = re.search(r"\b(\d{1,2}\.\d{1,2}(?:\.\d{4})?)\b", str(text or ""))
    if not match:
        return None, "не найдена дата"

    parsed, error = validate_manual_date_input(match.group(1))
    if error:
        return None, error
    return format_date(parsed), None


def extract_service_report_water(text):
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue

        match = re.search(r"вод(?:а|ы)?\s*[-: ]*\s*(\d+(?:[.,]\d+)?)", line, flags=re.IGNORECASE)
        if not match:
            match = re.search(r"(\d+(?:[.,]\d+)?)\s*бут", line, flags=re.IGNORECASE)
        if not match:
            continue

        try:
            return normalize_number_text(match.group(1))
        except ValueError:
            continue
    return ""


def extract_supply_names_from_text(text):
    normalized = normalize_text_key(text)
    found = []
    alias_items = sorted(SERVICE_REPORT_SUPPLY_ALIASES.items(), key=lambda item: len(item[0]), reverse=True)
    for alias, item_name in alias_items:
        if contains_normalized_alias(normalized, alias) and item_name not in found:
            found.append(item_name)
    return found


def is_service_report_water_line(text):
    return bool(re.search(r"\bвод(?:а|ы)?\b", normalize_text_key(text)))


def is_revision_like_service_report(text):
    inventory_lines = 0
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or is_service_report_water_line(line) or "-" not in line:
            continue

        raw_item, raw_value = [part.strip() for part in line.split("-", 1)]
        if extract_supply_names_from_text(raw_item) and parse_import_number(raw_value) is not None:
            inventory_lines += 1
        if inventory_lines >= 3:
            return True
    return False


def extract_service_report_shortage_items(text):
    normalized_text = normalize_text_key(text)
    if any(phrase in normalized_text for phrase in SERVICE_REPORT_OK_PHRASES):
        return []

    items = []
    in_shortage_block = False
    for raw_line in str(text or "").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue

        cleaned = stripped.lstrip("-•").strip()
        normalized_line = normalize_text_key(cleaned)
        if not normalized_line:
            continue

        if is_service_report_water_line(normalized_line):
            in_shortage_block = False
            continue

        trigger = next(
            (prefix for prefix in SERVICE_REPORT_SHORTAGE_TRIGGERS if normalized_line.startswith(prefix)),
            None,
        )
        if trigger:
            in_shortage_block = True
            remainder = re.sub(r"^[^:]+:\s*", "", cleaned, count=1)
            found = extract_supply_names_from_text(remainder)
            for item_name in found:
                if item_name not in items:
                    items.append(item_name)
            continue

        if in_shortage_block:
            found = extract_supply_names_from_text(cleaned)
            if found:
                for item_name in found:
                    if item_name not in items:
                        items.append(item_name)
                continue
            in_shortage_block = False

        if stripped.startswith(("-", "•")):
            found = extract_supply_names_from_text(cleaned)
            for item_name in found:
                if item_name not in items:
                    items.append(item_name)

    return items


def get_service_report_author(message):
    forward_origin = getattr(message, "forward_origin", None)
    if forward_origin:
        sender_user = getattr(forward_origin, "sender_user", None)
        if sender_user:
            return (
                get_configured_user_name(getattr(sender_user, "id", None))
                or resolve_worker_name(getattr(sender_user, "first_name", ""))
                or resolve_worker_name(getattr(sender_user, "username", ""))
                or getattr(sender_user, "first_name", None)
                or getattr(sender_user, "username", None)
                or str(getattr(sender_user, "id", "Неизвестно"))
            )

        sender_user_name = getattr(forward_origin, "sender_user_name", None)
        matched_name = resolve_worker_name(sender_user_name)
        if matched_name:
            return matched_name
        if sender_user_name:
            return str(sender_user_name).strip()

    tg_user = getattr(message, "from_user", None)
    if not tg_user:
        return "Неизвестно"
    return get_configured_user_name(tg_user.id) or tg_user.first_name or tg_user.username or str(tg_user.id)


def get_message_local_date(message):
    message_dt = getattr(message, "date", None)
    if not isinstance(message_dt, datetime):
        return today()
    try:
        local_dt = message_dt.astimezone(BOT_TIMEZONE)
    except Exception:
        local_dt = message_dt
    return format_date(local_dt.date())


def parse_group_travel_message_text(text):
    raw_text = str(text or "").strip()
    if not raw_text:
        return None

    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    if not lines:
        return None

    amounts = []
    labels = []
    for line in lines:
        match = re.match(
            r"^(автобус(?:ы)?|проезд|такси|метро|трамвай|электричка)\s*[-–—:]\s*(\d+(?:[.,]\d+)?)\s*(?:₽|р|руб(?:\.|лей|ля)?)?$",
            line,
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        try:
            amount = normalize_number_text(match.group(2))
        except ValueError:
            return None
        labels.append(match.group(1).strip())
        amounts.append(amount)

    if not amounts:
        return None

    return {
        "items": [{"label": label, "amount": amount} for label, amount in zip(labels, amounts)],
        "amounts": amounts,
        "source_text": raw_text,
    }


def parse_service_report_message_text(text, has_photo=False):
    raw_text = str(text or "").strip()
    if not raw_text:
        return None

    point = extract_service_report_point(raw_text)
    date, date_error = extract_service_report_date(raw_text)
    water = extract_service_report_water(raw_text)
    shortage_items = extract_service_report_shortage_items(raw_text)

    if not point or not date:
        return None

    if not has_photo and not water and not shortage_items:
        return None

    warnings = []
    if date_error:
        warnings.append(date_error)
    if not water:
        warnings.append("не нашёл воду, запись сохранится без воды")

    return {
        "point": point,
        "date": date,
        "water": water,
        "shortage_items": shortage_items,
        "warnings": warnings,
        "source_text": raw_text,
    }


def build_group_report_preview_text(draft):
    lines = [
        "📥 Черновик из сообщения",
        "",
        f"📍 {draft['point']}",
        f"📅 {draft['date']}",
        f"👤 {draft['who']}",
        f"💧 Вода: {format_number(draft.get('water', '')) or 'не указана'}",
    ]

    shortage_items = draft.get("shortage_items", [])
    if shortage_items:
        lines.append("⚠️ Нехватка:")
        lines.extend(f"• {item}" for item in shortage_items)
    else:
        lines.append("✅ Всё в наличии")

    photo_count = len(draft.get("photo_ids", []))
    if photo_count:
        lines.append(f"📸 Фото: {photo_count}")

    warnings = draft.get("warnings", [])
    if warnings:
        lines.append("")
        lines.append("⚠️ Проверь перед сохранением:")
        lines.extend(f"• {warning}" for warning in warnings)

    return "\n".join(lines)


def build_group_report_duplicate_warning_text(draft, duplicates):
    lines = [
        "⚠️ Похоже, такое обслуживание уже есть.",
        "",
        build_group_report_preview_text(draft),
        "",
        "Совпадения в базе:",
    ]

    for entry in duplicates[:3]:
        parts = [
            f"• #{entry.get('__row', '?')}",
            f"  📍 {entry.get('Точка', '?')}",
            f"  📅 {entry.get('Дата', '?')}",
            f"  👤 {entry.get('Кто', '?')}",
            f"  💧 Вода: {format_number(entry.get('Вода(бут)', '?')) or 'не указана'}",
        ]
        purchase_sum = parse_numeric_value(entry.get("Сумма закупок", ""))
        if purchase_sum:
            parts.append(f"  🛒 Закупки: {format_money(purchase_sum)}")
        lines.extend(parts)
        lines.append("")

    if len(duplicates) > 3:
        lines.append(f"… и ещё {len(duplicates) - 3} совпад.")
        lines.append("")

    lines.append("Если это отдельный повторный выезд, сохрани его отдельной записью.")
    return "\n".join(lines)


def build_group_report_duplicate_draft_markup(draft_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Сохранить как отдельное", callback_data=f"grp_report_save_{draft_id}")],
        [InlineKeyboardButton("❌ Не учитывать", callback_data=f"grp_report_ignore_{draft_id}")],
    ])


def get_group_report_drafts(bot_data):
    return bot_data.setdefault("group_report_drafts", {})


def cleanup_expired_group_report_drafts(bot_data):
    drafts = get_group_report_drafts(bot_data)
    now_ts = datetime.now().timestamp()
    expired_ids = []
    for draft_id, draft in drafts.items():
        created_at_ts = draft.get("created_at_ts")
        if not created_at_ts:
            continue
        if now_ts - created_at_ts > GROUP_REPORT_DRAFT_TTL_SECONDS:
            expired_ids.append(draft_id)

    for draft_id in expired_ids:
        drafts.pop(draft_id, None)


def next_group_report_draft_id(bot_data):
    counter = int(bot_data.get("group_report_draft_counter", 0)) + 1
    bot_data["group_report_draft_counter"] = counter
    return str(counter)


def build_group_report_payload(draft):
    shortage_items = draft.get("shortage_items", [])
    salary_workers = default_service_salary_workers(draft.get("who"))
    service_sum = calculate_service_sum_for_workers(salary_workers)
    return {
        "date": draft["date"],
        "who": draft["who"],
        "point": draft["point"],
        "water": draft.get("water", ""),
        "shortage": ", ".join(shortage_items),
        "shortage_qty": "",
        "purchases": "",
        "purchase_sum": 0,
        "service_sum": service_sum,
        "salary_workers": salary_workers,
    }


def build_group_report_revision_data(draft):
    raw_text = str(draft.get("source_text", "")).strip()
    if not raw_text or not is_revision_like_service_report(raw_text):
        return None, []

    parsed, warnings = parse_revision_import_text(raw_text)
    warnings = list(warnings)
    if not parsed:
        return None, warnings or ["не удалось распознать ревизию в сообщении"]

    location = draft.get("point", "")
    if location not in parsed:
        if len(parsed) == 1:
            location = next(iter(parsed))
            warnings.append(f"ревизию зачёл по локации «{location}»")
        else:
            warnings.append("нашёл несколько локаций, ревизию не сохранил автоматически")
            return None, warnings

    values = parsed.get(location, {})
    if not values:
        return None, warnings or ["не нашёл значений ревизии для точки"]

    period = get_period_key_for_date(draft.get("date", ""))
    if not period:
        warnings.append("не удалось определить месяц ревизии")
        return None, warnings

    return {
        "period": period,
        "location": location,
        "values": values,
    }, warnings


def build_group_report_source_key(message):
    media_group_id = getattr(message, "media_group_id", None)
    if media_group_id:
        return f"media:{media_group_id}"
    return f"msg:{message.message_id}"


def build_group_report_fingerprint(draft):
    shortage_items = sorted(normalize_text_key(item) for item in draft.get("shortage_items", []))
    normalized_source = re.sub(r"\s+", " ", normalize_text_key(draft.get("source_text", ""))).strip()
    raw = "||".join([
        normalize_text_key(draft.get("point", "")),
        normalize_text_key(draft.get("date", "")),
        normalize_text_key(draft.get("water", "")),
        normalize_text_key(draft.get("who", "")),
        "|".join(shortage_items),
        normalized_source,
        str(len(draft.get("photo_ids", []))),
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def build_group_report_revision_backup(record):
    if not record:
        return ""
    payload = {
        "who": record.get("Кто", ""),
        "filled_at": record.get("Дата заполнения", ""),
        "values": build_revision_values_from_record(record),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def find_group_report_log_entry(chat_id, source_key):
    for record in reversed(get_group_report_logs_with_rows()):
        if str(record.get("Chat_ID", "")) == str(chat_id) and str(record.get("Source_Key", "")) == str(source_key):
            return record
    return None


def get_group_report_log_entry_by_row(row_num):
    return next((entry for entry in get_group_report_logs_with_rows() if entry.get("__row") == row_num), None)


def find_group_report_duplicate(chat_id, source_key, fingerprint):
    matched_source = None
    matched_fingerprint = None
    for record in reversed(get_group_report_logs_with_rows()):
        same_chat = str(record.get("Chat_ID", "")) == str(chat_id)
        if same_chat and not matched_source and str(record.get("Source_Key", "")) == str(source_key):
            matched_source = record
        if (
            not matched_fingerprint
            and fingerprint
            and str(record.get("Fingerprint", "")) == str(fingerprint)
            and record.get("Статус") == "saved"
        ):
            matched_fingerprint = record
        if matched_source and matched_fingerprint:
            break
    return matched_source, matched_fingerprint


def serialize_row_numbers(row_numbers):
    return ",".join(str(row_num) for row_num in row_numbers if row_num)


def parse_logged_row_numbers(raw_value):
    values = []
    for chunk in str(raw_value or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            values.append(int(chunk))
        except ValueError:
            continue
    return values


def save_group_report_revision(draft):
    revision = draft.get("revision")
    if not revision:
        return None

    existing = find_revision_record(revision["period"], revision["location"], True)
    values = build_revision_values_from_record(existing) if existing else {item: "" for item in REVISION_ITEMS}
    for item_name, value in revision["values"].items():
        values[item_name] = value

    payload = {
        "period": revision["period"],
        "location": revision["location"],
        "who": draft.get("who", ""),
        "filled_at": today(),
        "values": values,
    }
    if existing:
        update_revision_row(existing["__row"], payload)
        return {
            "row": existing["__row"],
            "period": revision["period"],
            "location": revision["location"],
            "mode": "updated",
            "backup": build_group_report_revision_backup(existing),
        }

    row_num = add_revision_row(payload)
    return {
        "row": row_num,
        "period": revision["period"],
        "location": revision["location"],
        "mode": "created",
        "backup": "",
    }


def find_group_report_service_entry(record):
    service_row_raw = str(record.get("Service_Row", "")).strip()
    entries = get_all_services_with_rows()

    try:
        service_row = int(service_row_raw)
    except ValueError:
        service_row = None

    if service_row is not None:
        match = next((entry for entry in entries if entry.get("__row") == service_row), None)
        if match:
            return match

    point = str(record.get("Точка", "")).strip()
    date_value = str(record.get("Дата", "")).strip()
    who = str(record.get("Кто", "")).strip()
    matches = [
        entry for entry in entries
        if str(entry.get("Точка", "")).strip() == point
        and str(entry.get("Дата", "")).strip() == date_value
        and str(entry.get("Кто", "")).strip() == who
    ]
    return max(matches, key=lambda entry: entry.get("__row", 0)) if matches else None


def find_group_report_revision_entry(record):
    period = str(record.get("Revision_Period", "")).strip()
    location = str(record.get("Revision_Location", "")).strip()
    row_raw = str(record.get("Revision_Row", "")).strip()

    if period and location:
        try:
            row_num = int(row_raw)
        except ValueError:
            row_num = None

        matches = [
            entry for entry in get_all_revisions_with_rows()
            if str(entry.get("Период", "")).strip() == period
            and str(entry.get("Локация", "")).strip() == location
        ]
        if row_num is not None:
            exact = next((entry for entry in matches if entry.get("__row") == row_num), None)
            if exact:
                return exact
        if matches:
            return max(matches, key=lambda entry: entry.get("__row", 0))
    return None


def restore_group_report_revision(record):
    revision_mode = str(record.get("Revision_Mode", "")).strip() or "created"
    period = str(record.get("Revision_Period", "")).strip()
    location = str(record.get("Revision_Location", "")).strip()
    if not period or not location:
        return "missing"

    current = find_group_report_revision_entry(record)
    if revision_mode == "updated":
        backup_raw = str(record.get("Revision_Backup", "")).strip()
        if not backup_raw:
            return "missing"
        snapshot = json.loads(backup_raw)
        payload = {
            "period": period,
            "location": location,
            "who": snapshot.get("who", ""),
            "filled_at": snapshot.get("filled_at", ""),
            "values": snapshot.get("values", {}),
        }
        if current:
            update_revision_row(current["__row"], payload)
        else:
            add_revision_row(payload)
        return "restored"

    if not current:
        return "missing"

    delete_revision_row(current["__row"])
    return "deleted"


def build_group_report_saved_text(draft, save_result=None):
    save_result = save_result or {}
    lines = [
        "✅ Отчёт сохранён",
        "",
        f"📍 {draft['point']}",
        f"📅 {draft['date']}",
        f"👤 {draft['who']}",
        f"💧 Вода: {format_number(draft.get('water', '')) or 'не указана'}",
    ]

    shortage_items = draft.get("shortage_items", [])
    if shortage_items:
        lines.append("⚠️ Нехватка:")
        lines.extend(f"• {item}" for item in shortage_items)
    else:
        lines.append("✅ Всё в наличии")

    photo_count = len(draft.get("photo_ids", []))
    if photo_count:
        lines.append(f"📸 Фото: {photo_count}")

    revision_meta = save_result.get("revision") or {}
    revision_period = revision_meta.get("period", "")
    revision_location = revision_meta.get("location", "")
    if revision_period and revision_location:
        lines.append(
            f"📦 Ревизия: {format_period_label(revision_period)} · {revision_location}"
        )

    warnings = []
    for warning in list(draft.get("warnings", [])) + list(save_result.get("warnings", [])):
        warning = str(warning or "").strip()
        if warning and warning not in warnings:
            warnings.append(warning)
    if warnings:
        lines.append("")
        lines.append("⚠️ Что стоит проверить позже:")
        lines.extend(f"• {warning}" for warning in warnings)

    lines.append("")
    lines.append(f"⚡ Быстрые действия доступны {GROUP_REPORT_ACTION_WINDOW_SECONDS} сек.")
    lines.append("👤 Сотрудника можно быстро поменять кнопкой ниже.")
    lines.append("✏️ Потом запись можно поправить через обычные разделы «Исправить записи» и «Ревизия».")
    return "\n".join(lines)


def build_revision_message_saved_text(draft, save_result=None):
    save_result = save_result or {}
    values = draft.get("values", {})
    lines = [
        "✅ Ревизия сохранена",
        "",
        f"📍 {draft['point']}",
        f"📅 {format_period_label(draft['period'])}",
        f"👤 {draft['who']}",
        f"📦 Позиций: {len(values)}",
    ]

    item_lines = []
    for item_name in REVISION_ITEMS:
        value = values.get(item_name, "")
        if value in ("", None):
            continue
        item_lines.append(f"• {item_name} — {format_number(value)} {get_revision_unit(item_name)}")
    if item_lines:
        lines.append("")
        lines.append("Что записано:")
        lines.extend(item_lines[:12])

    warnings = []
    for warning in list(draft.get("warnings", [])) + list(save_result.get("warnings", [])):
        warning = str(warning or "").strip()
        if warning and warning not in warnings:
            warnings.append(warning)
    if warnings:
        lines.append("")
        lines.append("⚠️ Что стоит проверить:")
        lines.extend(f"• {warning}" for warning in warnings[:8])

    lines.append("")
    lines.append(f"⚡ Быстрые действия доступны {GROUP_REPORT_ACTION_WINDOW_SECONDS} сек.")
    lines.append("👤 Сотрудника можно быстро поменять кнопкой ниже.")
    lines.append("✏️ Потом ревизию можно поправить через раздел «Ревизия».")
    return "\n".join(lines)


def build_group_travel_fingerprint(draft):
    amounts = [normalize_text_key(item) for item in draft.get("travel_amounts", [])]
    normalized_source = re.sub(r"\s+", " ", normalize_text_key(draft.get("source_text", ""))).strip()
    raw = "||".join([
        normalize_text_key(draft.get("who", "")),
        normalize_text_key(draft.get("date", "")),
        "|".join(amounts),
        normalized_source,
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def build_revision_restock_fingerprint(draft):
    normalized_source = re.sub(r"\s+", " ", normalize_text_key(draft.get("source_text", ""))).strip()
    item_pairs = []
    for item_name, value in sorted((draft.get("values") or {}).items()):
        item_pairs.append(f"{normalize_text_key(item_name)}={normalize_text_key(value)}")
    raw = "||".join([
        normalize_text_key(draft.get("point", "")),
        normalize_text_key(draft.get("period", "")),
        normalize_text_key(draft.get("date", "")),
        "|".join(item_pairs),
        normalized_source,
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def build_revision_message_fingerprint(draft):
    return build_revision_restock_fingerprint(draft)


def build_group_travel_saved_text(draft):
    total = 0.0
    for amount in draft.get("travel_amounts", []):
        numeric = parse_numeric_value(amount)
        if numeric is not None:
            total += numeric

    lines = [
        "✅ Проезд сохранён",
        "",
        f"👤 {draft['who']}",
        f"📅 {draft['date']}",
        f"🚌 Поездок: {len(draft.get('travel_amounts', []))}",
        f"💰 Расходы: {format_money(total)}",
    ]

    travel_items = draft.get("travel_items", [])
    if travel_items:
        lines.append("")
        lines.extend(
            f"• {item.get('label', 'Поездка').capitalize()} — {format_money(item.get('amount', ''))}"
            for item in travel_items
        )

    return "\n".join(lines)


def build_revision_restock_saved_text(draft, save_result=None):
    save_result = save_result or {}
    lines = [
        "✅ Пополнение добавлено в ревизию",
        "",
        f"📍 {draft['point']}",
        f"📅 {format_period_label(draft['period'])}",
        f"👤 {draft['who']}",
    ]

    item_lines = []
    for item_name, value in sorted((draft.get("values") or {}).items()):
        unit = get_revision_unit(item_name)
        item_lines.append(f"• {item_name} +{format_number(value)} {unit}")
    if item_lines:
        lines.append("")
        lines.append("📦 Добавлено:")
        lines.extend(item_lines[:12])

    warnings = []
    for warning in list(draft.get("warnings", [])) + list(save_result.get("warnings", [])):
        warning = str(warning or "").strip()
        if warning and warning not in warnings:
            warnings.append(warning)
    if warnings:
        lines.append("")
        lines.append("⚠️ Что стоит проверить:")
        lines.extend(f"• {warning}" for warning in warnings[:8])

    lines.append("")
    lines.append(f"⚡ Быстрые действия доступны {GROUP_REPORT_ACTION_WINDOW_SECONDS} сек.")
    return "\n".join(lines)


def build_revision_restock_saved_markup(save_result):
    log_row = save_result.get("log_row")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Редактировать ревизию", callback_data=f"grp_report_edit_revision_{log_row}")],
        [InlineKeyboardButton("🗑 Отменить", callback_data=f"grp_report_delete_{log_row}")],
    ])


def save_group_report_entry(draft):
    payload = build_group_report_payload(draft)
    service_row = add_service_row(payload)
    auto_close_repair_for_point(draft.get("point"))
    photo_rows = []
    for file_id in draft.get("photo_ids", []):
        photo_rows.append(add_photo_row(draft["date"], draft["point"], draft["who"], file_id))
    revision_meta = None
    save_warnings = []
    try:
        revision_meta = save_group_report_revision(draft)
    except Exception:
        logger.exception(
            "Failed to auto-save revision from group message: point=%s date=%s who=%s",
            draft.get("point", ""),
            draft.get("date", ""),
            draft.get("who", ""),
        )
        save_warnings.append("ревизию не удалось сохранить автоматически, обслуживание сохранено")
    logger.info(
        "service saved: user_id=%s point=%s date=%s source=group",
        draft.get("user_id"),
        draft["point"],
        draft["date"],
    )

    log_row = append_group_report_log(
        {
            "chat_id": draft["chat_id"],
            "source_key": draft["source_key"],
            "source_message_id": draft["source_message_id"],
            "media_group_id": draft.get("media_group_id", ""),
            "who": draft["who"],
            "point": draft["point"],
            "date": draft["date"],
            "fingerprint": draft.get("fingerprint", ""),
            "service_row": service_row,
            "photo_rows": serialize_row_numbers(photo_rows),
            "revision_row": (revision_meta or {}).get("row", ""),
            "revision_period": (revision_meta or {}).get("period", ""),
            "revision_location": (revision_meta or {}).get("location", ""),
            "revision_mode": (revision_meta or {}).get("mode", ""),
            "revision_backup": (revision_meta or {}).get("backup", ""),
            "status": "saved",
            "created_at": format_group_report_created_at(),
        }
    )
    return {
        "log_row": log_row,
        "service_row": service_row,
        "photo_rows": photo_rows,
        "who": draft.get("who", ""),
        "revision": revision_meta,
        "warnings": save_warnings,
    }


def save_revision_restock_entry(draft):
    existing = find_revision_record(draft["period"], draft["point"], True)
    values = build_revision_values_from_record(existing) if existing else {item: "" for item in REVISION_ITEMS}

    for item_name, delta in draft.get("values", {}).items():
        current_value = parse_numeric_value(values.get(item_name, "")) or 0
        delta_value = parse_numeric_value(delta) or 0
        values[item_name] = format_number(current_value + delta_value)

    payload = {
        "period": draft["period"],
        "location": draft["point"],
        "who": draft.get("who", ""),
        "filled_at": today(),
        "values": values,
    }
    if existing:
        update_revision_row(existing["__row"], payload)
        revision_meta = {
            "row": existing["__row"],
            "period": draft["period"],
            "location": draft["point"],
            "mode": "updated",
            "backup": build_group_report_revision_backup(existing),
        }
    else:
        row_num = add_revision_row(payload)
        revision_meta = {
            "row": row_num,
            "period": draft["period"],
            "location": draft["point"],
            "mode": "created",
            "backup": "",
        }

    log_row = append_group_report_log(
        {
            "chat_id": draft["chat_id"],
            "source_key": draft["source_key"],
            "source_message_id": draft["source_message_id"],
            "media_group_id": draft.get("media_group_id", ""),
            "who": draft.get("who", ""),
            "point": draft["point"],
            "date": draft["date"],
            "fingerprint": draft.get("fingerprint", ""),
            "service_row": "",
            "photo_rows": "",
            "revision_row": revision_meta.get("row", ""),
            "revision_period": revision_meta.get("period", ""),
            "revision_location": revision_meta.get("location", ""),
            "revision_mode": revision_meta.get("mode", ""),
            "revision_backup": revision_meta.get("backup", ""),
            "status": "saved",
            "created_at": format_group_report_created_at(),
        }
    )
    return {
        "log_row": log_row,
        "service_row": "",
        "who": draft.get("who", ""),
        "revision": revision_meta,
        "warnings": [],
    }


def save_revision_message_entry(draft):
    existing = find_revision_record(draft["period"], draft["point"], True)
    values = build_revision_values_from_record(existing) if existing else {item: "" for item in REVISION_ITEMS}

    for item_name, value in draft.get("values", {}).items():
        values[item_name] = value

    payload = {
        "period": draft["period"],
        "location": draft["point"],
        "who": draft.get("who", ""),
        "filled_at": today(),
        "values": values,
    }
    if existing:
        update_revision_row(existing["__row"], payload)
        revision_meta = {
            "row": existing["__row"],
            "period": draft["period"],
            "location": draft["point"],
            "mode": "updated",
            "backup": build_group_report_revision_backup(existing),
        }
    else:
        row_num = add_revision_row(payload)
        revision_meta = {
            "row": row_num,
            "period": draft["period"],
            "location": draft["point"],
            "mode": "created",
            "backup": "",
        }

    log_row = append_group_report_log(
        {
            "chat_id": draft["chat_id"],
            "source_key": draft["source_key"],
            "source_message_id": draft["source_message_id"],
            "media_group_id": draft.get("media_group_id", ""),
            "who": draft.get("who", ""),
            "point": draft["point"],
            "date": draft["date"],
            "fingerprint": draft.get("fingerprint", ""),
            "service_row": "",
            "photo_rows": "",
            "revision_row": revision_meta.get("row", ""),
            "revision_period": revision_meta.get("period", ""),
            "revision_location": revision_meta.get("location", ""),
            "revision_mode": revision_meta.get("mode", ""),
            "revision_backup": revision_meta.get("backup", ""),
            "status": "saved",
            "created_at": format_group_report_created_at(),
        }
    )
    return {
        "log_row": log_row,
        "service_row": "",
        "who": draft.get("who", ""),
        "revision": revision_meta,
        "warnings": [],
    }


def save_group_travel_entry(draft):
    row_numbers = []
    for amount in draft.get("travel_amounts", []):
        row_numbers.append(add_travel_row(draft["date"], draft["who"], amount))

    log_row = append_group_report_log(
        {
            "chat_id": draft["chat_id"],
            "source_key": draft["source_key"],
            "source_message_id": draft["source_message_id"],
            "media_group_id": draft.get("media_group_id", ""),
            "who": draft["who"],
            "point": "__travel__",
            "date": draft["date"],
            "fingerprint": draft.get("fingerprint", ""),
            "service_row": serialize_row_numbers(row_numbers),
            "photo_rows": "",
            "revision_row": "",
            "revision_period": "",
            "revision_location": "",
            "revision_mode": "",
            "revision_backup": "",
            "status": "saved",
            "created_at": format_group_report_created_at(),
        }
    )
    return log_row


def delete_group_report_entry_by_log_row(log_row_num):
    record = next((entry for entry in get_group_report_logs_with_rows() if entry["__row"] == log_row_num), None)
    if not record:
        return "missing", None

    if record.get("Статус") != "saved":
        return record.get("Статус") or "missing", record

    book = get_sheet()
    service_row = record.get("Service_Row", "")
    photo_rows = parse_logged_row_numbers(record.get("Photo_Rows", ""))

    if photo_rows:
        photo_sheet = book.worksheet("Фото")
        for row_num in sorted(photo_rows, reverse=True):
            photo_sheet.delete_rows(row_num)

    if str(service_row).strip():
        book.worksheet("Обслуживание").delete_rows(int(service_row))

    restore_group_report_revision(record)

    updated = {
        "chat_id": record.get("Chat_ID", ""),
        "source_key": record.get("Source_Key", ""),
        "source_message_id": record.get("Source_Message_ID", ""),
        "media_group_id": record.get("Media_Group_ID", ""),
        "who": record.get("Кто", ""),
        "point": record.get("Точка", ""),
        "date": record.get("Дата", ""),
        "fingerprint": record.get("Fingerprint", ""),
        "service_row": record.get("Service_Row", ""),
        "photo_rows": record.get("Photo_Rows", ""),
        "revision_row": record.get("Revision_Row", ""),
        "revision_period": record.get("Revision_Period", ""),
        "revision_location": record.get("Revision_Location", ""),
        "revision_mode": record.get("Revision_Mode", ""),
        "revision_backup": record.get("Revision_Backup", ""),
        "status": "deleted",
        "created_at": record.get("Создано", ""),
    }
    update_group_report_log(log_row_num, updated)
    return "deleted", record


def resolve_runtime_path(path_value):
    path = Path(path_value)
    if path.is_absolute():
        return path
    return Path(__file__).resolve().parent / path


REMINDER_STATE_CACHE_KEY = "_reminder_state"


def load_reminder_state(application=None):
    if application is not None:
        cached_state = application.bot_data.get(REMINDER_STATE_CACHE_KEY)
        if isinstance(cached_state, dict):
            return cached_state

    path = resolve_runtime_path(REMINDER_STATE_FILE)
    if not path.exists():
        state = {}
    else:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Failed to load reminder state from %s; using empty state", path)
            state = {}

    if not isinstance(state, dict):
        state = {}

    if application is not None:
        application.bot_data[REMINDER_STATE_CACHE_KEY] = state

    return state


def save_reminder_state(state, application=None):
    if not isinstance(state, dict):
        state = {}

    if application is not None:
        application.bot_data[REMINDER_STATE_CACHE_KEY] = state

    path = resolve_runtime_path(REMINDER_STATE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    try:
        tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)
    except OSError:
        logger.exception("Failed to save reminder state to %s", path)


MONTH_NAMES_RU = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]


def build_period_key(year, month):
    return f"{month:02d}.{year}"


def parse_period_key(period_key):
    try:
        month_str, year_str = str(period_key).split(".", 1)
        month = int(month_str)
        year = int(year_str)
        if not 1 <= month <= 12:
            return None, None
        return year, month
    except (TypeError, ValueError):
        return None, None


def shift_period(period_key, delta_months):
    year, month = parse_period_key(period_key)
    if year is None:
        return None
    total = year * 12 + (month - 1) + delta_months
    new_year = total // 12
    new_month = total % 12 + 1
    return build_period_key(new_year, new_month)


def recent_period_keys(count=6):
    today_date = now_local().date()
    current = build_period_key(today_date.year, today_date.month)
    return [shift_period(current, -offset) for offset in range(count)]


def current_period_key():
    today_date = now_local().date()
    return build_period_key(today_date.year, today_date.month)


def recent_completed_period_keys(count=6):
    return [shift_period(current_period_key(), -(offset + 1)) for offset in range(count)]


def month_last_day(date_value):
    return calendar.monthrange(date_value.year, date_value.month)[1]


def should_send_midmonth_home_reminder(date_value):
    return date_value.day in {15, 16}


def should_send_month_close_revision_reminder(date_value):
    last_day = month_last_day(date_value)
    return date_value.day in {max(last_day - 2, 1), max(last_day - 1, 1)}


def format_period_label(period_key):
    year, month = parse_period_key(period_key)
    if year is None:
        return str(period_key)
    return f"{MONTH_NAMES_RU[month - 1]} {year}"


def build_rent_period_key(year, month):
    return f"{year:04d}-{month:02d}"


def parse_rent_period_key(period_key):
    try:
        year_str, month_str = str(period_key).split("-", 1)
        year = int(year_str)
        month = int(month_str)
        if not 1 <= month <= 12:
            return None, None
        return year, month
    except (TypeError, ValueError):
        return None, None


def current_rent_period_key():
    current_date = now_local().date()
    return build_rent_period_key(current_date.year, current_date.month)


def shift_rent_period(period_key, delta_months):
    year, month = parse_rent_period_key(period_key)
    if year is None:
        return None
    total = year * 12 + (month - 1) + delta_months
    new_year = total // 12
    new_month = total % 12 + 1
    return build_rent_period_key(new_year, new_month)


def recent_rent_period_keys(count=6):
    current = current_rent_period_key()
    return [shift_rent_period(current, -offset) for offset in range(count)]


def format_rent_period_label(period_key):
    year, month = parse_rent_period_key(period_key)
    if year is None:
        return str(period_key)
    return f"{MONTH_NAMES_RU[month - 1]} {year}"


def get_rent_period_bounds(period_key):
    year, month = parse_rent_period_key(period_key)
    if year is None:
        return None, None
    first_day = datetime(year, month, 1).date()
    last_day = datetime(year, month, calendar.monthrange(year, month)[1]).date()
    return first_day, last_day


def latest_revision_period(records):
    periods = {record.get("Период", "") for record in records if record.get("Период")}
    ordered = sorted(
        periods,
        key=lambda period: parse_period_key(period) if parse_period_key(period) != (None, None) else (0, 0),
        reverse=True,
    )
    return ordered[0] if ordered else None


def get_revision_unit(item_name):
    return REVISION_UNITS.get(item_name, "шт")


def get_revision_options(item_name):
    return REVISION_ITEM_OPTIONS.get(item_name, ["0", "1", "2", "3", "5"])


def normalize_text_key(value):
    text = str(value).strip().lower().replace("ё", "е")
    text = re.sub(r"[^\w\s./-]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" .")


def resolve_worker_name(value):
    normalized = normalize_text_key(value)
    if not normalized:
        return None

    for worker in get_worker_names():
        if normalize_text_key(worker) == normalized:
            return worker

    for user_name in get_user_directory().values():
        if normalize_text_key(user_name) == normalized:
            return user_name

    return None


SERVICE_REPORT_SUPPLY_ALIASES = {
    "стакан": "Стаканы",
    "стаканы": "Стаканы",
    "кофе": "Кофе",
    "шоколад": "Шоколад",
    "раф": "Раф",
    "молоко": "Молоко",
    "сироп": "Сиропы",
    "сиропы": "Сиропы",
    "труб": "Трубочки",
    "трубочки": "Трубочки",
    "палоч": "Палочки",
    "палочки": "Палочки",
    "сахар": "Сахар",
    "крышка б": "Крышки бел",
    "крышка бел": "Крышки бел",
    "крышки бел": "Крышки бел",
    "крышка ч": "Крышки чёрн",
    "крышка черн": "Крышки чёрн",
    "крышка черная": "Крышки чёрн",
    "крышка черн.": "Крышки чёрн",
    "крышки черн": "Крышки чёрн",
    "крышки черные": "Крышки чёрн",
    "манжета": "Манжеты",
    "манжеты": "Манжеты",
    "мус пакеты": "Мус.пакеты",
    "мусорные пакеты": "Мус.пакеты",
    "мус.пакеты": "Мус.пакеты",
    "влажные салф": "Влажные салф",
    "влажные салфетки": "Влажные салф",
    "салфетки влажные": "Влажные салф",
}

SERVICE_REPORT_SHORTAGE_TRIGGERS = (
    "нужен",
    "нужно",
    "нужны",
    "не хватает",
    "нужно купить",
    "заканчивается",
    "закончился",
    "закончились",
)

SERVICE_REPORT_OK_PHRASES = (
    "все ок",
    "всё ок",
    "все в наличии",
    "всё в наличии",
    "все нормально",
    "всё нормально",
)


REVISION_LOCATION_ALIASES = {
    "беломорский": "Беломорский",
    "бел": "Беломорский",
    "гагарина": "Гагарина",
    "гагар": "Гагарина",
    "гиппо": "Гиппо",
    "южный": "Южный",
    "юж": "Южный",
    "сити": "Сити",
    "макси": "Макси",
    "бел2": "Бел2",
    "бел 2": "Бел2",
    "б2": "Бел2",
    "дома": "Дома",
    "дом": "Дома",
    "склад": "Дома",
    "гараж": "Гараж",
    "гар": "Гараж",
    "garage": "Гараж",
}


REVISION_IMPORT_ITEM_SPECS = {
    "кофе": ("Кофе", False),
    "кофе исп": ("Кофе", True),
    "кофе кит": ("Кофе", True),
    "молоко": ("Молоко", False),
    "мока": ("Мока", False),
    "шоколад": ("Шоколад", False),
    "стакан": ("Стаканы", False),
    "стаканы": ("Стаканы", False),
    "сахар": ("Сахар", False),
    "сироп": ("Сиропы", False),
    "сиропы": ("Сиропы", False),
    "крышка б": ("Крышки бел", False),
    "крышка бел": ("Крышки бел", False),
    "крышки бел": ("Крышки бел", False),
    "крышка ч": ("Крышки чёрн", False),
    "крышка черн": ("Крышки чёрн", False),
    "крышка черная": ("Крышки чёрн", False),
    "крышка черн ": ("Крышки чёрн", False),
    "крышки черн": ("Крышки чёрн", False),
    "крышка черн.": ("Крышки чёрн", False),
    "крышка черн": ("Крышки чёрн", False),
    "крышка черн": ("Крышки чёрн", False),
    "труб": ("Трубочки", False),
    "трубочки": ("Трубочки", False),
    "палоч": ("Палочки", False),
    "палочки": ("Палочки", False),
    "манжеты": ("Манжеты", False),
    "манжета": ("Манжеты", False),
    "вода": ("Вода", False),
    "воды": ("Вода", False),
    "влажные салф": ("Влажные салф", False),
    "салфетки влажные": ("Влажные салф", False),
    "салф влаж": ("Влажные салф", False),
    "салф вл": ("Влажные салф", False),
    "салфетки сухие": ("Салфетки сухие", False),
    "салф сух": ("Салфетки сухие", False),
    "сухие салфетки": ("Салфетки сухие", False),
    "мусорные пакеты": ("Мус.пакеты", False),
    "мус пакеты": ("Мус.пакеты", False),
    "мус.пакеты": ("Мус.пакеты", False),
}


REVISION_RESTOCK_ITEM_ALIASES = {
    "вода": "Вода",
    "воды": "Вода",
    "бут воды": "Вода",
    "бутылки воды": "Вода",
    "кофе": "Кофе",
    "молоко": "Молоко",
    "молока": "Молоко",
    "мока": "Мока",
    "шоколад": "Шоколад",
    "шоколада": "Шоколад",
    "сироп": "Сиропы",
    "сиропы": "Сиропы",
    "стакан": "Стаканы",
    "стаканы": "Стаканы",
    "стаканов": "Стаканы",
    "сахар": "Сахар",
    "сахара": "Сахар",
    "крышка б": "Крышки бел",
    "крышки бел": "Крышки бел",
    "бел крыш": "Крышки бел",
    "белые крышки": "Крышки бел",
    "крышка ч": "Крышки чёрн",
    "крышки черн": "Крышки чёрн",
    "черн крыш": "Крышки чёрн",
    "черные крышки": "Крышки чёрн",
    "чёрные крышки": "Крышки чёрн",
    "труб": "Трубочки",
    "трубочки": "Трубочки",
    "трубочек": "Трубочки",
    "палоч": "Палочки",
    "палочки": "Палочки",
    "палочек": "Палочки",
    "манжета": "Манжеты",
    "манжеты": "Манжеты",
    "манжет": "Манжеты",
    "влажные салф": "Влажные салф",
    "влажные салфетки": "Влажные салф",
    "салфетки влажные": "Влажные салф",
    "сухие салфетки": "Салфетки сухие",
    "салфетки сухие": "Салфетки сухие",
    "салф сух": "Салфетки сухие",
    "мусорные пакеты": "Мус.пакеты",
    "мус пакеты": "Мус.пакеты",
    "мус.пакеты": "Мус.пакеты",
}


def normalize_revision_location_name(value):
    return REVISION_LOCATION_ALIASES.get(normalize_text_key(value))


def get_revision_import_item_spec(value):
    return REVISION_IMPORT_ITEM_SPECS.get(normalize_text_key(value))


def resolve_revision_restock_item_name(value):
    item_spec = get_revision_import_item_spec(value)
    if item_spec:
        return item_spec[0]

    normalized = normalize_text_key(value)
    if not normalized:
        return None

    alias_items = sorted(REVISION_RESTOCK_ITEM_ALIASES.items(), key=lambda item: len(item[0]), reverse=True)
    for alias, item_name in alias_items:
        if contains_normalized_alias(normalized, alias):
            return item_name

    return None


def parse_import_number(value):
    match = re.search(r"-?\d+(?:[.,]\d+)?", str(value))
    if not match:
        return None
    return normalize_number_text(match.group(0))


def add_revision_import_value(target, location, item_name, value, warnings=None, accumulate=False):
    if value in (None, ""):
        return
    location_values = target.setdefault(location, {})
    existing = location_values.get(item_name, "")
    if accumulate and existing not in ("", None):
        existing_num = parse_numeric_value(existing)
        new_num = parse_numeric_value(value)
        if existing_num is not None and new_num is not None:
            location_values[item_name] = format_number(existing_num + new_num)
            return
    if (
        not accumulate
        and existing not in ("", None)
        and str(existing).strip() != str(value).strip()
        and warnings is not None
    ):
        warnings.append(f"{location}: {item_name} было {existing}, заменено на {value}")
    location_values[item_name] = value


def parse_revision_import_text(text):
    parsed = {}
    warnings = []
    current_block_location = None
    slash_locations = None

    for raw_line in str(text).splitlines():
        line = raw_line.strip()
        if not line:
            continue

        slash_header_match = re.match(r"^/\s*(.+?)\s*$", line)
        if slash_header_match:
            current_block_location = normalize_revision_location_name(slash_header_match.group(1))
            slash_locations = None
            if not current_block_location:
                warnings.append(f"Не понял точку в строке: {line}")
            continue

        block_match = re.match(r"^\d{1,2}\.\d{1,2}\s+(.+)$", line)
        if block_match:
            current_block_location = normalize_revision_location_name(block_match.group(1))
            slash_locations = None
            if not current_block_location:
                warnings.append(f"Не понял точку в строке: {line}")
            continue

        slash_parts = [part.strip() for part in line.split("/") if part.strip()]
        normalized_locations = [normalize_revision_location_name(part) for part in slash_parts]
        if slash_parts and len(slash_parts) >= 2 and all(normalized_locations):
            slash_locations = normalized_locations
            current_block_location = None
            continue

        if "-" not in line:
            continue

        raw_item, raw_values = [part.strip() for part in line.split("-", 1)]
        item_spec = get_revision_import_item_spec(raw_item)
        if not item_spec:
            warnings.append(f"Не понял товар: {raw_item}")
            continue

        item_name, accumulate = item_spec

        if slash_locations and "/" in raw_values:
            values = [part.strip() for part in raw_values.split("/")]
            if len(values) < len(slash_locations):
                values += [""] * (len(slash_locations) - len(values))

            for location, raw_value in zip(slash_locations, values):
                parsed_value = parse_import_number(raw_value)
                if parsed_value is None:
                    continue
                add_revision_import_value(parsed, location, item_name, parsed_value, warnings=warnings, accumulate=accumulate)
            continue

        if current_block_location:
            parsed_value = parse_import_number(raw_values)
            if parsed_value is None:
                warnings.append(f"Не понял значение в строке: {line}")
                continue
            add_revision_import_value(parsed, current_block_location, item_name, parsed_value, warnings=warnings, accumulate=accumulate)

    return parsed, warnings


def extract_revision_restock_location(text):
    normalized = normalize_text_key(text)
    alias_items = sorted(REVISION_LOCATION_ALIASES.items(), key=lambda item: len(item[0]), reverse=True)
    for alias, location in alias_items:
        if contains_normalized_alias(normalized, alias):
            return location
    return None


def parse_revision_restock_message_text(text):
    raw_text = str(text or "").strip()
    if not raw_text:
        return None

    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    if len(lines) < 2:
        return None

    header_normalized = normalize_text_key(lines[0])
    if not any(trigger in header_normalized for trigger in ("довез", "довёз", "в ревизию", "добавить в ревизию")):
        return None

    location = extract_revision_restock_location(lines[0])
    if not location:
        return None

    values = {}
    warnings = []
    for raw_line in lines[1:]:
        normalized_line = normalize_text_key(raw_line)
        if not normalized_line:
            continue
        if "добавить в ревизию" in normalized_line:
            continue

        match = re.match(r"^\s*(\d+(?:[.,]\d+)?)\s+(.+?)\s*$", raw_line)
        if not match:
            warnings.append(f"Не понял строку: {raw_line}")
            continue

        try:
            value = normalize_number_text(match.group(1))
        except ValueError:
            warnings.append(f"Не понял количество в строке: {raw_line}")
            continue

        item_name = resolve_revision_restock_item_name(match.group(2))
        if not item_name:
            warnings.append(f"Не понял товар: {match.group(2).strip()}")
            continue

        add_revision_import_value(values, location, item_name, value, warnings=warnings, accumulate=True)

    location_values = values.get(location, {})
    if not location_values:
        return None

    return {
        "location": location,
        "values": location_values,
        "warnings": warnings,
        "source_text": raw_text,
    }


def parse_revision_snapshot_message_text(text):
    raw_text = str(text or "").strip()
    if not raw_text:
        return None

    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    if len(lines) < 2:
        return None

    header_match = re.match(r"^/\s*(.+?)\s*$", lines[0])
    if not header_match:
        return None

    location = normalize_revision_location_name(header_match.group(1))
    if not location:
        return None

    parsed, warnings = parse_revision_import_text(raw_text)
    location_values = parsed.get(location, {})
    if len(location_values) < 3:
        return None

    return {
        "location": location,
        "values": location_values,
        "warnings": warnings,
        "source_text": raw_text,
    }


def build_revision_import_preview(period, parsed, warnings):
    locations = order_revision_locations(parsed.keys())
    lines = [
        "📥 Импорт ревизии",
        f"📅 {format_period_label(period)}",
        "",
        f"Найдено локаций: {len(locations)}",
    ]

    if locations:
        lines.append("📍 Что импортируется:")
        for location in locations:
            lines.append(f"• {location}: {len(parsed.get(location, {}))} поз.")

    missing = [location for location in REVISION_LOCATIONS if location not in locations]
    if missing:
        lines.append("")
        lines.append("⚪ Пока не найдены:")
        lines.extend(f"• {location}" for location in missing)

    if warnings:
        lines.append("")
        lines.append("⚠️ Замечания:")
        lines.extend(f"• {warning}" for warning in warnings[:10])

    return "\n".join(lines)


def get_revision_author(update):
    tg_user = update.effective_user
    if not tg_user:
        return "Неизвестно"
    return get_configured_user_name(tg_user.id) or tg_user.first_name or tg_user.username or str(tg_user.id)


def parse_numeric_value(value):
    raw = str(value).strip().replace(",", ".")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def format_revision_value(item_name, value):
    if value in (None, ""):
        return "—"
    return f"{format_number(value)} {get_revision_unit(item_name)}"


def get_procurement_unit_short(unit):
    return PROCUREMENT_UNIT_SHORT.get(unit, unit)


def build_compact_value_text(value, unit=""):
    suffix = str(value)
    if unit:
        suffix = f"{suffix} {unit}"
    return suffix.strip()


def build_compact_table_lines(rows, indent=""):
    if not rows:
        return []

    label_width = max(len(label) for label, _, _ in rows)
    lines = []
    for label, value, unit in rows:
        suffix = build_compact_value_text(value, unit)
        filler = "." * max(2, label_width - len(label) + 4)
        lines.append(f"{indent}{label} {filler} {suffix}".rstrip())
    return lines


def build_preformatted_block(rows):
    if not rows:
        return ""

    label_width = max(len(label) for label, _, _ in rows)
    value_width = max(len(str(value)) for _, value, _ in rows)
    rendered = []
    for label, value, unit in rows:
        value_text = str(value)
        unit_text = unit or ""
        line = f"{label.ljust(label_width)}  {value_text.rjust(value_width)}"
        if unit_text:
            line += f" {unit_text}"
        rendered.append(escape_html(line.rstrip()))
    return "<pre>\n" + "\n".join(rendered) + "\n</pre>"


def get_rent_context(context):
    return context.user_data.setdefault("rent", {})


def get_payout_context(context):
    return context.user_data.setdefault("payout", {})


def clear_payout_context(context):
    context.user_data.pop("payout", None)


def get_selected_payout_worker(context):
    payout = get_payout_context(context)
    worker = str(payout.get("worker", "")).strip()
    paid_workers = get_paid_workers()
    if worker in paid_workers:
        return worker
    return paid_workers[0] if paid_workers else ""


def build_payout_return_context(period_key, screen="overview"):
    return {
        "return_mode": "payout",
        "return_period": period_key,
        "return_screen": screen,
        "return_worker": get_default_payout_worker_name(),
    }


def get_actor_label(update):
    tg_user = update.effective_user
    if not tg_user:
        return "Неизвестно"
    if tg_user.username:
        return f"@{tg_user.username}"
    return get_configured_user_name(tg_user.id) or tg_user.first_name or str(tg_user.id)


def format_money_spaced(value):
    numeric = parse_numeric_value(value)
    if numeric is None:
        return "—"
    if float(numeric).is_integer():
        return f"{int(numeric):,}".replace(",", " ") + " ₽"
    return f"{numeric:,.2f}".replace(",", " ").replace(".", ",").rstrip("0").rstrip(",") + " ₽"


def next_prefixed_id(records, prefix):
    max_number = 0
    pattern = re.compile(rf"^{re.escape(prefix)}-(\d+)$", re.IGNORECASE)
    for record in records:
        raw_id = str(record.get("id", "")).strip()
        match = pattern.match(raw_id)
        if not match:
            continue
        max_number = max(max_number, int(match.group(1)))
    return f"{prefix}-{max_number + 1:03d}"


def sort_by_point_name(records, key="Точка"):
    order_map = {point: index for index, point in enumerate(POINTS)}
    return sorted(
        records,
        key=lambda record: (
            order_map.get(record.get(key, ""), 999),
            record.get(key, ""),
        ),
    )


def is_rent_lease_closed(status_text):
    normalized = normalize_text_key(status_text)
    return "закры" in normalized or "неактив" in normalized


def is_rent_lease_active_for_period(lease, period_key):
    if is_rent_lease_closed(lease.get("Статус", "")):
        return False

    period_start, period_end = get_rent_period_bounds(period_key)
    if not period_start:
        return False

    start_dt = parse_date(lease.get("Дата начала", "")) or parse_date(lease.get("Дата заключения", ""))
    end_dt = parse_date(lease.get("Дата окончания", ""))

    if start_dt and start_dt.date() > period_end:
        return False
    if end_dt and end_dt.date() < period_start:
        return False
    return True


def build_rent_progress_bar(done_count, total_count, width=16):
    if total_count <= 0:
        return "░" * width + "  0/0  0%"
    filled = round((done_count / total_count) * width)
    filled = max(0, min(width, filled))
    percent = round((done_count / total_count) * 100)
    return f"{'█' * filled}{'░' * (width - filled)}  {done_count}/{total_count}  {percent}%"


def build_rent_status_block(rows):
    if not rows:
        return ""

    point_width = max(len(row[0]) for row in rows)
    amount_width = max(len(row[1]) for row in rows)
    rendered = []
    for point, amount, extra in rows:
        line = f"{point.ljust(point_width)}  {amount.rjust(amount_width)}"
        if extra:
            line += f"  {extra}"
        rendered.append(line.rstrip())
    return build_text_pre_block(rendered)


def build_rent_deadline_text(period_key, leases):
    deadlines = sorted(
        {
            int(str(lease.get("Дедлайн (число)", "")).strip())
            for lease in leases
            if str(lease.get("Дедлайн (число)", "")).strip().isdigit()
        }
    )
    if not deadlines:
        return "—"

    if len(deadlines) > 1:
        return "разные даты"

    deadline_day = deadlines[0]
    base_text = f"до {deadline_day} числа"
    current_period = current_rent_period_key()
    if period_key != current_period:
        return base_text

    year, month = parse_rent_period_key(period_key)
    if year is None:
        return base_text

    deadline_date = datetime(year, month, min(deadline_day, calendar.monthrange(year, month)[1])).date()
    diff = (deadline_date - now_local().date()).days
    if diff > 0:
        return f"{base_text} ({diff} дн)"
    if diff == 0:
        return f"{base_text} (сегодня)"
    return f"{base_text} (+{abs(diff)} дн)"


def find_rent_landlord(landlords, landlord_id):
    landlord_id = str(landlord_id or "").strip()
    return next((landlord for landlord in landlords if str(landlord.get("id", "")).strip() == landlord_id), None)


def find_rent_lease(leases, lease_id):
    lease_id = str(lease_id or "").strip()
    return next((lease for lease in leases if str(lease.get("id", "")).strip() == lease_id), None)


def get_rent_payment_for_period(payments, lease, period_key):
    lease_id = str(lease.get("id", "")).strip()
    point = str(lease.get("Точка", "")).strip()
    matches = [
        payment for payment in payments
        if payment.get("Период") == period_key
        and (
            (lease_id and str(payment.get("Договор ID", "")).strip() == lease_id)
            or (point and str(payment.get("Точка", "")).strip() == point)
        )
    ]
    if not matches:
        return None
    matches = sorted(matches, key=lambda payment: payment.get("__row", 0))
    return matches[-1]


def get_rent_dashboard_data(period_key):
    ensure_rent_worksheets()
    leases = sort_by_point_name(get_all_rent_leases())
    payments = get_all_rent_payments_with_rows()

    active_leases = [lease for lease in leases if is_rent_lease_active_for_period(lease, period_key)]
    paid = []
    unpaid = []
    paid_total = 0.0
    total = 0.0

    for lease in active_leases:
        amount = parse_numeric_value(lease.get("Текущая ставка", "")) or 0.0
        total += amount
        payment = get_rent_payment_for_period(payments, lease, period_key)
        item = {
            "lease_id": lease.get("id", ""),
            "point": lease.get("Точка", ""),
            "amount": amount,
            "deadline": lease.get("Дедлайн (число)", ""),
            "lease": lease,
            "payment": payment,
        }
        if payment and normalize_text_key(payment.get("Статус", "") or "оплачено") != "ожидает":
            item["paid_date"] = payment.get("Дата оплаты", "")
            item["paid_by"] = payment.get("Кто отметил", "")
            paid.append(item)
            paid_total += amount
        else:
            unpaid.append(item)

    return {
        "period": period_key,
        "leases": active_leases,
        "paid": paid,
        "unpaid": unpaid,
        "paid_total": paid_total,
        "total": total,
        "remaining_total": max(total - paid_total, 0),
        "deadline_text": build_rent_deadline_text(period_key, active_leases),
    }


def get_rent_selectable_leases(period_key=None):
    ensure_rent_worksheets()
    leases = sort_by_point_name(get_all_rent_leases())
    period_key = period_key or current_rent_period_key()
    return [lease for lease in leases if is_rent_lease_active_for_period(lease, period_key)]


def find_rent_dashboard_item(dashboard, lease_id):
    lease_id = str(lease_id or "").strip()
    for item in dashboard.get("unpaid", []) + dashboard.get("paid", []):
        if str(item.get("lease_id", "")).strip() == lease_id:
            return item
    return None


def build_rent_dashboard_text(period_key, dashboard):
    lines = [f"<b>🏠 Аренда — {escape_html(format_rent_period_label(period_key))}</b>", ""]

    unpaid_rows = [
        (item["point"], format_money_spaced(item["amount"]), "")
        for item in dashboard["unpaid"]
    ]
    paid_rows = [
        (item["point"], format_money_spaced(item["amount"]), item.get("paid_date", ""))
        for item in dashboard["paid"]
    ]

    if unpaid_rows:
        lines.append("🔴 Не оплачено")
        lines.append(build_rent_status_block(unpaid_rows))
        lines.append("")

    if paid_rows:
        lines.append("✅ Оплачено")
        lines.append(build_rent_status_block(paid_rows))
        lines.append("")

    if not dashboard["leases"]:
        lines.append("⚪ Пока нет активных договоров аренды.")
        return "\n".join(lines)

    summary_rows = [
        ("Оплачено", format_money_spaced(dashboard["paid_total"]), ""),
        ("Осталось", format_money_spaced(dashboard["remaining_total"]), ""),
        ("Дедлайн", dashboard["deadline_text"], ""),
    ]
    lines.append("Итого")
    lines.append(build_preformatted_block(summary_rows))
    lines.append("")
    lines.append(f"<pre>{escape_html(build_rent_progress_bar(len(dashboard['paid']), len(dashboard['leases'])))}</pre>")
    return "\n".join(lines)


def build_rent_unpaid_picker_text(period_key, dashboard):
    lines = [f"<b>✅ Отметить оплату — {escape_html(format_rent_period_label(period_key))}</b>", ""]
    if not dashboard["unpaid"]:
        lines.append("✅ За этот месяц всё уже оплачено.")
        return "\n".join(lines)

    lines.append("⏳ Не оплачено")
    rows = [(item["point"], format_money_spaced(item["amount"]), "") for item in dashboard["unpaid"]]
    lines.append(build_rent_status_block(rows))
    return "\n".join(lines)


def get_rent_requisites_data(lease_id):
    ensure_rent_worksheets()
    leases = get_all_rent_leases()
    landlords = get_all_rent_landlords()
    lease = find_rent_lease(leases, lease_id)
    if not lease:
        return None
    landlord = find_rent_landlord(landlords, lease.get("Арендодатель ID", ""))
    return {
        "lease": lease,
        "landlord": landlord,
    }


def build_rent_requisites_text(data):
    lease = data["lease"]
    landlord = data.get("landlord") or {}

    lines = [f"<b>🏦 Реквизиты — {escape_html(lease.get('Точка', '?'))}</b>", ""]
    if landlord.get("Имя / Название"):
        lines.append(f"{escape_html(landlord.get('Имя / Название', ''))}")
        lines.append("")

    rows = [
        ("ИНН", landlord.get("ИНН", "") or "—", ""),
        ("Р/счёт", landlord.get("Р/счёт", "") or "—", ""),
        ("Банк", landlord.get("Банк", "") or "—", ""),
        ("БИК", landlord.get("БИК", "") or "—", ""),
        ("К/с", landlord.get("К/с", "") or "—", ""),
        ("Сумма", format_money_spaced(lease.get("Текущая ставка", "")), ""),
    ]
    lines.append(build_preformatted_block(rows))

    phone = str(landlord.get("Телефон", "") or "").strip()
    if phone:
        lines.append("")
        lines.append(f"📞 {escape_html(phone)}")
    return "\n".join(lines)


def build_rent_copy_all_text(data):
    lease = data["lease"]
    landlord = data.get("landlord") or {}
    lines = [
        landlord.get("Имя / Название", ""),
        f"ИНН: {landlord.get('ИНН', '')}",
        f"Р/с: {landlord.get('Р/счёт', '')}",
        f"Банк: {landlord.get('Банк', '')}",
        f"БИК: {landlord.get('БИК', '')}",
        f"К/с: {landlord.get('К/с', '')}",
        f"Сумма: {format_money_spaced(lease.get('Текущая ставка', ''))}",
    ]
    return "\n".join(line for line in lines if line and not line.endswith(": "))


def get_rent_card_data(lease_id):
    ensure_rent_worksheets()
    leases = get_all_rent_leases()
    landlords = get_all_rent_landlords()
    payments = get_all_rent_payments()
    indexations = get_all_rent_indexations()
    lease = find_rent_lease(leases, lease_id)
    if not lease:
        return None
    landlord = find_rent_landlord(landlords, lease.get("Арендодатель ID", ""))
    lease_payments = [payment for payment in payments if str(payment.get("Договор ID", "")).strip() == str(lease_id).strip()]
    lease_indexations = [item for item in indexations if str(item.get("Договор ID", "")).strip() == str(lease_id).strip()]
    return {
        "lease": lease,
        "landlord": landlord,
        "payments": sorted(lease_payments, key=lambda item: item.get("Период", ""), reverse=True),
        "indexations": sorted(lease_indexations, key=lambda item: item.get("Дата применения", ""), reverse=True),
    }


def build_rent_card_text(data):
    lease = data["lease"]
    landlord = data.get("landlord") or {}
    lines = [f"<b>📄 Аренда — {escape_html(lease.get('Точка', '?'))}</b>", ""]

    end_dt = parse_date(lease.get("Дата окончания", ""))
    days_left = "—"
    if end_dt:
        days_left = str((end_dt.date() - now_local().date()).days)

    rows = [
        ("Договор", lease.get("Номер договора", "") or "—", ""),
        ("Заключён", lease.get("Дата заключения", "") or "—", ""),
        ("Начало", lease.get("Дата начала", "") or "—", ""),
        ("До", lease.get("Дата окончания", "") or "—", ""),
        ("Осталось", days_left, "дн" if days_left not in {"—", ""} else ""),
        ("Базовая", format_money_spaced(lease.get("Базовая ставка", "")), ""),
        ("Текущая", format_money_spaced(lease.get("Текущая ставка", "")), ""),
        ("Дедлайн", f"до {lease.get('Дедлайн (число)', '—')} числа" if lease.get("Дедлайн (число)", "") else "—", ""),
        ("Статус", lease.get("Статус", "") or "—", ""),
    ]
    lines.append(build_preformatted_block(rows))

    if landlord.get("Имя / Название"):
        lines.append("")
        lines.append(f"👤 {escape_html(landlord.get('Имя / Название', ''))}")

    notes = str(lease.get("Заметки", "") or "").strip()
    if notes:
        lines.append("")
        lines.append(f"📝 {escape_html(notes)}")
    return "\n".join(lines)


def build_rent_history_text(data):
    lease = data["lease"]
    payments = data["payments"]
    lines = [f"<b>📋 История оплат — {escape_html(lease.get('Точка', '?'))}</b>", ""]
    if not payments:
        lines.append("⚪ По этой точке ещё нет отмеченных оплат.")
        return "\n".join(lines)

    rows = []
    for payment in payments[:12]:
        rows.append((
            format_rent_period_label(payment.get("Период", "")),
            format_money_spaced(payment.get("Сумма", "")),
            payment.get("Дата оплаты", "") or "",
        ))
    lines.append(build_rent_status_block(rows))
    return "\n".join(lines)


def build_rent_indexations_text(data):
    lease = data["lease"]
    indexations = data["indexations"]
    lines = [f"<b>📈 Индексация — {escape_html(lease.get('Точка', '?'))}</b>", ""]
    if not indexations:
        lines.append("⚪ По этой точке индексации пока не указаны.")
        return "\n".join(lines)

    rows = []
    for item in indexations[:12]:
        percent = format_number(item.get("Процент", "")) or "0"
        rows.append((
            item.get("Дата применения", "") or "—",
            f"{format_money_spaced(item.get('Стало', ''))}",
            f"+{percent}%"
        ))
    lines.append(build_rent_status_block(rows))
    return "\n".join(lines)


def mark_rent_payment(lease_id, period_key, paid_by, receipt_file_id=""):
    ensure_rent_worksheets()
    leases = get_all_rent_leases()
    lease = find_rent_lease(leases, lease_id)
    if not lease:
        return {"status": "missing"}

    payments = get_all_rent_payments_with_rows()
    existing = get_rent_payment_for_period(payments, lease, period_key)
    if existing:
        return {"status": "exists", "payment": existing, "lease": lease}

    payment_id = next_prefixed_id(payments, "P")
    payload = {
        "id": payment_id,
        "lease_id": lease.get("id", ""),
        "point": lease.get("Точка", ""),
        "period": period_key,
        "amount": parse_numeric_value(lease.get("Текущая ставка", "")) or 0,
        "paid_date": today(),
        "paid_by": paid_by,
        "status": "оплачено",
        "receipt_file_id": receipt_file_id,
        "notes": "",
    }
    add_rent_payment_row(payload)

    if receipt_file_id:
        documents = get_all_rent_documents()
        document_id = next_prefixed_id(documents, "D")
        add_rent_document_row(
            {
                "id": document_id,
                "point": lease.get("Точка", ""),
                "doc_type": "Чек",
                "title": f"Чек аренды {format_rent_period_label(period_key)}",
                "related_type": "Оплата",
                "related_id": payment_id,
                "file_id": receipt_file_id,
                "uploaded_at": today(),
                "uploaded_by": paid_by,
            }
        )

    return {"status": "saved", "payment": payload, "lease": lease}


def clear_rent_payment_selection(rent):
    for key in ("selected_lease_id", "receipt_file_id", "period_target"):
        rent.pop(key, None)


def build_rent_menu_markup():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💳 Что оплатить", callback_data="rent_dashboard")],
            [InlineKeyboardButton("🏦 Реквизиты", callback_data="rent_requisites"), InlineKeyboardButton("📄 Карточки", callback_data="rent_cards")],
            [InlineKeyboardButton("⚙️ Управление", callback_data="rent_manage")],
            [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
        ]
    )


def build_rent_dashboard_markup(period_key, dashboard):
    keyboard = []
    if dashboard.get("unpaid"):
        keyboard.append([InlineKeyboardButton("✅ Отметить оплату", callback_data="rent_payments")])
    keyboard.append([InlineKeyboardButton("📅 Другой месяц", callback_data="rent_period_dashboard")])
    keyboard.append([
        InlineKeyboardButton("🏦 Реквизиты", callback_data="rent_requisites"),
        InlineKeyboardButton("📄 Карточки", callback_data="rent_cards"),
    ])
    keyboard.append([InlineKeyboardButton("⚙️ Управление", callback_data="rent_manage")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_rent_menu")])
    return InlineKeyboardMarkup(keyboard)


def build_rent_period_markup(target):
    period_keys = recent_rent_period_keys(6)
    keyboard = []
    row = []
    for period_key in period_keys:
        row.append(InlineKeyboardButton(format_rent_period_label(period_key), callback_data=f"rent_period_{period_key}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    back_callback = "rent_payments" if target == "payments" else "rent_dashboard"
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data=back_callback)])
    return InlineKeyboardMarkup(keyboard)


def build_rent_unpaid_markup(dashboard):
    keyboard = []
    for item in dashboard.get("unpaid", []):
        label = f"{item['point']} · {format_money_spaced(item['amount'])}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"rent_pay_pick_{item['lease_id']}")])
    keyboard.append([InlineKeyboardButton("📅 Другой месяц", callback_data="rent_period_payments")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="rent_dashboard")])
    return InlineKeyboardMarkup(keyboard)


def build_rent_requisites_select_markup(leases):
    keyboard = [[InlineKeyboardButton(lease.get("Точка", "—"), callback_data=f"rent_req_{lease.get('id', '')}")] for lease in leases]
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_rent_menu")])
    return InlineKeyboardMarkup(keyboard)


def build_rent_requisites_card_markup(lease_id, has_contract):
    keyboard = [
        [
            InlineKeyboardButton("📋 Р/счёт", callback_data=f"rent_copy_account_{lease_id}"),
            InlineKeyboardButton("📋 Всё", callback_data=f"rent_copy_all_{lease_id}"),
        ],
        [InlineKeyboardButton("💳 Отметить оплату", callback_data=f"rent_pay_direct_{lease_id}")],
    ]
    if has_contract:
        keyboard.append([InlineKeyboardButton("📄 Договор", callback_data=f"rent_req_doc_{lease_id}")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="rent_requisites")])
    return InlineKeyboardMarkup(keyboard)


def build_rent_cards_select_markup(leases):
    keyboard = [[InlineKeyboardButton(lease.get("Точка", "—"), callback_data=f"rent_card_{lease.get('id', '')}")] for lease in leases]
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_rent_menu")])
    return InlineKeyboardMarkup(keyboard)


def build_rent_card_markup(lease_id, data):
    has_contract = bool(str(data["lease"].get("Договор file_id", "") or "").strip())
    keyboard = [
        [InlineKeyboardButton("🏦 Реквизиты", callback_data=f"rent_card_req_{lease_id}")],
        [
            InlineKeyboardButton("📋 История оплат", callback_data=f"rent_card_history_{lease_id}"),
            InlineKeyboardButton("📈 Индексация", callback_data=f"rent_card_index_{lease_id}"),
        ],
    ]
    if has_contract:
        keyboard.append([InlineKeyboardButton("📄 Договор", callback_data=f"rent_card_doc_{lease_id}")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="rent_cards")])
    return InlineKeyboardMarkup(keyboard)


def build_rent_subview_back_markup(lease_id):
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"rent_card_{lease_id}")]])


def build_rent_manage_markup():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back_rent_menu")]])


def build_rent_payment_confirm_text(item, period_key, receipt_attached=False):
    lease = item.get("lease") or {}
    landlord_name = item.get("landlord_name", "—")

    lines = [f"<b>✅ Оплата — {escape_html(item.get('point', '?'))}</b>", ""]
    rows = [
        ("Период", format_rent_period_label(period_key), ""),
        ("Сумма", format_money_spaced(item.get("amount", "")), ""),
        ("Кому", landlord_name, ""),
        (
            "Дедлайн",
            f"до {lease.get('Дедлайн (число)', '—')} числа" if lease.get("Дедлайн (число)", "") else "—",
            "",
        ),
    ]
    lines.append(build_preformatted_block(rows))
    if receipt_attached:
        lines.extend(["", "📎 Чек прикреплён"])
    return "\n".join(lines)


def build_rent_payment_confirm_markup():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Подтвердить оплату", callback_data="rent_pay_confirm")],
            [
                InlineKeyboardButton("📎 Прикрепить чек", callback_data="rent_pay_receipt"),
                InlineKeyboardButton("🏦 Реквизиты", callback_data="rent_pay_requisites"),
            ],
            [InlineKeyboardButton("⬅️ Назад", callback_data="rent_payments")],
        ]
    )


def get_repair_context(context):
    return context.user_data.setdefault("repair", {})


def clear_repair_draft(repair_ctx):
    for key in (
        "point", "machine_id", "machine_quick_created", "reason", "description",
        "service_center_id", "broken_date", "selected_repair_id", "history_point",
        "history_machine_id", "status_options", "expense_type", "expense_amount",
        "expense_description", "machine_record", "machine_candidate",
        "photo_file_id", "date_broken_value", "date_sent_value", "date_plan_value", "doc_type",
    ):
        repair_ctx.pop(key, None)


def is_repair_active_status(status_text):
    return str(status_text or "").strip() in REPAIR_ACTIVE_STATUSES


def get_repair_status_icon(status_text):
    return REPAIR_STATUS_ICONS.get(str(status_text or "").strip(), "⚪")


def find_repair_center(centers, center_id):
    center_id = str(center_id or "").strip()
    return next((center for center in centers if str(center.get("id", "")).strip() == center_id), None)


def find_repair_machine(machines, machine_id):
    machine_id = str(machine_id or "").strip()
    return next((machine for machine in machines if str(machine.get("id", "")).strip() == machine_id), None)


def find_repair_record(repairs, repair_id):
    repair_id = str(repair_id or "").strip()
    return next((repair for repair in repairs if str(repair.get("id", "")).strip() == repair_id), None)


def get_machine_display_name(machine):
    if not machine:
        return "Аппарат"
    brand = str(machine.get("Бренд", "") or "").strip()
    model = str(machine.get("Модель", "") or "").strip()
    text = " ".join(part for part in (brand, model) if part)
    return text or str(machine.get("id", "") or "Аппарат")


def get_machine_button_label(machine):
    serial = str(machine.get("Серийный номер", "") or "").strip()
    serial_tail = serial[-5:] if len(serial) > 5 else serial
    name = get_machine_display_name(machine)
    if serial_tail:
        return f"{name} · {serial_tail}"
    return name


def get_repair_center_label(center):
    if not center:
        return "Сервис не указан"
    name = str(center.get("Название", "") or "").strip() or "Сервис"
    city = str(center.get("Город", "") or "").strip()
    return f"{name}, {city}" if city else name


def build_manual_repair_service_id(label):
    clean_label = str(label or "").strip()
    return f"{REPAIR_MANUAL_SERVICE_PREFIX}{clean_label}" if clean_label else ""


def get_manual_repair_service_label(service_id):
    raw_value = str(service_id or "").strip()
    if raw_value.startswith(REPAIR_MANUAL_SERVICE_PREFIX):
        return raw_value[len(REPAIR_MANUAL_SERVICE_PREFIX):].strip()
    return ""


def get_repair_service_label(repair=None, center=None):
    if center:
        return get_repair_center_label(center)
    manual_label = get_manual_repair_service_label((repair or {}).get("Сервис ID", ""))
    return manual_label or "Сервис не указан"


def is_google_sheets_busy_error(error):
    if not isinstance(error, APIError):
        return False
    text = str(error)
    return "Quota exceeded" in text or "[429]" in text or "Read requests" in text


def get_repair_status_options(current_status):
    options = REPAIR_STATUS_FLOW.get(str(current_status or "").strip())
    if options is None:
        return []
    return options


def get_repair_expenses_for_id(expenses, repair_id):
    repair_id = str(repair_id or "").strip()
    return [expense for expense in expenses if str(expense.get("Ремонт ID", "")).strip() == repair_id]


def compute_repair_totals(expenses):
    paid_total = 0.0
    pending_total = 0.0
    total = 0.0
    for expense in expenses:
        amount = parse_numeric_value(expense.get("Сумма", "")) or 0.0
        total += amount
        paid_raw = normalize_text_key(expense.get("Оплачено", ""))
        if paid_raw in {"да", "yes", "y", "оплачено"}:
            paid_total += amount
        else:
            pending_total += amount
    return paid_total, pending_total, total


def sync_repair_total(repair_id):
    repairs = get_all_repairs_with_rows()
    repair = find_repair_record(repairs, repair_id)
    if not repair:
        return None

    expenses = get_repair_expenses_for_id(get_all_repair_expenses(), repair_id)
    _, _, total = compute_repair_totals(expenses)
    repair["Итого расходов"] = format_number(total) if total else "0"
    update_repair_row(repair["__row"], repair)
    repair["expenses"] = expenses
    repair["total_cost"] = total
    return repair


def get_repair_days_open(repair):
    broken_dt = parse_date(repair.get("Дата поломки", ""))
    if not broken_dt:
        return 0

    end_dt = (
        parse_date(repair.get("Дата возврата", ""))
        or parse_date(repair.get("Дата готовности (факт)", ""))
    )
    if end_dt is None:
        end_dt = now_local()
    return max(0, (end_dt.date() - broken_dt.date()).days)


def get_repair_plan_delta(repair):
    plan_dt = parse_date(repair.get("Дата готовности (план)", ""))
    if not plan_dt:
        return None
    return (plan_dt.date() - now_local().date()).days


def build_repair_plan_text(repair):
    plan_dt = parse_date(repair.get("Дата готовности (план)", ""))
    if not plan_dt:
        return "—"
    delta = (plan_dt.date() - now_local().date()).days
    base = format_date(plan_dt.date())
    if delta > 0:
        return f"{base} (через {delta} дн)"
    if delta == 0:
        return f"{base} (сегодня)"
    return f"{base} (просрочен на {abs(delta)} дн)"


def build_repair_duration_text(days_open):
    return f"{days_open} дн" if days_open else "0 дн"


def build_active_repair_data():
    ensure_repair_worksheets()
    centers = get_all_repair_centers()
    machines = get_all_repair_machines()
    repairs = get_all_repairs_with_rows()
    expenses = get_all_repair_expenses()

    items = []
    for repair in repairs:
        status = str(repair.get("Статус", "") or "").strip()
        if not is_repair_active_status(status):
            continue

        repair_expenses = get_repair_expenses_for_id(expenses, repair.get("id", ""))
        paid_total, pending_total, total_cost = compute_repair_totals(repair_expenses)
        machine = find_repair_machine(machines, repair.get("Аппарат ID", ""))
        center = find_repair_center(centers, repair.get("Сервис ID", ""))
        item = {
            "repair": repair,
            "machine": machine,
            "center": center,
            "expenses": repair_expenses,
            "paid_total": paid_total,
            "pending_total": pending_total,
            "total_cost": total_cost,
            "days_open": get_repair_days_open(repair),
            "plan_delta": get_repair_plan_delta(repair),
            "machine_name": get_machine_display_name(machine),
            "status_icon": get_repair_status_icon(status),
        }
        items.append(item)

    def _sort_key(item):
        plan_delta = item.get("plan_delta")
        overdue_rank = 0
        if plan_delta is not None and plan_delta < 0:
            overdue_rank = -1000 + plan_delta
        return (
            overdue_rank,
            -item.get("days_open", 0),
            POINTS.index(item["repair"].get("Точка")) if item["repair"].get("Точка") in POINTS else 999,
            item["repair"].get("Точка", ""),
        )

    return sorted(items, key=_sort_key)


def get_active_repair_point_map():
    items = build_active_repair_data()
    result = {}
    for item in items:
        point = str(item["repair"].get("Точка", "") or "").strip()
        if not point:
            continue
        current = result.get(point)
        if not current or int(item["repair"].get("__row", 0)) > int(current["repair"].get("__row", 0)):
            result[point] = item
    return result


def get_active_repair_record_map():
    ensure_repair_worksheets()
    repairs = get_all_repairs_with_rows()
    result = {}
    for repair in repairs:
        status = str(repair.get("Статус", "") or "").strip()
        if not is_repair_active_status(status):
            continue
        point = str(repair.get("Точка", "") or "").strip()
        if not point:
            continue
        current = result.get(point)
        if not current or int(repair.get("__row", 0)) > int(current.get("__row", 0)):
            result[point] = repair
    return result


def get_repair_card_data(repair_id):
    ensure_repair_worksheets()
    centers = get_all_repair_centers()
    machines = get_all_repair_machines()
    repairs = get_all_repairs_with_rows()
    expenses = get_all_repair_expenses()
    documents = get_all_repair_documents()

    repair = find_repair_record(repairs, repair_id)
    if not repair:
        return None

    repair_expenses = get_repair_expenses_for_id(expenses, repair_id)
    paid_total, pending_total, total_cost = compute_repair_totals(repair_expenses)
    machine = find_repair_machine(machines, repair.get("Аппарат ID", ""))
    center = find_repair_center(centers, repair.get("Сервис ID", ""))
    related_docs = [
        doc for doc in documents
        if str(doc.get("Ремонт ID", "")).strip() == str(repair_id).strip()
    ]

    return {
        "repair": repair,
        "machine": machine,
        "center": center,
        "expenses": repair_expenses,
        "documents": related_docs,
        "paid_total": paid_total,
        "pending_total": pending_total,
        "total_cost": total_cost,
        "days_open": get_repair_days_open(repair),
    }


def get_machine_history_data(machine_id):
    ensure_repair_worksheets()
    machines = get_all_repair_machines()
    repairs = get_all_repairs()
    expenses = get_all_repair_expenses()

    machine = find_repair_machine(machines, machine_id)
    if not machine:
        return None

    history = []
    for repair in repairs:
        if str(repair.get("Аппарат ID", "")).strip() != str(machine_id).strip():
            continue
        repair_expenses = get_repair_expenses_for_id(expenses, repair.get("id", ""))
        _, _, total_cost = compute_repair_totals(repair_expenses)
        history.append(
            {
                "repair": repair,
                "total_cost": total_cost,
                "days_open": get_repair_days_open(repair),
            }
        )

    history.sort(
        key=lambda item: (
            parse_date(item["repair"].get("Дата поломки", "")) or datetime.min,
            item["repair"].get("id", ""),
        ),
        reverse=True,
    )

    total_cost = sum(item["total_cost"] for item in history)
    current_year = now_local().year
    year_total_cost = sum(
        item["total_cost"]
        for item in history
        if (parse_date(item["repair"].get("Дата поломки", "")) or datetime.min).year == current_year
    )
    return {
        "machine": machine,
        "history": history,
        "total_cost": total_cost,
        "year_total_cost": year_total_cost,
    }


def create_quick_repair_machine(point, raw_text):
    ensure_repair_worksheets()
    text = str(raw_text or "").strip()
    if not text:
        raise ValueError("empty-machine")

    machines = get_all_repair_machines_with_rows()
    machine_id = next_prefixed_id(machines, "M")
    brand = ""
    model = text
    serial = ""
    normalized = normalize_text_key(text)

    if normalized in {"не указано", "неизвестно", "не знаю"}:
        model = REPAIR_UNKNOWN_MACHINE_MODEL
    elif "/" in text:
        left, right = [part.strip() for part in text.split("/", 1)]
        model = left or text
        serial = right
    elif "серийн" in normalized:
        model = text
    else:
        parts = text.split()
        if len(parts) >= 2 and len(parts[0]) <= 12:
            brand = parts[0]
            model = " ".join(parts[1:])

    entry = {
        "id": machine_id,
        "Точка": point,
        "Бренд": brand,
        "Модель": model,
        "Серийный номер": serial,
        "Дата покупки": "",
        "Гарантия до": "",
        "Статус": REPAIR_MACHINE_REPAIR,
        "Заметки": f"Создано из ремонта {today()}",
    }
    row_num = add_repair_machine_row(entry)
    entry["__row"] = row_num
    return entry


def get_or_create_unknown_repair_machine(point):
    machines = get_point_repair_machines(point, include_discarded=True)
    for machine in machines:
        if normalize_text_key(machine.get("Модель", "")) == normalize_text_key(REPAIR_UNKNOWN_MACHINE_MODEL):
            return machine
    return create_quick_repair_machine(point, REPAIR_UNKNOWN_MACHINE_MODEL)


def get_point_repair_machines(point, include_discarded=False):
    machines = sort_by_point_name(get_all_repair_machines(), key="Точка")
    result = []
    for machine in machines:
        if machine.get("Точка") != point:
            continue
        if not include_discarded and str(machine.get("Статус", "")).strip() == REPAIR_MACHINE_DISCARDED:
            continue
        result.append(machine)
    return result


def create_repair_case(payload):
    ensure_repair_worksheets()
    repairs = get_all_repairs_with_rows()
    repair_id = next_prefixed_id(repairs, "R")
    machine = payload["machine"]
    broken_date = payload["broken_date"]
    guarantee_dt = parse_date(machine.get("Гарантия до", "")) if machine else None
    broken_dt = parse_date(broken_date)
    on_warranty = "Да" if guarantee_dt and broken_dt and broken_dt.date() <= guarantee_dt.date() else "Нет"

    entry = {
        "id": repair_id,
        "Аппарат ID": machine.get("id", "") if machine else "",
        "Точка": payload["point"],
        "Сервис ID": payload.get("service_center_id", ""),
        "Причина": payload["reason"],
        "Описание поломки": payload["description"],
        "Статус": REPAIR_STATUS_FIXED,
        "Дата поломки": broken_date,
        "Дата отправки": "",
        "Дата готовности (план)": "",
        "Дата готовности (факт)": "",
        "Дата возврата": "",
        "Перевозка откуда": "",
        "Перевозка куда": "",
        "На гарантии": on_warranty,
        "Итого расходов": "0",
        "Кто создал": payload["created_by"],
        "Заметки": "",
    }
    row_num = add_repair_row(entry)
    entry["__row"] = row_num

    if machine and machine.get("__row"):
        machine["Статус"] = REPAIR_MACHINE_REPAIR
        update_repair_machine_row(machine["__row"], machine)

    return entry


def update_repair_status_value(repair_id, new_status):
    repairs = get_all_repairs_with_rows()
    repair = find_repair_record(repairs, repair_id)
    if not repair:
        return None

    repair["Статус"] = new_status
    today_str = today()
    if new_status == REPAIR_STATUS_ON_THE_WAY and not str(repair.get("Дата отправки", "")).strip():
        repair["Дата отправки"] = today_str
    if new_status == REPAIR_STATUS_READY and not str(repair.get("Дата готовности (факт)", "")).strip():
        repair["Дата готовности (факт)"] = today_str
    if new_status == REPAIR_STATUS_INSTALLED:
        if not str(repair.get("Дата готовности (факт)", "")).strip():
            repair["Дата готовности (факт)"] = today_str
        if not str(repair.get("Дата возврата", "")).strip():
            repair["Дата возврата"] = today_str

    update_repair_row(repair["__row"], repair)

    machines = get_all_repair_machines_with_rows()
    machine = find_repair_machine(machines, repair.get("Аппарат ID", ""))
    if machine and machine.get("__row"):
        if new_status == REPAIR_STATUS_INSTALLED:
            machine["Статус"] = REPAIR_MACHINE_WORKING
        elif new_status == REPAIR_STATUS_DISCARDED:
            machine["Статус"] = REPAIR_MACHINE_DISCARDED
        else:
            machine["Статус"] = REPAIR_MACHINE_REPAIR
        update_repair_machine_row(machine["__row"], machine)

    repair["machine"] = machine
    return repair


def auto_close_repair_for_point(point_name):
    # A service report for the point means the machine is operating again,
    # so any active repair ticket should be closed (Установлен) and the
    # machine flipped back to Работает. Wrapped in try/except so a Sheets
    # hiccup never blocks the original service report save.
    try:
        normalized = str(point_name or "").strip()
        if not normalized:
            return None
        machines = get_all_repair_machines_with_rows()
        target = next(
            (m for m in machines
             if str(m.get("Точка", "")).strip() == normalized
             and str(m.get("Статус", "")).strip() == REPAIR_MACHINE_REPAIR),
            None,
        )
        if not target:
            return None

        machine_id = str(target.get("id", "")).strip()
        repairs = get_all_repairs_with_rows()
        active = [
            r for r in repairs
            if str(r.get("Аппарат ID", "")).strip() == machine_id
            and is_repair_active_status(r.get("Статус", ""))
        ]

        closed_ids = []
        if active:
            for repair in active:
                repair_id = str(repair.get("id", "")).strip()
                update_repair_status_value(repair_id, REPAIR_STATUS_INSTALLED)
                closed_ids.append(repair_id)
        else:
            target["Статус"] = REPAIR_MACHINE_WORKING
            update_repair_machine_row(target["__row"], target)

        logger.info(
            "auto-closed repair on service report: point=%s machine_id=%s closed_repairs=%s",
            normalized, machine_id, closed_ids,
        )
        return {"machine_id": machine_id, "closed_repairs": closed_ids}
    except Exception:
        logger.exception("auto_close_repair_for_point failed: point=%s", point_name)
        return None


def update_repair_service_value(repair_id, service_value):
    repairs = get_all_repairs_with_rows()
    repair = find_repair_record(repairs, repair_id)
    if not repair:
        return None

    repair["Сервис ID"] = str(service_value or "").strip()
    update_repair_row(repair["__row"], repair)
    return repair


def update_repair_broken_date_value(repair_id, broken_date):
    repairs = get_all_repairs_with_rows()
    repair = find_repair_record(repairs, repair_id)
    if not repair:
        return None

    repair["Дата поломки"] = str(broken_date or "").strip()

    machine_id = str(repair.get("Аппарат ID", "") or "").strip()
    if machine_id:
        machines = get_all_repair_machines_with_rows()
        machine = find_repair_machine(machines, machine_id)
        guarantee_dt = parse_date((machine or {}).get("Гарантия до", ""))
        broken_dt = parse_date(repair["Дата поломки"])
        if guarantee_dt and broken_dt and broken_dt.date() <= guarantee_dt.date():
            repair["На гарантии"] = "Да"
        else:
            repair["На гарантии"] = "Нет"

    update_repair_row(repair["__row"], repair)
    return repair


def update_repair_schedule_values(repair_id, sent_date=None, plan_date=None):
    repairs = get_all_repairs_with_rows()
    repair = find_repair_record(repairs, repair_id)
    if not repair:
        return None

    repair["Дата отправки"] = str(sent_date or "").strip()
    repair["Дата готовности (план)"] = str(plan_date or "").strip()
    update_repair_row(repair["__row"], repair)
    return repair


def delete_rows_from_worksheet(title, headers, row_numbers):
    if not row_numbers:
        return
    sheet = get_or_create_worksheet(title, headers)
    for row_num in sorted({int(row) for row in row_numbers if int(row) > 1}, reverse=True):
        sheet.delete_rows(row_num)


def get_all_repair_expenses_with_rows():
    ensure_repair_worksheets()
    return get_records_with_rows(REPAIR_SHEET_EXPENSES, REPAIR_EXPENSE_HEADERS)


def get_all_repair_documents_with_rows():
    ensure_repair_worksheets()
    return get_records_with_rows(REPAIR_SHEET_DOCUMENTS, REPAIR_DOCUMENT_HEADERS)


def delete_repair_case(repair_id):
    ensure_repair_worksheets()
    repairs = get_all_repairs_with_rows()
    repair = find_repair_record(repairs, repair_id)
    if not repair:
        return None

    machine_id = str(repair.get("Аппарат ID", "") or "").strip()
    expenses = get_all_repair_expenses_with_rows()
    documents = get_all_repair_documents_with_rows()

    delete_rows_from_worksheet(
        REPAIR_SHEET_EXPENSES,
        REPAIR_EXPENSE_HEADERS,
        [item["__row"] for item in expenses if str(item.get("Ремонт ID", "")).strip() == repair_id],
    )
    delete_rows_from_worksheet(
        REPAIR_SHEET_DOCUMENTS,
        REPAIR_DOCUMENT_HEADERS,
        [item["__row"] for item in documents if str(item.get("Ремонт ID", "")).strip() == repair_id],
    )
    delete_rows_from_worksheet(REPAIR_SHEET_REPAIRS, REPAIR_HEADERS, [repair["__row"]])

    if machine_id:
        machines = get_all_repair_machines_with_rows()
        machine = find_repair_machine(machines, machine_id)
        if machine and str(machine.get("Статус", "")).strip() != REPAIR_MACHINE_DISCARDED:
            remaining_active = any(
                str(item.get("id", "")).strip() != repair_id
                and str(item.get("Аппарат ID", "")).strip() == machine_id
                and is_repair_active_status(item.get("Статус", ""))
                for item in repairs
            )
            machine["Статус"] = REPAIR_MACHINE_REPAIR if remaining_active else REPAIR_MACHINE_WORKING
            update_repair_machine_row(machine["__row"], machine)

    return repair


def add_repair_expense(payload):
    ensure_repair_worksheets()
    expenses = get_all_repair_expenses()
    expense_id = next_prefixed_id(expenses, "RE")
    entry = {
        "id": expense_id,
        "Ремонт ID": payload["repair_id"],
        "Тип расхода": payload["expense_type"],
        "Описание": payload.get("description", ""),
        "Сумма": payload["amount"],
        "Дата": today(),
        "Оплачено": "Да" if payload.get("paid") else "Нет",
        "Кто отметил": payload["marked_by"],
        "Документ file_id": payload.get("file_id", ""),
        "Заметки": "",
    }
    add_repair_expense_row(entry)
    repair = sync_repair_total(payload["repair_id"])
    return entry, repair


def add_repair_document(payload):
    ensure_repair_worksheets()
    documents = get_all_repair_documents()
    document_id = next_prefixed_id(documents, "RD")
    entry = {
        "id": document_id,
        "Ремонт ID": payload["repair_id"],
        "Точка": payload["point"],
        "Тип": payload["doc_type"],
        "Название": payload.get("title", f"{payload['doc_type']} — {payload['repair_id']}"),
        "file_id": payload["file_id"],
        "Дата загрузки": payload.get("uploaded_at", today()),
        "Кто загрузил": payload.get("uploaded_by", ""),
    }
    add_repair_document_row(entry)
    return entry


def build_repair_dashboard_text(active_repairs):
    lines = [f"<b>🛠 Активные ремонты ({len(active_repairs)})</b>"]
    if not active_repairs:
        lines.extend(["", "⚪ Сейчас нет активных ремонтов."])
        return "\n".join(lines)

    for item in active_repairs:
        repair = item["repair"]
        plan_text = build_repair_plan_text(repair)
        icon = get_repair_status_icon(repair.get("Статус", ""))
        lines.extend(
            [
                "",
                f"<b>{icon} {escape_html(repair.get('id', '—'))} · {escape_html(repair.get('Точка', '—'))} · {escape_html(item['machine_name'])}</b>",
                build_preformatted_block(
                    [
                        ("Статус", repair.get("Статус", "—"), ""),
                        ("Дней", str(item["days_open"]), ""),
                        ("План", plan_text, ""),
                        ("Расходы", format_money_spaced(item["total_cost"]), ""),
                    ]
                ),
            ]
        )
    return "\n".join(lines)


def build_repair_expense_lines(expenses):
    if not expenses:
        return "⚪ Пока нет расходов."

    type_width = max(len(str(expense.get("Тип расхода", "") or "—")) for expense in expenses)
    lines = []
    for expense in expenses:
        paid = normalize_text_key(expense.get("Оплачено", ""))
        marker = "✅" if paid in {"да", "yes", "оплачено"} else "⏳"
        amount_text = format_money_spaced(expense.get("Сумма", ""))
        label = str(expense.get("Тип расхода", "—") or "—")
        line = f"{marker} {label.ljust(type_width)}  {amount_text}"
        description = str(expense.get("Описание", "") or "").strip()
        if description:
            line += f"  · {description}"
        lines.append(line.rstrip())
    return build_text_pre_block(lines)


def build_repair_documents_lines(documents):
    if not documents:
        return "⚪ Пока нет документов."

    lines = []
    for doc in documents[:8]:
        title = str(doc.get("Название", "") or doc.get("Тип", "Документ")).strip()
        uploaded_at = str(doc.get("Дата загрузки", "") or "—").strip()
        lines.append(f"📄 {title} — {uploaded_at}")
    return "\n".join(lines)


def build_repair_card_text(data):
    repair = data["repair"]
    machine = data.get("machine")
    center = data.get("center")
    lines = [f"<b>🛠 {escape_html(repair.get('id', '—'))} · {escape_html(repair.get('Точка', '—'))}</b>", ""]

    main_rows = [
        ("Аппарат", get_machine_display_name(machine), ""),
        ("Серийник", (machine or {}).get("Серийный номер", "") or "—", ""),
        ("Статус", f"{get_repair_status_icon(repair.get('Статус', ''))} {repair.get('Статус', '—')}", ""),
        ("Гарантия", repair.get("На гарантии", "—") or "—", ""),
    ]
    lines.append("<b>📋 Основное</b>")
    lines.append(build_preformatted_block(main_rows))
    lines.extend(["", "<b>🧩 Поломка</b>", escape_html(repair.get("Причина", "—") or "—")])

    description = str(repair.get("Описание поломки", "") or "").strip()
    if description:
        lines.append(escape_html(description))

    lines.extend(["", "<b>📅 Сроки</b>"])
    lines.append(
        build_preformatted_block(
            [
                ("Поломка", repair.get("Дата поломки", "—") or "—", ""),
                ("Отправлен", repair.get("Дата отправки", "—") or "—", ""),
                ("План", build_repair_plan_text(repair), ""),
                ("В ремонте", build_repair_duration_text(data["days_open"]), ""),
            ]
        )
    )

    lines.extend(["", "<b>🏭 Сервис</b>"])
    service_rows = [
        ("Сервис", get_repair_service_label(repair, center), ""),
        ("Телефон", (center or {}).get("Телефон", "") or "—", ""),
        ("Откуда", repair.get("Перевозка откуда", "—") or "—", ""),
        ("Куда", repair.get("Перевозка куда", "—") or "—", ""),
    ]
    lines.append(build_preformatted_block(service_rows))

    lines.extend(["", "<b>💰 Расходы</b>", build_repair_expense_lines(data.get("expenses", []))])
    lines.append(
        build_preformatted_block(
            [
                ("Оплачено", format_money_spaced(data["paid_total"]), ""),
                ("Ожидается", format_money_spaced(data["pending_total"]), ""),
                ("Итого", format_money_spaced(data["total_cost"]), ""),
            ]
        )
    )

    lines.extend(["", "<b>📎 Документы</b>", build_repair_documents_lines(data.get("documents", []))])
    return "\n".join(lines)


def build_machine_history_text(machine, history_data):
    history = history_data.get("history", [])
    title = f"<b>📚 История — {escape_html(get_machine_display_name(machine))} · {escape_html(machine.get('Точка', '—'))}</b>"
    lines = [title]
    if not history:
        lines.extend(["", "⚪ По этому аппарату пока нет истории ремонтов."])
        return "\n".join(lines)

    block_lines = []
    for item in history:
        repair = item["repair"]
        broken_dt = parse_date(repair.get("Дата поломки", ""))
        period_text = f"{broken_dt.month:02d}.{broken_dt.year}" if broken_dt else "—"
        block_lines.append(
            f"{repair.get('id', '—')}  {period_text}  {repair.get('Причина', '—')}  {format_money_spaced(item['total_cost'])}  {item['days_open']} дн"
        )
    lines.extend(["", build_text_pre_block(block_lines), ""])
    lines.append(
        build_preformatted_block(
            [
                ("Всего ремонтов", str(len(history)), ""),
                ("За год", format_money_spaced(history_data.get("year_total_cost", 0)), ""),
                ("Всего", format_money_spaced(history_data.get("total_cost", 0)), ""),
            ]
        )
    )
    lines.extend(["", "👇 Нажми на ремонт, чтобы открыть карточку и отредактировать."])
    return "\n".join(lines)


def build_machine_history_markup(history_data, back_callback):
    keyboard = []
    for item in history_data.get("history", []):
        repair = item["repair"]
        rid = str(repair.get("id", "")).strip()
        if not rid:
            continue
        broken_dt = parse_date(repair.get("Дата поломки", ""))
        period_text = f"{broken_dt.month:02d}.{broken_dt.year}" if broken_dt else "—"
        reason = str(repair.get("Причина", "") or "—").strip()
        if len(reason) > 30:
            reason = reason[:29] + "…"
        label = f"{rid} · {period_text} · {reason}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"repair_open_{rid}")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data=back_callback)])
    return InlineKeyboardMarkup(keyboard)


def build_repair_centers_text(centers):
    lines = ["<b>🏭 Сервисные центры</b>"]
    if not centers:
        lines.extend(["", "⚪ Пока нет сервисных центров."])
        return "\n".join(lines)

    block_lines = []
    for center in centers:
        city = str(center.get("Город", "") or "—").strip()
        phone = str(center.get("Телефон", "") or "—").strip()
        block_lines.append(f"{center.get('id', '—')}  {center.get('Название', '—')}  {city}  {phone}")
    lines.extend(["", build_text_pre_block(block_lines)])
    return "\n".join(lines)


def build_repair_machines_text(machines):
    lines = ["<b>☕ Аппараты</b>"]
    if not machines:
        lines.extend(["", "⚪ Пока нет аппаратов в реестре ремонта."])
        return "\n".join(lines)

    block_lines = []
    for machine in sort_by_point_name(machines, key="Точка"):
        block_lines.append(
            f"{machine.get('Точка', '—')}  {get_machine_display_name(machine)}  {machine.get('Статус', '—')}"
        )
    lines.extend(["", build_text_pre_block(block_lines)])
    return "\n".join(lines)


def build_repair_menu_markup():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🆕 Сломалось / В ремонт", callback_data="repair_new")],
            [InlineKeyboardButton("🛠 Активные ремонты", callback_data="repair_active")],
            [InlineKeyboardButton("📖 История аппаратов", callback_data="repair_history")],
            [InlineKeyboardButton("🔙 Назад", callback_data="back_main")],
        ]
    )


def build_repair_dashboard_markup(active_repairs):
    keyboard = []
    for item in active_repairs[:12]:
        repair = item["repair"]
        label = f"{repair.get('id', '—')} · {repair.get('Точка', '—')}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"repair_open_{repair.get('id', '')}")])
    keyboard.append([InlineKeyboardButton("🆕 Сломалось / В ремонт", callback_data="repair_new")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_repair_menu")])
    return InlineKeyboardMarkup(keyboard)


def build_repair_card_markup(repair_id, data):
    keyboard = [
        [
            InlineKeyboardButton("📝 Обновить статус", callback_data=f"repair_status_{repair_id}"),
            InlineKeyboardButton("🏭 Указать сервис", callback_data=f"repair_service_{repair_id}"),
        ],
        [
            InlineKeyboardButton("📅 Указать сроки", callback_data=f"repair_dates_{repair_id}"),
            InlineKeyboardButton("💸 Добавить расход", callback_data=f"repair_expense_{repair_id}"),
        ],
        [
            InlineKeyboardButton("📎 Документы", callback_data=f"repair_docs_{repair_id}"),
            InlineKeyboardButton("✅ Вернули на точку", callback_data=f"repair_return_{repair_id}", style="primary"),
        ],
        [
            InlineKeyboardButton("🗑 Удалить ремонт", callback_data=f"repair_delete_{repair_id}", style="danger"),
        ],
    ]
    machine = data.get("machine")
    if machine and machine.get("id"):
        keyboard.append([InlineKeyboardButton("📖 История аппарата", callback_data=f"repair_hist_machine_{machine.get('id')}")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="repair_active")])
    return InlineKeyboardMarkup(keyboard)


def build_repair_status_markup(repair_id, options):
    keyboard = [
        [InlineKeyboardButton(status, callback_data=f"repair_status_opt_{repair_id}_{index}")]
        for index, status in enumerate(options)
    ]
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"repair_status_back_{repair_id}")])
    return InlineKeyboardMarkup(keyboard)


def build_repair_point_markup():
    keyboard = []
    row = []
    for index, point in enumerate(POINTS):
        row.append(InlineKeyboardButton(point, callback_data=f"repair_point_{point}"))
        if len(row) == 2 or index == len(POINTS) - 1:
            keyboard.append(row)
            row = []
    keyboard.append([InlineKeyboardButton("⬅️ Отмена", callback_data="back_repair_menu")])
    return InlineKeyboardMarkup(keyboard)


def build_repair_machine_markup(point, machines):
    keyboard = [[InlineKeyboardButton(get_machine_button_label(machine), callback_data=f"repair_machine_{machine.get('id', '')}")] for machine in machines]
    keyboard.append([InlineKeyboardButton("✏️ Ввести модель", callback_data="repair_machine_manual")])
    keyboard.append([InlineKeyboardButton("🤷 Не знаю модель", callback_data="repair_machine_unknown")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="repair_new")])
    return InlineKeyboardMarkup(keyboard)


def build_repair_single_machine_markup():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Да, этот аппарат", callback_data="repair_machine_single_yes")],
            [InlineKeyboardButton("✏️ Ввести другую модель", callback_data="repair_machine_manual")],
            [InlineKeyboardButton("🤷 Не знаю модель", callback_data="repair_machine_unknown")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="repair_new")],
        ]
    )


def build_repair_no_machine_markup():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✏️ Ввести модель", callback_data="repair_machine_manual")],
            [InlineKeyboardButton("🤷 Не знаю модель", callback_data="repair_machine_unknown")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="repair_new")],
        ]
    )


def build_repair_reason_markup():
    keyboard = []
    row = []
    for index, reason in enumerate(REPAIR_REASONS):
        row.append(InlineKeyboardButton(reason, callback_data=f"repair_reason_{index}"))
        if len(row) == 2 or index == len(REPAIR_REASONS) - 1:
            keyboard.append(row)
            row = []
    keyboard.append([InlineKeyboardButton("✏️ Другое", callback_data="repair_reason_other")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="repair_back_machine_step")])
    return InlineKeyboardMarkup(keyboard)


def build_repair_description_markup():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⏭ Пропустить", callback_data="repair_description_skip")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="repair_back_reason_step")],
        ]
    )


def build_repair_photo_markup():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⏭ Пропустить", callback_data="repair_photo_skip")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="repair_back_description_step")],
        ]
    )


def build_repair_broken_date_markup(last_service_date=""):
    keyboard = [
        [InlineKeyboardButton(f"Сегодня ({today()})", callback_data="repair_date_today")],
        [InlineKeyboardButton(f"Вчера ({yesterday()})", callback_data="repair_date_yesterday")],
        [InlineKeyboardButton(f"Позавчера ({day_before_yesterday()})", callback_data="repair_date_daybefore")],
    ]
    if last_service_date:
        keyboard.append(
            [InlineKeyboardButton(f"🔁 Последнее обслуживание ({last_service_date})", callback_data="repair_date_last_service")]
        )
    keyboard.extend(
        [
            [InlineKeyboardButton("✏️ Ввести дату", callback_data="repair_date_custom")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="repair_date_back_photo")],
        ]
    )
    return InlineKeyboardMarkup(keyboard)


def build_repair_center_markup(centers, back_callback="repair_open_back"):
    keyboard = [[InlineKeyboardButton(get_repair_center_label(center), callback_data=f"repair_center_{center.get('id', '')}")] for center in centers]
    keyboard.append([InlineKeyboardButton("✏️ Ввести вручную", callback_data="repair_center_manual")])
    keyboard.append([InlineKeyboardButton("⏭ Пока не знаю", callback_data="repair_center_skip")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data=back_callback)])
    return InlineKeyboardMarkup(keyboard)


def build_repair_sent_date_markup():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"Сегодня ({today()})", callback_data="repair_sent_today")],
            [InlineKeyboardButton(f"Вчера ({yesterday()})", callback_data="repair_sent_yesterday")],
            [InlineKeyboardButton(f"Позавчера ({day_before_yesterday()})", callback_data="repair_sent_daybefore")],
            [InlineKeyboardButton("✏️ Ввести дату", callback_data="repair_sent_custom")],
            [InlineKeyboardButton("⏭ Пропустить", callback_data="repair_sent_skip")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="repair_sent_back_card")],
        ]
    )


def build_repair_broken_edit_markup(last_service_date=""):
    keyboard = [
        [InlineKeyboardButton(f"Сегодня ({today()})", callback_data="repair_broken_today")],
        [InlineKeyboardButton(f"Вчера ({yesterday()})", callback_data="repair_broken_yesterday")],
        [InlineKeyboardButton(f"Позавчера ({day_before_yesterday()})", callback_data="repair_broken_daybefore")],
    ]
    if last_service_date:
        keyboard.append(
            [InlineKeyboardButton(f"🔁 Последнее обслуживание ({last_service_date})", callback_data="repair_broken_last_service")]
        )
    keyboard.extend(
        [
            [InlineKeyboardButton("✏️ Ввести дату", callback_data="repair_broken_custom")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="repair_broken_back_menu")],
        ]
    )
    return InlineKeyboardMarkup(keyboard)


def build_repair_plan_date_markup():
    in_three_days = format_date(now_local() + timedelta(days=3))
    in_seven_days = format_date(now_local() + timedelta(days=7))
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"Через 3 дня ({in_three_days})", callback_data="repair_plan_3")],
            [InlineKeyboardButton(f"Через 7 дней ({in_seven_days})", callback_data="repair_plan_7")],
            [InlineKeyboardButton("✏️ Ввести дату", callback_data="repair_plan_custom")],
            [InlineKeyboardButton("⏭ Пропустить", callback_data="repair_plan_skip")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="repair_plan_back_menu")],
        ]
    )


def build_repair_delete_confirm_markup(repair_id):
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🗑 Да, удалить", callback_data=f"repair_delete_confirm_{repair_id}", style="danger")],
            [InlineKeyboardButton("⬅️ Назад", callback_data=f"repair_delete_cancel_{repair_id}")],
        ]
    )


def build_repair_dates_menu_markup(repair_id):
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🗓 Дата поломки", callback_data=f"repair_dates_field_broken_{repair_id}")],
            [InlineKeyboardButton("🚚 Дата отправки", callback_data=f"repair_dates_field_sent_{repair_id}")],
            [InlineKeyboardButton("🏁 Плановая готовность", callback_data=f"repair_dates_field_plan_{repair_id}")],
            [InlineKeyboardButton("⬅️ Назад", callback_data=f"repair_open_{repair_id}")],
        ]
    )


def build_repair_history_points_markup(points):
    keyboard = [[InlineKeyboardButton(point, callback_data=f"repair_hist_point_{point}")] for point in points]
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_repair_menu")])
    return InlineKeyboardMarkup(keyboard)


def build_repair_history_machines_markup(point, machines):
    keyboard = [[InlineKeyboardButton(get_machine_button_label(machine), callback_data=f"repair_hist_machine_{machine.get('id', '')}")] for machine in machines]
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="repair_history")])
    return InlineKeyboardMarkup(keyboard)


def build_repair_history_back_markup(point):
    if point:
        return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"repair_hist_point_{point}")]])
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="repair_history")]])


def build_repair_refs_markup():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🏭 Сервисные центры", callback_data="repair_refs_centers"), InlineKeyboardButton("☕ Аппараты", callback_data="repair_refs_machines")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_repair_menu")],
        ]
    )


def build_repair_expense_type_markup():
    keyboard = []
    row = []
    for index, expense_type in enumerate(REPAIR_EXPENSE_TYPES):
        row.append(InlineKeyboardButton(expense_type, callback_data=f"repair_exp_type_{index}"))
        if len(row) == 2 or index == len(REPAIR_EXPENSE_TYPES) - 1:
            keyboard.append(row)
            row = []
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="repair_expense_back_card")])
    return InlineKeyboardMarkup(keyboard)


def build_repair_expense_paid_markup():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Да", callback_data="repair_exp_paid_yes"), InlineKeyboardButton("⏳ Нет", callback_data="repair_exp_paid_no")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="repair_expense_back_description")],
        ]
    )


def build_repair_docs_markup(repair_id):
    keyboard = []
    row = []
    for index, doc_type in enumerate(REPAIR_DOCUMENT_TYPES):
        row.append(InlineKeyboardButton(doc_type, callback_data=f"repair_doc_type_{index}"))
        if len(row) == 2 or index == len(REPAIR_DOCUMENT_TYPES) - 1:
            keyboard.append(row)
            row = []
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"repair_open_{repair_id}")])
    return InlineKeyboardMarkup(keyboard)


def build_repair_docs_back_markup(repair_id):
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"repair_docs_{repair_id}")]])


def build_procurement_rows(items, value_key="network_total"):
    rows = []
    for item_data in items:
        raw_value = item_data.get(value_key)
        if raw_value is None:
            value = "—"
        else:
            value = format_number(round(raw_value, 2))
        rows.append(
            (
                item_data["item"],
                value,
                get_procurement_unit_short(item_data["unit"]),
            )
        )
    return rows


def build_revision_values_from_record(record):
    return {item: record.get(item, "") for item in REVISION_ITEMS}


def find_revision_record(period, location, with_rows=False):
    records = get_all_revisions_with_rows() if with_rows else get_all_revisions()
    matches = [
        record for record in records
        if record.get("Период") == period and record.get("Локация") == location
    ]
    if not matches:
        return None
    if with_rows:
        return max(matches, key=lambda record: record.get("__row", 0))
    return matches[-1]


def order_revision_locations(locations):
    known = [location for location in REVISION_LOCATIONS if location in locations]
    extras = sorted(location for location in locations if location not in REVISION_LOCATIONS)
    return known + extras


def build_revision_record_text(record):
    lines = [
        "<b>📦 Ревизия</b>",
        f"📅 {escape_html(format_period_label(record.get('Период', '')))}",
        f"📍 {escape_html(record.get('Локация', '?'))}",
        f"👤 {escape_html(record.get('Кто', '?'))}",
        f"🕒 Заполнено: {escape_html(record.get('Дата заполнения', '?'))}",
        "",
    ]
    rows = []
    for item in REVISION_ITEMS:
        value = record.get(item, "")
        rows.append(
            (
                item,
                "—" if value in (None, "") else format_number(value),
                "" if value in (None, "") else get_procurement_unit_short(get_revision_unit(item)),
            )
        )
    lines.append(build_preformatted_block(rows))
    return "\n".join(lines)


def build_revision_summary_text(period, records):
    period_records = [record for record in records if record.get("Период") == period]
    lines = [f"<b>📦 Общая ревизия — {escape_html(format_period_label(period))}</b>", ""]

    filled_locations = order_revision_locations({record.get("Локация", "") for record in period_records if record.get("Локация")})
    missing_locations = [location for location in REVISION_LOCATIONS if location not in filled_locations]

    lines.append(f"Заполнено: {len(filled_locations)}/{len(REVISION_LOCATIONS)}")
    if filled_locations:
        lines.append("")
        lines.append("📍 Есть ревизия")
        lines.extend(f"• {escape_html(location)}" for location in filled_locations)
    if missing_locations:
        lines.append("")
        lines.append("⚪ Нет ревизии")
        lines.extend(f"• {escape_html(location)}" for location in missing_locations)

    stock_totals = build_revision_stock_totals(period_records)

    def build_total_rows(value_key):
        rows = []
        for item_name in REVISION_ITEMS:
            item_data = stock_totals["items"][item_name]
            value = item_data.get(value_key)
            rows.append(
                (
                    item_name,
                    "—" if value is None else format_number(value),
                    "" if value is None else get_procurement_unit_short(get_revision_unit(item_name)),
                )
            )
        return rows

    lines.append("")
    lines.append("<b>📊 Всего</b>")
    lines.append(build_preformatted_block(build_total_rows("total_value")))
    lines.append("")
    lines.append("<b>📍 На точках</b>")
    lines.append(build_preformatted_block(build_total_rows("point_total")))
    lines.append("")
    lines.append("<b>🏠 Дома</b>")
    lines.append(build_preformatted_block(build_total_rows("home_value")))
    lines.append("")
    lines.append("<b>🚗 Гараж</b>")
    lines.append(build_preformatted_block(build_total_rows("garage_value")))

    return "\n".join(lines)


EXCEL_EXPORT_ROW_MAP = [
    ("Кофе", "Кофе"),
    ("Молоко", "Молоко"),
    ("Шоколад", "Шоколад"),
    ("Мока", "Мока"),
    ("Сахар", "Сахар"),
    ("Сироп", "Сиропы"),
    ("Стаканы", "Стаканы"),
    ("КрышкиЧ", "Крышки чёрн"),
    ("КрышкиБ", "Крышки бел"),
    ("Палочки", "Палочки"),
    ("Трубочки", "Трубочки"),
    ("Капхолдеры", "Манжеты"),
]

EXCEL_EXPORT_COL_MAP = [
    ("Сити", ["Сити"]),
    ("Белом", ["Беломорский"]),
    ("Бел 2", ["Бел2"]),
    ("Южн", ["Южный"]),
    ("Гагарина", ["Гагарина"]),
    ("Макси", ["Макси"]),
    ("Гиппо", ["Гиппо"]),
    ("Дома", ["Дома", "Гараж"]),
]


def build_revision_excel_export_text(period, records):
    period_records = [r for r in records if r.get("Период") == period]
    by_location = {r.get("Локация", ""): r for r in period_records}

    # matrix[row][col] = string value (or "" for empty)
    matrix = []
    has_any_data = False
    for _, bot_item in EXCEL_EXPORT_ROW_MAP:
        row = []
        for _, bot_locations in EXCEL_EXPORT_COL_MAP:
            if not bot_item:
                row.append("")
                continue
            total = 0.0
            has_value = False
            for loc in bot_locations:
                rec = by_location.get(loc)
                if not rec:
                    continue
                v = parse_numeric_value(rec.get(bot_item, ""))
                if v is None:
                    continue
                total += v
                has_value = True
            if has_value:
                has_any_data = True
                row.append(format_number(total))
            else:
                row.append("")
        matrix.append(row)

    lines = [
        f"<b>📤 Экспорт для Excel — {escape_html(format_period_label(period))}</b>",
        "",
        "Telegram не сохраняет табы при копировании, поэтому каждый столбец отдельно.",
        "Для каждого столбца: скопируй блок и вставь в Excel в первую ячейку «Кофе» соответствующей колонки.",
        "",
        "Порядок строк (одинаковый для всех 8 блоков):",
        "Кофе · Молоко · Шоколад · Мока · Сахар · Сироп · Стаканы · КрышкиЧ · КрышкиБ · Палочки · Трубочки · Капхолдеры",
        "",
    ]
    if not has_any_data:
        lines.append("⚪ За этот месяц нет ни одной заполненной ревизии — все столбцы будут пустыми.")
        lines.append("")

    for col_idx, (col_name, _) in enumerate(EXCEL_EXPORT_COL_MAP):
        col_values = [matrix[row_idx][col_idx] for row_idx in range(len(matrix))]
        block = "\n".join(col_values)
        lines.append(f"📍 <b>{escape_html(col_name)}</b>")
        lines.append("<pre>" + escape_html(block) + "</pre>")
    return "\n".join(lines)


def build_revision_all_points_detailed_text(period, records):
    period_records = [record for record in records if record.get("Период") == period]
    stock_totals = build_revision_stock_totals(period_records)
    by_location = stock_totals["by_location"]
    lines = [f"<b>📍 Ревизия по всем точкам — {escape_html(format_period_label(period))}</b>", ""]

    sections = [
        ("📍 " + p, p) for p in POINTS
    ] + [("🏠 Дома", "Дома"), ("🚗 Гараж", "Гараж")]

    any_filled = False
    for title, location in sections:
        record = by_location.get(location)
        if not record:
            lines.append(f"<b>{title}</b>")
            lines.append("⚪ нет данных")
            lines.append("")
            continue
        any_filled = True
        rows = []
        for item_name in REVISION_ITEMS:
            value = parse_numeric_value(record.get(item_name, ""))
            rows.append((
                item_name,
                "—" if value is None else format_number(value),
                "" if value is None else get_procurement_unit_short(get_revision_unit(item_name)),
            ))
        lines.append(f"<b>{title}</b>")
        lines.append(build_preformatted_block(rows))
        lines.append("")

    if not any_filled:
        lines.append("⚪ За этот месяц нет ни одной заполненной ревизии.")
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def build_revision_item_text(period, item_name, records):
    period_records = [record for record in records if record.get("Период") == period]
    lines = [
        f"<b>🧾 {escape_html(item_name)} — {escape_html(format_period_label(period))}</b>",
        "",
    ]

    stock_totals = build_revision_stock_totals(period_records)
    item_totals = stock_totals["items"][item_name]
    values_by_location = {record.get("Локация", ""): record.get(item_name, "") for record in period_records}

    summary_rows = [
        ("Всего", "—" if item_totals["total_value"] is None else format_number(item_totals["total_value"]), "" if item_totals["total_value"] is None else get_procurement_unit_short(get_revision_unit(item_name))),
        ("На точках", "—" if item_totals["point_total"] is None else format_number(item_totals["point_total"]), "" if item_totals["point_total"] is None else get_procurement_unit_short(get_revision_unit(item_name))),
        ("Дома", "—" if item_totals["home_value"] is None else format_number(item_totals["home_value"]), "" if item_totals["home_value"] is None else get_procurement_unit_short(get_revision_unit(item_name))),
    ]
    lines.append(build_preformatted_block(summary_rows))
    lines.append("")

    filled_rows = []
    empty = []
    for location in REVISION_LOCATIONS:
        value = values_by_location.get(location, "")
        if value in (None, ""):
            empty.append(location)
        else:
            filled_rows.append(
                (
                    location,
                    format_number(value),
                    get_procurement_unit_short(get_revision_unit(item_name)),
                )
            )

    if filled_rows:
        lines.append(build_preformatted_block(filled_rows))
    if empty:
        lines.append("")
        lines.append("⚪ Без данных:")
        lines.extend(f"• {escape_html(location)}" for location in empty)

    return "\n".join(lines)


def classify_threshold_level(value, critical_value, warning_value):
    if value is None:
        return None
    if value <= critical_value:
        return "critical"
    if value <= warning_value:
        return "warning"
    return None


def build_revision_stock_totals(period_records):
    by_location = {record.get("Локация", ""): record for record in period_records}
    home_record = by_location.get("Дома")
    garage_record = by_location.get("Гараж")
    items = {}

    for item_name in REVISION_ITEMS:
        point_values = {}
        point_total = 0.0
        has_point_data = False
        for location in POINTS:
            record = by_location.get(location)
            value = parse_numeric_value(record.get(item_name, "")) if record else None
            if value is None:
                continue
            point_values[location] = value
            point_total += value
            has_point_data = True

        home_value = parse_numeric_value(home_record.get(item_name, "")) if home_record else None
        garage_value = parse_numeric_value(garage_record.get(item_name, "")) if garage_record else None
        total_value = None
        if has_point_data or home_value is not None or garage_value is not None:
            total_value = point_total + (home_value or 0) + (garage_value or 0)

        items[item_name] = {
            "point_values": point_values,
            "point_total": point_total if has_point_data else None,
            "home_value": home_value,
            "garage_value": garage_value,
            "total_value": total_value,
        }

    home_has_data = bool(
        home_record and any(item_data["home_value"] is not None for item_data in items.values())
    )
    garage_has_data = bool(
        garage_record and any(item_data["garage_value"] is not None for item_data in items.values())
    )

    return {
        "by_location": by_location,
        "home_record": home_record,
        "garage_record": garage_record,
        "home_has_data": home_has_data,
        "garage_has_data": garage_has_data,
        "items": items,
    }


def analyze_revision_thresholds(period, records):
    period_records = [record for record in records if record.get("Период") == period]
    stock_totals = build_revision_stock_totals(period_records)
    by_location = stock_totals["by_location"]
    home_record = stock_totals["home_record"]
    home_has_data = stock_totals["home_has_data"]

    items = []
    point_critical = []
    point_warning = []

    for item_name, thresholds in REVISION_STOCK_THRESHOLDS.items():
        item_totals = stock_totals["items"][item_name]
        point_values = item_totals["point_values"]
        network_total = item_totals["total_value"]
        has_network_data = network_total is not None
        home_value = item_totals["home_value"]
        network_level = None
        if has_network_data:
            network_level = classify_threshold_level(
                network_total,
                thresholds["network_critical"],
                thresholds["network_warning"],
            )

        items.append(
            {
                "item": item_name,
                "unit": get_revision_unit(item_name),
                "thresholds": thresholds,
                "network_total": network_total,
                "has_network_data": has_network_data,
                "network_level": network_level,
                "home_value": home_value,
                "point_total": item_totals["point_total"],
                "point_values": point_values,
            }
        )

    for point in POINTS:
        critical_items = []
        warning_items = []
        for item_data in items:
            value = item_data["point_values"].get(point)
            if value is None:
                continue
            level = classify_threshold_level(
                value,
                item_data["thresholds"]["point_critical"],
                item_data["thresholds"]["point_warning"],
            )
            payload = {
                "item": item_data["item"],
                "value": value,
                "unit": item_data["unit"],
            }
            if level == "critical":
                critical_items.append(payload)
            elif level == "warning":
                warning_items.append(payload)

        critical_items.sort(key=lambda data: data["value"])
        warning_items.sort(key=lambda data: data["value"])
        if critical_items:
            point_critical.append({"point": point, "issues": critical_items, "record": by_location.get(point)})
        elif warning_items:
            point_warning.append({"point": point, "issues": warning_items, "record": by_location.get(point)})

    return {
        "period_records": period_records,
        "by_location": by_location,
        "home_record": home_record,
        "home_has_data": home_has_data,
        "network_critical": [item for item in items if item["network_level"] == "critical"],
        "network_warning": [item for item in items if item["network_level"] == "warning"],
        "point_critical": point_critical,
        "point_warning": point_warning,
    }


def build_revision_home_stock_text(period, records):
    analysis = analyze_revision_thresholds(period, records)
    if not analysis["period_records"]:
        return f"🏠 Склад дома\n📅 {format_period_label(period)}\n\n❌ За этот месяц ревизии нет."

    lines = [
        "<b>🏠 Промежуточная ревизия запасов</b>",
        f"📅 {escape_html(format_period_label(period))}",
        "",
    ]

    home_record = analysis["home_record"]
    if not home_record or not analysis["home_has_data"]:
        lines.append("⚪ По локации «Дома» ревизия не заполнена.")
        return "\n".join(lines)

    empty = []
    filled_rows = []
    for item_name in REVISION_STOCK_THRESHOLDS:
        value = home_record.get(item_name, "")
        if value in (None, ""):
            empty.append(item_name)
        else:
            filled_rows.append(
                (
                    item_name,
                    format_number(value),
                    get_procurement_unit_short(get_revision_unit(item_name)),
                )
            )

    if filled_rows:
        lines.append(build_preformatted_block(filled_rows))

    if empty:
        lines.append("")
        lines.append("⚪ Без данных:")
        lines.extend(f"• {escape_html(item_name)}" for item_name in empty)

    return "\n".join(lines)


def build_revision_network_detail_text(period, records, level):
    analysis = analyze_revision_thresholds(period, records)
    if not analysis["period_records"]:
        return f"🛒 Что нужно закупить\n📅 {format_period_label(period)}\n\n❌ За этот месяц ревизии нет."

    items = analysis["network_critical"] if level == "critical" else analysis["network_warning"]
    title = "🚨 Срочно" if level == "critical" else "🟡 Скоро заказать"
    lines = [
        f"<b>{escape_html(title)}</b>",
        f"📅 {escape_html(format_period_label(period))}",
        "",
    ]

    if not items:
        lines.append("✅ По этой группе товаров сейчас пусто.")
        return "\n".join(lines)

    for item_data in items:
        lines.append(f"<b>{escape_html(item_data['item'])}</b>")
        rows = [
            (
                "Всего",
                "—" if item_data["network_total"] is None else format_number(round(item_data["network_total"], 2)),
                "" if item_data["network_total"] is None else get_procurement_unit_short(item_data["unit"]),
            ),
            (
                "На точках",
                "—" if item_data["point_total"] is None else format_number(round(item_data["point_total"], 2)),
                "" if item_data["point_total"] is None else get_procurement_unit_short(item_data["unit"]),
            ),
            (
                "Дома",
                "—" if item_data["home_value"] is None else format_number(item_data["home_value"]),
                "" if item_data["home_value"] is None else get_procurement_unit_short(item_data["unit"]),
            ),
        ]
        lines.append(build_preformatted_block(rows))
        lines.append("")

    if lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def build_revision_problem_points_text(period, records):
    analysis = analyze_revision_thresholds(period, records)
    if not analysis["period_records"]:
        return f"⚠️ Проблемные точки\n📅 {format_period_label(period)}\n\n❌ За этот месяц ревизии нет."

    lines = [
        "<b>⚠️ Проблемные точки</b>",
        f"📅 {escape_html(format_period_label(period))}",
        "",
    ]

    def append_point_group(title, groups):
        if not groups:
            return
        lines.append(title)
        for group in groups:
            lines.append(f"• {escape_html(group['point'])}")
            issue_rows = [
                (
                    issue["item"],
                    format_number(issue["value"]),
                    get_procurement_unit_short(issue["unit"]),
                )
                for issue in group["issues"]
            ]
            lines.append(build_preformatted_block(issue_rows))
        lines.append("")

    append_point_group("🚨 Критично", analysis["point_critical"])
    append_point_group("🟡 На контроле", analysis["point_warning"])

    if len(lines) == 3:
        lines.append("✅ По текущим порогам проблемных точек нет.")
    elif lines[-1] == "":
        lines.pop()

    return "\n".join(lines)


def build_revision_procurement_summary_text(period, records):
    analysis = analyze_revision_thresholds(period, records)
    if not analysis["period_records"]:
        return f"<b>🛒 Закупка — {escape_html(format_period_label(period))}</b>\n\n❌ За этот месяц ревизии нет."

    parts = [f"<b>🛒 Закупка — {escape_html(format_period_label(period))}</b>"]

    if analysis["network_critical"]:
        parts.append("")
        parts.append("<b>🔴 СРОЧНО</b>")
        parts.append(build_preformatted_block(build_procurement_rows(analysis["network_critical"])))

    if analysis["network_warning"]:
        parts.append("")
        parts.append("<b>🟡 Скоро заказать</b>")
        parts.append(build_preformatted_block(build_procurement_rows(analysis["network_warning"])))

    focus_items = analysis["network_critical"] + analysis["network_warning"]
    if focus_items:
        unique_items = []
        seen = set()
        for item_data in focus_items:
            if item_data["item"] in seen:
                continue
            seen.add(item_data["item"])
            unique_items.append(item_data)

        parts.append("")
        parts.append("<b>📍 На точках сейчас</b>")
        parts.append(build_preformatted_block(build_procurement_rows(unique_items, value_key="point_total")))

        if analysis["home_has_data"]:
            parts.append("")
            parts.append("<b>🏠 Дома в запасе</b>")
            parts.append(build_preformatted_block(build_procurement_rows(unique_items, value_key="home_value")))
        else:
            parts.append("")
            parts.append("⚪ Нет данных по складу «Дома»")

    if not analysis["network_critical"] and not analysis["network_warning"]:
        parts.append("")
        parts.append("✅ По текущим порогам по сети закупка не требуется.")

    return "\n".join(part for part in parts if part is not None)


def build_revision_procurement_markup(period, records, view="summary"):
    analysis = analyze_revision_thresholds(period, records)
    keyboard = []

    if view == "summary":
        keyboard.append([InlineKeyboardButton("📦 Общая ревизия", callback_data="rev_proc_to_view_summary")])
        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_revision_period")])
    else:
        keyboard.append([InlineKeyboardButton("🛒 К сводке", callback_data="rev_proc_summary")])
        keyboard.append([InlineKeyboardButton("⬅️ К месяцам", callback_data="back_revision_period")])

    keyboard.append([InlineKeyboardButton("🏠 В меню", callback_data="back_main")])
    return InlineKeyboardMarkup(keyboard)


def build_revision_compare_text(period, location, current_record, previous_record):
    previous_period = shift_period(period, -1)
    lines = [
        f"<b>📊 {escape_html(location)}</b>",
        f"📅 {escape_html(format_period_label(period))} vs {escape_html(format_period_label(previous_period))}",
        "",
    ]

    if not current_record:
        return "\n".join(lines + ["❌ За выбранный месяц ревизия не найдена."])
    if not previous_record:
        return "\n".join(lines + ["⚪ За прошлый месяц ревизии нет."])

    compare_rows = []
    for item in REVISION_ITEMS:
        current_value = current_record.get(item, "")
        previous_value = previous_record.get(item, "")
        current_num = parse_numeric_value(current_value)
        previous_num = parse_numeric_value(previous_value)

        if current_num is None and previous_num is None:
            continue

        if current_num is None or previous_num is None:
            if str(current_value).strip() == str(previous_value).strip():
                continue
            old_text = format_revision_value(item, previous_value)
            new_text = format_revision_value(item, current_value)
            compare_rows.append((item, f"{old_text} → {new_text}", ""))
            continue

        diff = current_num - previous_num
        if abs(diff) < 1e-9:
            continue
        diff_sign = "+" if diff > 0 else ""
        compare_rows.append(
            (
                item,
                f"{format_number(previous_value)} → {format_number(current_value)} ({diff_sign}{format_number(round(diff, 2))})",
                get_procurement_unit_short(get_revision_unit(item)),
            )
        )

    if compare_rows:
        lines.append(build_preformatted_block(compare_rows))
    elif len(lines) == 3:
        lines.append("Нет данных для сравнения.")

    return "\n".join(lines)


def sorted_by_date(items, date_key="Дата"):
    return [
        item for _, item in sorted(
            enumerate(items),
            key=lambda pair: (parse_date(pair[1].get(date_key, "")) or datetime.min, pair[0]),
            reverse=True,
        )
    ]


def latest_item(items, date_key="Дата"):
    ordered = sorted_by_date(items, date_key=date_key)
    return ordered[0] if ordered else None


def latest_service_date(entries):
    last = latest_item(entries)
    return last.get("Дата") if last else None


def get_latest_service_date_for_point(point):
    point = str(point or "").strip()
    if not point:
        return ""
    entries = [entry for entry in get_all_services() if str(entry.get("Точка", "")).strip() == point]
    return latest_service_date(entries) or ""


def order_points(points):
    known = [point for point in POINTS if point in points]
    extras = sorted(point for point in points if point not in POINTS)
    return known + extras


def build_service_entry_label(entry):
    who = entry.get("Кто", "?")
    water = format_number(entry.get("Вода(бут)", "?"))
    shortage = entry.get("Нехватка", "")
    purchases = entry.get("Закупки", "")
    salary_workers = get_service_salary_workers(entry)
    default_workers = default_service_salary_workers(entry.get("Кто", ""))
    flags = []
    if purchases:
        flags.append("закупки")
    if shortage:
        flags.append("нехватка")
    if salary_workers != default_workers:
        flags.append("зп")
    suffix = f" • {', '.join(flags)}" if flags else ""
    return f"#{entry['__row']} {who} • {water} бут{suffix}"


def get_supply_unit(item_name):
    return SUPPLY_UNITS.get(item_name, "шт")


def get_shortage_options(item_name):
    return SHORTAGE_ITEM_OPTIONS.get(item_name, DEFAULT_SHORTAGE_OPTIONS)


def build_purchase_summary(items):
    filled_items = [
        item for item in items
        if item.get("qty") not in (None, "", "?") and item.get("sum") is not None
    ]
    total = sum(float(item.get("sum", 0) or 0) for item in filled_items)
    parts = [
        f"{item['name']}({format_number(item['qty'])}) {format_money(item.get('sum', 0))}"
        for item in filled_items
    ]
    return ", ".join(parts), total


def parse_purchase_summary(value):
    text = str(value or "").strip()
    if not text:
        return []

    parsed = []
    for part in [chunk.strip() for chunk in text.split(", ") if chunk.strip()]:
        match = re.match(r"^(?P<name>.+?)\((?P<qty>[^()]*)\)\s+(?P<amount>[0-9]+(?:[.,][0-9]+)?)₽$", part)
        if not match:
            return []

        qty_raw = match.group("qty").strip()
        try:
            qty = normalize_number_text(qty_raw)
        except ValueError:
            return []

        amount = parse_numeric_value(match.group("amount"))
        if amount is None:
            return []

        name = match.group("name").strip()
        parsed.append(
            {
                "name": name,
                "qty": qty,
                "sum": amount,
                "is_custom": name not in PURCHASE_ITEMS,
            }
        )

    return parsed


def get_next_unfilled_purchase_index(items):
    for idx, item in enumerate(items):
        if item.get("qty") in (None, "", "?") or item.get("sum") is None:
            return idx
    return None


def ensure_shortage_item_shape(item):
    item.setdefault("reserve_qty", None)
    item.setdefault("no_reserve", False)
    item.setdefault("next_visit_status", None)
    item.setdefault("skipped", False)
    if "qty" in item and item.get("reserve_qty") is None and not item.get("no_reserve"):
        item["reserve_qty"] = item.get("qty")
    return item


def build_shortage_item_line(item):
    item = ensure_shortage_item_shape(item)
    if item.get("no_reserve"):
        status = SHORTAGE_NEXT_VISIT_LABELS.get(item.get("next_visit_status"), "статус не указан")
        return f"{item['name']} — запаса для пополнения нет, {status}"

    if item.get("skipped") or item.get("reserve_qty") in (None, "", "?"):
        return f"{item['name']} — запас: не указано"

    return f"{item['name']} — запас: {format_number(item['reserve_qty'])} {get_supply_unit(item['name'])}"


def build_shortage_summary_and_details(items):
    normalized = [ensure_shortage_item_shape(dict(item)) for item in items]
    shortage = ", ".join(item["name"] for item in normalized)
    details = "\n".join(build_shortage_item_line(item) for item in normalized)
    return shortage, details


def parse_shortage_state(shortage, shortage_details):
    lines = build_shortage_display_lines(shortage, shortage_details)
    if not lines:
        return []

    parsed = []
    for line in lines:
        raw_line = str(line or "").strip()
        if not raw_line:
            continue

        name, separator, body = raw_line.partition(" — ")
        name = name.strip()
        body = body.strip()
        if not name:
            return []

        item = {
            "name": name,
            "reserve_qty": None,
            "no_reserve": False,
            "next_visit_status": None,
            "skipped": False,
        }

        if not separator:
            parsed.append(item)
            continue

        if body == "запас: не указано":
            item["skipped"] = True
            parsed.append(item)
            continue

        if body.startswith("запас: "):
            quantity_part = body.replace("запас: ", "", 1).rsplit(" ", 1)[0].strip()
            try:
                item["reserve_qty"] = normalize_number_text(quantity_part)
            except ValueError:
                return []
            parsed.append(item)
            continue

        if body.startswith("запаса для пополнения нет"):
            item["no_reserve"] = True
            if "не хватит до следующего приезда" in body:
                item["next_visit_status"] = "not_enough"
            elif "хватит до следующего приезда" in body:
                item["next_visit_status"] = "enough"
            parsed.append(item)
            continue

        parsed.append(item)

    return parsed


def extract_shortage_detail_lines(value):
    text = str(value or "").strip()
    if not text:
        return []
    if "\n" in text:
        return [line.lstrip("• ").strip() for line in text.splitlines() if line.strip()]
    if " — " in text:
        return [text]
    if ", " in text:
        return [part.strip() for part in text.split(", ") if part.strip()]
    return [text]


def get_critical_shortage_names(value):
    critical = []
    for line in extract_shortage_detail_lines(value):
        normalized = line.lower()
        if (
            ("запаса для пополнения нет" in normalized or "запаса нет" in normalized)
            and "не хватит до следующего приезда" in normalized
        ):
            critical.append(line.split(" —", 1)[0].strip())
    return critical


def build_shortage_display_lines(shortage, shortage_details):
    lines = extract_shortage_detail_lines(shortage_details)
    if lines:
        return lines

    shortage_text = str(shortage or "").strip()
    if not shortage_text:
        return []

    return [item.strip() for item in shortage_text.split(",") if item.strip()]


def append_shortage_block(lines, shortage, shortage_details):
    detail_lines = build_shortage_display_lines(shortage, shortage_details)
    if not detail_lines:
        lines.append("✅ Всё в наличии")
        return

    lines.append("⚠️ Нехватка:")
    lines.extend(f"• {line}" for line in detail_lines)


def build_service_entry_text(entry):
    lines = [
        f"📍 {entry.get('Точка', '?')}",
        f"📅 {entry.get('Дата', '?')}",
        f"👤 {entry.get('Кто', '?')}",
        f"💧 Вода: {format_number(entry.get('Вода(бут)', '?'))} бут",
    ]
    salary_workers = get_service_salary_workers(entry)
    default_workers = default_service_salary_workers(entry.get("Кто", ""))
    if salary_workers != default_workers:
        lines.append(f"💸 В ЗП: {', '.join(salary_workers) if salary_workers else 'не считать'}")

    purchases = entry.get("Закупки", "")
    shortage = entry.get("Нехватка", "")
    shortage_qty = entry.get("Остатки", "")

    if purchases:
        lines.append(f"🛒 Закупки: {purchases} ({format_money(entry.get('Сумма закупок', 0))})")
    else:
        lines.append("🛒 Закупок нет")

    append_shortage_block(lines, shortage, shortage_qty)
    return "\n".join(lines)


def build_point_summary_line(point, record):
    if not record:
        return f"⚪ {point} — нет данных"

    date = record.get("Дата", "?")
    who = record.get("Кто", "?")
    water = format_number(record.get("Вода(бут)", "?"))
    shortage = record.get("Нехватка", "")
    critical_names = get_critical_shortage_names(record.get("Остатки", ""))

    if critical_names:
        return f"🚨 {point} — срочно: {', '.join(critical_names)}"
    if shortage:
        return f"⚠️ {point} — {shortage}"

    dt = parse_date(date)
    if not dt:
        status = "⚪"
    else:
        diff = (now_local().date() - dt.date()).days
        status = "🟢" if diff <= 2 else ("🟡" if diff <= 4 else "🔴")

    return f"{status} {point} — {who}, {water} бут"


def build_point_card_text(point, record, include_history=False, history_records=None):
    if not record:
        return f"📍 {point}\n\n❌ Нет данных"

    lines = [
        f"📍 {point}",
        f"📅 {record.get('Дата', '?')}",
        f"👤 {record.get('Кто', '?')}",
        f"💧 Вода: {format_number(record.get('Вода(бут)', '?'))} бут",
    ]

    shortage = record.get("Нехватка", "")
    shortage_qty = record.get("Остатки", "")
    purchases = record.get("Закупки", "")

    append_shortage_block(lines, shortage, shortage_qty)

    if purchases:
        lines.append(f"🛒 Закупки: {purchases} ({format_money(record.get('Сумма закупок', 0))})")

    if include_history and history_records:
        lines.append("")
        lines.append("📜 История (посл. 5):")
        for item in history_records[:5]:
            item_shortage = item.get("Нехватка", "")
            item_status = f"нехв: {item_shortage}" if item_shortage else "ок"
            lines.append(
                f"{item.get('Дата', '')} ({item.get('Кто', '')}) "
                f"{format_number(item.get('Вода(бут)', ''))} бут • {item_status}"
            )

    return "\n".join(lines)


def get_point_short_label(point):
    return POINT_SHORT_LABELS.get(point, point)


def build_text_pre_block(lines):
    if not lines:
        return ""
    rendered = [escape_html(line.rstrip()) for line in lines]
    return "<pre>\n" + "\n".join(rendered) + "\n</pre>"


def build_point_summary_groups(points_data):
    grouped = {
        "problems": [],
        "low_water": [],
        "ok": [],
    }

    for point, record in points_data:
        point_label = get_point_short_label(point)
        if not record:
            grouped["problems"].append({
                "point": point_label,
                "detail": "нет данных",
            })
            continue

        shortage = str(record.get("Нехватка", "") or "").strip()
        critical_names = get_critical_shortage_names(record.get("Остатки", ""))
        issue_text = shortage or ", ".join(critical_names)
        water_num = parse_numeric_value(record.get("Вода(бут)", ""))
        who = str(record.get("Кто", "—") or "—")
        if issue_text:
            grouped["problems"].append({
                "point": point_label,
                "water": format_number(water_num) if water_num is not None else "—",
                "who": who,
                "detail": f"нехватка: {issue_text}",
            })
            continue

        if water_num is None:
            grouped["problems"].append({
                "point": point_label,
                "water": "—",
                "who": who,
                "detail": "вода: -",
            })
            continue

        item = {
            "point": point_label,
            "water": format_number(water_num),
            "who": who,
        }

        if water_num <= 1:
            grouped["low_water"].append(item)
        else:
            grouped["ok"].append(item)

    return grouped


def build_info_all_summary(points_data):
    groups = build_point_summary_groups(points_data)
    lines = ["<b>📋 Сводка по точкам</b>", ""]

    def add_problem_section(title, items):
        if not items:
            return
        lines.append(f"<b>{title}</b>")
        lines.append("")
        point_width = max(len(item["point"]) for item in items)
        water_width = max(len(str(item.get("water", "—"))) for item in items)
        block_lines = [
            (
                f"⚠️ {item['point'].ljust(point_width)} — "
                f"{str(item.get('water', '—')).rjust(water_width)} бут"
                f"{'' if not item.get('detail') else ' · ' + item['detail']}"
            )
            for item in items
        ]
        lines.append(build_text_pre_block(block_lines))
        lines.append("")

    def add_water_section(title, icon, items):
        if not items:
            return
        lines.append(f"<b>{title}</b>")
        lines.append("")
        point_width = max(len(item["point"]) for item in items)
        water_width = max(len(item["water"]) for item in items)
        block_lines = [
            f"{icon} {item['point'].ljust(point_width)} — {item['water'].rjust(water_width)} бут · {item['who']}"
            for item in items
        ]
        lines.append(build_text_pre_block(block_lines))
        lines.append("")

    add_problem_section(f"⚠️ ПРОБЛЕМЫ ({len(groups['problems'])})", groups["problems"])
    add_water_section(f"🟡 Мало воды ({len(groups['low_water'])})", "🟡", groups["low_water"])
    add_water_section(f"🟢 Норма ({len(groups['ok'])})", "🟢", groups["ok"])

    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def format_days_ago(days):
    days = abs(int(days))
    if days % 10 == 1 and days % 100 != 11:
        word = "день"
    elif days % 10 in (2, 3, 4) and days % 100 not in (12, 13, 14):
        word = "дня"
    else:
        word = "дней"
    return f"{days} {word} назад"


def format_service_days_short(days):
    return f"{abs(int(days))} дн"


def merge_service_items(primary, secondary):
    merged = []
    seen = set()
    for item in primary + secondary:
        point = item.get("point")
        if not point or point in seen:
            continue
        seen.add(point)
        merged.append(item)
    return merged


def get_today_service_status(item, mode):
    record = item.get("record") or {}
    date_str = record.get("Дата", "")
    dt = parse_date(date_str)
    if not dt:
        return item.get("reason", "нет данных")

    days_diff = (now_local().date() - dt.date()).days
    if mode == "need":
        return format_service_days_short(days_diff) if days_diff > 0 else date_str
    if mode in {"monitor", "skip"} and days_diff == 1:
        return "вчера ✓"
    if mode == "done" and days_diff == 0:
        return "сегодня ✓"
    if days_diff > 1:
        return format_service_days_short(days_diff)
    return date_str


def get_today_service_shortage_text(item):
    record = item.get("record") or {}
    lines = build_shortage_display_lines(record.get("Нехватка", ""), record.get("Остатки", ""))
    if not lines:
        return ""
    return ", ".join(format_today_service_shortage_line(line) for line in lines)


def format_today_service_shortage_line(line):
    text = str(line or "").strip()
    if " — " not in text:
        return text

    item_name, detail = text.split(" — ", 1)
    detail = detail.strip()
    normalized = detail.lower()

    if "запас: не указано" in normalized:
        return f"{item_name}: -"

    if detail.startswith("запас: "):
        return f"{item_name}: {detail.replace('запас: ', '', 1)}"

    if detail.startswith("запаса для пополнения нет, "):
        status = detail.replace("запаса для пополнения нет, ", "", 1)
        if "не хватит до следующего приезда" in status.lower():
            return f"{item_name}: нет запаса, не хватит"
        if "хватит до следующего приезда" in status.lower():
            return f"{item_name}: нет запаса, хватит"
        return f"{item_name}: нет запаса"

    return f"{item_name}: {detail}"


def append_today_service_section(lines, title, items, mode):
    if not items:
        return

    lines.append("──────────────")
    lines.append(title)
    lines.append("──────────────")
    lines.append("")

    for item in items:
        point = item.get("point", "—")
        status = get_today_service_status(item, mode)
        shortage_text = get_today_service_shortage_text(item)
        point_prefix = "⚠️ " if mode == "need" and shortage_text else ""
        lines.append(f"{point_prefix}{point} · {status}")
        if shortage_text:
            lines.append(f"  ⛔ нехватка: {shortage_text}")
        lines.append("")


def build_today_service_pre_rows(items, mode):
    rows = []
    for item in items:
        point = item.get("point", "—")
        status = get_today_service_status(item, mode)
        shortage_text = get_today_service_shortage_text(item)
        marker = "⚠" if mode == "need" and shortage_text else " "
        rows.append((f"{marker} {point}".strip(), status, f"⛔ {shortage_text}" if shortage_text else ""))
    return rows


def build_today_service_pre_block(items, mode):
    rows = build_today_service_pre_rows(items, mode)
    if not rows:
        return ""

    point_width = max(len(point) for point, _, _ in rows)
    status_width = max(len(status) for _, status, _ in rows)
    rendered = []
    for point, status, shortage in rows:
        line = f"{point.ljust(point_width)}  {status.ljust(status_width)}"
        if shortage:
            line += f"  {shortage}"
        rendered.append(escape_html(line.rstrip()))
    return "<pre>\n" + "\n".join(rendered) + "\n</pre>"


def build_today_repair_pre_block(items):
    if not items:
        return ""

    rows = []
    for item in items:
        repair_item = item.get("repair_item") or {}
        machine_name = repair_item.get("machine_name") or "Аппарат"
        days_text = build_repair_duration_text(repair_item.get("days_open", 0))
        rows.append((item.get("point", "—"), days_text, machine_name))

    point_width = max(len(point) for point, _, _ in rows)
    status_width = max(len(status) for _, status, _ in rows)
    rendered = []
    for point, status, machine_name in rows:
        rendered.append(escape_html(f"{point.ljust(point_width)}  {status.ljust(status_width)}  {machine_name}".rstrip()))
    return "<pre>\n" + "\n".join(rendered) + "\n</pre>"


def build_today_service_notice(records, repair_points=None, groups=None):
    groups = groups or analyze_today_service_groups(records, repair_points)
    need_items = merge_service_items(groups["urgent"], groups["need_today"])

    parts = [
        f"<b>🔔 Обслуживание — {escape_html(today())}</b>",
        "",
    ]

    def add_section(title, items, mode):
        if not items:
            return
        parts.append(f"<b>{escape_html(title)}</b>")
        parts.append(build_today_service_pre_block(items, mode))
        parts.append("")

    add_section(f"🔴 Нужно сегодня ({len(need_items)})", need_items, "need")
    add_section(f"🟡 На контроле ({len(groups['monitor'])})", groups["monitor"], "monitor")
    add_section(f"✅ Можно пропустить ({len(groups['skip_today'])})", groups["skip_today"], "skip")

    if groups["done_today"]:
        add_section(f"☑️ Уже обслужены сегодня ({len(groups['done_today'])})", groups["done_today"], "done")

    if groups["planned"]:
        add_section(f"🗓 Отмечены заранее ({len(groups['planned'])})", groups["planned"], "planned")

    if groups.get("repair"):
        parts.append(f"<b>🛠 На ремонте ({len(groups['repair'])})</b>")
        parts.append(build_today_repair_pre_block(groups["repair"]))
        parts.append("")

    while parts and parts[-1] == "":
        parts.pop()

    return "\n".join(parts)


def analyze_today_service_groups(records, repair_points=None):
    repair_points = repair_points or {}
    latest_by_point = {}
    for point in POINTS:
        point_records = [r for r in records if r.get("Точка") == point]
        latest_by_point[point] = latest_item(point_records)

    groups = {
        "repair": [],
        "urgent": [],
        "need_today": [],
        "monitor": [],
        "skip_today": [],
        "done_today": [],
        "planned": [],
    }
    today_date = now_local().date()

    for point in POINTS:
        if point in repair_points:
            groups["repair"].append(
                {
                    "point": point,
                    "repair_item": repair_points[point],
                    "reason": "точка на ремонте",
                }
            )
            continue

        record = latest_by_point.get(point)
        if not record:
            groups["need_today"].append({"point": point, "record": None, "reason": "нет данных"})
            continue

        date_str = record.get("Дата", "?")
        who = record.get("Кто", "?")
        shortage = str(record.get("Нехватка", "") or "").strip()
        critical_names = get_critical_shortage_names(record.get("Остатки", ""))
        dt = parse_date(date_str)

        if not dt:
            groups["need_today"].append({
                "point": point,
                "record": record,
                "reason": "не удалось распознать дату последней записи",
            })
            continue

        days_diff = (today_date - dt.date()).days
        shortage_suffix = f", нехватка: {shortage}" if shortage else ""

        if critical_names:
            groups["urgent"].append({
                "point": point,
                "record": record,
                "reason": f"{', '.join(critical_names)} — запаса для пополнения нет и не хватит до следующего приезда",
            })

        if days_diff < 0:
            groups["planned"].append({
                "point": point,
                "record": record,
                "reason": f"уже отмечено на {date_str} ({who}){shortage_suffix}",
            })
        elif days_diff == 0:
            groups["done_today"].append({
                "point": point,
                "record": record,
                "reason": f"{who}{shortage_suffix}",
            })
        elif days_diff == 1:
            if shortage:
                groups["monitor"].append({
                    "point": point,
                    "record": record,
                    "reason": f"обслужен вчера ({date_str}), нехватка: {shortage}",
                })
            else:
                groups["skip_today"].append({
                    "point": point,
                    "record": record,
                    "reason": f"обслужен вчера ({date_str})",
                })
        else:
            groups["need_today"].append({
                "point": point,
                "record": record,
                "reason": f"последняя запись {date_str} ({format_days_ago(days_diff)}){shortage_suffix}",
            })

    return groups


def build_service_today_snapshot():
    records = get_all_services()
    repair_points = get_active_repair_point_map()
    groups = analyze_today_service_groups(records, repair_points)
    text = build_today_service_notice(records, repair_points, groups=groups)
    return {
        "records": records,
        "repair_points": repair_points,
        "groups": groups,
        "text": text,
    }


def get_point_latest_record_and_photo(point, records, photos):
    point_records = [r for r in records if r.get("Точка") == point]
    point_photos = [p for p in photos if p.get("Точка") == point]
    ordered_records = sorted_by_date(point_records)
    ordered_photos = sorted_by_date(point_photos)
    record = ordered_records[0] if ordered_records else None
    photo = None
    if record and ordered_photos:
        photo = next((p for p in ordered_photos if p.get("Дата") == record.get("Дата")), ordered_photos[0])
    elif ordered_photos:
        photo = ordered_photos[0]
    return record, photo


def find_matching_photo_row(entry, photos):
    matches = [
        photo for photo in photos
        if photo.get("Дата") == entry.get("Дата")
        and photo.get("Точка") == entry.get("Точка")
        and photo.get("Кто") == entry.get("Кто")
    ]
    if not matches:
        return None
    return max(matches, key=lambda photo: photo["__row"])


def back_markup(callback_data, text="⬅️ Назад"):
    return InlineKeyboardMarkup([[InlineKeyboardButton(text, callback_data=callback_data)]])


async def safe_delete_callback_message(query):
    try:
        await query.delete_message()
    except BadRequest as e:
        if "Message to delete not found" not in str(e):
            raise


async def show_text_screen(query, context, text, reply_markup=None, parse_mode=None):
    message = getattr(query, "message", None)
    has_photo = bool(getattr(message, "photo", None))

    if has_photo:
        await safe_delete_callback_message(query)
        await context.bot.send_message(
            chat_id=message.chat_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
        return

    try:
        await query.edit_message_text(
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
    except BadRequest as e:
        err = str(e)
        if "There is no text in the message to edit" in err or "Message to edit not found" in err:
            if message:
                await context.bot.send_message(
                    chat_id=message.chat_id,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                )
                return
        if "Message is not modified" in err:
            return
        raise


async def render_text_screen(target, context, text, reply_markup=None, parse_mode=None):
    if hasattr(target, "reply_text"):
        await target.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        return
    await show_text_screen(target, context, text, reply_markup=reply_markup, parse_mode=parse_mode)


def remember_cleanup_message(context, message):
    if not message:
        return
    tracked = context.user_data.setdefault("_cleanup_message_ids", [])
    tracked.append(message.message_id)


# Process-local Task registries: must NOT live in application.bot_data, because
# PicklePersistence deep-copies bot_data and asyncio.Task objects are unpicklable.
# Past incident: every periodic persistence dump crashed the bot with
# TypeError: cannot pickle '_asyncio.Task' object → systemd restart → pending
# cleanup tasks died before firing → "saved" notifications never auto-deleted.
_SINGLE_CLEANUP_TASKS = {}
_CARD_CLEANUP_TASKS = {}


async def delete_messages_by_ids(bot, chat_id, message_ids):
    deleted = 0
    for message_id in message_ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
            deleted += 1
        except BadRequest as e:
            err = str(e)
            if "message to delete not found" in err.lower():
                continue
            logger.warning("Failed to delete tracked message %s: %s", message_id, e)
    return deleted


def schedule_single_message_cleanup(application, chat_id, message_id, delay_seconds):
    if delay_seconds <= 0 or not message_id:
        logger.info("cleanup skipped: delay=%s message_id=%s", delay_seconds, message_id)
        return

    cleanup_tasks = _SINGLE_CLEANUP_TASKS
    task_key = f"{chat_id}:{message_id}"
    previous_task = cleanup_tasks.get(task_key)
    if previous_task and not previous_task.done():
        previous_task.cancel()

    async def _cleanup():
        logger.info("cleanup awake-wait: chat=%s msg=%s delay=%s", chat_id, message_id, delay_seconds)
        try:
            await asyncio.sleep(delay_seconds)
            logger.info("cleanup deleting: chat=%s msg=%s", chat_id, message_id)
            deleted = await delete_messages_by_ids(application.bot, chat_id, [message_id])
            logger.info("cleanup deleted: chat=%s msg=%s deleted=%s", chat_id, message_id, deleted)
        except Exception:
            logger.exception("Failed to auto-clean single message %s in chat %s", message_id, chat_id)
        finally:
            current = cleanup_tasks.get(task_key)
            if current is asyncio.current_task():
                cleanup_tasks.pop(task_key, None)

    cleanup_tasks[task_key] = asyncio.create_task(_cleanup())
    logger.info("cleanup scheduled: chat=%s msg=%s delay=%s", chat_id, message_id, delay_seconds)


def schedule_card_messages_cleanup(context, chat_id, user_id):
    if CARD_MESSAGES_AUTO_CLEANUP_SECONDS <= 0:
        return

    tracked = list(context.user_data.get("_cleanup_message_ids", []))
    if not tracked:
        return

    task_key = f"{chat_id}:{user_id}"
    cleanup_tasks = _CARD_CLEANUP_TASKS
    previous_task = cleanup_tasks.get(task_key)
    if previous_task and not previous_task.done():
        previous_task.cancel()

    async def _auto_cleanup():
        try:
            await asyncio.sleep(CARD_MESSAGES_AUTO_CLEANUP_SECONDS)
            await delete_messages_by_ids(context.bot, chat_id, tracked)
            user_data = context.application.user_data.get(user_id)
            if user_data:
                remaining = [msg_id for msg_id in user_data.get("_cleanup_message_ids", []) if msg_id not in tracked]
                user_data["_cleanup_message_ids"] = remaining
        except Exception:
            logger.exception("Failed to auto-clean card messages")
        finally:
            current = cleanup_tasks.get(task_key)
            if current is asyncio.current_task():
                cleanup_tasks.pop(task_key, None)

    cleanup_tasks[task_key] = asyncio.create_task(_auto_cleanup())


async def cleanup_tracked_messages(context, bot, chat_id):
    tracked = context.user_data.pop("_cleanup_message_ids", [])
    return await delete_messages_by_ids(bot, chat_id, tracked)


SERVICE_FLOW_STEPS = (
    "Кто",
    "Дата",
    "Точка",
    "Фото",
    "Вода",
    "Закупки",
    "Нехватка",
    "Подтверждение",
)


def build_service_progress_text(current_step_index):
    return build_progress_text(SERVICE_FLOW_STEPS, current_step_index)


TRAVEL_FLOW_STEPS = ("Кто", "Дата", "Поездки")
REPAIR_NEW_FLOW_STEPS = ("Точка", "Аппарат", "Причина", "Описание", "Фото", "Дата поломки")


def build_progress_text(steps, current_step_index):
    total = len(steps)
    index = max(1, min(current_step_index, total))
    step_name = steps[index - 1]
    bar = "█" * index + "░" * (total - index)
    return f"📍 Шаг {index}/{total}: {step_name}\n{bar}"


async def show_loading_state(query, context, text):
    await show_text_screen(query, context, f"⏳ {text}")


async def show_sheets_busy_notice(target, context=None, retry_callback=None, back_callback=None):
    text = "⏳ Google Sheets перегружен, попробуй через минуту."
    if retry_callback is None:
        try:
            if hasattr(target, "answer"):
                await target.answer(text, show_alert=True)
                return
            if hasattr(target, "reply_text"):
                await target.reply_text(text)
                return
        except Exception:
            logger.exception("Failed to show Google Sheets busy notice")
        return

    keyboard = [[InlineKeyboardButton("🔄 Повторить", callback_data=retry_callback)]]
    if back_callback:
        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data=back_callback)])
    try:
        if hasattr(target, "answer"):
            await target.answer()
        await show_text_screen(
            target,
            context,
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception:
        logger.exception("Failed to show Google Sheets busy retry screen")


def build_service_points_markup(repair_points=None):
    repair_points = repair_points or {}
    kb = []
    row = []
    available_points = [point for point in POINTS if point not in repair_points]
    for i, p in enumerate(available_points):
        row.append(InlineKeyboardButton(p, callback_data=f"sp_{p}"))
        if len(row) == 2 or i == len(available_points) - 1:
            kb.append(row)
            row = []
    kb.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_service_date")])
    return InlineKeyboardMarkup(kb)


async def show_service_date_menu(query, context, notice=None):
    svc = context.user_data.get("svc", {})
    who = svc.get("who", "")
    selected_date = svc.get("date")
    date_line = f"📅 Выбрано: {selected_date}\n\n" if selected_date else ""
    period_key = svc.get("allowed_period")
    period_line = f"🗓 Месяц: {format_period_label(period_key)}\n\n" if period_key else ""
    kb = [
        [InlineKeyboardButton(f"Сегодня ({today()})", callback_data="svc_date_today")],
        [InlineKeyboardButton(f"Вчера ({yesterday()})", callback_data="svc_date_yesterday")],
        [InlineKeyboardButton(f"Позавчера ({day_before_yesterday()})", callback_data="svc_date_daybefore")],
        [InlineKeyboardButton("✏️ Другая дата", callback_data="svc_date_custom")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_service_who")],
    ]
    text = f"{build_service_progress_text(2)}\n\n🔧 {who}\n\n{date_line}{period_line}За какую дату внести обслуживание?"
    if notice:
        text = f"{notice}\n\n{text}"
    await show_text_screen(
        query,
        context,
        text,
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return SERVICE_DATE


async def show_service_points(query, context, notice=None):
    who = context.user_data.get("svc", {}).get("who", "")
    date = context.user_data.get("svc", {}).get("date", "")
    repair_points = await run_blocking(get_active_repair_record_map)
    if who and date:
        title = f"{build_service_progress_text(3)}\n\n🔧 {who}\n📅 {date}\n\nВыберите точку:"
    elif who:
        title = f"{build_service_progress_text(3)}\n\n🔧 {who} — выберите точку:"
    else:
        title = f"{build_service_progress_text(3)}\n\n🔧 Выберите точку:"
    if repair_points:
        blocked = ", ".join(repair_points.keys())
        title += f"\n\n🛠 На ремонте: {blocked}"
    if notice:
        title = f"{notice}\n\n{title}"
    await show_text_screen(query, context, title, reply_markup=build_service_points_markup(repair_points))
    return SERVICE_POINT


async def show_service_photo_prompt(query, context):
    svc = context.user_data.get("svc", {})
    point = svc.get("point", "")
    date = svc.get("date", "")
    date_line = f"📅 {date}\n" if date else ""
    kb = []
    if svc.get("edit_mode") and svc.get("photo"):
        kb.append([InlineKeyboardButton("✅ Оставить текущее фото", callback_data="keep_service_photo")])
    kb.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_service_point")])
    prompt = "Отправьте новое фото точки:" if svc.get("edit_mode") else "Отправьте фото точки:"
    await show_text_screen(
        query,
        context,
        f"{build_service_progress_text(4)}\n\n📸 {point}\n{date_line}\n{prompt}",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return SERVICE_PHOTO


async def show_service_water_menu(query, context):
    point = context.user_data.get("svc", {}).get("point", "")
    kb = [
        [InlineKeyboardButton(str(i), callback_data=f"swa_{i}") for i in range(6)],
        [InlineKeyboardButton("Другое", callback_data="swa_custom")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_service_photo")],
    ]
    await query.edit_message_text(
        f"{build_service_progress_text(5)}\n\n💧 {point}\n\nСколько бутылок воды?",
        reply_markup=InlineKeyboardMarkup(kb)
    )
    return SERVICE_WATER


async def show_purchase_question(query, context):
    point = context.user_data.get("svc", {}).get("point", "")
    kb = [
        [InlineKeyboardButton("✅ Да", callback_data="spu_yes"),
         InlineKeyboardButton("❌ Нет", callback_data="spu_no")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_service_water")],
    ]
    await query.edit_message_text(
        f"{build_service_progress_text(6)}\n\n🛒 {point}\n\nБыли закупки?",
        reply_markup=InlineKeyboardMarkup(kb)
    )
    return SERVICE_PURCHASE


async def back_to_purchase_stage(query, context):
    svc = context.user_data.get("svc", {})
    if svc.get("plist"):
        return await show_purch_select(query, context)
    return await show_purchase_question(query, context)


async def back_to_shortage_stage(query, context):
    svc = context.user_data.get("svc", {})
    if svc.get("slist"):
        svc["sidx"] = max(len(svc["slist"]) - 1, 0)
        return await ask_short_qty(query, context)
    return await ask_shortage(query, context)


def get_travel_selected_date(context):
    return context.user_data.get("travel_date") or today()


def build_travel_day_summary(who, date_str, travels):
    day_travels = [travel for travel in travels if travel.get("Дата") == date_str and travel.get("Кто") == who]
    if not day_travels:
        return f"📋 За {date_str} поездок пока нет."

    total = 0.0
    for travel in day_travels:
        numeric = parse_numeric_value(travel.get("Сумма", ""))
        if numeric is not None:
            total += numeric

    return (
        f"📋 За {date_str}\n"
        f"• Поездок: {len(day_travels)}\n"
        f"• Расходы: {format_money(total)}"
    )


def get_travel_reference_date(context):
    selected_date = get_travel_selected_date(context)
    parsed = parse_date(selected_date)
    return parsed or now_local()


def filter_travels_by_month(travels, reference_date):
    filtered = []
    for travel in travels:
        dt = parse_date(travel.get("Дата", ""))
        if not dt:
            continue
        if dt.year == reference_date.year and dt.month == reference_date.month:
            filtered.append((dt, travel))
    return filtered


def build_travel_edit_date_groups(who, travels, reference_date):
    grouped = {}
    for dt, travel in filter_travels_by_month(travels, reference_date):
        if str(travel.get("Кто", "")).strip() != str(who).strip():
            continue
        amount = parse_numeric_value(travel.get("Сумма", ""))
        if amount is None:
            continue
        date_key = format_date(dt)
        bucket = grouped.setdefault(date_key, {"date": date_key, "count": 0, "amount": 0.0})
        bucket["count"] += 1
        bucket["amount"] += amount

    return sorted(grouped.values(), key=lambda bucket: parse_date(bucket["date"]) or datetime.min, reverse=True)


def build_travel_edit_entries(who, travels, date_str):
    entries = [
        travel for travel in travels
        if str(travel.get("Кто", "")).strip() == str(who).strip()
        and str(travel.get("Дата", "")).strip() == str(date_str).strip()
    ]
    entries.sort(key=lambda item: int(item.get("__row", 0)), reverse=True)
    return entries


def format_month_caption(reference_date):
    return format_period_label(build_period_key(reference_date.year, reference_date.month))


def build_travel_action_markup():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("1 поездка", callback_data="tr_count_1"),
                InlineKeyboardButton("2 поездки", callback_data="tr_count_2"),
            ],
            [
                InlineKeyboardButton("3 поездки", callback_data="tr_count_3"),
                InlineKeyboardButton("4 поездки", callback_data="tr_count_4"),
            ],
            [InlineKeyboardButton("🔢 Другое количество", callback_data="tr_count_custom")],
            [InlineKeyboardButton("💰 Другая сумма", callback_data="tr_custom")],
            [InlineKeyboardButton("📋 За выбранную дату", callback_data="tr_summary")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_travel_date")],
            [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
        ]
    )


def build_travel_month_person_text(who, travels, reference_date):
    month_caption = format_month_caption(reference_date)
    month_travels = [
        (dt, travel)
        for dt, travel in filter_travels_by_month(travels, reference_date)
        if travel.get("Кто") == who
    ]
    if not month_travels:
        return f"💰 Проезд — {who}\n\n📆 {month_caption}\n\n⚪ За этот месяц записей пока нет."

    totals_by_day = {}
    counts_by_day = {}
    total_sum = 0.0
    total_records = 0

    for dt, travel in month_travels:
        date_key = format_date(dt)
        amount = parse_numeric_value(travel.get("Сумма", ""))
        if amount is None:
            continue
        totals_by_day[date_key] = totals_by_day.get(date_key, 0.0) + amount
        counts_by_day[date_key] = counts_by_day.get(date_key, 0) + 1
        total_sum += amount
        total_records += 1

    if not totals_by_day:
        return f"💰 Проезд — {who}\n\n📆 {month_caption}\n\n⚪ За этот месяц нет корректных сумм."

    lines = [f"💰 Проезд — {who}", "", f"📆 {month_caption}", "", "📅 По дням"]
    ordered_dates = sorted(totals_by_day.keys(), key=lambda value: parse_date(value) or now_local())
    for date_key in ordered_dates:
        lines.append(
            f"• {date_key} — {format_money_spaced(totals_by_day[date_key])} · {counts_by_day[date_key]} зап."
        )

    lines.extend(
        [
            "",
            f"Итого за месяц: {format_money_spaced(total_sum)}",
            f"Записей: {total_records}",
        ]
    )
    return "\n".join(lines)


def build_travel_month_all_text(travels, reference_date):
    month_caption = format_month_caption(reference_date)
    month_travels = filter_travels_by_month(travels, reference_date)
    if not month_travels:
        return f"💰 Проезд — все сотрудники\n\n📆 {month_caption}\n\n⚪ За этот месяц записей пока нет."

    totals_by_day = {}
    totals_by_day_people = {}
    totals_by_person = {}
    counts_by_person = {}
    total_sum = 0.0
    total_records = 0

    for dt, travel in month_travels:
        who = str(travel.get("Кто", "")).strip() or "Не указано"
        amount = parse_numeric_value(travel.get("Сумма", ""))
        if amount is None:
            continue
        date_key = format_date(dt)
        totals_by_day[date_key] = totals_by_day.get(date_key, 0.0) + amount
        totals_by_person[who] = totals_by_person.get(who, 0.0) + amount
        counts_by_person[who] = counts_by_person.get(who, 0) + 1
        totals_by_day_people.setdefault(date_key, {})
        totals_by_day_people[date_key][who] = totals_by_day_people[date_key].get(who, 0.0) + amount
        total_sum += amount
        total_records += 1

    if not totals_by_day:
        return f"💰 Проезд — все сотрудники\n\n📆 {month_caption}\n\n⚪ За этот месяц нет корректных сумм."

    worker_names = get_worker_names()
    known_people = [name for name in worker_names if name in totals_by_person]
    other_people = sorted(name for name in totals_by_person if name not in worker_names)
    ordered_people = known_people + other_people

    lines = ["💰 Проезд — все сотрудники", "", f"📆 {month_caption}", "", "📅 По дням"]
    ordered_dates = sorted(totals_by_day.keys(), key=lambda value: parse_date(value) or now_local())
    for date_key in ordered_dates:
        person_parts = [
            f"{name} {format_money_spaced(totals_by_day_people[date_key][name])}"
            for name in ordered_people
            if name in totals_by_day_people.get(date_key, {})
        ]
        line = f"• {date_key} — {format_money_spaced(totals_by_day[date_key])}"
        if person_parts:
            line += f" ({'; '.join(person_parts)})"
        lines.append(line)

    lines.extend(["", "👤 По людям"])
    for name in ordered_people:
        lines.append(
            f"• {name} — {format_money_spaced(totals_by_person[name])} · {counts_by_person[name]} зап."
        )

    lines.extend(
        [
            "",
            f"Итого за месяц: {format_money_spaced(total_sum)}",
            f"Записей: {total_records}",
        ]
    )
    return "\n".join(lines)


async def show_travel_menu(query, context):
    keyboard = [
        [InlineKeyboardButton("➕ Добавить проезд", callback_data="travel_add")],
        [InlineKeyboardButton("📋 История по сотруднику", callback_data="travel_history_person")],
        [InlineKeyboardButton("📊 Все за месяц", callback_data="travel_history_all")],
    ]
    if is_payout_editor_target(query):
        keyboard.append([InlineKeyboardButton("✏️ Исправить проезд", callback_data="travel_edit")])
    keyboard.extend([
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_service_menu")],
        [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
    ])
    await show_text_screen(
        query,
        context,
        "💰 Проезд\n\nВыберите действие:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return TRAVEL_MENU


async def show_travel_history_period_menu(query, context, mode):
    context.user_data["travel_history_mode"] = mode
    who = context.user_data.get("travel_who", "")
    if mode == "person":
        title = f"📋 История по сотруднику\n\n{who}\n\nКакой месяц показать?"
        back_callback = "back_travel_who"
    elif mode == "edit":
        title = f"✏️ Исправить проезд\n\n{who}\n\nКакой месяц открыть?"
        back_callback = "back_travel_who"
    else:
        title = "📊 Проезд — все сотрудники\n\nКакой месяц показать?"
        back_callback = "back_travel_menu"

    keyboard = [
        [InlineKeyboardButton("📅 Текущий месяц", callback_data="tr_hist_current")],
        [InlineKeyboardButton("🗓 Прошлый месяц", callback_data="tr_hist_prev")],
        [InlineKeyboardButton("⬅️ Назад", callback_data=back_callback)],
        [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
    ]
    await show_text_screen(query, context, title, reply_markup=InlineKeyboardMarkup(keyboard))
    return TRAVEL_HISTORY_PERIOD


async def show_travel_date_menu(query, context, notice=None):
    who = context.user_data.get("travel_who", "?")
    selected_date = context.user_data.get("travel_date")
    selected_line = f"📅 Выбрано: {selected_date}\n\n" if selected_date else ""
    period_key = context.user_data.get("travel_allowed_period")
    period_line = f"🗓 Месяц: {format_period_label(period_key)}\n\n" if period_key else ""
    keyboard = [
        [InlineKeyboardButton(f"Сегодня ({today()})", callback_data="tr_date_today")],
        [InlineKeyboardButton(f"Вчера ({yesterday()})", callback_data="tr_date_yesterday")],
        [InlineKeyboardButton(f"Позавчера ({day_before_yesterday()})", callback_data="tr_date_daybefore")],
        [InlineKeyboardButton("✏️ Другая дата", callback_data="tr_date_custom")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_travel_who")],
        [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
    ]
    text = (
        f"{build_progress_text(TRAVEL_FLOW_STEPS, 2)}\n\n"
        f"💰 Проезд — {who}\n\n{selected_line}{period_line}За какую дату добавить поездки?"
    )
    if notice:
        text = f"{notice}\n\n{text}"
    await show_text_screen(
        query,
        context,
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return TRAVEL_DATE


async def show_travel_action_menu(query, context):
    who = context.user_data.get("travel_who", "?")
    selected_date = get_travel_selected_date(context)
    await show_text_screen(
        query,
        context,
        f"{build_progress_text(TRAVEL_FLOW_STEPS, 3)}\n\n💰 Проезд — {who}\n\n📅 {selected_date}\n\n➕ Добавить поездки:",
        reply_markup=build_travel_action_markup(),
    )
    return TRAVEL_ACTION


async def show_travel_month_person_screen(query, context, back_callback="back_travel_action"):
    who = context.user_data.get("travel_who", "?")
    reference_date = get_travel_reference_date(context)

    try:
        travels = await run_blocking(get_all_travels)
        text = build_travel_month_person_text(who, travels, reference_date)
    except Exception:
        logger.exception("Failed to build monthly travel summary for person")
        text = f"💰 Проезд — {who}\n\n❌ Не удалось получить историю за месяц."

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⬅️ Назад", callback_data=back_callback)],
            [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
        ]
    )
    await show_text_screen(query, context, text, reply_markup=keyboard)
    return TRAVEL_ACTION if back_callback == "back_travel_action" else TRAVEL_HISTORY_PERIOD


async def show_travel_month_all_screen(query, context, back_callback="back_travel_who"):
    reference_date = get_travel_reference_date(context)

    try:
        travels = await run_blocking(get_all_travels)
        text = build_travel_month_all_text(travels, reference_date)
    except Exception:
        logger.exception("Failed to build monthly travel summary for all workers")
        text = "💰 Проезд — все сотрудники\n\n❌ Не удалось получить свод за месяц."

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⬅️ Назад", callback_data=back_callback)],
            [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
        ]
    )
    await show_text_screen(query, context, text, reply_markup=keyboard)
    if back_callback == "back_travel_action":
        return TRAVEL_ACTION
    if back_callback in {"back_travel_who", "back_travel_menu"}:
        return TRAVEL_MENU
    return TRAVEL_HISTORY_PERIOD


async def get_travel_entry_by_row(row_num):
    travels = await run_blocking(get_all_travels_with_rows)
    return next((item for item in travels if int(item.get("__row", 0)) == int(row_num)), None)


async def show_travel_edit_date_screen(query, context, back_callback="back_travel_history_period", notice=None):
    who = context.user_data.get("travel_who", "?")
    reference_date = get_travel_reference_date(context)

    try:
        travels = await run_blocking(get_all_travels_with_rows)
        groups = build_travel_edit_date_groups(who, travels, reference_date)
    except Exception:
        logger.exception("Failed to build travel edit date groups")
        groups = None

    month_caption = format_month_caption(reference_date)
    lines = [f"✏️ Проезд — {who}", "", f"📆 {month_caption}"]
    if notice:
        lines.extend(["", notice])
    lines.append("")

    keyboard = []
    if not groups:
        lines.append("⚪ За этот месяц записей для редактирования нет.")
    else:
        lines.append("Выберите дату:")
        lines.append("")
        for bucket in groups:
            lines.append(
                f"• {bucket['date']} — {format_money_spaced(bucket['amount'])} · {bucket['count']} зап."
            )
            keyboard.append(
                [InlineKeyboardButton(
                    f"{bucket['date']} · {format_money_spaced(bucket['amount'])} · {bucket['count']} зап.",
                    callback_data=f"tr_edit_date:{bucket['date']}",
                )]
            )

    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data=back_callback)])
    keyboard.append([InlineKeyboardButton("🏠 В меню", callback_data="back_main")])
    await show_text_screen(query, context, "\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard))
    return TRAVEL_HISTORY_PERIOD


async def render_travel_edit_entries_screen(target, context, notice=None):
    who = context.user_data.get("travel_who", "?")
    date_str = context.user_data.get("travel_date") or today()

    try:
        travels = await run_blocking(get_all_travels_with_rows)
        entries = build_travel_edit_entries(who, travels, date_str)
    except Exception:
        logger.exception("Failed to build travel edit entries")
        entries = None

    lines = [f"✏️ Проезд — {who}", "", f"📅 {date_str}"]
    if notice:
        lines.extend(["", notice])
    lines.append("")

    keyboard = []
    if not entries:
        lines.append("⚪ За эту дату записей не найдено.")
    else:
        for index, entry in enumerate(entries, start=1):
            lines.extend(
                [
                    f"{index}. #{entry['__row']}",
                    f"💰 {format_money_spaced(entry.get('Сумма', 0))}",
                ]
            )
            if index != len(entries):
                lines.extend(["", "────────", ""])
            keyboard.append(
                [InlineKeyboardButton(
                    f"✏️ #{entry['__row']} · {format_money_spaced(entry.get('Сумма', 0))}",
                    callback_data=f"tr_edit_entry:{entry['__row']}",
                )]
            )

    keyboard.append([InlineKeyboardButton("⬅️ К датам", callback_data="tr_edit_back_dates")])
    keyboard.append([InlineKeyboardButton("🏠 В меню", callback_data="back_main")])
    await render_text_screen(target, context, "\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard))
    return TRAVEL_HISTORY_PERIOD


async def show_travel_edit_card(query, context, row_num, notice=None):
    entry = await get_travel_entry_by_row(row_num)
    who = context.user_data.get("travel_who", "?")
    date_str = context.user_data.get("travel_date") or today()
    if not entry or str(entry.get("Кто", "")).strip() != str(who).strip():
        return await render_travel_edit_entries_screen(query, context, notice="❌ Запись проезда не найдена.")

    text_lines = [f"<b>✏️ Проезд — {escape_html(who)}</b>"]
    if notice:
        text_lines.extend([escape_html(notice), ""])
    text_lines.extend(
        [
            f"📅 {escape_html(entry.get('Дата', ''))}",
            f"💰 {format_money_spaced(entry.get('Сумма', 0))}",
            f"🧾 Строка: #{entry.get('__row', '?')}",
            "",
            "Что изменить?",
        ]
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💰 Изменить сумму", callback_data=f"tr_edit_amount:{row_num}")],
            [InlineKeyboardButton("📅 Изменить дату", callback_data=f"tr_edit_date_prompt:{row_num}")],
            [InlineKeyboardButton("🗑 Удалить", callback_data=f"tr_edit_delete:{row_num}", style="danger")],
            [InlineKeyboardButton("⬅️ К записям", callback_data="tr_edit_back_entries")],
        ]
    )
    await show_text_screen(query, context, "\n".join(text_lines), reply_markup=keyboard, parse_mode="HTML")
    return TRAVEL_HISTORY_PERIOD


async def show_delete_date_menu(query, context):
    entries = await run_blocking(get_all_services_with_rows)
    last_date = latest_service_date(entries)
    selected_date = context.user_data.get("delete", {}).get("date")
    selected_line = f"📅 Выбрано: {selected_date}\n\n" if selected_date else ""
    kb = []
    if last_date:
        kb.append([InlineKeyboardButton(f"🕘 Последняя дата ({last_date})", callback_data="del_date_latest")])
    kb.extend([
        [InlineKeyboardButton(f"Сегодня ({today()})", callback_data="del_date_today")],
        [InlineKeyboardButton(f"Вчера ({yesterday()})", callback_data="del_date_yesterday")],
        [InlineKeyboardButton(f"Позавчера ({day_before_yesterday()})", callback_data="del_date_daybefore")],
        [InlineKeyboardButton("✏️ Другая дата", callback_data="del_date_custom")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_service_fix")],
        [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
    ])
    await show_text_screen(
        query,
        context,
        f"✏️ Исправить запись\n\n{selected_line}За какую дату открыть записи?",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return DELETE_DATE


async def show_fix_action_menu(query, context):
    selected_date = context.user_data.get("delete", {}).get("date")
    all_entries = await run_blocking(get_all_services_with_rows)
    entries = [entry for entry in all_entries if entry.get("Дата") == selected_date]

    if not entries:
        await show_text_screen(
            query,
            context,
            f"✏️ Исправить запись\n\nЗа {selected_date} записей обслуживания нет.",
            reply_markup=back_markup("back_delete_date"),
        )
        return DELETE_POINT

    points = order_points({entry.get("Точка", "") for entry in entries if entry.get("Точка")})
    points_text = ", ".join(points)
    kb = [
        [InlineKeyboardButton("✏️ Изменить запись", callback_data="fix_action_edit")],
        [InlineKeyboardButton("🗑 Удалить одну запись", callback_data="fix_action_delete_one", style="danger")],
        [InlineKeyboardButton(f"🗑 Удалить все записи за день ({len(entries)})", callback_data="fix_action_delete_day", style="danger")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_delete_date")],
    ]
    await show_text_screen(
        query,
        context,
        f"✏️ Исправить запись\n\n📅 {selected_date}\n"
        f"Записей: {len(entries)}\n"
        f"Точки: {points_text}",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return DELETE_POINT


async def show_delete_point_menu(query, context):
    mode = context.user_data.get("delete", {}).get("mode")
    selected_date = context.user_data.get("delete", {}).get("date")
    all_entries = await run_blocking(get_all_services_with_rows)
    entries = [entry for entry in all_entries if entry.get("Дата") == selected_date]
    points = order_points({entry.get("Точка", "") for entry in entries if entry.get("Точка")})
    action_text = "изменения" if mode == "edit" else "удаления"

    if not points:
        await show_text_screen(
            query,
            context,
            f"✏️ Исправить запись\n\nЗа {selected_date} записей обслуживания нет.",
            reply_markup=back_markup("back_delete_date"),
        )
        return DELETE_POINT

    kb = [[InlineKeyboardButton(point, callback_data=f"del_point_{point}")] for point in points]
    kb.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_fix_actions")])
    await show_text_screen(
        query,
        context,
        f"✏️ Исправить запись\n\n📅 {selected_date}\nВыберите точку для {action_text}:",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return DELETE_POINT


async def show_delete_entry_menu(query, context):
    delete_data = context.user_data.get("delete", {})
    selected_date = delete_data.get("date")
    point = delete_data.get("point")
    mode = delete_data.get("mode")
    action_text = "изменения" if mode == "edit" else "удаления"
    all_entries = await run_blocking(get_all_services_with_rows)
    entries = [
        entry for entry in all_entries
        if entry.get("Дата") == selected_date and entry.get("Точка") == point
    ]
    entries.sort(key=lambda entry: entry["__row"], reverse=True)

    if not entries:
        await show_text_screen(
            query,
            context,
            f"✏️ Исправить запись\n\n📅 {selected_date}\n📍 {point}\n\nЗаписей не найдено.",
            reply_markup=back_markup("back_delete_point"),
        )
        return DELETE_ENTRY

    kb = [[InlineKeyboardButton(build_service_entry_label(entry), callback_data=f"del_entry_{entry['__row']}")] for entry in entries]
    kb.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_delete_point")])
    await show_text_screen(
        query,
        context,
        f"✏️ Исправить запись\n\n📅 {selected_date}\n📍 {point}\nВыберите запись для {action_text}:",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return DELETE_ENTRY


def build_service_update_data_from_entry(entry, salary_workers=None):
    selected_workers = (
        normalize_salary_workers(salary_workers)
        if salary_workers is not None
        else get_service_salary_workers(entry)
    )
    return {
        "date": entry.get("Дата", ""),
        "who": entry.get("Кто", ""),
        "point": entry.get("Точка", ""),
        "water": entry.get("Вода(бут)", ""),
        "shortage": entry.get("Нехватка", ""),
        "shortage_qty": entry.get("Остатки", ""),
        "purchases": entry.get("Закупки", ""),
        "purchase_sum": entry.get("Сумма закупок", 0),
        "service_sum": calculate_service_sum_for_workers(selected_workers),
        "salary_workers": selected_workers,
    }


def build_service_edit_context(entry, photo_entry=None):
    purchases = str(entry.get("Закупки", "")).strip()
    shortage = str(entry.get("Нехватка", "")).strip()
    shortage_qty = str(entry.get("Остатки", "")).strip()
    parsed_purchase_sum = parse_numeric_value(entry.get("Сумма закупок", ""))
    parsed_purchases = parse_purchase_summary(purchases) if purchases else []
    parsed_shortage = parse_shortage_state(shortage, shortage_qty) if shortage or shortage_qty else []

    return {
        "edit_mode": True,
        "service_row": entry["__row"],
        "photo_row": photo_entry["__row"] if photo_entry else None,
        "photo": photo_entry.get("File_ID") if photo_entry else None,
        "who": entry.get("Кто", ""),
        "date": entry.get("Дата", ""),
        "point": entry.get("Точка", ""),
        "water": entry.get("Вода(бут)", ""),
        "purchases": purchases,
        "purchase_sum": parsed_purchase_sum if parsed_purchase_sum is not None else 0,
        "plist": parsed_purchases,
        "purchase_parse_failed": bool(purchases) and not parsed_purchases,
        "shortage": shortage,
        "shortage_qty": shortage_qty,
        "slist": parsed_shortage,
        "shortage_parse_failed": bool(shortage or shortage_qty) and not parsed_shortage,
        "salary_workers": get_service_salary_workers(entry),
    }


async def show_fix_entry_salary_menu(query, context, notice=None):
    delete_data = context.user_data.setdefault("delete", {})
    entry = delete_data.get("entry")
    if not entry:
        await show_text_screen(
            query,
            context,
            "❌ Не удалось определить запись.",
            reply_markup=back_markup("back_delete_entry"),
        )
        return DELETE_CONFIRM

    selected_workers = normalize_salary_workers(
        delete_data.get("salary_workers_draft", get_service_salary_workers(entry))
    )
    delete_data["salary_workers_draft"] = list(selected_workers)

    current_label = ", ".join(selected_workers) if selected_workers else "не считать"
    text = (
        "💸 Кому зачесть обслуживание в ЗП?\n\n"
        f"{build_service_entry_text(entry)}\n\n"
        f"Сейчас в ЗП: {current_label}\n"
        f"За каждого отмеченного сотрудника начисляется {format_money(SERVICE_PRICE)}."
    )
    if notice:
        text = f"{notice}\n\n{text}"

    kb = []
    for idx, worker in enumerate(get_paid_workers()):
        prefix = "✅" if worker in selected_workers else "⚪"
        kb.append([InlineKeyboardButton(f"{prefix} {worker}", callback_data=f"fix_entry_salary_toggle_{idx}")])
    kb.append([
        InlineKeyboardButton("↺ Как в записи", callback_data="fix_entry_salary_reset"),
        InlineKeyboardButton("🚫 Не считать", callback_data="fix_entry_salary_clear"),
    ])
    kb.append([InlineKeyboardButton("✅ Сохранить", callback_data="fix_entry_salary_save", style="primary")])
    kb.append([
        InlineKeyboardButton("⬅️ Назад", callback_data="back_fix_entry_actions"),
        InlineKeyboardButton("🏠 В меню", callback_data="back_main"),
    ])
    await show_text_screen(query, context, text, reply_markup=InlineKeyboardMarkup(kb))
    return DELETE_CONFIRM


async def show_fix_entry_action_menu(query, context, notice=None):
    entry = context.user_data.get("delete", {}).get("entry")
    text = "✏️ Что сделать с записью?\n\n" + build_service_entry_text(entry)
    if notice:
        text = f"{notice}\n\n{text}"
    kb = [
        [InlineKeyboardButton("✏️ Изменить запись", callback_data="fix_entry_edit")],
        [InlineKeyboardButton("💸 Кому в ЗП", callback_data="fix_entry_salary")],
        [InlineKeyboardButton("🗑 Удалить запись", callback_data="fix_entry_delete", style="danger")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_delete_entry")],
        [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
    ]
    await show_text_screen(query, context, text, reply_markup=InlineKeyboardMarkup(kb))
    return DELETE_CONFIRM


async def show_delete_confirm_menu(query, context):
    entry = context.user_data.get("delete", {}).get("entry")
    text = "🗑 Удалить эту запись?\n\n" + build_service_entry_text(entry)
    kb = [
        [InlineKeyboardButton("🗑 Удалить", callback_data="del_confirm_yes", style="danger")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_delete_entry")],
        [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
    ]
    await show_text_screen(query, context, text, reply_markup=InlineKeyboardMarkup(kb))
    return DELETE_CONFIRM


async def show_delete_day_confirm_menu(query, context):
    selected_date = context.user_data.get("delete", {}).get("date")
    all_entries = await run_blocking(get_all_services_with_rows)
    entries = [entry for entry in all_entries if entry.get("Дата") == selected_date]
    points = order_points({entry.get("Точка", "") for entry in entries if entry.get("Точка")})
    points_text = ", ".join(points)
    text = (
        "🗑 Удалить все записи за день?\n\n"
        f"📅 {selected_date}\n"
        f"Записей обслуживания: {len(entries)}\n"
        f"Точки: {points_text}\n\n"
        "Это удалит все записи обслуживания и фото за эту дату."
    )
    kb = [
        [InlineKeyboardButton("🗑 Удалить всё за день", callback_data="del_day_confirm_yes", style="danger")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_fix_actions")],
        [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
    ]
    await show_text_screen(query, context, text, reply_markup=InlineKeyboardMarkup(kb))
    return DELETE_CONFIRM


def build_service_update_data(svc, service_sum):
    return {
        "date": svc["date"],
        "who": svc["who"],
        "point": svc["point"],
        "water": svc.get("water", ""),
        "shortage": svc.get("shortage", ""),
        "shortage_qty": svc.get("shortage_qty", ""),
        "purchases": svc.get("purchases", ""),
        "purchase_sum": svc.get("purchase_sum", 0),
        "service_sum": service_sum,
        "salary_workers": get_service_salary_workers_from_context(svc),
    }


def normalize_service_duplicate_value(value):
    numeric = parse_numeric_value(value)
    if numeric is not None:
        return format_number(numeric)
    return normalize_text_key(value)


def build_service_duplicate_signature(data):
    return (
        normalize_text_key(data.get("date", "")),
        normalize_text_key(data.get("who", "")),
        normalize_text_key(data.get("point", "")),
        normalize_service_duplicate_value(data.get("water", "")),
        normalize_text_key(data.get("shortage", "")),
        normalize_text_key(data.get("shortage_qty", "")),
        normalize_text_key(data.get("purchases", "")),
        normalize_service_duplicate_value(data.get("purchase_sum", 0)),
        tuple(normalize_salary_workers(data.get("salary_workers", []))),
    )


def build_service_duplicate_signature_from_entry(entry):
    return build_service_duplicate_signature(
        {
            "date": entry.get("Дата", ""),
            "who": entry.get("Кто", ""),
            "point": entry.get("Точка", ""),
            "water": entry.get("Вода(бут)", ""),
            "shortage": entry.get("Нехватка", ""),
            "shortage_qty": entry.get("Остатки", ""),
            "purchases": entry.get("Закупки", ""),
            "purchase_sum": entry.get("Сумма закупок", 0),
            "salary_workers": get_service_salary_workers(entry),
        }
    )


def find_service_semantic_duplicates(payload, exclude_row=None):
    signature = build_service_duplicate_signature(payload)
    matches = []
    for entry in get_all_services_with_rows():
        row_num = entry.get("__row")
        if exclude_row and row_num == exclude_row:
            continue
        if build_service_duplicate_signature_from_entry(entry) == signature:
            matches.append(entry)
    matches.sort(key=lambda entry: int(entry.get("__row", 0)), reverse=True)
    return matches


def build_service_duplicate_warning_text(svc, duplicates):
    lines = [
        "⚠️ Похоже, такая запись уже есть.",
        "",
        build_confirm_text(svc),
        "",
        "Совпадения в базе:",
    ]
    for entry in duplicates[:3]:
        lines.extend(
            [
                f"• #{entry.get('__row', '?')}",
                f"  📍 {entry.get('Точка', '?')}",
                f"  📅 {entry.get('Дата', '?')}",
                f"  👤 {entry.get('Кто', '?')}",
                f"  💧 Вода: {format_number(entry.get('Вода(бут)', '?'))} бут",
            ]
        )
        purchase_sum = parse_numeric_value(entry.get("Сумма закупок", ""))
        if purchase_sum:
            lines.append(f"  🛒 Закупки: {format_money(purchase_sum)}")
        lines.append("")
    if len(duplicates) > 3:
        lines.append(f"… и ещё {len(duplicates) - 3} совпад.")
        lines.append("")
    lines.append("Если это отдельный повторный выезд, сохрани запись как ещё одно обслуживание.")
    return "\n".join(lines)


def build_service_duplicate_photo_key(entry):
    return (
        str(entry.get("Дата", "")).strip(),
        str(entry.get("Точка", "")).strip(),
        str(entry.get("Кто", "")).strip(),
    )


def build_service_duplicate_photo_index(photos):
    photo_index = {}
    for photo in photos:
        key = build_service_duplicate_photo_key(photo)
        photo_index.setdefault(key, []).append(int(photo.get("__row", 0)))
    for rows in photo_index.values():
        rows.sort(reverse=True)
    return photo_index


def entry_matches_service_duplicate_filters(entry, filters=None):
    filters = filters or {}
    date_filter = str(filters.get("date", "")).strip()
    period_filter = str(filters.get("period", "")).strip()
    worker_filter = str(filters.get("worker", "")).strip()
    point_filter = str(filters.get("point", "")).strip()

    if date_filter and str(entry.get("Дата", "")).strip() != date_filter:
        return False
    if period_filter and get_period_key_for_date(entry.get("Дата", "")) != period_filter:
        return False
    if worker_filter and str(entry.get("Кто", "")).strip() != worker_filter:
        return False
    if point_filter and str(entry.get("Точка", "")).strip() != point_filter:
        return False
    return True


def build_service_duplicate_group_summary(entries, photo_rows):
    newest_first = sorted(entries, key=lambda item: int(item.get("__row", 0)), reverse=True)
    sample = newest_first[0]
    purchase_sum = parse_numeric_value(sample.get("Сумма закупок", "")) or 0

    return {
        "date": str(sample.get("Дата", "")).strip(),
        "point": str(sample.get("Точка", "")).strip(),
        "who": str(sample.get("Кто", "")).strip(),
        "water": format_number(sample.get("Вода(бут)", "")) or "",
        "shortage": str(sample.get("Нехватка", "")).strip(),
        "shortage_qty": str(sample.get("Остатки", "")).strip(),
        "purchases": str(sample.get("Закупки", "")).strip(),
        "purchase_sum": purchase_sum,
        "salary_workers": serialize_salary_workers(get_service_salary_workers(sample)),
        "rows": [int(item.get("__row", 0)) for item in newest_first],
        "keep_row": int(newest_first[0].get("__row", 0)),
        "candidate_rows": [int(item.get("__row", 0)) for item in newest_first[1:]],
        "matching_photo_rows": list(photo_rows),
        "entries": [
            {
                "row": int(item.get("__row", 0)),
                "date": str(item.get("Дата", "")).strip(),
                "point": str(item.get("Точка", "")).strip(),
                "who": str(item.get("Кто", "")).strip(),
                "water": format_number(item.get("Вода(бут)", "")) or "",
                "shortage": str(item.get("Нехватка", "")).strip(),
                "shortage_qty": str(item.get("Остатки", "")).strip(),
                "purchases": str(item.get("Закупки", "")).strip(),
                "purchase_sum": parse_numeric_value(item.get("Сумма закупок", "")) or 0,
                "service_sum": parse_numeric_value(item.get("Сумма обслуж", "")) or 0,
                "salary_workers": serialize_salary_workers(get_service_salary_workers(item)),
            }
            for item in newest_first
        ],
    }


def find_service_duplicate_groups(filters=None, limit=0):
    filters = filters or {}
    services = get_all_services_with_rows()
    photos = get_all_photos_with_rows()
    photo_index = build_service_duplicate_photo_index(photos)

    grouped = {}
    for entry in services:
        if not entry_matches_service_duplicate_filters(entry, filters):
            continue
        signature = build_service_duplicate_signature_from_entry(entry)
        grouped.setdefault(signature, []).append(entry)

    groups = []
    for entries in grouped.values():
        if len(entries) < 2:
            continue
        groups.append(
            build_service_duplicate_group_summary(
                entries,
                photo_index.get(build_service_duplicate_photo_key(entries[0]), []),
            )
        )

    def group_sort_key(group):
        parsed = parse_date(group.get("date", ""))
        timestamp = parsed.timestamp() if parsed else 0
        return (timestamp, group.get("point", ""), group.get("who", ""))

    groups.sort(key=group_sort_key, reverse=True)
    if limit and limit > 0:
        groups = groups[:limit]
    return groups


def build_service_duplicate_report_payload(groups, filters=None, limit=0):
    filters = filters or {}
    return {
        "filters": {
            "date": str(filters.get("date", "")).strip(),
            "period": str(filters.get("period", "")).strip(),
            "worker": str(filters.get("worker", "")).strip(),
            "point": str(filters.get("point", "")).strip(),
            "limit": int(limit or 0),
        },
        "summary": {
            "group_count": len(groups),
            "candidate_delete_count": sum(len(group.get("candidate_rows", [])) for group in groups),
        },
        "groups": groups,
    }


def build_service_duplicate_report_text(groups, filters=None, limit=0, include_delete_commands=False):
    filters = filters or {}
    applied_filters = []
    if filters.get("period"):
        applied_filters.append(f"период {filters['period']}")
    if filters.get("date"):
        applied_filters.append(f"дата {filters['date']}")
    if filters.get("worker"):
        applied_filters.append(f"сотрудник {filters['worker']}")
    if filters.get("point"):
        applied_filters.append(f"точка {filters['point']}")

    total_duplicate_rows = sum(len(group.get("candidate_rows", [])) for group in groups)
    lines = ["Поиск дублей обслуживания", ""]
    if applied_filters:
        lines.append("Фильтры: " + ", ".join(applied_filters))
        lines.append("")
    if limit and limit > 0:
        lines.append(f"Показаны первые {limit} групп.")
        lines.append("")

    lines.append(f"Найдено групп дублей: {len(groups)}")
    lines.append(f"Кандидатов на удаление: {total_duplicate_rows}")

    if not groups:
        return "\n".join(lines)

    for index, group in enumerate(groups, start=1):
        lines.extend(
            [
                "",
                f"{index}. {group['date']} · {group['point']} · {group['who']}",
                f"   Строки: {', '.join(f'#{row}' for row in group['rows'])}",
                f"   Оставить по умолчанию: #{group['keep_row']}",
                "   Кандидаты на удаление: "
                + ", ".join(f"#{row}" for row in group["candidate_rows"]),
                f"   Вода: {group['water'] or 'не указана'}",
                f"   Нехватка: {group['shortage'] or 'всё в наличии'}",
            ]
        )
        if group.get("shortage_qty"):
            lines.append(f"   Остатки: {group['shortage_qty']}")
        if group.get("purchases"):
            lines.append(f"   Закупки: {group['purchases']}")
        if group.get("purchase_sum"):
            lines.append(f"   Сумма закупок: {format_money(group['purchase_sum'])}")
        if group.get("salary_workers"):
            lines.append(f"   В ЗП: {group['salary_workers']}")
        if group.get("matching_photo_rows"):
            lines.append(
                "   Фото с той же датой/точкой/сотрудником: "
                + ", ".join(f"#{row}" for row in group["matching_photo_rows"])
            )

    if include_delete_commands:
        lines.extend(
            [
                "",
                "Удаление строк:",
                "/delete_service_rows 79 76",
                "/delete_service_rows confirm 79 76",
            ]
        )
    return "\n".join(lines)


def build_service_duplicates_command_help():
    return (
        "🔎 Поиск дублей обслуживания\n\n"
        "Команда работает в личке и ничего не удаляет.\n\n"
        "Примеры:\n"
        "/service_duplicates\n"
        "/service_duplicates 2026-04\n"
        "/service_duplicates 05.04.2026\n"
        "/service_duplicates 05.04.2026 point=Гиппо worker=Кирилл\n"
        "/service_duplicates 2026-04 limit=20"
    )


def build_delete_service_rows_command_help():
    return (
        "🗑 Удаление строк обслуживания\n\n"
        "Сначала покажи превью:\n"
        "/delete_service_rows 79 76\n\n"
        "Потом подтверди удаление:\n"
        "/delete_service_rows confirm 79 76\n\n"
        "Можно указывать и так:\n"
        "/delete_service_rows confirm #79,76,4"
    )


def parse_service_duplicates_command_args(args):
    filters = {"date": "", "period": "", "worker": "", "point": "", "limit": 10}

    for raw_token in list(args or []):
        token = str(raw_token or "").strip()
        if not token:
            continue

        if "=" in token:
            key, value = token.split("=", 1)
            key = normalize_text_key(key)
            value = value.strip()
            if not value:
                return None, f"❌ Пустое значение в аргументе `{token}`."

            if key in {"date", "дата"}:
                filters["date"] = value
            elif key in {"period", "month", "месяц", "период"}:
                filters["period"] = value
            elif key in {"worker", "who", "сотрудник"}:
                filters["worker"] = value
            elif key in {"point", "location", "точка"}:
                filters["point"] = value
            elif key in {"limit", "лимит"}:
                try:
                    filters["limit"] = max(int(value), 0)
                except ValueError:
                    return None, "❌ `limit` должен быть числом."
            else:
                return None, f"❌ Неизвестный фильтр `{key}`."
            continue

        if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", token):
            filters["date"] = token
            continue
        if re.fullmatch(r"\d{4}-\d{2}", token):
            filters["period"] = token
            continue

        return None, f"❌ Не понял аргумент `{token}`."

    if filters["date"] and not parse_date(filters["date"]):
        return None, "❌ Дата должна быть в формате дд.мм.гггг."

    if filters["period"]:
        if not re.fullmatch(r"\d{4}-\d{2}", filters["period"]):
            return None, "❌ Период должен быть в формате гггг-мм, например 2026-04."
        year_str, month_str = filters["period"].split("-", 1)
        month = int(month_str)
        if month < 1 or month > 12:
            return None, "❌ В периоде месяц должен быть от 01 до 12."
        filters["period"] = f"{int(year_str):04d}-{month:02d}"

    if filters["date"] and filters["period"]:
        date_period = get_period_key_for_date(filters["date"])
        if date_period and date_period != filters["period"]:
            return None, "❌ Дата и период противоречат друг другу."

    return filters, None


def parse_delete_service_rows_command_args(args):
    confirm = False
    row_numbers = []
    seen = set()

    for raw_token in list(args or []):
        token = str(raw_token or "").strip()
        if not token:
            continue

        normalized = normalize_text_key(token)
        if normalized in {"confirm", "ok", "yes"}:
            confirm = True
            continue

        parts = [part.strip() for part in token.split(",") if part.strip()]
        if not parts:
            continue

        for part in parts:
            clean = part.lstrip("#")
            if not clean.isdigit():
                return None, None, f"❌ Не понял номер строки `{part}`."
            row_num = int(clean)
            if row_num < 2:
                return None, None, "❌ Номер строки должен быть 2 или больше."
            if row_num in seen:
                continue
            seen.add(row_num)
            row_numbers.append(row_num)

    if not row_numbers:
        return None, None, "❌ Укажи номера строк для удаления."

    return row_numbers, confirm, None


def split_text_for_telegram(text, limit=3900):
    chunks = []
    current = []
    current_len = 0

    for block in str(text or "").split("\n\n"):
        block = block.strip()
        if not block:
            continue
        block_len = len(block) + (2 if current else 0)
        if current and current_len + block_len > limit:
            chunks.append("\n\n".join(current))
            current = [block]
            current_len = len(block)
            continue
        current.append(block)
        current_len += block_len

    if current:
        chunks.append("\n\n".join(current))
    return chunks or [str(text or "")]


def get_service_duplicate_reviews(bot_data):
    return bot_data.setdefault("service_duplicate_reviews", {})


def cleanup_expired_service_duplicate_reviews(bot_data):
    reviews = get_service_duplicate_reviews(bot_data)
    now_ts = now_local().timestamp()
    expired_ids = []
    for review_id, review in reviews.items():
        created_at_ts = review.get("created_at_ts")
        if not created_at_ts:
            continue
        if now_ts - created_at_ts > SERVICE_DUPLICATE_REVIEW_TTL_SECONDS:
            expired_ids.append(review_id)
    for review_id in expired_ids:
        reviews.pop(review_id, None)


def next_service_duplicate_review_id(bot_data):
    counter = int(bot_data.get("service_duplicate_review_counter", 0)) + 1
    bot_data["service_duplicate_review_counter"] = counter
    return str(counter)


def create_service_duplicate_review(bot_data, chat_id, user_id, groups, filters=None, limit=0):
    cleanup_expired_service_duplicate_reviews(bot_data)
    review_id = next_service_duplicate_review_id(bot_data)
    candidate_rows = []
    row_seen = set()
    for group in groups:
        for row_num in group.get("candidate_rows", []):
            row_num = int(row_num)
            if row_num in row_seen:
                continue
            row_seen.add(row_num)
            candidate_rows.append(row_num)
    candidate_rows.sort(reverse=True)
    review = {
        "id": review_id,
        "chat_id": chat_id,
        "user_id": user_id,
        "filters": dict(filters or {}),
        "limit": int(limit or 0),
        "groups": groups,
        "candidate_rows": candidate_rows,
        "selected_rows": list(candidate_rows),
        "created_at_ts": now_local().timestamp(),
    }
    get_service_duplicate_reviews(bot_data)[review_id] = review
    return review


def get_service_duplicate_review(bot_data, review_id, user_id=None, chat_id=None):
    cleanup_expired_service_duplicate_reviews(bot_data)
    review = get_service_duplicate_reviews(bot_data).get(str(review_id))
    if not review:
        return None
    if user_id is not None and review.get("user_id") != user_id:
        return None
    if chat_id is not None and review.get("chat_id") != chat_id:
        return None
    return review


def drop_service_duplicate_review(bot_data, review_id):
    get_service_duplicate_reviews(bot_data).pop(str(review_id), None)


def build_service_duplicate_review_text(review, mode="overview"):
    groups = review.get("groups", [])
    filters = review.get("filters", {})
    limit = review.get("limit", 0)
    total_candidates = len(review.get("candidate_rows", []))
    selected_rows = review.get("selected_rows", [])

    base_text = build_service_duplicate_report_text(groups, filters, limit=limit)
    if len(base_text) > 3400:
        compact_lines = ["Поиск дублей обслуживания", ""]
        if filters.get("period") or filters.get("date") or filters.get("worker") or filters.get("point"):
            filter_parts = []
            if filters.get("period"):
                filter_parts.append(f"период {filters['period']}")
            if filters.get("date"):
                filter_parts.append(f"дата {filters['date']}")
            if filters.get("worker"):
                filter_parts.append(f"сотрудник {filters['worker']}")
            if filters.get("point"):
                filter_parts.append(f"точка {filters['point']}")
            compact_lines.append("Фильтры: " + ", ".join(filter_parts))
            compact_lines.append("")
        compact_lines.append(f"Найдено групп дублей: {len(groups)}")
        compact_lines.append(f"Кандидатов на удаление: {total_candidates}")
        for index, group in enumerate(groups[:12], start=1):
            compact_lines.append(
                f"{index}. {group['date']} · {group['point']} · {group['who']} · "
                f"к удалению {', '.join(f'#{row}' for row in group['candidate_rows'])}"
            )
        if len(groups) > 12:
            compact_lines.append(f"… и ещё {len(groups) - 12} групп.")
        base_text = "\n".join(compact_lines)

    lines = [base_text]
    lines.append("")
    if mode == "overview":
        if total_candidates:
            lines.append("Дальше выбери действие кнопками ниже.")
        else:
            lines.append("Удалять здесь нечего.")
    elif mode == "select":
        lines.append(
            f"Выбрано строк: {len(selected_rows)} из {total_candidates}."
        )
        if selected_rows:
            lines.append("Сейчас выбраны: " + ", ".join(f"#{row}" for row in selected_rows[:20]))
            if len(selected_rows) > 20:
                lines.append(f"… и ещё {len(selected_rows) - 20}.")
        lines.append("Нажимай по строкам ниже, чтобы снять или вернуть выбор.")
    elif mode == "confirm_all":
        lines.append(f"Подтвердить удаление всех кандидатов: {total_candidates} строк?")
        if total_candidates:
            lines.append(", ".join(f"#{row}" for row in review.get("candidate_rows", [])[:30]))
            if total_candidates > 30:
                lines.append(f"… и ещё {total_candidates - 30}.")
        lines.append("Фото автоматически не удаляются.")
    elif mode == "confirm_selected":
        lines.append(f"Подтвердить удаление выбранных строк: {len(selected_rows)}?")
        if selected_rows:
            lines.append(", ".join(f"#{row}" for row in selected_rows[:30]))
            if len(selected_rows) > 30:
                lines.append(f"… и ещё {len(selected_rows) - 30}.")
        lines.append("Фото автоматически не удаляются.")
    return "\n".join(lines)


def build_service_duplicate_review_overview_markup(review_id, has_candidates):
    keyboard = []
    if has_candidates:
        keyboard.append([InlineKeyboardButton("🗑 Удалить все кандидаты", callback_data=f"svcdup:confirm_all:{review_id}", style="danger")])
        keyboard.append([InlineKeyboardButton("🎯 Выбрать строки", callback_data=f"svcdup:select:{review_id}")])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data=f"svcdup:cancel:{review_id}")])
    return InlineKeyboardMarkup(keyboard)


def build_service_duplicate_selection_markup(review):
    review_id = review["id"]
    selected_rows = {int(row) for row in review.get("selected_rows", [])}
    row_meta = {}
    for group in review.get("groups", []):
        for entry in group.get("entries", []):
            row_num = int(entry.get("row", 0))
            if row_num not in review.get("candidate_rows", []):
                continue
            row_meta[row_num] = entry

    keyboard = []
    for row_num in review.get("candidate_rows", []):
        entry = row_meta.get(int(row_num), {})
        prefix = "✅" if int(row_num) in selected_rows else "⬜"
        label = (
            f"{prefix} #{row_num} · {entry.get('date', '—')} · "
            f"{entry.get('point', '—')} · {entry.get('who', '—')}"
        )
        keyboard.append([InlineKeyboardButton(label[:64], callback_data=f"svcdup:toggle:{review_id}:{row_num}")])

    actions = []
    if review.get("candidate_rows"):
        actions.append(InlineKeyboardButton("✅ Все", callback_data=f"svcdup:select_all:{review_id}"))
        actions.append(InlineKeyboardButton("⬜ Снять все", callback_data=f"svcdup:clear:{review_id}"))
    if actions:
        keyboard.append(actions)

    if selected_rows:
        keyboard.append([InlineKeyboardButton("🗑 Удалить выбранное", callback_data=f"svcdup:confirm_selected:{review_id}", style="danger")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"svcdup:back:{review_id}")])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data=f"svcdup:cancel:{review_id}")])
    return InlineKeyboardMarkup(keyboard)


def build_service_duplicate_confirm_markup(review_id, confirm_action):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подтвердить", callback_data=f"svcdup:{confirm_action}:{review_id}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data=f"svcdup:back:{review_id}")],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"svcdup:cancel:{review_id}")],
    ])


def build_service_rows_map():
    return {int(entry.get("__row", 0)): entry for entry in get_all_services_with_rows()}


def build_delete_service_rows_preview(row_numbers):
    service_rows = build_service_rows_map()
    photos = get_all_photos_with_rows()
    photo_index = build_service_duplicate_photo_index(photos)

    found_entries = []
    missing_rows = []
    for row_num in row_numbers:
        entry = service_rows.get(row_num)
        if entry:
            found_entries.append(entry)
        else:
            missing_rows.append(row_num)

    lines = ["🗑 К удалению из Обслуживание", ""]
    if found_entries:
        for entry in sorted(found_entries, key=lambda item: int(item.get("__row", 0)), reverse=True):
            row_num = int(entry.get("__row", 0))
            lines.extend(
                [
                    f"• #{row_num}",
                    f"  📅 {entry.get('Дата', '—')}",
                    f"  📍 {entry.get('Точка', '—')}",
                    f"  👤 {entry.get('Кто', '—')}",
                    f"  💧 Вода: {format_number(entry.get('Вода(бут)', '')) or 'не указана'}",
                ]
            )
            purchases = str(entry.get("Закупки", "")).strip()
            purchase_sum = parse_numeric_value(entry.get("Сумма закупок", "")) or 0
            if purchases:
                lines.append(f"  🛒 Закупки: {purchases}")
            if purchase_sum:
                lines.append(f"  💸 Сумма закупок: {format_money(purchase_sum)}")
            photo_rows = photo_index.get(build_service_duplicate_photo_key(entry), [])
            if photo_rows:
                lines.append(
                    "  📸 Похожие фото не удаляются автоматически: "
                    + ", ".join(f"#{photo_row}" for photo_row in photo_rows)
                )
            lines.append("")

    if missing_rows:
        lines.append("⚪ Не найдены строки: " + ", ".join(f"#{row}" for row in missing_rows))
        lines.append("")

    if found_entries:
        confirm_command = "/delete_service_rows confirm " + " ".join(
            str(int(entry.get("__row", 0))) for entry in found_entries
        )
        lines.append("Для подтверждения отправь:")
        lines.append(confirm_command)
    else:
        lines.append("Удалять нечего.")

    return "\n".join(lines), found_entries, missing_rows


def delete_service_rows_by_numbers(row_numbers):
    service_rows = build_service_rows_map()
    existing_rows = []
    missing_rows = []
    deleted_entries = []

    for row_num in row_numbers:
        entry = service_rows.get(row_num)
        if entry:
            existing_rows.append(row_num)
            deleted_entries.append(entry)
        else:
            missing_rows.append(row_num)

    if existing_rows:
        sheet = get_or_create_worksheet("Обслуживание", SERVICE_HEADERS)
        for row_num in sorted(existing_rows, reverse=True):
            sheet.delete_rows(row_num)

    deleted_entries.sort(key=lambda item: int(item.get("__row", 0)), reverse=True)
    return {
        "deleted_rows": sorted(existing_rows, reverse=True),
        "missing_rows": missing_rows,
        "deleted_entries": deleted_entries,
    }


def build_delete_service_rows_result_text(result):
    deleted_rows = result.get("deleted_rows", [])
    missing_rows = result.get("missing_rows", [])
    deleted_entries = result.get("deleted_entries", [])

    lines = ["✅ Строки обслуживания удалены", ""]
    if deleted_rows:
        lines.append("Удалены: " + ", ".join(f"#{row}" for row in deleted_rows))
        lines.append("")
        for entry in deleted_entries[:10]:
            lines.append(
                f"• #{entry.get('__row', '?')} — {entry.get('Дата', '—')} · "
                f"{entry.get('Точка', '—')} · {entry.get('Кто', '—')}"
            )
        if len(deleted_entries) > 10:
            lines.append(f"… и ещё {len(deleted_entries) - 10} строк.")
        lines.append("")

    if missing_rows:
        lines.append("Не найдены: " + ", ".join(f"#{row}" for row in missing_rows))
        lines.append("")

    lines.append("Фото не удалялись автоматически.")
    return "\n".join(lines)


async def begin_service_edit_from_entry(query, context):
    delete_data = context.user_data.get("delete", {})
    entry = delete_data.get("entry")
    if not entry:
        await show_text_screen(
            query,
            context,
            "❌ Не удалось открыть запись для изменения.",
            reply_markup=back_markup("back_delete_entry"),
        )
        return DELETE_CONFIRM

    photos = await run_blocking(get_all_photos_with_rows)
    photo_entry = find_matching_photo_row(entry, photos)
    context.user_data["svc"] = build_service_edit_context(entry, photo_entry=photo_entry)
    if delete_data.get("return_mode") == "payout":
        context.user_data["svc"].update(
            build_payout_return_context(
                delete_data.get("return_period"),
                screen=delete_data.get("return_screen", "overview"),
            )
        )
        context.user_data["svc"]["allowed_period"] = delete_data.get("return_period")
    return await show_service_photo_prompt(query, context)


async def edit_last_service(query, context):
    await show_loading_state(query, context, "Ищу последнюю запись...")
    try:
        entries = await run_blocking(get_all_services_with_rows)
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(
                query,
                context,
                retry_callback="service_fix_latest",
                back_callback="back_service_fix",
            )
            return SERVICE_MENU_SECTION
        raise

    last_entry = latest_item(entries) if entries else None
    if not last_entry:
        await show_text_screen(
            query,
            context,
            "⚪ Записей обслуживания пока нет.",
            reply_markup=back_markup("back_service_fix"),
        )
        return SERVICE_MENU_SECTION

    context.user_data["delete"] = {"entry": last_entry}
    return await begin_service_edit_from_entry(query, context)


# ============ РЕВИЗИЯ ============
def get_revision_context(context):
    return context.user_data.setdefault("revision", {})


def build_revision_period_markup(show_all=False, action=None):
    periods = recent_completed_period_keys(8 if show_all else 2)
    keyboard = []
    row = []
    for i, period in enumerate(periods):
        row.append(
            InlineKeyboardButton(
                format_period_label(period),
                callback_data=f"rev_period_{period}",
            )
        )
        if len(row) == 2 or i == len(periods) - 1:
            keyboard.append(row)
            row = []
    if show_all:
        keyboard.append([InlineKeyboardButton("⬅️ Текущий и прошлый", callback_data="rev_period_less")])
    else:
        keyboard.append([InlineKeyboardButton("📅 Выбрать другой месяц", callback_data="rev_period_more")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_revision_menu")])
    return InlineKeyboardMarkup(keyboard)


def build_revision_location_markup(back_callback, action=None):
    keyboard = []
    row = []
    for i, location in enumerate(REVISION_LOCATIONS):
        row.append(InlineKeyboardButton(location, callback_data=f"revloc_{location}"))
        if len(row) == 2 or i == len(REVISION_LOCATIONS) - 1:
            keyboard.append(row)
            row = []
    if action == "fill":
        keyboard.append([InlineKeyboardButton("🏠 Промежуточная ревизия (текущий месяц)", callback_data="rev_home_check")])
    elif action == "view":
        keyboard.append([InlineKeyboardButton("🏠 Промежуточная ревизия (текущий месяц)", callback_data="rev_home_view")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data=back_callback)])
    return InlineKeyboardMarkup(keyboard)


def build_revision_item_markup(item_name):
    options = get_revision_options(item_name)
    keyboard = []
    row = []
    for i, value in enumerate(options):
        row.append(InlineKeyboardButton(value.replace(".", ","), callback_data=f"revi_{value}"))
        if len(row) == 3 or i == len(options) - 1:
            keyboard.append(row)
            row = []
    keyboard.append([InlineKeyboardButton("Пропустить", callback_data="revi_skip")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_revision_item")])
    return InlineKeyboardMarkup(keyboard)


def build_revision_item_prompt_text(revision):
    item_name = revision["order"][revision["idx"]]
    unit = get_revision_unit(item_name)
    current_value = revision["values"].get(item_name, "")
    current_line = ""
    if current_value not in (None, ""):
        current_line = f"\nСейчас: {format_revision_value(item_name, current_value)}"
    current_step = revision["idx"] + 1
    total_steps = len(revision["order"])
    bar = "█" * current_step + "░" * (total_steps - current_step)
    return (
        f"📍 Шаг {current_step}/{total_steps}\n"
        f"{bar}\n"
        f"📦 {item_name} — сколько осталось ({unit})?\n"
        f"💡 Нажми кнопку или просто напиши число в чат"
        f"{current_line}"
    )


def build_revision_confirm_text(revision):
    lines = [
        "📦 Итог ревизии",
        "",
        f"📅 {format_period_label(revision['period'])}",
        f"📍 {revision['location']}",
        f"👤 {revision['who']}",
        "",
    ]
    items_to_show = REVISION_ITEMS if revision["mode"] != "edit_one" else revision["order"]
    filled = sum(1 for item in items_to_show if revision["values"].get(item, "") not in ("", None))
    lines.append(f"✅ Заполнено: {filled} из {len(items_to_show)}")
    lines.append("")
    for item in items_to_show:
        lines.append(f"• {item}: {format_revision_value(item, revision['values'].get(item, ''))}")
    return "\n".join(lines)


async def show_revision_menu(query, context):
    keyboard = [
        [InlineKeyboardButton("📝 Заполнить ревизию", callback_data="rev_fill", style="primary")],
        [InlineKeyboardButton("📋 Посмотреть ревизию", callback_data="rev_view")],
        [InlineKeyboardButton("📥 Импорт из текста", callback_data="rev_import")],
        [InlineKeyboardButton("🛒 Закупка и остатки", callback_data="rev_procurement")],
        [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
    ]
    await show_text_screen(query, context, "📦 Ревизия\n\nВыберите действие:", reply_markup=InlineKeyboardMarkup(keyboard))
    return REVISION_MENU


async def show_current_home_revision_view(query, context):
    revision = get_revision_context(context)
    revision.clear()
    revision["action"] = "home_view"
    revision["period"] = current_period_key()
    revision["location"] = "Дома"

    await show_loading_state(query, context, "Загружаю промежуточную ревизию запасов...")
    record = await run_blocking(find_revision_record, revision["period"], "Дома", True)

    if record:
        text = "🏠 Промежуточная ревизия запасов\n\n" + build_revision_record_text(record)
        keyboard = [
            [InlineKeyboardButton("✏️ Изменить сейчас", callback_data="rev_home_check")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_revision_root")],
            [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
        ]
    else:
        text = (
            "🏠 Промежуточная ревизия запасов\n\n"
            f"⚪ За {format_period_label(revision['period'])} промежуточная ревизия запасов ещё не заполнена."
        )
        keyboard = [
            [InlineKeyboardButton("📝 Заполнить сейчас", callback_data="rev_home_check")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_revision_root")],
            [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
        ]

    await show_text_screen(
        query,
        context,
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML" if record else None,
    )
    return REVISION_MENU


async def start_current_home_revision_check(update: Update, context):
    query = update.callback_query
    revision = get_revision_context(context)
    revision.clear()
    revision["action"] = "home_check"
    revision["period"] = current_period_key()
    revision["location"] = "Дома"
    await show_loading_state(query, context, "Открываю заполнение промежуточной ревизии...")
    existing_record = await run_blocking(find_revision_record, revision["period"], "Дома", True)
    if existing_record:
        start_revision_wizard(context, update, existing_record=existing_record, mode="edit_all")
    else:
        start_revision_wizard(context, update, mode="create")
    return await ask_revision_item(query, context)


async def show_revision_period_menu(query, context):
    revision = get_revision_context(context)
    action = revision.get("action")
    titles = {
        "fill": "📦 Ревизия\n\nВыберите завершённый месяц:",
        "import": "📥 Импорт ревизии\n\nЗа какой завершённый месяц импортировать данные?",
        "edit": "✏️ Ревизия\n\nКакой завершённый месяц изменить?",
        "view": "📋 Ревизия\n\nКакой завершённый месяц посмотреть?",
        "procurement": "🛒 Закупка по ревизии\n\nЗа какой завершённый месяц показать закупку?",
        "compare": "📊 Ревизия\n\nКакой завершённый месяц сравнить с прошлым?",
    }
    await show_text_screen(
        query,
        context,
        titles.get(action, "📦 Выберите месяц:"),
        reply_markup=build_revision_period_markup(
            show_all=revision.get("show_all_periods", False),
            action=action,
        ),
    )
    return REVISION_PERIOD


async def show_revision_location_menu(query, context):
    revision = get_revision_context(context)
    action = revision.get("action")
    titles = {
        "fill": f"📦 {format_period_label(revision['period'])}\n\nВыберите точку:",
        "edit": f"✏️ {format_period_label(revision['period'])}\n\nВыберите точку:",
    }
    await show_text_screen(
        query,
        context,
        titles.get(action, "📦 Выберите точку:"),
        reply_markup=build_revision_location_markup("back_revision_period", action=action),
    )
    return REVISION_LOCATION


async def show_revision_existing_menu(query, context):
    revision = get_revision_context(context)
    record = revision["existing_record"]
    keyboard = [
        [InlineKeyboardButton("✏️ Открыть для изменения", callback_data="rev_existing_edit")],
        [InlineKeyboardButton("📝 Перезаписать", callback_data="rev_existing_overwrite")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_revision_location")],
    ]
    text = (
        "📦 За этот месяц ревизия уже есть.\n\n"
        + build_revision_record_text(record)
    )
    await show_text_screen(query, context, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return REVISION_EXISTING


async def show_revision_edit_action_menu(query, context):
    revision = get_revision_context(context)
    record = revision["existing_record"]
    keyboard = [
        [InlineKeyboardButton("✏️ Изменить один пункт", callback_data="rev_edit_one")],
        [InlineKeyboardButton("📝 Пройти ревизию заново", callback_data="rev_edit_all")],
    ]
    if revision.get("group_report_undo_log_row"):
        keyboard.append([InlineKeyboardButton("↩️ Отменить это пополнение", callback_data="rev_edit_undo_group_import")])
    keyboard.extend([
        [InlineKeyboardButton("🗑 Удалить ревизию", callback_data="rev_edit_delete", style="danger")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_revision_location")],
    ])
    text = "✏️ Что сделать с ревизией?\n\n" + build_revision_record_text(record)
    await show_text_screen(query, context, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return REVISION_EDIT_ACTION


async def show_revision_edit_item_select_menu(query, context):
    keyboard = []
    row = []
    for i, item_name in enumerate(REVISION_ITEMS):
        row.append(InlineKeyboardButton(item_name, callback_data=f"revedititem_{item_name}"))
        if len(row) == 2 or i == len(REVISION_ITEMS) - 1:
            keyboard.append(row)
            row = []
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_revision_edit_action")])
    await show_text_screen(query, context, "✏️ Какой пункт изменить?", reply_markup=InlineKeyboardMarkup(keyboard))
    return REVISION_EDIT_ITEM_SELECT


async def show_revision_view_mode_menu(query, context):
    revision = get_revision_context(context)
    keyboard = [
        [InlineKeyboardButton("📋 Общая по месяцу", callback_data="rev_view_summary")],
        [InlineKeyboardButton("📍 По всем точкам подробно", callback_data="rev_view_all_points")],
        [InlineKeyboardButton("📍 По точке", callback_data="rev_view_location")],
        [InlineKeyboardButton("🧾 По товару", callback_data="rev_view_item")],
        [InlineKeyboardButton("📊 Сравнить с прошлым месяцем", callback_data="rev_view_compare")],
        [InlineKeyboardButton("🛒 Что нужно закупить", callback_data="rev_view_to_procurement")],
        [InlineKeyboardButton("📤 Экспорт для Excel", callback_data="rev_view_excel_export")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_revision_root")],
    ]
    period = revision.get("period")
    if period:
        title = f"📋 {format_period_label(period)}\n\nЧто показать?"
    else:
        title = "📋 Посмотреть ревизию\n\nЧто показать?"
    await show_text_screen(query, context, title, reply_markup=InlineKeyboardMarkup(keyboard))
    return REVISION_VIEW_MODE


async def show_revision_view_location_menu(query, context):
    await show_text_screen(
        query,
        context,
        "📍 Выберите точку:",
        reply_markup=build_revision_location_markup("back_revision_period"),
    )
    return REVISION_VIEW_LOCATION


async def show_revision_view_item_menu(query, context):
    keyboard = []
    row = []
    for i, item_name in enumerate(REVISION_ITEMS):
        row.append(InlineKeyboardButton(item_name, callback_data=f"revviewitem_{item_name}"))
        if len(row) == 2 or i == len(REVISION_ITEMS) - 1:
            keyboard.append(row)
            row = []
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_revision_period")])
    await show_text_screen(query, context, "🧾 Выберите товар:", reply_markup=InlineKeyboardMarkup(keyboard))
    return REVISION_VIEW_ITEM


async def show_revision_compare_location_menu(query, context):
    await show_text_screen(
        query,
        context,
        "📊 Выберите точку для сравнения:",
        reply_markup=build_revision_location_markup("back_revision_period"),
    )
    return REVISION_COMPARE_LOCATION


async def show_revision_import_prompt(query, context):
    revision = get_revision_context(context)
    await show_text_screen(
        query,
        context,
        "📥 Импорт ревизии\n\n"
        f"Период: {format_period_label(revision['period'])}\n\n"
        "Вставьте одним сообщением текст ревизии.\n"
        "Можно блоками по точкам или строками через `/`.",
        reply_markup=back_markup("back_revision_period"),
    )
    return REVISION_IMPORT_TEXT


async def show_revision_import_confirm(query, context):
    revision = get_revision_context(context)
    keyboard = [
        [InlineKeyboardButton("✅ Сохранить импорт", callback_data="rev_import_save")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_revision_import_text")],
        [InlineKeyboardButton("❌ Отмена", callback_data="back_revision_root")],
    ]
    await show_text_screen(
        query,
        context,
        build_revision_import_preview(revision["period"], revision["import_parsed"], revision["import_warnings"]),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return REVISION_IMPORT_CONFIRM


async def show_revision_import_confirm_message(message, context):
    revision = get_revision_context(context)
    keyboard = [
        [InlineKeyboardButton("✅ Сохранить импорт", callback_data="rev_import_save")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_revision_import_text")],
        [InlineKeyboardButton("❌ Отмена", callback_data="back_revision_root")],
    ]
    await message.reply_text(
        build_revision_import_preview(revision["period"], revision["import_parsed"], revision["import_warnings"]),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return REVISION_IMPORT_CONFIRM


async def show_revision_procurement_screen(query, context, view="summary"):
    revision = get_revision_context(context)
    period = revision["period"]
    await show_loading_state(query, context, "Загружаю ревизию...")
    records = await run_blocking(get_all_revisions)

    if view == "urgent":
        text = build_revision_network_detail_text(period, records, "critical")
    elif view == "warning":
        text = build_revision_network_detail_text(period, records, "warning")
    elif view == "points":
        text = build_revision_problem_points_text(period, records)
    elif view == "home":
        text = build_revision_home_stock_text(period, records)
    else:
        text = build_revision_procurement_summary_text(period, records)
        view = "summary"

    await show_text_screen(
        query,
        context,
        text,
        reply_markup=build_revision_procurement_markup(period, records, view=view),
        parse_mode="HTML",
    )
    return REVISION_PROCUREMENT_REPORT


def start_revision_wizard(context, update, existing_record=None, mode="create", items=None):
    revision = get_revision_context(context)
    revision["mode"] = mode
    revision["who"] = get_revision_author(update)
    revision["row"] = existing_record.get("__row") if existing_record else None
    revision["values"] = build_revision_values_from_record(existing_record) if existing_record else {item: "" for item in REVISION_ITEMS}
    revision["order"] = list(items or REVISION_ITEMS)
    revision["idx"] = 0


async def ask_revision_item(query, context):
    revision = get_revision_context(context)
    if revision["idx"] >= len(revision["order"]):
        return await show_revision_confirm(query, context)
    item_name = revision["order"][revision["idx"]]
    await show_text_screen(
        query,
        context,
        build_revision_item_prompt_text(revision),
        reply_markup=build_revision_item_markup(item_name),
    )
    if query.message:
        revision["bot_msg"] = {"chat_id": query.message.chat_id, "message_id": query.message.message_id}
    return REVISION_ITEM


async def ask_revision_item_message(message, context):
    revision = get_revision_context(context)
    if revision["idx"] >= len(revision["order"]):
        return await show_revision_confirm_message(message, context)
    item_name = revision["order"][revision["idx"]]
    text = build_revision_item_prompt_text(revision)
    markup = build_revision_item_markup(item_name)

    bot_msg = revision.get("bot_msg")
    if bot_msg:
        try:
            await context.bot.edit_message_text(
                chat_id=bot_msg["chat_id"],
                message_id=bot_msg["message_id"],
                text=text,
                reply_markup=markup,
            )
            return REVISION_ITEM
        except Exception:
            logger.info("revision edit failed, falling back to reply: %s", bot_msg)

    sent = await message.reply_text(text, reply_markup=markup)
    revision["bot_msg"] = {"chat_id": sent.chat_id, "message_id": sent.message_id}
    return REVISION_ITEM


async def revision_item_text_input_handler(update: Update, context):
    # Direct numeric input from REVISION_ITEM state (no need to press "Другое" first).
    # Also implements message clean-up: delete user's text, edit the bot's existing
    # message instead of replying with a fresh one (no more "polotno of messages").
    revision = get_revision_context(context)
    if revision["idx"] >= len(revision["order"]):
        return REVISION_ITEM
    item_name = revision["order"][revision["idx"]]
    raw = (update.message.text or "").strip()
    try:
        value = normalize_number_text(raw)
    except ValueError:
        try:
            await update.message.delete()
        except Exception:
            pass
        bot_msg = revision.get("bot_msg")
        if bot_msg:
            try:
                await context.bot.edit_message_text(
                    chat_id=bot_msg["chat_id"],
                    message_id=bot_msg["message_id"],
                    text=build_revision_item_prompt_text(revision) + f"\n\n❌ «{raw}» — не число, попробуй ещё раз.",
                    reply_markup=build_revision_item_markup(item_name),
                )
                return REVISION_ITEM
            except Exception:
                pass
        await update.message.reply_text(f"❌ Введите число для «{item_name}»:")
        return REVISION_ITEM

    revision["values"][item_name] = value
    revision["idx"] += 1
    try:
        await update.message.delete()
    except Exception:
        pass
    return await ask_revision_item_message(update.message, context)


async def show_revision_confirm(query, context):
    keyboard = [
        [InlineKeyboardButton("✅ Сохранить", callback_data="rev_confirm_save", style="primary")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_revision_confirm")],
        [InlineKeyboardButton("❌ Отмена", callback_data="rev_confirm_cancel")],
    ]
    await show_text_screen(
        query,
        context,
        build_revision_confirm_text(get_revision_context(context)),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return REVISION_CONFIRM


async def show_revision_confirm_message(message, context):
    revision = get_revision_context(context)
    keyboard = [
        [InlineKeyboardButton("✅ Сохранить", callback_data="rev_confirm_save", style="primary")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_revision_confirm")],
        [InlineKeyboardButton("❌ Отмена", callback_data="rev_confirm_cancel")],
    ]
    text = build_revision_confirm_text(revision)
    markup = InlineKeyboardMarkup(keyboard)

    bot_msg = revision.get("bot_msg")
    if bot_msg:
        try:
            await context.bot.edit_message_text(
                chat_id=bot_msg["chat_id"],
                message_id=bot_msg["message_id"],
                text=text,
                reply_markup=markup,
            )
            return REVISION_CONFIRM
        except Exception:
            logger.info("revision confirm edit failed, falling back: %s", bot_msg)

    sent = await message.reply_text(text, reply_markup=markup)
    revision["bot_msg"] = {"chat_id": sent.chat_id, "message_id": sent.message_id}
    return REVISION_CONFIRM


async def show_revision_delete_confirm_menu(query, context):
    revision = get_revision_context(context)
    record = revision["existing_record"]
    keyboard = [
        [InlineKeyboardButton("🗑 Удалить ревизию", callback_data="rev_delete_yes", style="danger")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_revision_edit_action")],
    ]
    text = "🗑 Удалить ревизию?\n\n" + build_revision_record_text(record)
    await show_text_screen(query, context, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return REVISION_DELETE_CONFIRM


async def revision_menu_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "back_main":
        return await start(update, context)
    if data in {"revision", "back_revision_root"}:
        return await show_revision_menu(query, context)
    action_map = {
        "rev_fill": "fill",
        "rev_import": "import",
        "rev_edit": "edit",
        "rev_view": "view",
        "rev_procurement": "procurement",
        "rev_compare": "compare",
    }
    revision = get_revision_context(context)
    revision.clear()
    if data == "rev_home_view":
        return await show_current_home_revision_view(query, context)
    if data == "rev_home_check":
        return await start_current_home_revision_check(update, context)
    revision["action"] = action_map[data]
    if data == "rev_view":
        revision["view_mode"] = None
        return await show_revision_view_mode_menu(query, context)
    return await show_revision_period_menu(query, context)


async def revision_period_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    revision = get_revision_context(context)
    if query.data == "back_revision_menu":
        return await show_revision_menu(query, context)
    if query.data == "rev_home_check":
        return await start_current_home_revision_check(update, context)
    if query.data == "rev_home_view":
        return await show_current_home_revision_view(query, context)
    if query.data == "rev_period_more":
        revision["show_all_periods"] = True
        return await show_revision_period_menu(query, context)
    if query.data == "rev_period_less":
        revision["show_all_periods"] = False
        return await show_revision_period_menu(query, context)
    if not query.data.startswith("rev_period_"):
        return REVISION_PERIOD

    period = query.data.replace("rev_period_", "")
    revision["period"] = period
    revision["show_all_periods"] = False

    if revision.get("action") == "view":
        view_mode = revision.get("view_mode")
        if view_mode:
            return await _render_revision_view(query, context, view_mode)
        return await show_revision_view_mode_menu(query, context)
    if revision.get("action") == "procurement":
        return await show_revision_procurement_screen(query, context, view="summary")
    if revision.get("action") == "compare":
        return await show_revision_compare_location_menu(query, context)
    if revision.get("action") == "import":
        return await show_revision_import_prompt(query, context)
    return await show_revision_location_menu(query, context)


async def revision_location_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    revision = get_revision_context(context)

    if query.data == "back_revision_period":
        return await show_revision_period_menu(query, context)

    if query.data == "rev_home_check":
        return await start_current_home_revision_check(update, context)
    if query.data == "rev_home_view":
        return await show_current_home_revision_view(query, context)

    location = query.data.replace("revloc_", "")
    revision["location"] = location
    await show_loading_state(query, context, "Загружаю ревизию точки...")
    existing_record = await run_blocking(find_revision_record, revision["period"], location, True)

    if revision.get("action") == "fill":
        if existing_record:
            revision["existing_record"] = existing_record
            return await show_revision_existing_menu(query, context)
        start_revision_wizard(context, update, mode="create")
        return await ask_revision_item(query, context)

    if not existing_record:
        await show_text_screen(
            query,
            context,
            f"✏️ За {format_period_label(revision['period'])} для «{location}» ревизия не найдена.",
            reply_markup=back_markup("back_revision_period"),
        )
        return REVISION_LOCATION

    revision["existing_record"] = existing_record
    return await show_revision_edit_action_menu(query, context)


async def revision_existing_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    revision = get_revision_context(context)

    if query.data == "back_revision_location":
        return await show_revision_location_menu(query, context)
    if query.data == "rev_existing_edit":
        return await show_revision_edit_action_menu(query, context)

    if query.data != "rev_existing_overwrite":
        logger.warning("revision_existing_handler got unexpected callback %r — ignoring", query.data)
        return REVISION_EXISTING

    start_revision_wizard(context, update, existing_record=revision["existing_record"], mode="edit_all")
    return await ask_revision_item(query, context)


async def revision_edit_action_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    revision = get_revision_context(context)

    if query.data == "back_revision_location":
        return await show_revision_location_menu(query, context)
    if query.data == "rev_edit_one":
        return await show_revision_edit_item_select_menu(query, context)
    if query.data == "rev_edit_all":
        start_revision_wizard(context, update, existing_record=revision["existing_record"], mode="edit_all")
        return await ask_revision_item(query, context)
    if query.data == "rev_edit_undo_group_import":
        log_row_num = revision.get("group_report_undo_log_row")
        if not log_row_num:
            return await show_revision_edit_action_menu(query, context)

        try:
            status, deleted_record = await run_blocking(delete_group_report_entry_by_log_row, log_row_num)
        except APIError as error:
            if is_google_sheets_busy_error(error):
                await show_sheets_busy_notice(
                    query,
                    context,
                    retry_callback="rev_edit_undo_group_import",
                    back_callback="back_revision_location",
                )
                return REVISION_EDIT_ACTION
            logger.exception("Failed to undo group revision import log row %s", log_row_num)
            await show_text_screen(
                query,
                context,
                "❌ Не удалось отменить это пополнение.",
                reply_markup=back_markup("back_revision_location"),
            )
            return REVISION_EDIT_ACTION
        except Exception:
            logger.exception("Failed to undo group revision import log row %s", log_row_num)
            await show_text_screen(
                query,
                context,
                "❌ Не удалось отменить это пополнение.",
                reply_markup=back_markup("back_revision_location"),
            )
            return REVISION_EDIT_ACTION

        revision.pop("group_report_undo_log_row", None)
        if status == "deleted" and deleted_record:
            keyboard = [
                [InlineKeyboardButton("📦 К ревизии", callback_data="revision")],
                [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
            ]
            await show_text_screen(
                query,
                context,
                build_group_report_delete_result_text(deleted_record),
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return REVISION_MENU

        await show_text_screen(
            query,
            context,
            "⚪ Это пополнение уже отменено или недоступно.",
            reply_markup=back_markup("back_revision_location"),
        )
        return REVISION_EDIT_ACTION
    if query.data == "rev_edit_delete":
        return await show_revision_delete_confirm_menu(query, context)
    return REVISION_EDIT_ACTION


async def revision_edit_item_select_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    revision = get_revision_context(context)

    if query.data == "back_revision_edit_action":
        return await show_revision_edit_action_menu(query, context)

    item_name = query.data.replace("revedititem_", "")
    start_revision_wizard(
        context,
        update,
        existing_record=revision["existing_record"],
        mode="edit_one",
        items=[item_name],
    )
    return await ask_revision_item(query, context)


async def revision_item_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    revision = get_revision_context(context)

    if query.data == "back_revision_item":
        if revision["idx"] > 0:
            revision["idx"] -= 1
            return await ask_revision_item(query, context)
        if revision["mode"] == "edit_one":
            return await show_revision_edit_item_select_menu(query, context)
        if revision.get("action") == "edit":
            return await show_revision_edit_action_menu(query, context)
        if revision.get("action") == "fill" and revision.get("row"):
            return await show_revision_existing_menu(query, context)
        return await show_revision_location_menu(query, context)

    item_name = revision["order"][revision["idx"]]
    if query.data == "revi_custom":
        await show_text_screen(
            query,
            context,
            f"📦 {item_name} — введите остаток ({get_revision_unit(item_name)}):",
            reply_markup=back_markup("back_revision_item_custom"),
        )
        return REVISION_ITEM_CUSTOM

    if query.data == "revi_skip":
        revision["values"][item_name] = ""
    else:
        value = query.data.replace("revi_", "")
        revision["values"][item_name] = normalize_number_text(value)

    revision["idx"] += 1
    return await ask_revision_item(query, context)


async def revision_item_custom_back_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "back_revision_item_custom":
        return await ask_revision_item(query, context)
    return REVISION_ITEM_CUSTOM


async def revision_item_custom_handler(update: Update, context):
    revision = get_revision_context(context)
    item_name = revision["order"][revision["idx"]]
    try:
        revision["values"][item_name] = normalize_number_text(update.message.text)
    except ValueError:
        await update.message.reply_text(
            f"❌ Введите число для «{item_name}»:",
            reply_markup=back_markup("back_revision_item_custom"),
        )
        return REVISION_ITEM_CUSTOM

    revision["idx"] += 1
    try:
        await update.message.delete()
    except Exception:
        pass
    return await ask_revision_item_message(update.message, context)


async def revision_confirm_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    revision = get_revision_context(context)

    if query.data == "back_revision_confirm":
        revision["idx"] = max(len(revision["order"]) - 1, 0)
        return await ask_revision_item(query, context)

    if query.data == "rev_confirm_cancel":
        return await show_revision_menu(query, context)

    payload = {
        "period": revision["period"],
        "location": revision["location"],
        "who": revision["who"],
        "filled_at": today(),
        "values": revision["values"],
    }

    try:
        if revision.get("row"):
            await run_blocking(update_revision_row, revision["row"], payload)
            text = "✅ Ревизия обновлена."
        else:
            await run_blocking(add_revision_row, payload)
            text = "✅ Ревизия сохранена."
        logger.info(
            "revision saved: user_id=%s location=%s period=%s mode=%s",
            getattr(update.effective_user, "id", None),
            revision["location"],
            revision["period"],
            "update" if revision.get("row") else "create",
        )
    except APIError as e:
        if is_google_sheets_busy_error(e):
            await show_sheets_busy_notice(
                query,
                context,
                retry_callback="rev_confirm_save",
                back_callback="back_revision_confirm",
            )
            return REVISION_CONFIRM
        await show_text_screen(query, context, f"❌ Ошибка сохранения ревизии: {e}")
        return REVISION_CONFIRM
    except Exception as e:
        await show_text_screen(query, context, f"❌ Ошибка сохранения ревизии: {e}")
        return REVISION_CONFIRM

    keyboard = [
        [InlineKeyboardButton("📦 К ревизии", callback_data="revision")],
        [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
    ]
    await show_text_screen(
        query,
        context,
        f"{text}\n\n📅 {format_period_label(revision['period'])}\n📍 {revision['location']}",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return REVISION_MENU


REV_VIEW_KEY_MAP = {
    "rev_view_summary": "summary",
    "rev_view_all_points": "all_points",
    "rev_view_location": "location",
    "rev_view_item": "item",
    "rev_view_compare": "compare",
    "rev_view_to_procurement": "to_procurement",
    "rev_view_excel_export": "excel_export",
}


async def _render_revision_view(query, context, view_mode):
    revision = get_revision_context(context)
    if view_mode == "summary":
        await show_loading_state(query, context, "Загружаю сводную ревизию...")
        records = await run_blocking(get_all_revisions)
        text = build_revision_summary_text(revision["period"], records)
        await show_text_screen(query, context, text, reply_markup=back_markup("back_revision_period", "⬅️ Назад"), parse_mode="HTML")
        return REVISION_VIEW_MODE
    if view_mode == "all_points":
        await show_loading_state(query, context, "Собираю ревизии по всем точкам...")
        records = await run_blocking(get_all_revisions)
        text = build_revision_all_points_detailed_text(revision["period"], records)
        await show_text_screen(query, context, text, reply_markup=back_markup("back_revision_period", "⬅️ Назад"), parse_mode="HTML")
        return REVISION_VIEW_MODE
    if view_mode == "excel_export":
        await show_loading_state(query, context, "Готовлю экспорт для Excel...")
        records = await run_blocking(get_all_revisions)
        text = build_revision_excel_export_text(revision["period"], records)
        await show_text_screen(query, context, text, reply_markup=back_markup("back_revision_period", "⬅️ Назад"), parse_mode="HTML")
        return REVISION_VIEW_MODE
    if view_mode == "compare":
        revision["action"] = "compare"
        return await show_revision_compare_location_menu(query, context)
    if view_mode == "to_procurement":
        revision["action"] = "procurement"
        return await show_revision_procurement_screen(query, context, view="summary")
    if view_mode == "location":
        return await show_revision_view_location_menu(query, context)
    if view_mode == "item":
        return await show_revision_view_item_menu(query, context)
    return REVISION_VIEW_MODE


async def revision_view_mode_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    revision = get_revision_context(context)

    if query.data == "back_revision_period":
        return await show_revision_period_menu(query, context)
    if query.data == "back_revision_view_mode":
        return await show_revision_view_mode_menu(query, context)
    if query.data == "back_revision_root":
        return await show_revision_menu(query, context)

    view_mode = REV_VIEW_KEY_MAP.get(query.data)
    if view_mode:
        revision["view_mode"] = view_mode
        # If no period chosen yet, ask for period; otherwise render now.
        if not revision.get("period"):
            return await show_revision_period_menu(query, context)
        return await _render_revision_view(query, context, view_mode)
    return REVISION_VIEW_MODE


async def revision_view_location_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    revision = get_revision_context(context)

    if query.data == "back_revision_view_mode":
        return await show_revision_view_mode_menu(query, context)
    if query.data == "back_revision_view_locations":
        return await show_revision_view_location_menu(query, context)
    if query.data == "rev_view_to_procurement":
        revision["action"] = "procurement"
        return await show_revision_procurement_screen(query, context, view="summary")

    location = query.data.replace("revloc_", "")
    await show_loading_state(query, context, "Загружаю ревизию точки...")
    record = await run_blocking(find_revision_record, revision["period"], location)
    if not record:
        text = f"⚪ За {format_period_label(revision['period'])} для «{location}» ревизии нет."
        parse_mode = None
    else:
        text = build_revision_record_text(record)
        parse_mode = "HTML"
    await show_text_screen(
        query,
        context,
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🛒 Что нужно закупить", callback_data="rev_view_to_procurement")],
            [InlineKeyboardButton("⬅️ К точкам", callback_data="back_revision_view_locations")],
        ]),
        parse_mode=parse_mode,
    )
    return REVISION_VIEW_LOCATION


async def revision_view_item_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    revision = get_revision_context(context)

    if query.data == "back_revision_view_mode":
        return await show_revision_view_mode_menu(query, context)
    if query.data == "back_revision_view_items":
        return await show_revision_view_item_menu(query, context)
    if query.data == "rev_view_to_procurement":
        revision["action"] = "procurement"
        return await show_revision_procurement_screen(query, context, view="summary")

    item_name = query.data.replace("revviewitem_", "")
    await show_loading_state(query, context, "Загружаю ревизию по товару...")
    records = await run_blocking(get_all_revisions)
    text = build_revision_item_text(revision["period"], item_name, records)
    await show_text_screen(
        query,
        context,
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🛒 Что нужно закупить", callback_data="rev_view_to_procurement")],
            [InlineKeyboardButton("⬅️ К товарам", callback_data="back_revision_view_items")],
        ]),
        parse_mode="HTML",
    )
    return REVISION_VIEW_ITEM


async def revision_compare_location_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    revision = get_revision_context(context)

    if query.data == "back_revision_period":
        return await show_revision_period_menu(query, context)
    if query.data == "back_revision_compare_locations":
        return await show_revision_compare_location_menu(query, context)

    location = query.data.replace("revloc_", "")
    await show_loading_state(query, context, "Сравниваю ревизии...")
    current_record = await run_blocking(find_revision_record, revision["period"], location)
    previous_period = shift_period(revision["period"], -1)
    previous_record = await run_blocking(find_revision_record, previous_period, location)
    text = build_revision_compare_text(revision["period"], location, current_record, previous_record)
    await show_text_screen(
        query,
        context,
        text,
        reply_markup=back_markup("back_revision_compare_locations", "⬅️ К точкам"),
        parse_mode="HTML" if current_record and previous_record else None,
    )
    return REVISION_COMPARE_LOCATION


async def revision_delete_confirm_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    revision = get_revision_context(context)

    if query.data == "back_revision_edit_action":
        return await show_revision_edit_action_menu(query, context)

    if query.data != "rev_delete_yes":
        logger.warning("revision_delete_confirm_handler got unexpected callback %r — ignoring", query.data)
        return REVISION_DELETE_CONFIRM

    try:
        await run_blocking(delete_revision_row, revision["existing_record"]["__row"])
        text = (
            "✅ Ревизия удалена.\n\n"
            f"📅 {format_period_label(revision['period'])}\n"
            f"📍 {revision['location']}"
        )
    except Exception as e:
        text = f"❌ Не удалось удалить ревизию: {e}"

    keyboard = [
        [InlineKeyboardButton("📦 К ревизии", callback_data="revision")],
        [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
    ]
    await show_text_screen(query, context, text, reply_markup=InlineKeyboardMarkup(keyboard))
    return REVISION_MENU


async def revision_import_text_back_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "back_revision_period":
        return await show_revision_period_menu(query, context)
    return REVISION_IMPORT_TEXT


async def revision_import_text_handler(update: Update, context):
    revision = get_revision_context(context)
    parsed, warnings = parse_revision_import_text(update.message.text)

    if not parsed:
        await update.message.reply_text(
            "❌ Не удалось распознать ревизию.\n"
            "Вставьте текст блоками по точкам или таблицей через `/`.",
            reply_markup=back_markup("back_revision_period"),
        )
        return REVISION_IMPORT_TEXT

    revision["import_parsed"] = parsed
    revision["import_warnings"] = warnings
    return await show_revision_import_confirm_message(update.message, context)


async def revision_import_confirm_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    revision = get_revision_context(context)

    if query.data == "back_revision_import_text":
        return await show_revision_import_prompt(query, context)
    if query.data == "back_revision_root":
        return await show_revision_menu(query, context)

    parsed = revision.get("import_parsed", {})
    if not parsed:
        return await show_revision_import_prompt(query, context)

    created = 0
    updated = 0
    try:
        for location, imported_values in parsed.items():
            existing = await run_blocking(find_revision_record, revision["period"], location, True)
            values = build_revision_values_from_record(existing) if existing else {item: "" for item in REVISION_ITEMS}
            for item_name, value in imported_values.items():
                values[item_name] = value

            payload = {
                "period": revision["period"],
                "location": location,
                "who": get_revision_author(update),
                "filled_at": today(),
                "values": values,
            }
            if existing:
                await run_blocking(update_revision_row, existing["__row"], payload)
                updated += 1
            else:
                await run_blocking(add_revision_row, payload)
                created += 1

        text = (
            "✅ Импорт ревизии завершён.\n\n"
            f"📅 {format_period_label(revision['period'])}\n"
            f"Создано: {created}\n"
            f"Обновлено: {updated}"
        )
        logger.info(
            "revision import saved: user_id=%s period=%s created=%s updated=%s locations=%s",
            getattr(update.effective_user, "id", None),
            revision["period"],
            created,
            updated,
            len(parsed),
        )
    except APIError as e:
        if is_google_sheets_busy_error(e):
            await show_sheets_busy_notice(
                query,
                context,
                retry_callback="rev_import_save",
                back_callback="back_revision_import_text",
            )
            return REVISION_IMPORT_CONFIRM
        text = f"❌ Ошибка импорта: {e}"
    except Exception as e:
        text = f"❌ Ошибка импорта: {e}"

    keyboard = [
        [InlineKeyboardButton("📦 К ревизии", callback_data="revision")],
        [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
    ]
    await show_text_screen(query, context, text, reply_markup=InlineKeyboardMarkup(keyboard))
    return REVISION_MENU


async def revision_procurement_report_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "back_revision_period":
        return await show_revision_period_menu(query, context)
    if query.data == "back_main":
        return await start(update, context)
    if query.data == "rev_proc_to_view_summary":
        revision = get_revision_context(context)
        revision["action"] = "view"
        return await show_revision_view_mode_menu(query, context)
    if query.data == "rev_proc_summary":
        return await show_revision_procurement_screen(query, context, view="summary")
    if query.data == "rev_proc_urgent":
        return await show_revision_procurement_screen(query, context, view="urgent")
    if query.data == "rev_proc_warning":
        return await show_revision_procurement_screen(query, context, view="warning")
    if query.data == "rev_proc_points":
        return await show_revision_procurement_screen(query, context, view="points")
    if query.data == "rev_proc_home":
        return await show_revision_procurement_screen(query, context, view="home")
    return REVISION_PROCUREMENT_REPORT
# ============ ГЛАВНОЕ МЕНЮ ============
async def start(update: Update, context):
    if not is_allowed_user(update):
        return await deny_private_access(update)
    context.user_data.clear()
    keyboard = [
        [InlineKeyboardButton("👀 К обслуживанию сегодня", callback_data="service_today", style="success")],
        [InlineKeyboardButton("🔧 Обслуживание", callback_data="service")],
        [InlineKeyboardButton("📦 Ревизия", callback_data="revision")],
        [InlineKeyboardButton("📊 Отчёты", callback_data="reports")],
    ]
    text = "<b>☕ Кофе-бот</b>\n\nВыберите действие:"
    if update.callback_query:
        await show_text_screen(
            update.callback_query,
            context,
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return MAIN_MENU


async def show_rent_menu(query, context):
    rent = get_rent_context(context)
    rent.setdefault("period", current_rent_period_key())
    clear_rent_payment_selection(rent)
    await show_text_screen(query, context, "🏠 Аренда\n\nВыберите действие:", reply_markup=build_rent_menu_markup())
    return RENT_MENU_SECTION


async def show_rent_dashboard_screen(query, context, period_key=None, notice=None):
    rent = get_rent_context(context)
    clear_rent_payment_selection(rent)
    rent["period"] = period_key or rent.get("period") or current_rent_period_key()
    await show_loading_state(query, context, "Загружаю аренду...")
    dashboard = await run_blocking(get_rent_dashboard_data, rent["period"])

    text = build_rent_dashboard_text(rent["period"], dashboard)
    if notice:
        text = f"{notice}\n\n{text}"

    await show_text_screen(
        query,
        context,
        text,
        reply_markup=build_rent_dashboard_markup(rent["period"], dashboard),
        parse_mode="HTML",
    )
    return RENT_MENU_SECTION


async def send_rent_dashboard_message(message, context, period_key=None, notice=None):
    rent = get_rent_context(context)
    clear_rent_payment_selection(rent)
    rent["period"] = period_key or rent.get("period") or current_rent_period_key()
    dashboard = await run_blocking(get_rent_dashboard_data, rent["period"])
    text = build_rent_dashboard_text(rent["period"], dashboard)
    if notice:
        text = f"{notice}\n\n{text}"
    await message.reply_text(
        text,
        reply_markup=build_rent_dashboard_markup(rent["period"], dashboard),
        parse_mode="HTML",
    )
    return RENT_MENU_SECTION


async def show_rent_period_menu(query, context, target="dashboard"):
    rent = get_rent_context(context)
    rent["period_target"] = target
    title = "📅 Выберите месяц для оплаты:" if target == "payments" else "📅 Выберите месяц:"
    await show_text_screen(query, context, title, reply_markup=build_rent_period_markup(target))
    return RENT_MENU_SECTION


async def show_rent_payment_select(query, context, period_key=None, notice=None):
    rent = get_rent_context(context)
    rent["period"] = period_key or rent.get("period") or current_rent_period_key()
    clear_rent_payment_selection(rent)
    await show_loading_state(query, context, "Загружаю неоплаченные точки...")
    dashboard = await run_blocking(get_rent_dashboard_data, rent["period"])
    text = build_rent_unpaid_picker_text(rent["period"], dashboard)
    if notice:
        text = f"{notice}\n\n{text}"
    await show_text_screen(
        query,
        context,
        text,
        reply_markup=build_rent_unpaid_markup(dashboard),
        parse_mode="HTML",
    )
    return RENT_MENU_SECTION


async def build_rent_payment_item(context, lease_id, period_key):
    dashboard = await run_blocking(get_rent_dashboard_data, period_key)
    item = find_rent_dashboard_item(dashboard, lease_id)
    if not item:
        return None, dashboard, "missing"

    if any(str(entry.get("lease_id", "")).strip() == str(lease_id).strip() for entry in dashboard.get("paid", [])):
        return item, dashboard, "paid"

    req_data = await run_blocking(get_rent_requisites_data, lease_id)
    landlord_name = "—"
    if req_data and (req_data.get("landlord") or {}).get("Имя / Название"):
        landlord_name = req_data["landlord"]["Имя / Название"]

    item = dict(item)
    item["landlord_name"] = landlord_name
    return item, dashboard, "ok"


async def show_rent_payment_confirm(query, context, lease_id=None, notice=None):
    rent = get_rent_context(context)
    if lease_id is not None:
        rent["selected_lease_id"] = str(lease_id)

    period_key = rent.get("period") or current_rent_period_key()
    selected_lease_id = rent.get("selected_lease_id")
    if not selected_lease_id:
        return await show_rent_payment_select(query, context, period_key)

    await show_loading_state(query, context, "Проверяю оплату...")
    item, dashboard, status = await build_rent_payment_item(context, selected_lease_id, period_key)
    if status == "missing":
        return await show_rent_dashboard_screen(query, context, period_key, notice="⚪ Не удалось найти договор аренды.")
    if status == "paid":
        return await show_rent_dashboard_screen(query, context, period_key, notice="⚪ За этот месяц точка уже оплачена.")

    text = build_rent_payment_confirm_text(item, period_key, receipt_attached=bool(rent.get("receipt_file_id")))
    if notice:
        text = f"{notice}\n\n{text}"
    await show_text_screen(
        query,
        context,
        text,
        reply_markup=build_rent_payment_confirm_markup(),
        parse_mode="HTML",
    )
    return RENT_MENU_SECTION


async def send_rent_payment_confirm_message(message, context, notice=None):
    rent = get_rent_context(context)
    period_key = rent.get("period") or current_rent_period_key()
    selected_lease_id = rent.get("selected_lease_id")
    if not selected_lease_id:
        return await send_rent_dashboard_message(message, context, period_key, notice="⚪ Сначала выбери точку для оплаты.")

    item, dashboard, status = await build_rent_payment_item(context, selected_lease_id, period_key)
    if status == "missing":
        return await send_rent_dashboard_message(message, context, period_key, notice="⚪ Не удалось найти договор аренды.")
    if status == "paid":
        return await send_rent_dashboard_message(message, context, period_key, notice="⚪ За этот месяц точка уже оплачена.")

    text = build_rent_payment_confirm_text(item, period_key, receipt_attached=bool(rent.get("receipt_file_id")))
    if notice:
        text = f"{notice}\n\n{text}"
    await message.reply_text(
        text,
        reply_markup=build_rent_payment_confirm_markup(),
        parse_mode="HTML",
    )
    return RENT_MENU_SECTION


async def show_rent_requisites_select(query, context):
    rent = get_rent_context(context)
    period_key = rent.get("period") or current_rent_period_key()
    await show_loading_state(query, context, "Загружаю реквизиты...")
    leases = await run_blocking(get_rent_selectable_leases, period_key)
    if not leases:
        await show_text_screen(
            query,
            context,
            "🏦 Реквизиты\n\n⚪ Пока нет активных договоров аренды.",
            reply_markup=back_markup("back_rent_menu"),
        )
        return RENT_MENU_SECTION

    await show_text_screen(
        query,
        context,
        "🏦 Реквизиты\n\nВыберите точку:",
        reply_markup=build_rent_requisites_select_markup(leases),
    )
    return RENT_MENU_SECTION


async def show_rent_requisites_card(query, context, lease_id):
    await show_loading_state(query, context, "Загружаю реквизиты...")
    data = await run_blocking(get_rent_requisites_data, lease_id)
    if not data:
        await show_text_screen(
            query,
            context,
            "❌ Не удалось найти реквизиты по этой точке.",
            reply_markup=back_markup("rent_requisites"),
        )
        return RENT_MENU_SECTION

    await show_text_screen(
        query,
        context,
        build_rent_requisites_text(data),
        reply_markup=build_rent_requisites_card_markup(
            lease_id,
            bool(str(data["lease"].get("Договор file_id", "") or "").strip()),
        ),
        parse_mode="HTML",
    )
    return RENT_MENU_SECTION


async def show_rent_cards_select(query, context):
    rent = get_rent_context(context)
    period_key = rent.get("period") or current_rent_period_key()
    await show_loading_state(query, context, "Загружаю карточки аренды...")
    leases = await run_blocking(get_rent_selectable_leases, period_key)
    if not leases:
        await show_text_screen(
            query,
            context,
            "📄 Карточки аренды\n\n⚪ Пока нет активных договоров аренды.",
            reply_markup=back_markup("back_rent_menu"),
        )
        return RENT_MENU_SECTION

    await show_text_screen(
        query,
        context,
        "📄 Карточки аренды\n\nВыберите точку:",
        reply_markup=build_rent_cards_select_markup(leases),
    )
    return RENT_MENU_SECTION


async def show_rent_card(query, context, lease_id):
    await show_loading_state(query, context, "Загружаю карточку аренды...")
    data = await run_blocking(get_rent_card_data, lease_id)
    if not data:
        await show_text_screen(
            query,
            context,
            "❌ Карточка аренды не найдена.",
            reply_markup=back_markup("rent_cards"),
        )
        return RENT_MENU_SECTION

    await show_text_screen(
        query,
        context,
        build_rent_card_text(data),
        reply_markup=build_rent_card_markup(lease_id, data),
        parse_mode="HTML",
    )
    return RENT_MENU_SECTION


async def show_rent_history(query, context, lease_id):
    await show_loading_state(query, context, "Загружаю историю оплат...")
    data = await run_blocking(get_rent_card_data, lease_id)
    if not data:
        await show_text_screen(query, context, "❌ История оплат недоступна.", reply_markup=build_rent_subview_back_markup(lease_id))
        return RENT_MENU_SECTION

    await show_text_screen(
        query,
        context,
        build_rent_history_text(data),
        reply_markup=build_rent_subview_back_markup(lease_id),
        parse_mode="HTML",
    )
    return RENT_MENU_SECTION


async def show_rent_indexations(query, context, lease_id):
    await show_loading_state(query, context, "Загружаю индексации...")
    data = await run_blocking(get_rent_card_data, lease_id)
    if not data:
        await show_text_screen(query, context, "❌ Индексации недоступны.", reply_markup=build_rent_subview_back_markup(lease_id))
        return RENT_MENU_SECTION

    await show_text_screen(
        query,
        context,
        build_rent_indexations_text(data),
        reply_markup=build_rent_subview_back_markup(lease_id),
        parse_mode="HTML",
    )
    return RENT_MENU_SECTION


async def show_rent_manage_menu(query, context):
    await show_text_screen(
        query,
        context,
        "⚙️ Управление арендой\n\n"
        "Добавление и изменение договоров вынесу следующим этапом.\n\n"
        "Сейчас модуль уже умеет:\n"
        "• показывать, что оплатить\n"
        "• отмечать оплату\n"
        "• открывать реквизиты\n"
        "• показывать карточки аренды",
        reply_markup=build_rent_manage_markup(),
    )
    return RENT_MENU_SECTION


async def send_rent_copy_account_message(query, context, lease_id):
    data = await run_blocking(get_rent_requisites_data, lease_id)
    if not data:
        await query.message.reply_text("❌ Реквизиты не найдены.")
        return RENT_MENU_SECTION

    account = str((data.get("landlord") or {}).get("Р/счёт", "") or "").strip()
    if not account:
        await query.message.reply_text("⚪ Р/счёт не указан.")
        return RENT_MENU_SECTION

    await query.message.reply_text(f"<pre>{escape_html(account)}</pre>", parse_mode="HTML")
    return RENT_MENU_SECTION


async def send_rent_copy_all_message(query, context, lease_id):
    data = await run_blocking(get_rent_requisites_data, lease_id)
    if not data:
        await query.message.reply_text("❌ Реквизиты не найдены.")
        return RENT_MENU_SECTION

    await query.message.reply_text(
        build_text_pre_block(build_rent_copy_all_text(data).splitlines()),
        parse_mode="HTML",
    )
    return RENT_MENU_SECTION


async def send_rent_contract_document(query, context, lease_id):
    data = await run_blocking(get_rent_card_data, lease_id)
    if not data:
        await query.message.reply_text("❌ Договор не найден.")
        return RENT_MENU_SECTION

    file_id = str(data["lease"].get("Договор file_id", "") or "").strip()
    if not file_id:
        await query.message.reply_text("⚪ Файл договора не прикреплён.")
        return RENT_MENU_SECTION

    try:
        await context.bot.send_document(
            chat_id=query.message.chat_id,
            document=file_id,
            caption=f"📄 Договор — {data['lease'].get('Точка', '—')}",
        )
    except Exception:
        logger.exception("Failed to send rent contract document for lease %s", lease_id)
        await query.message.reply_text("❌ Не удалось открыть договор.")
    return RENT_MENU_SECTION


async def rent_menu_handler(update: Update, context):
    query = update.callback_query
    if not is_allowed_user(update):
        await deny_callback_access(query)
        return ConversationHandler.END
    await query.answer()
    data = query.data
    rent = get_rent_context(context)

    if data == "back_main":
        return await start(update, context)
    if data in {"rent", "back_rent_menu"}:
        return await show_rent_menu(query, context)
    if data == "rent_dashboard":
        return await show_rent_dashboard_screen(query, context)
    if data == "rent_period_dashboard":
        return await show_rent_period_menu(query, context, "dashboard")
    if data == "rent_period_payments":
        return await show_rent_period_menu(query, context, "payments")
    if data.startswith("rent_period_"):
        period_key = data.replace("rent_period_", "", 1)
        target = rent.get("period_target") or "dashboard"
        rent["period"] = period_key
        if target == "payments":
            return await show_rent_payment_select(query, context, period_key)
        return await show_rent_dashboard_screen(query, context, period_key)
    if data == "rent_payments":
        return await show_rent_payment_select(query, context)
    if data.startswith("rent_pay_pick_"):
        lease_id = data.replace("rent_pay_pick_", "", 1)
        return await show_rent_payment_confirm(query, context, lease_id=lease_id)
    if data.startswith("rent_pay_direct_"):
        lease_id = data.replace("rent_pay_direct_", "", 1)
        return await show_rent_payment_confirm(query, context, lease_id=lease_id)
    if data == "rent_pay_confirm":
        lease_id = rent.get("selected_lease_id")
        period_key = rent.get("period") or current_rent_period_key()
        if not lease_id:
            return await show_rent_payment_select(query, context, period_key, notice="⚪ Сначала выбери точку для оплаты.")

        result = await run_blocking(
            mark_rent_payment,
            lease_id,
            period_key,
            get_actor_label(update),
            rent.get("receipt_file_id", ""),
        )
        clear_rent_payment_selection(rent)

        if result.get("status") == "saved":
            logger.info(
                "rent payment saved: user_id=%s point=%s period=%s lease_id=%s",
                getattr(update.effective_user, "id", None),
                result["lease"].get("Точка", ""),
                period_key,
                result["lease"].get("id", ""),
            )
            notice = (
                f"<b>✅ Оплата отмечена</b>\n"
                f"📍 {escape_html(result['lease'].get('Точка', '—'))}\n"
                f"💰 {escape_html(format_money_spaced(result['payment'].get('amount', '')))}"
            )
        elif result.get("status") == "exists":
            notice = "⚪ За этот месяц оплата уже была отмечена."
        else:
            notice = "❌ Не удалось отметить оплату."
        return await show_rent_dashboard_screen(query, context, period_key, notice=notice)
    if data == "rent_pay_receipt":
        await show_text_screen(
            query,
            context,
            "📎 Отправь фото или документ чека.\n\n"
            "После этого я верну подтверждение оплаты.",
            reply_markup=back_markup("back_rent_payment_confirm"),
        )
        return RENT_PAYMENT_RECEIPT
    if data == "rent_pay_requisites":
        lease_id = rent.get("selected_lease_id")
        if not lease_id:
            return await show_rent_payment_select(query, context, notice="⚪ Сначала выбери точку.")
        data_obj = await run_blocking(get_rent_requisites_data, lease_id)
        if not data_obj:
            await query.message.reply_text("❌ Реквизиты не найдены.")
            return RENT_MENU_SECTION
        await query.message.reply_text(build_rent_requisites_text(data_obj), parse_mode="HTML")
        return RENT_MENU_SECTION
    if data == "rent_requisites":
        return await show_rent_requisites_select(query, context)
    if data.startswith("rent_req_") and not data.startswith("rent_req_doc_"):
        lease_id = data.replace("rent_req_", "", 1)
        return await show_rent_requisites_card(query, context, lease_id)
    if data.startswith("rent_copy_account_"):
        lease_id = data.replace("rent_copy_account_", "", 1)
        return await send_rent_copy_account_message(query, context, lease_id)
    if data.startswith("rent_copy_all_"):
        lease_id = data.replace("rent_copy_all_", "", 1)
        return await send_rent_copy_all_message(query, context, lease_id)
    if data.startswith("rent_req_doc_"):
        lease_id = data.replace("rent_req_doc_", "", 1)
        return await send_rent_contract_document(query, context, lease_id)
    if data == "rent_cards":
        return await show_rent_cards_select(query, context)
    if data.startswith("rent_card_history_"):
        lease_id = data.replace("rent_card_history_", "", 1)
        return await show_rent_history(query, context, lease_id)
    if data.startswith("rent_card_index_"):
        lease_id = data.replace("rent_card_index_", "", 1)
        return await show_rent_indexations(query, context, lease_id)
    if data.startswith("rent_card_req_"):
        lease_id = data.replace("rent_card_req_", "", 1)
        return await show_rent_requisites_card(query, context, lease_id)
    if data.startswith("rent_card_doc_"):
        lease_id = data.replace("rent_card_doc_", "", 1)
        return await send_rent_contract_document(query, context, lease_id)
    if data.startswith("rent_card_"):
        lease_id = data.replace("rent_card_", "", 1)
        return await show_rent_card(query, context, lease_id)
    if data == "rent_manage":
        return await show_rent_manage_menu(query, context)
    return RENT_MENU_SECTION


async def rent_payment_receipt_back_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "back_rent_payment_confirm":
        return await show_rent_payment_confirm(query, context)
    return RENT_PAYMENT_RECEIPT


async def rent_payment_receipt_handler(update: Update, context):
    message = update.message
    rent = get_rent_context(context)
    if not message:
        return RENT_PAYMENT_RECEIPT

    file_id = ""
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document:
        file_id = message.document.file_id

    if not file_id:
        await message.reply_text(
            "❌ Отправь фото или документ чека.",
            reply_markup=back_markup("back_rent_payment_confirm"),
        )
        return RENT_PAYMENT_RECEIPT

    rent["receipt_file_id"] = file_id
    return await send_rent_payment_confirm_message(message, context, notice="📎 Чек прикреплён.")


def build_repair_confirm_text(point, machine, reason, description, center, broken_date):
    lines = [f"<b>🆕 Новый ремонт — {escape_html(point)}</b>", ""]
    rows = [
        ("Аппарат", get_machine_display_name(machine), ""),
        ("Причина", reason or "—", ""),
        ("Сервис", get_repair_service_label({}, center), ""),
        ("Дата", broken_date or "—", ""),
    ]
    lines.append(build_preformatted_block(rows))
    description = str(description or "").strip()
    if description:
        lines.extend(["", "<b>Описание</b>", escape_html(description)])
    return "\n".join(lines)


async def send_repair_card_message(message, context, repair_id, notice=None):
    try:
        data = await run_blocking(get_repair_card_data, repair_id)
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(message)
            return REPAIR_MENU_SECTION
        raise

    if not data:
        await message.reply_text("❌ Ремонт не найден.")
        return REPAIR_MENU_SECTION

    text = build_repair_card_text(data)
    if notice:
        text = f"{notice}\n\n{text}"
    await message.reply_text(
        text,
        reply_markup=build_repair_card_markup(repair_id, data),
        parse_mode="HTML",
    )
    return REPAIR_MENU_SECTION


async def complete_repair_creation(target, context, created_by, photo_file_id=""):
    repair_ctx = get_repair_context(context)
    payload = {
        "point": repair_ctx.get("point", ""),
        "machine": repair_ctx.get("machine_record"),
        "reason": repair_ctx.get("reason", ""),
        "description": repair_ctx.get("description", ""),
        "service_center_id": "",
        "broken_date": repair_ctx.get("broken_date", today()),
        "created_by": created_by,
    }

    try:
        repair = await run_blocking(create_repair_case, payload)
        if photo_file_id:
            await run_blocking(
                add_repair_document,
                {
                    "repair_id": repair.get("id", ""),
                    "point": repair.get("Точка", ""),
                    "doc_type": "Фото поломки",
                    "file_id": photo_file_id,
                    "uploaded_by": created_by,
                },
            )
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(target)
            return REPAIR_NEW_DATE
        raise

    clear_repair_draft(repair_ctx)
    await refresh_group_service_today_posts(context.application, force=True)

    if hasattr(target, "edit_message_text"):
        return await show_repair_card_screen(target, context, repair.get("id", ""), notice="✅ Ремонт создан.")
    return await send_repair_card_message(target, context, repair.get("id", ""), notice="✅ Ремонт создан.")


async def show_repair_menu(query, context):
    try:
        await run_blocking(ensure_repair_worksheets)
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(query)
            return REPAIR_MENU_SECTION
        raise
    clear_repair_draft(get_repair_context(context))
    await show_text_screen(query, context, "🛠 Ремонт\n\nВыберите действие:", reply_markup=build_repair_menu_markup())
    return REPAIR_MENU_SECTION


async def show_active_repairs_screen(query, context, notice=None):
    await show_loading_state(query, context, "Загружаю активные ремонты...")
    try:
        active_repairs = await run_blocking(build_active_repair_data)
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(query)
            return REPAIR_MENU_SECTION
        raise
    text = build_repair_dashboard_text(active_repairs)
    if notice:
        text = f"{notice}\n\n{text}"
    await show_text_screen(
        query,
        context,
        text,
        reply_markup=build_repair_dashboard_markup(active_repairs),
        parse_mode="HTML",
    )
    return REPAIR_MENU_SECTION


async def show_repair_card_screen(query, context, repair_id, notice=None):
    await show_loading_state(query, context, "Загружаю карточку ремонта...")
    try:
        data = await run_blocking(get_repair_card_data, repair_id)
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(query)
            return REPAIR_MENU_SECTION
        raise
    if not data:
        await show_text_screen(
            query,
            context,
            "❌ Ремонт не найден.",
            reply_markup=back_markup("repair_active"),
        )
        return REPAIR_MENU_SECTION

    get_repair_context(context)["selected_repair_id"] = repair_id
    text = build_repair_card_text(data)
    if notice:
        text = f"{notice}\n\n{text}"
    await show_text_screen(
        query,
        context,
        text,
        reply_markup=build_repair_card_markup(repair_id, data),
        parse_mode="HTML",
    )
    return REPAIR_MENU_SECTION


async def show_repair_history_points(query, context):
    await show_loading_state(query, context, "Загружаю историю ремонтов...")
    try:
        repairs = await run_blocking(get_all_repairs)
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(query)
            return REPAIR_MENU_SECTION
        raise
    points = [point for point in POINTS if any(str(repair.get("Точка", "")).strip() == point for repair in repairs)]
    if not points:
        await show_text_screen(
            query,
            context,
            "📚 История аппаратов\n\n⚪ Пока нет истории ремонтов.",
            reply_markup=back_markup("back_repair_menu"),
        )
        return REPAIR_MENU_SECTION

    await show_text_screen(
        query,
        context,
        "📚 История аппаратов\n\nВыберите точку:",
        reply_markup=build_repair_history_points_markup(points),
    )
    return REPAIR_HISTORY_POINT


async def show_repair_history_machine_picker(query, context, point):
    await show_loading_state(query, context, "Подбираю аппараты...")
    try:
        repairs = await run_blocking(get_all_repairs)
        machines = await run_blocking(get_point_repair_machines, point, True)
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(query)
            return REPAIR_MENU_SECTION
        raise
    repair_machine_ids = {
        str(repair.get("Аппарат ID", "")).strip()
        for repair in repairs
        if str(repair.get("Точка", "")).strip() == point
    }
    machines = [machine for machine in machines if str(machine.get("id", "")).strip() in repair_machine_ids]

    if not machines:
        await show_text_screen(
            query,
            context,
            f"📚 История аппаратов — {point}\n\n⚪ По этой точке пока нет истории ремонтов.",
            reply_markup=build_repair_history_back_markup(""),
        )
        return REPAIR_HISTORY_POINT

    repair_ctx = get_repair_context(context)
    repair_ctx["history_point"] = point
    if len(machines) == 1:
        return await show_repair_history_screen(query, context, machines[0].get("id", ""))

    await show_text_screen(
        query,
        context,
        f"📚 История аппаратов — {point}\n\nВыберите аппарат:",
        reply_markup=build_repair_history_machines_markup(point, machines),
    )
    return REPAIR_HISTORY_MACHINE


async def show_repair_history_screen(query, context, machine_id):
    await show_loading_state(query, context, "Загружаю историю аппарата...")
    try:
        data = await run_blocking(get_machine_history_data, machine_id)
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(query)
            return REPAIR_MENU_SECTION
        raise
    if not data:
        await show_text_screen(
            query,
            context,
            "❌ История аппарата не найдена.",
            reply_markup=build_repair_history_back_markup(get_repair_context(context).get("history_point", "")),
        )
        return REPAIR_HISTORY_MACHINE

    repair_ctx = get_repair_context(context)
    repair_ctx["history_machine_id"] = machine_id
    point = repair_ctx.get("history_point", "")
    back_cb = f"repair_hist_point_{point}" if point else "repair_history"
    await show_text_screen(
        query,
        context,
        build_machine_history_text(data["machine"], data),
        reply_markup=build_machine_history_markup(data, back_cb),
        parse_mode="HTML",
    )
    return REPAIR_HISTORY_MACHINE


async def show_repair_refs_menu(query, context):
    await show_text_screen(
        query,
        context,
        "⚙️ Справочники ремонта\n\nВыберите раздел:",
        reply_markup=build_repair_refs_markup(),
    )
    return REPAIR_MENU_SECTION


async def show_repair_centers_screen(query, context):
    await show_loading_state(query, context, "Загружаю сервисные центры...")
    try:
        centers = await run_blocking(get_all_repair_centers)
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(query)
            return REPAIR_MENU_SECTION
        raise
    keyboard = [
        [InlineKeyboardButton("➕ Добавить сервисный центр", callback_data="repair_refs_centers_add")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="repair_refs")],
    ]
    await show_text_screen(
        query,
        context,
        build_repair_centers_text(centers),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )
    return REPAIR_MENU_SECTION


async def show_repair_machines_screen(query, context):
    await show_loading_state(query, context, "Загружаю аппараты...")
    try:
        machines = await run_blocking(get_all_repair_machines)
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(query)
            return REPAIR_MENU_SECTION
        raise
    keyboard = [
        [InlineKeyboardButton("➕ Добавить аппарат", callback_data="repair_refs_machines_add")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="repair_refs")],
    ]
    await show_text_screen(
        query,
        context,
        build_repair_machines_text(machines),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )
    return REPAIR_MENU_SECTION


async def show_repair_docs_screen(query, context, repair_id):
    try:
        data = await run_blocking(get_repair_card_data, repair_id)
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(query)
            return REPAIR_MENU_SECTION
        raise
    if not data:
        await show_text_screen(query, context, "❌ Документы не найдены.", reply_markup=back_markup("repair_active"))
        return REPAIR_MENU_SECTION

    get_repair_context(context)["selected_repair_id"] = repair_id
    text = (
        f"<b>📎 Документы — {escape_html(repair_id)}</b>\n\n"
        f"{build_repair_documents_lines(data.get('documents', []))}\n\n"
        "Что добавить?"
    )
    await show_text_screen(
        query,
        context,
        text,
        reply_markup=build_repair_docs_markup(repair_id),
        parse_mode="HTML",
    )
    return REPAIR_MENU_SECTION


async def show_repair_contact_screen(query, context, repair_id):
    try:
        data = await run_blocking(get_repair_card_data, repair_id)
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(query)
            return REPAIR_MENU_SECTION
        raise
    if not data:
        await show_text_screen(query, context, "❌ Контакты сервиса недоступны.", reply_markup=back_markup("repair_active"))
        return REPAIR_MENU_SECTION

    center = data.get("center")
    lines = [f"<b>📞 Сервис — {escape_html(repair_id)}</b>", ""]
    rows = [
        ("Название", get_repair_service_label(data["repair"], center), ""),
        ("Контакт", (center or {}).get("Контактное лицо", "") or "—", ""),
        ("Телефон", (center or {}).get("Телефон", "") or "—", ""),
        ("Email", (center or {}).get("Email", "") or "—", ""),
        ("Адрес", (center or {}).get("Адрес", "") or "—", ""),
    ]
    lines.append(build_preformatted_block(rows))
    await show_text_screen(
        query,
        context,
        "\n".join(lines),
        reply_markup=build_repair_docs_back_markup(repair_id),
        parse_mode="HTML",
    )
    return REPAIR_MENU_SECTION


async def show_repair_new_point(query, context, notice=None):
    repair_ctx = get_repair_context(context)
    clear_repair_draft(repair_ctx)
    text = f"{build_progress_text(REPAIR_NEW_FLOW_STEPS, 1)}\n\n🆕 Новый ремонт\n\nВыберите точку:"
    if notice:
        text = f"{text}\n\n{notice}"
    await show_text_screen(query, context, text, reply_markup=build_repair_point_markup())
    return REPAIR_NEW_POINT


async def show_repair_machine_step(query, context, point, notice=None):
    await show_loading_state(query, context, "Проверяю аппараты на точке...")
    try:
        machines = await run_blocking(get_point_repair_machines, point)
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(query)
            return REPAIR_NEW_POINT
        raise

    repair_ctx = get_repair_context(context)
    repair_ctx["point"] = point
    repair_ctx.pop("machine_id", None)
    repair_ctx.pop("machine_record", None)
    repair_ctx.pop("machine_candidate", None)

    if not machines:
        text = (
            f"{build_progress_text(REPAIR_NEW_FLOW_STEPS, 2)}\n\n🆕 Новый ремонт — {point}\n\n"
            "На этой точке пока нет аппарата в реестре.\n"
            "Если знаешь, можно ввести модель вручную.\n"
            "Например: <b>Saeco Aulika</b>"
        )
        if notice:
            text = f"{text}\n\n{notice}"
        await show_text_screen(query, context, text, reply_markup=build_repair_no_machine_markup(), parse_mode="HTML")
        return REPAIR_NEW_MACHINE

    if len(machines) == 1:
        machine = machines[0]
        repair_ctx["machine_candidate"] = machine
        text = (
            f"{build_progress_text(REPAIR_NEW_FLOW_STEPS, 2)}\n\n🆕 Новый ремонт — {point}\n\n"
            f"На точке сейчас стоит:\n<b>{escape_html(get_machine_display_name(machine))}</b>\n\n"
            "Ремонтируем этот аппарат?"
        )
        if notice:
            text = f"{text}\n\n{notice}"
        await show_text_screen(query, context, text, reply_markup=build_repair_single_machine_markup(), parse_mode="HTML")
        return REPAIR_NEW_MACHINE

    text = f"{build_progress_text(REPAIR_NEW_FLOW_STEPS, 2)}\n\n🆕 Новый ремонт — {point}\n\nВыберите аппарат:"
    if notice:
        text = f"{text}\n\n{notice}"
    await show_text_screen(query, context, text, reply_markup=build_repair_machine_markup(point, machines))
    return REPAIR_NEW_MACHINE


async def show_repair_reason_step(query, context, notice=None):
    repair_ctx = get_repair_context(context)
    machine = repair_ctx.get("machine_record") or repair_ctx.get("machine_candidate")
    text = f"{build_progress_text(REPAIR_NEW_FLOW_STEPS, 3)}\n\n🧩 Причина поломки\n\nВыберите причину:"
    if machine:
        text = f"{text}\n\n☕ Аппарат: {escape_html(get_machine_display_name(machine))}"
    if notice:
        text = f"{text}\n\n{notice}"
    await show_text_screen(query, context, text, reply_markup=build_repair_reason_markup(), parse_mode="HTML")
    return REPAIR_NEW_REASON


async def show_repair_description_step(query, context, notice=None):
    text = (
        f"{build_progress_text(REPAIR_NEW_FLOW_STEPS, 4)}\n\n"
        "📝 Коротко опиши, что случилось.\n\nМожно пропустить и добавить детали позже."
    )
    if notice:
        text = f"{text}\n\n{notice}"
    await show_text_screen(query, context, text, reply_markup=build_repair_description_markup(), parse_mode="HTML")
    return REPAIR_NEW_DESCRIPTION


async def show_repair_photo_step(query, context, notice=None):
    text = (
        f"{build_progress_text(REPAIR_NEW_FLOW_STEPS, 5)}\n\n"
        "📎 Пришли фото поломки, если оно есть.\n\nМожно пропустить и добавить позже из карточки."
    )
    if notice:
        text = f"{text}\n\n{notice}"
    await show_text_screen(query, context, text, reply_markup=build_repair_photo_markup())
    return REPAIR_NEW_PHOTO


async def show_repair_broken_date_step(query, context, notice=None):
    last_service_date = ""
    point = get_repair_context(context).get("point", "")
    if point:
        try:
            last_service_date = await run_blocking(get_latest_service_date_for_point, point)
        except APIError as error:
            if is_google_sheets_busy_error(error):
                await show_sheets_busy_notice(query)
                return REPAIR_NEW_DATE
            raise
    text = f"{build_progress_text(REPAIR_NEW_FLOW_STEPS, 6)}\n\n📅 Когда случилась поломка?"
    if notice:
        text = f"{text}\n\n{notice}"
    await show_text_screen(query, context, text, reply_markup=build_repair_broken_date_markup(last_service_date))
    return REPAIR_NEW_DATE


async def show_repair_service_step(query, context, repair_id, notice=None):
    await show_loading_state(query, context, "Загружаю сервисные центры...")
    try:
        centers = await run_blocking(get_all_repair_centers)
        data = await run_blocking(get_repair_card_data, repair_id)
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(query)
            return REPAIR_MENU_SECTION
        raise

    if not data:
        return await show_active_repairs_screen(query, context, notice="❌ Ремонт не найден.")

    get_repair_context(context)["selected_repair_id"] = repair_id
    current_label = get_repair_service_label(data["repair"], data.get("center"))
    text = (
        f"🏭 Сервис — {escape_html(repair_id)}\n\n"
        f"Сейчас: <b>{escape_html(current_label)}</b>\n\n"
        "Куда отправляем ремонт?"
    )
    if notice:
        text = f"{notice}\n\n{text}"
    await show_text_screen(
        query,
        context,
        text,
        reply_markup=build_repair_center_markup(centers, "repair_service_back_card"),
        parse_mode="HTML",
    )
    return REPAIR_SET_SERVICE


async def show_repair_broken_date_edit_step(query, context, repair_id, notice=None):
    try:
        data = await run_blocking(get_repair_card_data, repair_id)
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(query)
            return REPAIR_MENU_SECTION
        raise
    if not data:
        return await show_active_repairs_screen(query, context, notice="❌ Ремонт не найден.")

    repair_ctx = get_repair_context(context)
    repair_ctx["selected_repair_id"] = repair_id
    repair_ctx["date_broken_value"] = str(data["repair"].get("Дата поломки", "") or "").strip()
    last_service_date = ""
    point = str(data["repair"].get("Точка", "") or "").strip()
    if point:
        try:
            last_service_date = await run_blocking(get_latest_service_date_for_point, point)
        except APIError as error:
            if is_google_sheets_busy_error(error):
                await show_sheets_busy_notice(query)
                return REPAIR_SET_DATE_BROKEN
            raise
    current_value = repair_ctx["date_broken_value"] or "—"
    text = (
        f"🗓 Дата поломки — {escape_html(repair_id)}\n\n"
        f"Сейчас: <b>{escape_html(current_value)}</b>\n\n"
        "Выбери дату поломки или введи её вручную."
    )
    if notice:
        text = f"{notice}\n\n{text}"
    await show_text_screen(query, context, text, reply_markup=build_repair_broken_edit_markup(last_service_date), parse_mode="HTML")
    return REPAIR_SET_DATE_BROKEN


def build_repair_dates_menu_text(repair_id, repair):
    rows = [
        ("Поломка", repair.get("Дата поломки", "") or "—", ""),
        ("Отправлен", repair.get("Дата отправки", "") or "—", ""),
        ("План", repair.get("Дата готовности (план)", "") or "—", ""),
    ]
    return (
        f"<b>📅 Указать сроки — {escape_html(repair_id)}</b>\n\n"
        f"{build_preformatted_block(rows)}\n\n"
        "Что хочешь изменить?"
    )


async def show_repair_dates_menu(query, context, repair_id, notice=None):
    try:
        data = await run_blocking(get_repair_card_data, repair_id)
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(query)
            return REPAIR_MENU_SECTION
        raise
    if not data:
        return await show_active_repairs_screen(query, context, notice="❌ Ремонт не найден.")

    repair_ctx = get_repair_context(context)
    repair_ctx["selected_repair_id"] = repair_id
    text = build_repair_dates_menu_text(repair_id, data["repair"])
    if notice:
        text = f"{notice}\n\n{text}"
    await show_text_screen(
        query,
        context,
        text,
        reply_markup=build_repair_dates_menu_markup(repair_id),
        parse_mode="HTML",
    )
    return REPAIR_MENU_SECTION


async def send_repair_dates_menu_message(message, context, repair_id, notice=None):
    try:
        data = await run_blocking(get_repair_card_data, repair_id)
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(message)
            return REPAIR_MENU_SECTION
        raise
    if not data:
        await message.reply_text("❌ Ремонт не найден.")
        return REPAIR_MENU_SECTION

    text = build_repair_dates_menu_text(repair_id, data["repair"])
    if notice:
        text = f"{notice}\n\n{text}"
    await message.reply_text(
        text,
        reply_markup=build_repair_dates_menu_markup(repair_id),
        parse_mode="HTML",
    )
    return REPAIR_MENU_SECTION


async def show_repair_date_sent_step(query, context, repair_id, notice=None):
    try:
        data = await run_blocking(get_repair_card_data, repair_id)
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(query)
            return REPAIR_MENU_SECTION
        raise
    if not data:
        return await show_active_repairs_screen(query, context, notice="❌ Ремонт не найден.")

    repair_ctx = get_repair_context(context)
    repair_ctx["selected_repair_id"] = repair_id
    if "date_sent_value" not in repair_ctx:
        repair_ctx["date_sent_value"] = str(data["repair"].get("Дата отправки", "") or "").strip()
    if "date_plan_value" not in repair_ctx:
        repair_ctx["date_plan_value"] = str(data["repair"].get("Дата готовности (план)", "") or "").strip()
    current_value = repair_ctx["date_sent_value"] or "—"
    text = (
        f"🚚 Дата отправки — {escape_html(repair_id)}\n\n"
        f"Дата отправки: <b>{escape_html(current_value)}</b>\n\n"
        "Укажи дату отправки или пропусти."
    )
    if notice:
        text = f"{notice}\n\n{text}"
    await show_text_screen(query, context, text, reply_markup=build_repair_sent_date_markup(), parse_mode="HTML")
    return REPAIR_SET_DATE_SENT


async def show_repair_date_plan_step(query, context, repair_id, notice=None):
    try:
        data = await run_blocking(get_repair_card_data, repair_id)
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(query)
            return REPAIR_MENU_SECTION
        raise
    if not data:
        return await show_active_repairs_screen(query, context, notice="❌ Ремонт не найден.")

    repair_ctx = get_repair_context(context)
    repair_ctx["selected_repair_id"] = repair_id
    if "date_sent_value" not in repair_ctx:
        repair_ctx["date_sent_value"] = str(data["repair"].get("Дата отправки", "") or "").strip()
    if "date_plan_value" not in repair_ctx:
        repair_ctx["date_plan_value"] = str(data["repair"].get("Дата готовности (план)", "") or "").strip()
    current_value = repair_ctx.get("date_plan_value", "") or "—"
    text = (
        f"🏁 Плановая готовность — {escape_html(repair_id)}\n\n"
        f"Плановая готовность: <b>{escape_html(current_value)}</b>\n\n"
        "Укажи плановую дату или пропусти."
    )
    if notice:
        text = f"{notice}\n\n{text}"
    await show_text_screen(query, context, text, reply_markup=build_repair_plan_date_markup(), parse_mode="HTML")
    return REPAIR_SET_DATE_PLAN


async def show_repair_status_step(query, context, repair_id, notice=None):
    try:
        data = await run_blocking(get_repair_card_data, repair_id)
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(query)
            return REPAIR_MENU_SECTION
        raise
    if not data:
        return await show_active_repairs_screen(query, context, notice="❌ Ремонт не найден.")

    current_status = data["repair"].get("Статус", "")
    options = get_repair_status_options(current_status)
    if not options:
        return await show_repair_card_screen(query, context, repair_id, notice="⚪ Для этого статуса нет следующих шагов.")

    repair_ctx = get_repair_context(context)
    repair_ctx["selected_repair_id"] = repair_id
    repair_ctx["status_options"] = options
    text = f"📝 Обновить статус — {escape_html(repair_id)}\n\nСейчас: <b>{escape_html(current_status)}</b>\n\nВыберите новый статус:"
    if notice:
        text = f"{notice}\n\n{text}"
    await show_text_screen(query, context, text, reply_markup=build_repair_status_markup(repair_id, options), parse_mode="HTML")
    return REPAIR_STATUS_UPDATE


async def show_repair_expense_type_step(query, context, repair_id, notice=None):
    repair_ctx = get_repair_context(context)
    repair_ctx["selected_repair_id"] = repair_id
    repair_ctx.pop("expense_type", None)
    repair_ctx.pop("expense_amount", None)
    repair_ctx.pop("expense_description", None)
    text = f"💰 Новый расход — {escape_html(repair_id)}\n\nВыберите тип расхода:"
    if notice:
        text = f"{notice}\n\n{text}"
    await show_text_screen(query, context, text, reply_markup=build_repair_expense_type_markup(), parse_mode="HTML")
    return REPAIR_EXPENSE_TYPE


async def repair_menu_handler(update: Update, context):
    query = update.callback_query
    if not is_allowed_user(update):
        await deny_callback_access(query)
        return ConversationHandler.END
    await query.answer()
    data = query.data

    if data == "back_main":
        return await start(update, context)
    if data in {"repair", "back_repair_menu"}:
        return await show_repair_menu(query, context)
    if data == "repair_active":
        return await show_active_repairs_screen(query, context)
    if data == "repair_new":
        return await show_repair_new_point(query, context)
    if data == "repair_history":
        return await show_repair_history_points(query, context)
    if data == "repair_refs":
        return await show_repair_refs_menu(query, context)
    if data == "repair_refs_centers":
        return await show_repair_centers_screen(query, context)
    if data == "repair_refs_machines":
        return await show_repair_machines_screen(query, context)
    if data == "repair_refs_centers_add":
        await show_text_screen(
            query, context,
            "➕ <b>Добавить сервисный центр</b>\n\n"
            "Отправь команду в личке боту:\n"
            "<code>/add_center Название;Город;Контакт;Телефон;Email;Адрес;Специализация;Заметки</code>\n\n"
            "Обязательно только название. Остальные поля можно оставить пустыми (но разделители <code>;</code> сохраняй).\n\n"
            "Пример: <code>/add_center Кофесервис;Москва;Иван;+79991234567</code>",
            reply_markup=back_markup("repair_refs_centers"),
            parse_mode="HTML",
        )
        return REPAIR_MENU_SECTION
    if data == "repair_refs_machines_add":
        await show_text_screen(
            query, context,
            "➕ <b>Добавить аппарат</b>\n\n"
            "Отправь команду в личке боту:\n"
            "<code>/add_machine Точка;Бренд;Модель;Серийный;ДатаПокупки;Гарантия;Заметки</code>\n\n"
            "Обязательны: Точка и Бренд. Точки: " + ", ".join(POINTS) + "\n\n"
            "Пример: <code>/add_machine Сити;Saeco;Aulika EVO;SN-12345</code>",
            reply_markup=back_markup("repair_refs_machines"),
            parse_mode="HTML",
        )
        return REPAIR_MENU_SECTION
    if data.startswith("repair_open_"):
        return await show_repair_card_screen(query, context, data.replace("repair_open_", "", 1))
    if data.startswith("repair_status_"):
        return await show_repair_status_step(query, context, data.replace("repair_status_", "", 1))
    if data.startswith("repair_service_"):
        return await show_repair_service_step(query, context, data.replace("repair_service_", "", 1))
    if data.startswith("repair_dates_field_broken_"):
        return await show_repair_broken_date_edit_step(query, context, data.replace("repair_dates_field_broken_", "", 1))
    if data.startswith("repair_dates_field_sent_"):
        return await show_repair_date_sent_step(query, context, data.replace("repair_dates_field_sent_", "", 1))
    if data.startswith("repair_dates_field_plan_"):
        return await show_repair_date_plan_step(query, context, data.replace("repair_dates_field_plan_", "", 1))
    if data.startswith("repair_dates_"):
        return await show_repair_dates_menu(query, context, data.replace("repair_dates_", "", 1))
    if data.startswith("repair_expense_"):
        return await show_repair_expense_type_step(query, context, data.replace("repair_expense_", "", 1))
    if data.startswith("repair_docs_"):
        return await show_repair_docs_screen(query, context, data.replace("repair_docs_", "", 1))
    if data.startswith("repair_doc_type_"):
        repair_id = get_repair_context(context).get("selected_repair_id", "")
        try:
            doc_index = int(data.replace("repair_doc_type_", "", 1))
        except ValueError:
            return REPAIR_MENU_SECTION
        if not repair_id or not 0 <= doc_index < len(REPAIR_DOCUMENT_TYPES):
            return REPAIR_MENU_SECTION
        repair_ctx = get_repair_context(context)
        repair_ctx["doc_type"] = REPAIR_DOCUMENT_TYPES[doc_index]
        await show_text_screen(
            query,
            context,
            f"📎 {escape_html(REPAIR_DOCUMENT_TYPES[doc_index])}\n\nОтправь фото или документ.",
            reply_markup=build_repair_docs_back_markup(repair_id),
            parse_mode="HTML",
        )
        return REPAIR_DOC_UPLOAD
    if data.startswith("repair_contact_"):
        return await show_repair_contact_screen(query, context, data.replace("repair_contact_", "", 1))
    if data.startswith("repair_return_"):
        repair_id = data.replace("repair_return_", "", 1)
        try:
            repair_data = await run_blocking(get_repair_card_data, repair_id)
        except APIError as error:
            if is_google_sheets_busy_error(error):
                await show_sheets_busy_notice(query)
                return REPAIR_MENU_SECTION
            raise
        if not repair_data:
            return await show_active_repairs_screen(query, context, notice="❌ Ремонт не найден.")
        current_status = str(repair_data["repair"].get("Статус", "") or "").strip()
        if current_status == REPAIR_STATUS_INSTALLED:
            return await show_repair_card_screen(query, context, repair_id, notice="⚪ Эта точка уже возвращена в работу.")
        if REPAIR_STATUS_INSTALLED not in get_repair_status_options(current_status):
            return await show_repair_status_step(
                query,
                context,
                repair_id,
                notice="⚪ Сначала переведи ремонт в допустимый следующий статус.",
            )
        try:
            repair = await run_blocking(update_repair_status_value, repair_id, REPAIR_STATUS_INSTALLED)
        except APIError as error:
            if is_google_sheets_busy_error(error):
                await show_sheets_busy_notice(query)
                return REPAIR_MENU_SECTION
            raise
        logger.info(
            "repair status changed: user_id=%s repair_id=%s point=%s status=%s",
            getattr(update.effective_user, "id", None),
            repair_id,
            (repair or {}).get("Точка", ""),
            REPAIR_STATUS_INSTALLED,
        )
        await refresh_group_service_today_posts(context.application, force=True)
        return await show_repair_card_screen(query, context, repair_id, notice="✅ Точка возвращена в работу.")
    if data.startswith("repair_delete_") and not data.startswith("repair_delete_confirm_") and not data.startswith("repair_delete_cancel_"):
        repair_id = data.replace("repair_delete_", "", 1)
        await show_text_screen(
            query,
            context,
            f"🗑 Удалить ремонт {escape_html(repair_id)}?\n\n"
            "Будут удалены сама карточка ремонта, расходы и документы по этому ремонту.",
            reply_markup=build_repair_delete_confirm_markup(repair_id),
            parse_mode="HTML",
        )
        return REPAIR_MENU_SECTION
    if data.startswith("repair_delete_cancel_"):
        repair_id = data.replace("repair_delete_cancel_", "", 1)
        return await show_repair_card_screen(query, context, repair_id)
    if data.startswith("repair_delete_confirm_"):
        repair_id = data.replace("repair_delete_confirm_", "", 1)
        try:
            deleted = await run_blocking(delete_repair_case, repair_id)
        except APIError as error:
            if is_google_sheets_busy_error(error):
                await show_sheets_busy_notice(query)
                return REPAIR_MENU_SECTION
            raise
        if not deleted:
            return await show_active_repairs_screen(query, context, notice="❌ Ремонт не найден.")
        await refresh_group_service_today_posts(context.application, force=True)
        return await show_active_repairs_screen(
            query,
            context,
            notice=f"🗑 Ремонт {escape_html(repair_id)} удалён.",
        )
    if data.startswith("repair_hist_point_"):
        return await show_repair_history_machine_picker(query, context, data.replace("repair_hist_point_", "", 1))
    if data.startswith("repair_hist_machine_"):
        return await show_repair_history_screen(query, context, data.replace("repair_hist_machine_", "", 1))
    return REPAIR_MENU_SECTION


async def repair_new_point_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "back_repair_menu":
        return await show_repair_menu(query, context)
    if not query.data.startswith("repair_point_"):
        return REPAIR_NEW_POINT

    point = query.data.replace("repair_point_", "", 1)
    try:
        repair_map = await run_blocking(get_active_repair_record_map)
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(query)
            return REPAIR_NEW_POINT
        raise
    if point in repair_map:
        repair = repair_map[point]
        return await show_repair_new_point(
            query,
            context,
            notice=f"⚪ По точке {point} уже есть активный ремонт {repair.get('id', '—')}. Сначала закрой его или обнови статус.",
        )

    repair_ctx = get_repair_context(context)
    repair_ctx["point"] = point
    return await show_repair_machine_step(query, context, point)


async def repair_new_machine_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    repair_ctx = get_repair_context(context)
    point = repair_ctx.get("point", "")

    if query.data == "repair_new":
        return await show_repair_new_point(query, context)
    if query.data == "repair_back_machine_step":
        return await show_repair_machine_step(query, context, point)
    if query.data == "repair_machine_manual":
        await show_text_screen(
            query,
            context,
            (
                f"🆕 Новый ремонт — {point}\n\n"
                "Если знаешь, напиши модель аппарата.\n"
                "Например: <b>Saeco Aulika</b>\n\n"
                "Серийный номер писать не обязательно."
            ),
            reply_markup=back_markup("repair_back_machine_step"),
            parse_mode="HTML",
        )
        return REPAIR_NEW_MACHINE_QUICK
    if query.data == "repair_machine_unknown":
        machine = repair_ctx.get("machine_candidate")
        if machine is None:
            try:
                machine = await run_blocking(get_or_create_unknown_repair_machine, point)
            except APIError as error:
                if is_google_sheets_busy_error(error):
                    await show_sheets_busy_notice(query)
                    return REPAIR_NEW_MACHINE
                raise
        repair_ctx["machine_id"] = machine.get("id", "")
        repair_ctx["machine_record"] = machine
        return await show_repair_reason_step(query, context, notice="🤷 Модель не указана, можно уточнить позже.")
    if query.data == "repair_machine_single_yes":
        machine = repair_ctx.get("machine_candidate")
        if not machine:
            return await show_repair_machine_step(query, context, point)
        repair_ctx["machine_id"] = machine.get("id", "")
        repair_ctx["machine_record"] = machine
        return await show_repair_reason_step(query, context)
    if query.data.startswith("repair_machine_"):
        machine_id = query.data.replace("repair_machine_", "", 1)
        try:
            machines = await run_blocking(get_point_repair_machines, point, True)
        except APIError as error:
            if is_google_sheets_busy_error(error):
                await show_sheets_busy_notice(query)
                return REPAIR_NEW_MACHINE
            raise
        machine = find_repair_machine(machines, machine_id)
        if not machine:
            return await show_repair_machine_step(query, context, point, notice="⚪ Не удалось найти аппарат, выбери ещё раз.")
        repair_ctx["machine_id"] = machine_id
        repair_ctx["machine_record"] = machine
        return await show_repair_reason_step(query, context)
    return REPAIR_NEW_MACHINE


async def repair_new_machine_quick_handler(update: Update, context):
    message = update.message
    if not message:
        return REPAIR_NEW_MACHINE_QUICK

    repair_ctx = get_repair_context(context)
    point = repair_ctx.get("point", "")
    raw_text = message.text or ""
    try:
        machine = await run_blocking(create_quick_repair_machine, point, raw_text)
    except ValueError:
        await message.reply_text("❌ Напиши модель аппарата или вернись назад.", reply_markup=back_markup("repair_back_machine_step"))
        return REPAIR_NEW_MACHINE_QUICK
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(message)
            return REPAIR_NEW_MACHINE_QUICK
        raise

    repair_ctx["machine_id"] = machine.get("id", "")
    repair_ctx["machine_record"] = machine
    repair_ctx["machine_quick_created"] = True
    await message.reply_text(
        f"☕ Аппарат добавлен: {get_machine_display_name(machine)}\n\n"
        "Теперь выбери причину поломки:",
        reply_markup=build_repair_reason_markup(),
    )
    return REPAIR_NEW_REASON


async def repair_new_reason_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "repair_continue_reason":
        return await show_repair_reason_step(query, context)
    if query.data == "repair_back_machine_step":
        point = get_repair_context(context).get("point", "")
        return await show_repair_machine_step(query, context, point)
    if query.data == "repair_reason_other":
        await show_text_screen(
            query,
            context,
            "✏️ Напиши причину поломки одним сообщением.",
            reply_markup=back_markup("repair_back_reason_picker"),
        )
        return REPAIR_NEW_REASON_CUSTOM
    if query.data == "repair_back_reason_picker":
        return await show_repair_reason_step(query, context)
    if query.data.startswith("repair_reason_"):
        try:
            index = int(query.data.replace("repair_reason_", "", 1))
        except ValueError:
            return REPAIR_NEW_REASON
        if not 0 <= index < len(REPAIR_REASONS):
            return REPAIR_NEW_REASON
        repair_ctx = get_repair_context(context)
        repair_ctx["reason"] = REPAIR_REASONS[index]
        return await show_repair_description_step(query, context)
    return REPAIR_NEW_REASON


async def repair_new_reason_custom_handler(update: Update, context):
    message = update.message
    if not message:
        return REPAIR_NEW_REASON_CUSTOM
    reason = str(message.text or "").strip()
    if not reason:
        await message.reply_text("❌ Напиши причину поломки текстом.", reply_markup=back_markup("repair_back_reason_picker"))
        return REPAIR_NEW_REASON_CUSTOM
    repair_ctx = get_repair_context(context)
    repair_ctx["reason"] = reason
    await message.reply_text(
        "📝 Коротко опиши, что случилось.\n\nМожно пропустить и добавить детали позже.",
        reply_markup=build_repair_description_markup(),
    )
    return REPAIR_NEW_DESCRIPTION


async def repair_new_description_handler(update: Update, context):
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        if query.data == "repair_back_reason_step":
            return await show_repair_reason_step(query, context)
        if query.data == "repair_description_skip":
            get_repair_context(context)["description"] = ""
            return await show_repair_photo_step(query, context)
        return REPAIR_NEW_DESCRIPTION

    message = update.message
    if not message:
        return REPAIR_NEW_DESCRIPTION
    description = str(message.text or "").strip()
    repair_ctx = get_repair_context(context)
    repair_ctx["description"] = description
    await message.reply_text(
        f"{build_progress_text(REPAIR_NEW_FLOW_STEPS, 5)}\n\n📎 Пришли фото поломки, если оно есть.\n\nМожно пропустить и добавить позже из карточки.",
        reply_markup=build_repair_photo_markup(),
    )
    return REPAIR_NEW_PHOTO


async def repair_new_photo_handler(update: Update, context):
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        if query.data == "repair_back_description_step":
            return await show_repair_description_step(query, context)
        if query.data == "repair_photo_skip":
            get_repair_context(context)["photo_file_id"] = ""
            return await show_repair_broken_date_step(query, context)
        return REPAIR_NEW_PHOTO

    message = update.message
    if not message:
        return REPAIR_NEW_PHOTO

    file_id = ""
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document:
        file_id = message.document.file_id

    if not file_id:
        await message.reply_text(
            "❌ Отправь фото/документ или нажми «Пропустить».",
            reply_markup=build_repair_photo_markup(),
        )
        return REPAIR_NEW_PHOTO
    repair_ctx = get_repair_context(context)
    repair_ctx["photo_file_id"] = file_id
    last_service_date = ""
    point = repair_ctx.get("point", "")
    if point:
        try:
            last_service_date = await run_blocking(get_latest_service_date_for_point, point)
        except APIError as error:
            if is_google_sheets_busy_error(error):
                await show_sheets_busy_notice(message)
                return REPAIR_NEW_PHOTO
            raise
    await message.reply_text(
        f"{build_progress_text(REPAIR_NEW_FLOW_STEPS, 6)}\n\n📅 Когда случилась поломка?",
        reply_markup=build_repair_broken_date_markup(last_service_date),
    )
    return REPAIR_NEW_DATE


async def repair_new_date_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    repair_ctx = get_repair_context(context)
    if query.data == "repair_date_back_photo":
        return await show_repair_photo_step(query, context)
    if query.data == "repair_date_today":
        repair_ctx["broken_date"] = today()
        return await complete_repair_creation(
            query,
            context,
            get_actor_label(update),
            repair_ctx.get("photo_file_id", ""),
        )
    if query.data == "repair_date_yesterday":
        repair_ctx["broken_date"] = yesterday()
        return await complete_repair_creation(
            query,
            context,
            get_actor_label(update),
            repair_ctx.get("photo_file_id", ""),
        )
    if query.data == "repair_date_daybefore":
        repair_ctx["broken_date"] = day_before_yesterday()
        return await complete_repair_creation(
            query,
            context,
            get_actor_label(update),
            repair_ctx.get("photo_file_id", ""),
        )
    if query.data == "repair_date_last_service":
        last_service_date = ""
        point = repair_ctx.get("point", "")
        try:
            last_service_date = await run_blocking(get_latest_service_date_for_point, point)
        except APIError as error:
            if is_google_sheets_busy_error(error):
                await show_sheets_busy_notice(query)
                return REPAIR_NEW_DATE
            raise
        if not last_service_date:
            return await show_repair_broken_date_step(query, context, notice="⚪ Последняя дата обслуживания не найдена.")
        repair_ctx["broken_date"] = last_service_date
        return await complete_repair_creation(
            query,
            context,
            get_actor_label(update),
            repair_ctx.get("photo_file_id", ""),
        )
    if query.data == "repair_date_custom":
        await show_text_screen(
            query,
            context,
            "✏️ Введи дату поломки в формате дд.мм или дд.мм.гггг.",
            reply_markup=back_markup("repair_back_date_picker"),
        )
        return REPAIR_NEW_DATE_CUSTOM
    return REPAIR_NEW_DATE


async def repair_new_date_custom_handler(update: Update, context):
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        if query.data == "repair_back_date_picker":
            return await show_repair_broken_date_step(query, context)
        return REPAIR_NEW_DATE_CUSTOM

    message = update.message
    if not message:
        return REPAIR_NEW_DATE_CUSTOM

    parsed, error = validate_manual_date_input(message.text or "")
    if error:
        await message.reply_text(error, reply_markup=back_markup("repair_back_date_picker"))
        return REPAIR_NEW_DATE_CUSTOM

    repair_ctx = get_repair_context(context)
    repair_ctx["broken_date"] = format_date(parsed.date())
    return await complete_repair_creation(
        message,
        context,
        get_actor_label(update),
        repair_ctx.get("photo_file_id", ""),
    )


async def repair_status_update_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    if query.data.startswith("repair_status_back_"):
        repair_id = query.data.replace("repair_status_back_", "", 1)
        return await show_repair_card_screen(query, context, repair_id)
    if not query.data.startswith("repair_status_opt_"):
        return REPAIR_STATUS_UPDATE

    try:
        payload = query.data.replace("repair_status_opt_", "", 1)
        repair_id, index_raw = payload.rsplit("_", 1)
        index = int(index_raw)
    except ValueError:
        return REPAIR_STATUS_UPDATE
    try:
        data = await run_blocking(get_repair_card_data, repair_id)
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(query)
            return REPAIR_STATUS_UPDATE
        raise
    if not data:
        return await show_active_repairs_screen(query, context, notice="❌ Ремонт не найден.")
    current_status = data["repair"].get("Статус", "")
    options = get_repair_status_options(current_status)
    if not 0 <= index < len(options):
        return await show_repair_status_step(query, context, repair_id, notice="⚪ Список статусов обновился, выбери ещё раз.")

    new_status = options[index]
    try:
        repair = await run_blocking(update_repair_status_value, repair_id, new_status)
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(query)
            return REPAIR_STATUS_UPDATE
        raise
    logger.info(
        "repair status changed: user_id=%s repair_id=%s point=%s status=%s",
        getattr(update.effective_user, "id", None),
        repair_id,
        (repair or {}).get("Точка", ""),
        new_status,
    )
    await refresh_group_service_today_posts(context.application, force=True)
    return await show_repair_card_screen(query, context, repair_id, notice=f"✅ Статус обновлён: {escape_html(new_status)}")


async def repair_service_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    repair_ctx = get_repair_context(context)
    repair_id = repair_ctx.get("selected_repair_id", "")

    if query.data == "repair_service_back_card":
        return await show_repair_card_screen(query, context, repair_id)
    if query.data == "repair_center_manual":
        await show_text_screen(
            query,
            context,
            "✏️ Напиши сервис или город одним сообщением.\n\nНапример: Архангельск / ИП Смирнов",
            reply_markup=back_markup("repair_service_back_card"),
        )
        return REPAIR_SET_SERVICE_MANUAL
    if query.data == "repair_center_skip":
        try:
            await run_blocking(update_repair_service_value, repair_id, "")
        except APIError as error:
            if is_google_sheets_busy_error(error):
                await show_sheets_busy_notice(query)
                return REPAIR_SET_SERVICE
            raise
        return await show_repair_card_screen(query, context, repair_id, notice="✅ Сервис очищен.")
    if query.data.startswith("repair_center_"):
        center_id = query.data.replace("repair_center_", "", 1)
        try:
            await run_blocking(update_repair_service_value, repair_id, center_id)
        except APIError as error:
            if is_google_sheets_busy_error(error):
                await show_sheets_busy_notice(query)
                return REPAIR_SET_SERVICE
            raise
        return await show_repair_card_screen(query, context, repair_id, notice="✅ Сервис обновлён.")
    return REPAIR_SET_SERVICE


async def repair_service_manual_handler(update: Update, context):
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        if query.data == "repair_service_back_card":
            repair_id = get_repair_context(context).get("selected_repair_id", "")
            return await show_repair_card_screen(query, context, repair_id)
        return REPAIR_SET_SERVICE_MANUAL

    message = update.message
    if not message:
        return REPAIR_SET_SERVICE_MANUAL

    label = str(message.text or "").strip()
    if not label:
        await message.reply_text("❌ Напиши сервис текстом.", reply_markup=back_markup("repair_service_back_card"))
        return REPAIR_SET_SERVICE_MANUAL

    repair_id = get_repair_context(context).get("selected_repair_id", "")
    try:
        await run_blocking(update_repair_service_value, repair_id, build_manual_repair_service_id(label))
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(message)
            return REPAIR_SET_SERVICE_MANUAL
        raise
    return await send_repair_card_message(message, context, repair_id, notice="✅ Сервис обновлён.")


async def repair_broken_date_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    repair_ctx = get_repair_context(context)
    repair_id = repair_ctx.get("selected_repair_id", "")

    if query.data == "repair_broken_back_menu":
        repair_ctx.pop("date_broken_value", None)
        return await show_repair_dates_menu(query, context, repair_id)
    if query.data == "repair_broken_today":
        value = today()
    elif query.data == "repair_broken_yesterday":
        value = yesterday()
    elif query.data == "repair_broken_daybefore":
        value = day_before_yesterday()
    elif query.data == "repair_broken_last_service":
        value = ""
        try:
            data = await run_blocking(get_repair_card_data, repair_id)
        except APIError as error:
            if is_google_sheets_busy_error(error):
                await show_sheets_busy_notice(query)
                return REPAIR_SET_DATE_BROKEN
            raise
        if not data:
            return await show_active_repairs_screen(query, context, notice="❌ Ремонт не найден.")
        point = str(data["repair"].get("Точка", "") or "").strip()
        try:
            value = await run_blocking(get_latest_service_date_for_point, point)
        except APIError as error:
            if is_google_sheets_busy_error(error):
                await show_sheets_busy_notice(query)
                return REPAIR_SET_DATE_BROKEN
            raise
        if not value:
            return await show_repair_broken_date_edit_step(query, context, repair_id, notice="⚪ Последняя дата обслуживания не найдена.")
    elif query.data == "repair_broken_custom":
        await show_text_screen(
            query,
            context,
            "✏️ Введи дату поломки в формате дд.мм или дд.мм.гггг.",
            reply_markup=back_markup("repair_broken_back_picker"),
        )
        return REPAIR_SET_DATE_BROKEN_CUSTOM
    else:
        return REPAIR_SET_DATE_BROKEN

    try:
        await run_blocking(update_repair_broken_date_value, repair_id, value)
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(query)
            return REPAIR_SET_DATE_BROKEN
        raise
    await refresh_group_service_today_posts(context.application, force=True)
    repair_ctx.pop("date_broken_value", None)
    return await show_repair_dates_menu(query, context, repair_id, notice="✅ Дата поломки обновлена.")


async def repair_broken_date_custom_handler(update: Update, context):
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        if query.data == "repair_broken_back_picker":
            repair_id = get_repair_context(context).get("selected_repair_id", "")
            return await show_repair_broken_date_edit_step(query, context, repair_id)
        return REPAIR_SET_DATE_BROKEN_CUSTOM

    message = update.message
    if not message:
        return REPAIR_SET_DATE_BROKEN_CUSTOM

    parsed, error = validate_manual_date_input(message.text or "")
    if error:
        await message.reply_text(error, reply_markup=back_markup("repair_broken_back_picker"))
        return REPAIR_SET_DATE_BROKEN_CUSTOM

    repair_id = get_repair_context(context).get("selected_repair_id", "")
    try:
        await run_blocking(update_repair_broken_date_value, repair_id, format_date(parsed.date()))
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(message)
            return REPAIR_SET_DATE_BROKEN_CUSTOM
        raise
    await refresh_group_service_today_posts(context.application, force=True)
    get_repair_context(context).pop("date_broken_value", None)
    return await send_repair_dates_menu_message(message, context, repair_id, notice="✅ Дата поломки обновлена.")


async def repair_date_sent_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    repair_ctx = get_repair_context(context)
    repair_id = repair_ctx.get("selected_repair_id", "")

    if query.data == "repair_sent_back_card":
        repair_ctx.pop("date_sent_value", None)
        repair_ctx.pop("date_plan_value", None)
        return await show_repair_dates_menu(query, context, repair_id)
    if query.data == "repair_sent_today":
        repair_ctx["date_sent_value"] = today()
    elif query.data == "repair_sent_yesterday":
        repair_ctx["date_sent_value"] = yesterday()
    elif query.data == "repair_sent_daybefore":
        repair_ctx["date_sent_value"] = day_before_yesterday()
    elif query.data == "repair_sent_skip":
        repair_ctx["date_sent_value"] = ""
    elif query.data == "repair_sent_custom":
        await show_text_screen(
            query,
            context,
            "✏️ Введи дату отправки в формате дд.мм или дд.мм.гггг.",
            reply_markup=back_markup("repair_sent_back_picker"),
        )
        return REPAIR_SET_DATE_SENT_CUSTOM
    else:
        return REPAIR_SET_DATE_SENT

    try:
        await run_blocking(
            update_repair_schedule_values,
            repair_id,
            repair_ctx.get("date_sent_value", ""),
            repair_ctx.get("date_plan_value", ""),
        )
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(query)
            return REPAIR_SET_DATE_SENT
        raise
    repair_ctx.pop("date_sent_value", None)
    repair_ctx.pop("date_plan_value", None)
    return await show_repair_dates_menu(query, context, repair_id, notice="✅ Дата отправки обновлена.")


async def repair_date_sent_custom_handler(update: Update, context):
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        if query.data == "repair_sent_back_picker":
            repair_id = get_repair_context(context).get("selected_repair_id", "")
            return await show_repair_date_sent_step(query, context, repair_id)
        return REPAIR_SET_DATE_SENT_CUSTOM

    message = update.message
    if not message:
        return REPAIR_SET_DATE_SENT_CUSTOM
    parsed, error = validate_manual_date_input(message.text or "")
    if error:
        await message.reply_text(error, reply_markup=back_markup("repair_sent_back_picker"))
        return REPAIR_SET_DATE_SENT_CUSTOM
    repair_ctx = get_repair_context(context)
    repair_ctx["date_sent_value"] = format_date(parsed.date())
    repair_id = repair_ctx.get("selected_repair_id", "")
    try:
        await run_blocking(
            update_repair_schedule_values,
            repair_id,
            repair_ctx.get("date_sent_value", ""),
            repair_ctx.get("date_plan_value", ""),
        )
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(message)
            return REPAIR_SET_DATE_SENT_CUSTOM
        raise
    repair_ctx.pop("date_sent_value", None)
    repair_ctx.pop("date_plan_value", None)
    return await send_repair_dates_menu_message(message, context, repair_id, notice="✅ Дата отправки обновлена.")


async def repair_date_plan_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    repair_ctx = get_repair_context(context)
    repair_id = repair_ctx.get("selected_repair_id", "")

    if query.data == "repair_plan_back_menu":
        repair_ctx.pop("date_sent_value", None)
        repair_ctx.pop("date_plan_value", None)
        return await show_repair_dates_menu(query, context, repair_id)
    if query.data == "repair_plan_3":
        repair_ctx["date_plan_value"] = format_date(now_local() + timedelta(days=3))
    elif query.data == "repair_plan_7":
        repair_ctx["date_plan_value"] = format_date(now_local() + timedelta(days=7))
    elif query.data == "repair_plan_skip":
        repair_ctx["date_plan_value"] = ""
    elif query.data == "repair_plan_custom":
        await show_text_screen(
            query,
            context,
            "✏️ Введи плановую дату в формате дд.мм или дд.мм.гггг.",
            reply_markup=back_markup("repair_plan_back_picker"),
        )
        return REPAIR_SET_DATE_PLAN_CUSTOM
    else:
        return REPAIR_SET_DATE_PLAN

    try:
        await run_blocking(
            update_repair_schedule_values,
            repair_id,
            repair_ctx.get("date_sent_value", ""),
            repair_ctx.get("date_plan_value", ""),
        )
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(query)
            return REPAIR_SET_DATE_PLAN
        raise
    repair_ctx.pop("date_sent_value", None)
    repair_ctx.pop("date_plan_value", None)
    return await show_repair_dates_menu(query, context, repair_id, notice="✅ Плановая готовность обновлена.")


async def repair_date_plan_custom_handler(update: Update, context):
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        if query.data == "repair_plan_back_picker":
            repair_id = get_repair_context(context).get("selected_repair_id", "")
            return await show_repair_date_plan_step(query, context, repair_id)
        return REPAIR_SET_DATE_PLAN_CUSTOM

    message = update.message
    if not message:
        return REPAIR_SET_DATE_PLAN_CUSTOM
    parsed, error = validate_manual_date_input(message.text or "", allow_future=True)
    if error:
        await message.reply_text(error, reply_markup=back_markup("repair_plan_back_picker"))
        return REPAIR_SET_DATE_PLAN_CUSTOM
    repair_ctx = get_repair_context(context)
    repair_ctx["date_plan_value"] = format_date(parsed.date())
    repair_id = repair_ctx.get("selected_repair_id", "")
    try:
        await run_blocking(
            update_repair_schedule_values,
            repair_id,
            repair_ctx.get("date_sent_value", ""),
            repair_ctx.get("date_plan_value", ""),
        )
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(message)
            return REPAIR_SET_DATE_PLAN_CUSTOM
        raise
    repair_ctx.pop("date_sent_value", None)
    repair_ctx.pop("date_plan_value", None)
    return await send_repair_dates_menu_message(message, context, repair_id, notice="✅ Плановая готовность обновлена.")


async def repair_doc_upload_handler(update: Update, context):
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        repair_id = get_repair_context(context).get("selected_repair_id", "")
        if query.data == f"repair_docs_{repair_id}" or query.data == f"repair_open_{repair_id}":
            return await show_repair_docs_screen(query, context, repair_id)
        return REPAIR_DOC_UPLOAD

    message = update.message
    if not message:
        return REPAIR_DOC_UPLOAD

    file_id = ""
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document:
        file_id = message.document.file_id

    if not file_id:
        await message.reply_text("❌ Отправь фото или документ.", reply_markup=back_markup(f"repair_docs_{get_repair_context(context).get('selected_repair_id', '')}"))
        return REPAIR_DOC_UPLOAD

    repair_ctx = get_repair_context(context)
    repair_id = repair_ctx.get("selected_repair_id", "")
    doc_type = repair_ctx.get("doc_type", "Документ")

    try:
        repair_data = await run_blocking(get_repair_card_data, repair_id)
        if not repair_data:
            await message.reply_text("❌ Ремонт не найден.")
            return REPAIR_MENU_SECTION
        await run_blocking(
            add_repair_document,
            {
                "repair_id": repair_id,
                "point": repair_data["repair"].get("Точка", ""),
                "doc_type": doc_type,
                "file_id": file_id,
                "uploaded_by": get_actor_label(update),
            },
        )
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(message)
            return REPAIR_DOC_UPLOAD
        raise
    repair_ctx.pop("doc_type", None)
    return await send_repair_card_message(message, context, repair_id, notice=f"✅ Документ добавлен: {doc_type}")


async def repair_expense_type_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    repair_ctx = get_repair_context(context)
    repair_id = repair_ctx.get("selected_repair_id", "")
    if query.data == "repair_expense_back_card":
        return await show_repair_card_screen(query, context, repair_id)
    if not query.data.startswith("repair_exp_type_"):
        return REPAIR_EXPENSE_TYPE

    try:
        index = int(query.data.replace("repair_exp_type_", "", 1))
    except ValueError:
        return REPAIR_EXPENSE_TYPE
    if not 0 <= index < len(REPAIR_EXPENSE_TYPES):
        return REPAIR_EXPENSE_TYPE

    repair_ctx["expense_type"] = REPAIR_EXPENSE_TYPES[index]
    await show_text_screen(
        query,
        context,
        f"💰 Новый расход — {escape_html(repair_id)}\n\nВведи сумму в рублях, например <b>3500</b>.",
        reply_markup=back_markup("repair_expense_back_type"),
        parse_mode="HTML",
    )
    return REPAIR_EXPENSE_AMOUNT


async def repair_expense_amount_handler(update: Update, context):
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        if query.data == "repair_expense_back_type":
            repair_id = get_repair_context(context).get("selected_repair_id", "")
            return await show_repair_expense_type_step(query, context, repair_id)
        return REPAIR_EXPENSE_AMOUNT

    message = update.message
    if not message:
        return REPAIR_EXPENSE_AMOUNT
    raw_amount = str(message.text or "").strip()
    numeric = parse_numeric_value(raw_amount)
    if numeric is None or numeric <= 0:
        await message.reply_text("❌ Введи сумму больше нуля.", reply_markup=back_markup("repair_expense_back_type"))
        return REPAIR_EXPENSE_AMOUNT

    repair_ctx = get_repair_context(context)
    repair_ctx["expense_amount"] = format_number(numeric)
    await message.reply_text("📝 Добавь описание расхода или отправь «-» если без описания.", reply_markup=back_markup("repair_expense_back_amount"))
    return REPAIR_EXPENSE_DESCRIPTION


async def repair_expense_description_handler(update: Update, context):
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        if query.data == "repair_expense_back_amount":
            await show_text_screen(
                query,
                context,
                "💰 Введи сумму в рублях.",
                reply_markup=back_markup("repair_expense_back_type"),
            )
            return REPAIR_EXPENSE_AMOUNT
        if query.data == "repair_expense_back_description":
            repair_id = get_repair_context(context).get("selected_repair_id", "")
            return await show_repair_card_screen(query, context, repair_id)
        return REPAIR_EXPENSE_DESCRIPTION

    message = update.message
    if not message:
        return REPAIR_EXPENSE_DESCRIPTION
    description = str(message.text or "").strip()
    repair_ctx = get_repair_context(context)
    repair_ctx["expense_description"] = "" if description == "-" else description
    await message.reply_text("Уже оплачено?", reply_markup=build_repair_expense_paid_markup())
    return REPAIR_EXPENSE_PAID


async def repair_expense_paid_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    repair_ctx = get_repair_context(context)
    repair_id = repair_ctx.get("selected_repair_id", "")
    if query.data == "repair_expense_back_description":
        await show_text_screen(
            query,
            context,
            "📝 Добавь описание расхода или отправь «-».",
            reply_markup=back_markup("repair_expense_back_amount"),
        )
        return REPAIR_EXPENSE_DESCRIPTION
    if query.data not in {"repair_exp_paid_yes", "repair_exp_paid_no"}:
        return REPAIR_EXPENSE_PAID

    try:
        entry, repair = await run_blocking(
            add_repair_expense,
            {
                "repair_id": repair_id,
                "expense_type": repair_ctx.get("expense_type", ""),
                "amount": repair_ctx.get("expense_amount", ""),
                "description": repair_ctx.get("expense_description", ""),
                "paid": query.data == "repair_exp_paid_yes",
                "marked_by": get_actor_label(update),
            },
        )
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(query)
            return REPAIR_EXPENSE_PAID
        raise
    repair_ctx.pop("expense_type", None)
    repair_ctx.pop("expense_amount", None)
    repair_ctx.pop("expense_description", None)
    amount_text = format_money_spaced(entry.get("Сумма", ""))
    return await show_repair_card_screen(
        query,
        context,
        repair_id,
        notice=f"✅ Расход добавлен: {escape_html(entry.get('Тип расхода', ''))} — {escape_html(amount_text)}",
    )


async def show_service_section_menu(query, context):
    keyboard = [
        [InlineKeyboardButton("📝 Начать обслуживание", callback_data="service_start", style="primary")],
        [InlineKeyboardButton("🛠 Ремонт", callback_data="repair")],
        [InlineKeyboardButton("📋 Информация по точкам", callback_data="info")],
        [InlineKeyboardButton("⚠️ Проблемные точки", callback_data="service_problem_points")],
        [InlineKeyboardButton("✏️ Исправить запись", callback_data="service_fix")],
        [InlineKeyboardButton("💰 Проезд", callback_data="travel")],
        [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
    ]
    await show_text_screen(query, context, "🔧 Обслуживание\n\nВыберите действие:", reply_markup=InlineKeyboardMarkup(keyboard))
    return SERVICE_MENU_SECTION


async def show_service_fix_menu(query, context):
    keyboard = [
        [InlineKeyboardButton("🕘 Последняя запись", callback_data="service_fix_latest")],
        [InlineKeyboardButton("📅 Выбрать дату", callback_data="service_fix_by_date")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_service_menu")],
        [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
    ]
    await show_text_screen(
        query,
        context,
        "✏️ Исправить запись\n\n"
        "Можно быстро открыть последнюю запись или выбрать дату, чтобы изменить или удалить нужную.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return SERVICE_MENU_SECTION


def get_salary_task_context(context):
    return context.user_data.setdefault("salary_task", {})


def clear_salary_task_context(context):
    context.user_data.pop("salary_task", None)


def build_salary_task_confirm_text(task):
    return "\n".join(
        [
            "🧰 Задача / доплата",
            "",
            f"👤 {task.get('who', '?')}",
            f"📅 {task.get('date', '?')}",
            f"📝 {task.get('description', '—')}",
            f"💰 {format_money_spaced(task.get('amount', ''))}",
        ]
    )


async def start_salary_task_flow(query, context, preset_worker=None, return_context=None):
    salary_task = get_salary_task_context(context)
    salary_task.clear()
    if return_context:
        salary_task.update(return_context)
    if preset_worker:
        salary_task["who"] = preset_worker
        return await show_salary_task_date_menu(query, context)
    return await show_salary_task_worker_menu(query, context)


async def show_salary_task_worker_menu(query, context, notice=None):
    text = "🧰 Задача / доплата\n\nКому добавить выплату?"
    if notice:
        text = f"{notice}\n\n{text}"
    keyboard = [
        [InlineKeyboardButton(worker, callback_data=f"salary_task_worker_{idx}")]
        for idx, worker in enumerate(get_paid_workers())
    ]
    keyboard.append([InlineKeyboardButton("⬅️ К отчётам", callback_data="back_reports_menu")])
    keyboard.append([InlineKeyboardButton("🏠 В меню", callback_data="back_main")])
    await show_text_screen(query, context, text, reply_markup=InlineKeyboardMarkup(keyboard))
    return SALARY_TASK_WORKER


async def show_salary_task_date_menu(query, context, notice=None):
    salary_task = get_salary_task_context(context)
    worker = salary_task.get("who")
    if not worker:
        return await show_salary_task_worker_menu(query, context)

    period_key = salary_task.get("return_period") if salary_task.get("return_mode") == "payout" else None
    period_line = f"\n🗓 Месяц: {format_period_label(period_key)}\n" if period_key else "\n"
    text = f"🧰 Задача / доплата\n\n👤 {worker}{period_line}\nЗа какую дату добавить?"
    if notice:
        text = f"{notice}\n\n{text}"
    keyboard = [
        [InlineKeyboardButton(f"Сегодня ({today()})", callback_data="salary_task_date_today")],
        [InlineKeyboardButton(f"Вчера ({yesterday()})", callback_data="salary_task_date_yesterday")],
        [InlineKeyboardButton(f"Позавчера ({day_before_yesterday()})", callback_data="salary_task_date_daybefore")],
        [InlineKeyboardButton("✏️ Другая дата", callback_data="salary_task_date_custom")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_salary_task_worker")],
        [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
    ]
    await show_text_screen(query, context, text, reply_markup=InlineKeyboardMarkup(keyboard))
    return SALARY_TASK_DATE


async def show_salary_task_description_prompt(query, context, notice=None):
    salary_task = get_salary_task_context(context)
    text = (
        "🧰 Задача / доплата\n\n"
        f"👤 {salary_task.get('who', '?')}\n"
        f"📅 {salary_task.get('date', '?')}\n\n"
        "Напишите коротко, за что выплата.\n"
        "Например: починка терминала."
    )
    if notice:
        text = f"{notice}\n\n{text}"
    if hasattr(query, "reply_text"):
        await query.reply_text(text, reply_markup=back_markup("back_salary_task_date"))
    else:
        await show_text_screen(query, context, text, reply_markup=back_markup("back_salary_task_date"))
    return SALARY_TASK_DESCRIPTION


async def show_salary_task_amount_prompt(query, context, notice=None):
    salary_task = get_salary_task_context(context)
    text = (
        "🧰 Задача / доплата\n\n"
        f"👤 {salary_task.get('who', '?')}\n"
        f"📅 {salary_task.get('date', '?')}\n"
        f"📝 {salary_task.get('description', '—')}\n\n"
        "Введите сумму в рублях.\n"
        "Например: 500"
    )
    if notice:
        text = f"{notice}\n\n{text}"
    if hasattr(query, "reply_text"):
        await query.reply_text(text, reply_markup=back_markup("back_salary_task_description"))
    else:
        await show_text_screen(query, context, text, reply_markup=back_markup("back_salary_task_description"))
    return SALARY_TASK_AMOUNT


async def show_salary_task_confirm_screen(query, context, notice=None):
    salary_task = get_salary_task_context(context)
    text = build_salary_task_confirm_text(salary_task)
    if notice:
        text = f"{notice}\n\n{text}"
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Сохранить", callback_data="salary_task_save", style="primary")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_salary_task_amount")],
            [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
        ]
    )
    if hasattr(query, "reply_text"):
        await query.reply_text(text, reply_markup=keyboard)
    else:
        await show_text_screen(query, context, text, reply_markup=keyboard)
    return SALARY_TASK_CONFIRM


async def show_salary_task_saved_screen(query, context, entry):
    worker = entry.get("who", "")
    text = "✅ Задача добавлена в ЗП\n\n" + build_salary_task_confirm_text(entry)
    keyboard_rows = []
    user_id = getattr(getattr(query, "from_user", None), "id", None)
    if worker in get_paid_workers() and user_id in get_payout_viewer_ids():
        keyboard_rows.append(
            [InlineKeyboardButton(get_salary_button_label(worker), callback_data=f"report_salary_worker:{worker}")]
        )
    keyboard_rows.append([InlineKeyboardButton("➕ Ещё задача", callback_data="report_salary_task")])
    keyboard_rows.append([InlineKeyboardButton("⬅️ К отчётам", callback_data="back_reports_menu")])
    keyboard_rows.append([InlineKeyboardButton("🏠 В меню", callback_data="back_main")])
    clear_salary_task_context(context)
    await show_text_screen(query, context, text, reply_markup=InlineKeyboardMarkup(keyboard_rows))
    return REPORT_MENU_SECTION


def get_default_payout_period_key():
    config = get_report_period_config(get_default_salary_period_code())
    if config and config.get("period_key"):
        return config["period_key"]
    return get_previous_month_period_key() or current_period_key()


def is_payout_editor_target(target):
    user_id = getattr(getattr(target, "from_user", None), "id", None)
    return user_id in get_payout_editor_ids()


def get_payout_screen_access(query, settlement):
    return is_payout_editor_target(query) and not is_payout_period_locked(settlement)


def build_payout_delete_markup(confirm_callback, back_screen):
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Да, удалить", callback_data=confirm_callback, style="danger")],
            [InlineKeyboardButton("⬅️ Назад", callback_data=f"payout_screen:{back_screen}")],
        ]
    )


def get_payout_retry_callback(screen):
    screen = str(screen or "overview")
    if screen == "overview":
        return "payout_open"
    return f"payout_screen:{screen}"


def build_payout_failure_markup(retry_callback, back_callback):
    keyboard = []
    if retry_callback:
        keyboard.append([InlineKeyboardButton("🔄 Повторить", callback_data=retry_callback)])
    if back_callback:
        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data=back_callback)])
    keyboard.append([InlineKeyboardButton("🏠 В меню", callback_data="back_main")])
    return InlineKeyboardMarkup(keyboard)


async def show_payout_failure_screen(query, context, text, retry_callback, back_callback):
    await show_text_screen(
        query,
        context,
        text,
        reply_markup=build_payout_failure_markup(retry_callback, back_callback),
    )
    return PAYOUT_SCREEN


async def load_payout_settlement_with_timeout(period_key, worker=None):
    worker = str(worker or get_default_payout_worker_name()).strip()
    return await asyncio.wait_for(
        run_blocking(compute_payout_settlement, period_key, worker=worker),
        timeout=PAYOUT_SCREEN_LOAD_TIMEOUT_SECONDS,
    )


async def load_payout_sources_with_timeout():
    return await asyncio.wait_for(
        run_blocking(build_payout_sources),
        timeout=PAYOUT_SCREEN_LOAD_TIMEOUT_SECONDS,
    )


async def show_payout_overview_screen(query, context, period_key=None, notice=None):
    payout = get_payout_context(context)
    payout["worker"] = payout.get("worker") or get_default_payout_worker_name()
    payout["period"] = period_key or payout.get("period") or get_default_payout_period_key()
    payout["screen"] = "overview"
    worker = get_selected_payout_worker(context)
    await show_loading_state(query, context, f"Собираю итог по {worker}...")
    try:
        settlement = await asyncio.wait_for(
            run_blocking(compute_payout_settlement, payout["period"], worker=worker),
            timeout=PAYOUT_SCREEN_LOAD_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning("Payout overview timed out for period %s", payout["period"])
        return await show_payout_failure_screen(
            query,
            context,
            "⏳ Экран выплат отвечает слишком долго. Попробуй ещё раз.",
            retry_callback="payout_open",
            back_callback="back_reports_menu",
        )
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(query, context, retry_callback="payout_open", back_callback="back_reports_menu")
            return PAYOUT_SCREEN
        logger.exception("Failed to load payout overview for period %s", payout["period"])
        return await show_payout_failure_screen(
            query,
            context,
            "❌ Не удалось открыть экран выплат.",
            retry_callback="payout_open",
            back_callback="back_reports_menu",
        )
    except Exception:
        logger.exception("Failed to load payout overview for period %s", payout["period"])
        return await show_payout_failure_screen(
            query,
            context,
            "❌ Не удалось открыть экран выплат.",
            retry_callback="payout_open",
            back_callback="back_reports_menu",
        )
    can_edit = get_payout_screen_access(query, settlement)
    try:
        await show_text_screen(
            query,
            context,
            build_payout_overview_text(settlement, notice=notice),
            reply_markup=build_payout_overview_markup(can_edit, is_payout_editor_target(query), settlement),
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("Failed to render payout overview for period %s", payout["period"])
        return await show_payout_failure_screen(
            query,
            context,
            "❌ Экран выплат не удалось отрисовать.",
            retry_callback="payout_open",
            back_callback="back_reports_menu",
        )
    return PAYOUT_SCREEN


async def show_payout_month_menu_screen(query, context):
    payout = get_payout_context(context)
    payout["worker"] = payout.get("worker") or get_default_payout_worker_name()
    payout["period"] = payout.get("period") or get_default_payout_period_key()
    payout["screen"] = "months"
    await show_loading_state(query, context, "Загружаю месяцы...")
    period_keys = recent_completed_period_keys(6)
    worker = get_selected_payout_worker(context)
    try:
        sources = await load_payout_sources_with_timeout()
        await show_text_screen(
            query,
            context,
            build_payout_month_menu_text(worker),
            reply_markup=build_payout_month_menu_markup(period_keys, sources, worker),
            parse_mode="HTML",
        )
    except asyncio.TimeoutError:
        logger.warning("Payout months screen timed out")
        return await show_payout_failure_screen(
            query,
            context,
            "⏳ Список месяцев загружается слишком долго. Попробуй ещё раз.",
            retry_callback="payout_screen:months",
            back_callback="payout_screen:overview",
        )
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(
                query,
                context,
                retry_callback="payout_screen:months",
                back_callback="payout_screen:overview",
            )
            return PAYOUT_SCREEN
        logger.exception("Failed to load payout months")
        return await show_payout_failure_screen(
            query,
            context,
            "❌ Не удалось загрузить месяцы выплат.",
            retry_callback="payout_screen:months",
            back_callback="payout_screen:overview",
        )
    except Exception:
        logger.exception("Failed to load payout months")
        return await show_payout_failure_screen(
            query,
            context,
            "❌ Не удалось загрузить месяцы выплат.",
            retry_callback="payout_screen:months",
            back_callback="payout_screen:overview",
        )
    return PAYOUT_SCREEN


def build_payout_screen_payload(context, target, settlement, screen, notice=None):
    can_edit = get_payout_screen_access(target, settlement)
    payout = get_payout_context(context)
    effective_screen = screen

    if screen == "services":
        text = build_payout_date_menu_text("services", settlement)
        markup = build_payout_date_menu_markup("services", settlement, can_edit)
    elif screen == "services_points":
        date_str = payout.get("services_date")
        if not date_str:
            return build_payout_screen_payload(context, target, settlement, "services", notice=notice)
        text = build_payout_point_menu_text("services", settlement, date_str)
        markup = build_payout_point_menu_markup("services", settlement, date_str)
    elif screen == "services_entries":
        date_str = payout.get("services_date")
        point = payout.get("services_point")
        if not date_str:
            return build_payout_screen_payload(context, target, settlement, "services", notice=notice)
        if not point:
            return build_payout_screen_payload(context, target, settlement, "services_points", notice=notice)
        text = build_payout_entries_menu_text("services", settlement, date_str, point=point)
        markup = build_payout_entries_menu_markup("services", settlement, can_edit, date_str, point=point)
    elif screen == "purchases":
        text = build_payout_date_menu_text("purchases", settlement)
        markup = build_payout_date_menu_markup("purchases", settlement, can_edit)
    elif screen == "purchases_points":
        date_str = payout.get("purchases_date")
        if not date_str:
            return build_payout_screen_payload(context, target, settlement, "purchases", notice=notice)
        text = build_payout_point_menu_text("purchases", settlement, date_str)
        markup = build_payout_point_menu_markup("purchases", settlement, date_str)
    elif screen == "purchases_entries":
        date_str = payout.get("purchases_date")
        point = payout.get("purchases_point")
        if not date_str:
            return build_payout_screen_payload(context, target, settlement, "purchases", notice=notice)
        if not point:
            return build_payout_screen_payload(context, target, settlement, "purchases_points", notice=notice)
        text = build_payout_entries_menu_text("purchases", settlement, date_str, point=point)
        markup = build_payout_entries_menu_markup("purchases", settlement, can_edit, date_str, point=point)
    elif screen == "travels":
        text = build_payout_date_menu_text("travels", settlement)
        markup = build_payout_date_menu_markup("travels", settlement, can_edit)
    elif screen == "travels_entries":
        date_str = payout.get("travels_date")
        if not date_str:
            return build_payout_screen_payload(context, target, settlement, "travels", notice=notice)
        text = build_payout_entries_menu_text("travels", settlement, date_str)
        markup = build_payout_entries_menu_markup("travels", settlement, can_edit, date_str)
    elif screen == "tasks":
        text = build_payout_date_menu_text("tasks", settlement)
        markup = build_payout_date_menu_markup("tasks", settlement, can_edit)
    elif screen == "tasks_entries":
        date_str = payout.get("tasks_date")
        if not date_str:
            return build_payout_screen_payload(context, target, settlement, "tasks", notice=notice)
        text = build_payout_entries_menu_text("tasks", settlement, date_str)
        markup = build_payout_entries_menu_markup("tasks", settlement, can_edit, date_str)
    else:
        effective_screen = "overview"
        text = build_payout_overview_text(settlement, notice=notice)
        markup = build_payout_overview_markup(can_edit, is_payout_editor_target(target), settlement)
        return effective_screen, text, markup

    if notice:
        text = f"{escape_html(notice)}\n\n{text}"
    return effective_screen, text, markup


async def show_payout_services_screen(query, context, notice=None):
    payout = get_payout_context(context)
    payout["worker"] = payout.get("worker") or get_default_payout_worker_name()
    payout["period"] = payout.get("period") or get_default_payout_period_key()
    payout["screen"] = "services"
    await show_loading_state(query, context, "Загружаю обслуживания...")
    settlement = await run_blocking(compute_payout_settlement, payout["period"], worker=get_selected_payout_worker(context))
    _, text, markup = build_payout_screen_payload(context, query, settlement, "services", notice=notice)
    await show_text_screen(
        query,
        context,
        text,
        reply_markup=markup,
        parse_mode="HTML",
    )
    return PAYOUT_SCREEN


async def show_payout_purchases_screen(query, context, notice=None):
    payout = get_payout_context(context)
    payout["worker"] = payout.get("worker") or get_default_payout_worker_name()
    payout["period"] = payout.get("period") or get_default_payout_period_key()
    payout["screen"] = "purchases"
    await show_loading_state(query, context, "Загружаю закупки...")
    settlement = await run_blocking(compute_payout_settlement, payout["period"], worker=get_selected_payout_worker(context))
    _, text, markup = build_payout_screen_payload(context, query, settlement, "purchases", notice=notice)
    await show_text_screen(
        query,
        context,
        text,
        reply_markup=markup,
        parse_mode="HTML",
    )
    return PAYOUT_SCREEN


async def show_payout_travels_screen(query, context, notice=None):
    payout = get_payout_context(context)
    payout["worker"] = payout.get("worker") or get_default_payout_worker_name()
    payout["period"] = payout.get("period") or get_default_payout_period_key()
    payout["screen"] = "travels"
    await show_loading_state(query, context, "Загружаю проезд...")
    settlement = await run_blocking(compute_payout_settlement, payout["period"], worker=get_selected_payout_worker(context))
    _, text, markup = build_payout_screen_payload(context, query, settlement, "travels", notice=notice)
    await show_text_screen(
        query,
        context,
        text,
        reply_markup=markup,
        parse_mode="HTML",
    )
    return PAYOUT_SCREEN


async def show_payout_salary_tasks_screen(query, context, notice=None):
    payout = get_payout_context(context)
    payout["worker"] = payout.get("worker") or get_default_payout_worker_name()
    payout["period"] = payout.get("period") or get_default_payout_period_key()
    payout["screen"] = "tasks"
    await show_loading_state(query, context, "Загружаю допзадачи...")
    settlement = await run_blocking(compute_payout_settlement, payout["period"], worker=get_selected_payout_worker(context))
    _, text, markup = build_payout_screen_payload(context, query, settlement, "tasks", notice=notice)
    await show_text_screen(
        query,
        context,
        text,
        reply_markup=markup,
        parse_mode="HTML",
    )
    return PAYOUT_SCREEN


async def show_payout_screen(query, context, screen="overview", period_key=None, notice=None):
    if screen == "months":
        return await show_payout_month_menu_screen(query, context)
    if period_key:
        get_payout_context(context)["period"] = period_key
    payout = get_payout_context(context)
    payout["worker"] = payout.get("worker") or get_default_payout_worker_name()
    payout["period"] = payout.get("period") or get_default_payout_period_key()
    payout["screen"] = screen
    worker = get_selected_payout_worker(context)
    await show_loading_state(query, context, f"Обновляю экран выплат: {worker}...")
    retry_callback = get_payout_retry_callback(screen)
    back_callback = "payout_screen:overview" if screen != "overview" else "back_reports_menu"
    try:
        settlement = await asyncio.wait_for(
            run_blocking(compute_payout_settlement, payout["period"], worker=worker),
            timeout=PAYOUT_SCREEN_LOAD_TIMEOUT_SECONDS,
        )
        effective_screen, text, markup = build_payout_screen_payload(context, query, settlement, screen, notice=notice)
        payout["screen"] = effective_screen
        await show_text_screen(query, context, text, reply_markup=markup, parse_mode="HTML")
    except asyncio.TimeoutError:
        logger.warning("Payout screen %s timed out for period %s", screen, payout["period"])
        return await show_payout_failure_screen(
            query,
            context,
            "⏳ Экран выплат отвечает слишком долго. Попробуй ещё раз.",
            retry_callback=retry_callback,
            back_callback=back_callback,
        )
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(query, context, retry_callback=retry_callback, back_callback=back_callback)
            return PAYOUT_SCREEN
        logger.exception("Failed to open payout screen %s for period %s", screen, payout["period"])
        return await show_payout_failure_screen(
            query,
            context,
            "❌ Не удалось открыть экран выплат.",
            retry_callback=retry_callback,
            back_callback=back_callback,
        )
    except Exception:
        logger.exception("Failed to open payout screen %s for period %s", screen, payout["period"])
        return await show_payout_failure_screen(
            query,
            context,
            "❌ Не удалось открыть экран выплат.",
            retry_callback=retry_callback,
            back_callback=back_callback,
        )
    return PAYOUT_SCREEN


async def render_payout_screen_target(target, context, screen="overview", period_key=None, notice=None):
    payout = get_payout_context(context)
    payout["worker"] = payout.get("worker") or get_default_payout_worker_name()
    payout["period"] = period_key or payout.get("period") or get_default_payout_period_key()
    payout["screen"] = screen

    if screen == "months":
        sources = await run_blocking(build_payout_sources)
        await render_text_screen(
            target,
            context,
            build_payout_month_menu_text(get_selected_payout_worker(context)),
            reply_markup=build_payout_month_menu_markup(
                recent_completed_period_keys(6),
                sources,
                get_selected_payout_worker(context),
            ),
            parse_mode="HTML",
        )
        return PAYOUT_SCREEN

    settlement = await run_blocking(
        compute_payout_settlement,
        payout["period"],
        worker=get_selected_payout_worker(context),
    )
    effective_screen, text, markup = build_payout_screen_payload(context, target, settlement, screen, notice=notice)
    payout["screen"] = effective_screen
    await render_text_screen(target, context, text, reply_markup=markup, parse_mode="HTML")
    return PAYOUT_SCREEN


async def get_payout_service_entry(period_key, row_num, worker, mode="service"):
    services = await run_blocking(get_all_services_with_rows)
    entry = next((item for item in services if int(item.get("__row", 0)) == row_num), None)
    if not entry or not is_date_in_period_key(entry.get("Дата", ""), period_key):
        return None
    if mode == "purchase":
        if str(entry.get("Кто", "")).strip() != worker:
            return None
        has_purchase = parse_numeric_value(entry.get("Сумма закупок", "")) or str(entry.get("Закупки", "")).strip()
        return entry if has_purchase else None
    return entry if worker in get_service_salary_workers(entry) else None


async def begin_payout_service_edit(query, context, row_num, screen):
    payout = get_payout_context(context)
    period_key = payout.get("period") or get_default_payout_period_key()
    worker = get_selected_payout_worker(context)
    entry = await get_payout_service_entry(
        period_key,
        row_num,
        worker,
        mode="purchase" if str(screen).startswith("purchases") else "service",
    )
    if not entry:
        return await show_payout_screen(query, context, screen=screen, notice="❌ Запись не найдена.")
    context.user_data["delete"] = {
        "entry": entry,
        **build_payout_return_context(period_key, screen=screen),
    }
    return await begin_service_edit_from_entry(query, context)


async def confirm_payout_service_delete(query, context, row_num, screen):
    payout = get_payout_context(context)
    period_key = payout.get("period") or get_default_payout_period_key()
    worker = get_selected_payout_worker(context)
    entry = await get_payout_service_entry(
        period_key,
        row_num,
        worker,
        mode="purchase" if str(screen).startswith("purchases") else "service",
    )
    if not entry:
        return await show_payout_screen(query, context, screen=screen, notice="❌ Запись не найдена.")
    title = "🗑 Удалить запись обслуживания?"
    if str(screen).startswith("purchases"):
        title = "🗑 Удалить всю запись обслуживания вместе с закупками?"
    await show_text_screen(
        query,
        context,
        f"{title}\n\n{build_service_entry_text(entry)}",
        reply_markup=build_payout_delete_markup(f"payout_service_del_yes:{row_num}:{screen}", screen),
    )
    return PAYOUT_SCREEN


async def delete_payout_service(query, context, row_num, screen):
    payout = get_payout_context(context)
    period_key = payout.get("period") or get_default_payout_period_key()
    worker = get_selected_payout_worker(context)
    entry = await get_payout_service_entry(
        period_key,
        row_num,
        worker,
        mode="purchase" if str(screen).startswith("purchases") else "service",
    )
    if not entry:
        return await show_payout_screen(query, context, screen=screen, notice="❌ Запись не найдена.")
    photos = await run_blocking(get_all_photos_with_rows)
    photo_entry = find_matching_photo_row(entry, photos)
    await run_blocking(delete_service_entry, entry["__row"], photo_entry["__row"] if photo_entry else None)
    try:
        await refresh_group_service_today_posts(context.application, force=True)
    except Exception:
        logger.exception("Failed to refresh group service-today post after payout service delete")
    return await show_payout_screen(query, context, screen=screen, notice="✅ Запись удалена.")


async def get_payout_travel_entry(period_key, row_num, worker):
    travels = await run_blocking(get_all_travels_with_rows)
    entry = next((item for item in travels if int(item.get("__row", 0)) == row_num), None)
    if not entry:
        return None
    if str(entry.get("Кто", "")).strip() != worker:
        return None
    if not is_date_in_period_key(entry.get("Дата", ""), period_key):
        return None
    return entry


async def get_payout_salary_task_entry(period_key, row_num, worker):
    tasks = await run_blocking(get_all_salary_tasks_with_rows)
    entry = next((item for item in tasks if int(item.get("__row", 0)) == row_num), None)
    if not entry:
        return None
    if str(entry.get("Кто", "")).strip() != worker:
        return None
    if not is_date_in_period_key(entry.get("Дата", ""), period_key):
        return None
    return entry


async def show_payout_travel_edit_card(query, context, row_num, notice=None):
    payout = get_payout_context(context)
    period_key = payout.get("period") or get_default_payout_period_key()
    entry = await get_payout_travel_entry(period_key, row_num, get_selected_payout_worker(context))
    if not entry:
        return await show_payout_screen(query, context, screen="travels_entries", notice="❌ Запись проезда не найдена.")
    payout["editing_travel_row"] = row_num
    text_lines = ["<b>🚌 Редактирование проезда</b>"]
    if notice:
        text_lines.extend([escape_html(notice), ""])
    text_lines.extend(
        [
            f"📅 {escape_html(entry.get('Дата', ''))}",
            f"💰 {format_money_spaced(entry.get('Сумма', 0))}",
            "",
            "Что изменить?",
        ]
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💰 Изменить сумму", callback_data=f"payout_travel_edit_amount:{row_num}")],
            [InlineKeyboardButton("📅 Изменить дату", callback_data=f"payout_travel_edit_date:{row_num}")],
            [InlineKeyboardButton("🗑 Удалить", callback_data=f"payout_travel_del:{row_num}")],
            [InlineKeyboardButton("⬅️ К проезду", callback_data="payout_screen:travels_entries")],
        ]
    )
    await show_text_screen(query, context, "\n".join(text_lines), reply_markup=keyboard, parse_mode="HTML")
    return PAYOUT_SCREEN


async def show_payout_salary_task_edit_card(query, context, row_num, notice=None):
    payout = get_payout_context(context)
    period_key = payout.get("period") or get_default_payout_period_key()
    entry = await get_payout_salary_task_entry(period_key, row_num, get_selected_payout_worker(context))
    if not entry:
        return await show_payout_screen(query, context, screen="tasks_entries", notice="❌ Допзадача не найдена.")
    payout["editing_task_row"] = row_num
    description = str(entry.get("Описание", "")).strip() or "Без описания"
    text_lines = ["<b>🧰 Редактирование допзадачи</b>"]
    if notice:
        text_lines.extend([escape_html(notice), ""])
    text_lines.extend(
        [
            f"📅 {escape_html(entry.get('Дата', ''))}",
            f"📝 {escape_html(description)}",
            f"💰 {format_money_spaced(entry.get('Сумма', 0))}",
            "",
            "Что изменить?",
        ]
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📝 Изменить описание", callback_data=f"payout_task_edit_description:{row_num}")],
            [InlineKeyboardButton("💰 Изменить сумму", callback_data=f"payout_task_edit_amount:{row_num}")],
            [InlineKeyboardButton("📅 Изменить дату", callback_data=f"payout_task_edit_date:{row_num}")],
            [InlineKeyboardButton("🗑 Удалить", callback_data=f"payout_task_del:{row_num}")],
            [InlineKeyboardButton("⬅️ К допзадачам", callback_data="payout_screen:tasks_entries")],
        ]
    )
    await show_text_screen(query, context, "\n".join(text_lines), reply_markup=keyboard, parse_mode="HTML")
    return PAYOUT_SCREEN


async def start_payout_service_add_flow(query, context):
    payout = get_payout_context(context)
    period_key = payout.get("period") or get_default_payout_period_key()
    return_screen = payout.get("screen", "services")
    worker = get_selected_payout_worker(context)
    if get_payout_screen_section(return_screen) != "services":
        return_screen = "services"
    context.user_data["svc"] = {
        "who": worker,
        "locked_who": True,
        "allowed_period": period_key,
        **{
            **build_payout_return_context(period_key, screen=return_screen),
            "return_worker": worker,
        },
    }
    return await show_service_date_menu(query, context)


async def start_payout_travel_add_flow(query, context):
    payout = get_payout_context(context)
    period_key = payout.get("period") or get_default_payout_period_key()
    return_screen = payout.get("screen", "travels")
    worker = get_selected_payout_worker(context)
    if get_payout_screen_section(return_screen) != "travels":
        return_screen = "travels"
    context.user_data["travel_mode"] = "add"
    context.user_data["travel_who"] = worker
    context.user_data.pop("travel_date", None)
    context.user_data["travel_allowed_period"] = period_key
    context.user_data["travel_return_mode"] = "payout"
    context.user_data["travel_return_period"] = period_key
    context.user_data["travel_return_screen"] = return_screen
    context.user_data["travel_return_worker"] = worker
    return await show_travel_date_menu(query, context)


async def start_payout_salary_task_add_flow(query, context):
    payout = get_payout_context(context)
    period_key = payout.get("period") or get_default_payout_period_key()
    return_screen = payout.get("screen", "tasks")
    worker = get_selected_payout_worker(context)
    if get_payout_screen_section(return_screen) != "tasks":
        return_screen = "tasks"
    return await start_salary_task_flow(
        query,
        context,
        preset_worker=worker,
        return_context={
            **build_payout_return_context(period_key, screen=return_screen),
            "return_worker": worker,
        },
    )


async def payout_handler(update: Update, context):
    query = update.callback_query
    if not is_payout_viewer(update):
        await deny_callback_access(query)
        return REPORT_MENU_SECTION
    await query.answer()
    data = query.data
    payout = get_payout_context(context)
    payout["worker"] = payout.get("worker") or get_default_payout_worker_name()
    worker = get_selected_payout_worker(context)
    period_key = payout.get("period") or get_default_payout_period_key()

    if data == "back_main":
        clear_payout_context(context)
        return await start(update, context)
    if data == "back_reports_menu":
        clear_payout_context(context)
        return await show_reports_section_menu(query, context)
    if data == "payout_open":
        return await show_payout_overview_screen(query, context)
    if data.startswith("payout_month:"):
        return await show_payout_overview_screen(query, context, period_key=data.split(":", 1)[1])
    if data.startswith("payout_screen:"):
        return await show_payout_screen(query, context, screen=data.split(":", 1)[1])
    if data.startswith("payout_date:"):
        _, section, date_str = data.split(":", 2)
        payout[f"{section}_date"] = date_str
        if section in {"services", "purchases"}:
            payout.pop(f"{section}_point", None)
            return await show_payout_screen(query, context, screen=f"{section}_points")
        return await show_payout_screen(query, context, screen=f"{section}_entries")
    if data.startswith("payout_point:"):
        _, section, date_str, token = data.split(":", 3)
        settlement = await run_blocking(compute_payout_settlement, period_key, worker=worker)
        point_groups = build_payout_point_groups(settlement, section, date_str)
        point = next(
            (
                bucket["point"]
                for bucket in point_groups
                if build_payout_point_token(section, date_str, bucket["point"]) == token
            ),
            None,
        )
        payout[f"{section}_date"] = date_str
        if not point:
            return await show_payout_screen(query, context, screen=f"{section}_points", notice="❌ Точка не найдена.")
        payout[f"{section}_point"] = point
        return await show_payout_screen(query, context, screen=f"{section}_entries")

    if data == "payout_correction":
        settlement = await run_blocking(compute_payout_settlement, period_key, worker=worker)
        if not get_payout_screen_access(query, settlement):
            await query.answer("Только редактор выплат", show_alert=True)
            return PAYOUT_SCREEN
        payout["screen"] = "overview"
        text = (
            f"<b>✏️ Ручная корректировка — {escape_html(settlement['period_label'])}</b>\n\n"
            "Это ручной плюс или минус к итоговой выплате за месяц.\n"
            "Например: аванс, штраф, доплата, округление.\n\n"
            f"Текущая сумма: {format_money_spaced(settlement['correction'])}\n\n"
            "Введите сумму, например <code>-500</code> или <code>300</code>."
        )
        await show_text_screen(
            query,
            context,
            text,
            reply_markup=back_markup("payout_screen:overview"),
            parse_mode="HTML",
        )
        return PAYOUT_CORRECTION_AMOUNT

    if data == "payout_mark_paid":
        settlement = await run_blocking(compute_payout_settlement, period_key, worker=worker)
        if not get_payout_screen_access(query, settlement):
            await query.answer("Только редактор выплат", show_alert=True)
            return PAYOUT_SCREEN
        await show_text_screen(
            query,
            context,
            (
                "<b>✅ Подтвердить перевод?</b>\n\n"
                f"📅 {escape_html(settlement['period_label'])}\n"
                f"💰 {format_money_spaced(settlement['total'])}"
            ),
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("✅ Подтвердить", callback_data="payout_mark_paid_yes")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="payout_screen:overview")],
                ]
            ),
            parse_mode="HTML",
        )
        return PAYOUT_SCREEN

    if data == "payout_mark_paid_yes":
        settlement = await run_blocking(compute_payout_settlement, period_key, worker=worker)
        if not get_payout_screen_access(query, settlement):
            await query.answer("Только редактор выплат", show_alert=True)
            return PAYOUT_SCREEN
        await run_blocking(mark_payout_paid, period_key, worker, get_actor_label(update), settlement)
        return await show_payout_overview_screen(query, context, period_key=period_key, notice="✅ Месяц закрыт.")

    if data == "payout_unmark_paid":
        settlement = await run_blocking(compute_payout_settlement, period_key, worker=worker)
        user_id = getattr(getattr(query, "from_user", None), "id", None)
        if user_id not in get_payout_editor_ids():
            await query.answer("Только редактор выплат", show_alert=True)
            return PAYOUT_SCREEN
        await show_text_screen(
            query,
            context,
            "<b>↩️ Снять отметку о переводе?</b>\n\nМесяц снова станет редактируемым.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("↩️ Снять отметку", callback_data="payout_unmark_paid_yes")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="payout_screen:overview")],
                ]
            ),
            parse_mode="HTML",
        )
        return PAYOUT_SCREEN

    if data == "payout_unmark_paid_yes":
        user_id = getattr(getattr(query, "from_user", None), "id", None)
        if user_id not in get_payout_editor_ids():
            await query.answer("Только редактор выплат", show_alert=True)
            return PAYOUT_SCREEN
        await run_blocking(unmark_payout_paid, period_key, worker)
        return await show_payout_overview_screen(query, context, period_key=period_key, notice="↩️ Отметка снята.")

    if data == "payout_service_add":
        settlement = await run_blocking(compute_payout_settlement, period_key, worker=worker)
        if not get_payout_screen_access(query, settlement):
            await query.answer("Месяц закрыт для редактирования.", show_alert=True)
            return PAYOUT_SCREEN
        return await start_payout_service_add_flow(query, context)

    if data == "payout_travel_add":
        settlement = await run_blocking(compute_payout_settlement, period_key, worker=worker)
        if not get_payout_screen_access(query, settlement):
            await query.answer("Месяц закрыт для редактирования.", show_alert=True)
            return PAYOUT_SCREEN
        return await start_payout_travel_add_flow(query, context)

    if data == "payout_task_add":
        settlement = await run_blocking(compute_payout_settlement, period_key, worker=worker)
        if not get_payout_screen_access(query, settlement):
            await query.answer("Месяц закрыт для редактирования.", show_alert=True)
            return PAYOUT_SCREEN
        return await start_payout_salary_task_add_flow(query, context)

    if data.startswith(
        (
            "payout_service_edit:",
            "payout_purchase_edit:",
            "payout_service_del:",
            "payout_purchase_del:",
            "payout_service_del_yes:",
            "payout_travel_edit:",
            "payout_travel_edit_amount:",
            "payout_travel_edit_date:",
            "payout_travel_del:",
            "payout_travel_del_yes:",
            "payout_task_edit:",
            "payout_task_edit_description:",
            "payout_task_edit_amount:",
            "payout_task_edit_date:",
            "payout_task_del:",
            "payout_task_del_yes:",
        )
    ):
        settlement = await run_blocking(compute_payout_settlement, period_key, worker=worker)
        if not get_payout_screen_access(query, settlement):
            await query.answer("Месяц закрыт для редактирования.", show_alert=True)
            return PAYOUT_SCREEN

    if data.startswith("payout_service_edit:"):
        screen = payout.get("screen", "services_entries")
        if get_payout_screen_section(screen) != "services":
            screen = "services_entries"
        return await begin_payout_service_edit(query, context, int(data.split(":")[1]), screen)
    if data.startswith("payout_purchase_edit:"):
        screen = payout.get("screen", "purchases_entries")
        if get_payout_screen_section(screen) != "purchases":
            screen = "purchases_entries"
        return await begin_payout_service_edit(query, context, int(data.split(":")[1]), screen)
    if data.startswith("payout_service_del:"):
        screen = payout.get("screen", "services_entries")
        if get_payout_screen_section(screen) != "services":
            screen = "services_entries"
        return await confirm_payout_service_delete(query, context, int(data.split(":")[1]), screen)
    if data.startswith("payout_purchase_del:"):
        screen = payout.get("screen", "purchases_entries")
        if get_payout_screen_section(screen) != "purchases":
            screen = "purchases_entries"
        return await confirm_payout_service_delete(query, context, int(data.split(":")[1]), screen)
    if data.startswith("payout_service_del_yes:"):
        _, row_str, screen = data.split(":", 2)
        return await delete_payout_service(query, context, int(row_str), screen)

    if data.startswith("payout_travel_edit:"):
        return await show_payout_travel_edit_card(query, context, int(data.split(":")[1]))
    if data.startswith("payout_travel_edit_amount:"):
        row_num = int(data.split(":")[1])
        entry = await get_payout_travel_entry(period_key, row_num, worker)
        if not entry:
            return await show_payout_screen(query, context, screen="travels_entries", notice="❌ Запись проезда не найдена.")
        payout["editing_travel_row"] = row_num
        await show_text_screen(
            query,
            context,
            (
                "<b>💰 Изменить сумму проезда</b>\n\n"
                f"📅 {escape_html(entry.get('Дата', ''))}\n"
                f"Текущая сумма: {format_money_spaced(entry.get('Сумма', 0))}\n\n"
                "Введите новую сумму числом, например <code>96</code>."
            ),
            reply_markup=back_markup(f"payout_travel_edit:{row_num}"),
            parse_mode="HTML",
        )
        return PAYOUT_TRAVEL_EDIT_AMOUNT
    if data.startswith("payout_travel_edit_date:"):
        row_num = int(data.split(":")[1])
        entry = await get_payout_travel_entry(period_key, row_num, worker)
        if not entry:
            return await show_payout_screen(query, context, screen="travels_entries", notice="❌ Запись проезда не найдена.")
        payout["editing_travel_row"] = row_num
        await show_text_screen(
            query,
            context,
            (
                "<b>📅 Изменить дату проезда</b>\n\n"
                f"Текущая дата: {escape_html(entry.get('Дата', ''))}\n"
                f"Месяц: {escape_html(format_period_label(period_key))}\n\n"
                "Введите новую дату в формате <code>дд.мм</code> или <code>дд.мм.гггг</code>."
            ),
            reply_markup=back_markup(f"payout_travel_edit:{row_num}"),
            parse_mode="HTML",
        )
        return PAYOUT_TRAVEL_EDIT_DATE
    if data.startswith("payout_travel_del:"):
        row_num = int(data.split(":")[1])
        entry = await get_payout_travel_entry(period_key, row_num, worker)
        if not entry:
            return await show_payout_screen(query, context, screen="travels_entries", notice="❌ Запись проезда не найдена.")
        await show_text_screen(
            query,
            context,
            (
                "<b>🗑 Удалить запись проезда?</b>\n\n"
                f"📅 {escape_html(entry.get('Дата', ''))}\n"
                f"💰 {format_money_spaced(entry.get('Сумма', 0))}"
            ),
            reply_markup=build_payout_delete_markup(f"payout_travel_del_yes:{row_num}", "travels_entries"),
            parse_mode="HTML",
        )
        return PAYOUT_SCREEN
    if data.startswith("payout_travel_del_yes:"):
        row_num = int(data.split(":")[1])
        entry = await get_payout_travel_entry(period_key, row_num, worker)
        if not entry:
            return await show_payout_screen(query, context, screen="travels_entries", notice="❌ Запись проезда не найдена.")
        await run_blocking(delete_travel_row, row_num)
        return await show_payout_screen(query, context, screen="travels_entries", notice="✅ Запись проезда удалена.")

    if data.startswith("payout_task_edit:"):
        return await show_payout_salary_task_edit_card(query, context, int(data.split(":")[1]))
    if data.startswith("payout_task_edit_description:"):
        row_num = int(data.split(":")[1])
        entry = await get_payout_salary_task_entry(period_key, row_num, worker)
        if not entry:
            return await show_payout_screen(query, context, screen="tasks_entries", notice="❌ Допзадача не найдена.")
        payout["editing_task_row"] = row_num
        await show_text_screen(
            query,
            context,
            (
                "<b>📝 Изменить описание допзадачи</b>\n\n"
                f"Текущее описание: {escape_html(str(entry.get('Описание', '')).strip() or 'Без описания')}\n\n"
                "Отправьте новое описание."
            ),
            reply_markup=back_markup(f"payout_task_edit:{row_num}"),
            parse_mode="HTML",
        )
        return PAYOUT_TASK_EDIT_DESCRIPTION
    if data.startswith("payout_task_edit_amount:"):
        row_num = int(data.split(":")[1])
        entry = await get_payout_salary_task_entry(period_key, row_num, worker)
        if not entry:
            return await show_payout_screen(query, context, screen="tasks_entries", notice="❌ Допзадача не найдена.")
        payout["editing_task_row"] = row_num
        await show_text_screen(
            query,
            context,
            (
                "<b>💰 Изменить сумму допзадачи</b>\n\n"
                f"Текущая сумма: {format_money_spaced(entry.get('Сумма', 0))}\n\n"
                "Введите новую сумму числом, например <code>500</code>."
            ),
            reply_markup=back_markup(f"payout_task_edit:{row_num}"),
            parse_mode="HTML",
        )
        return PAYOUT_TASK_EDIT_AMOUNT
    if data.startswith("payout_task_edit_date:"):
        row_num = int(data.split(":")[1])
        entry = await get_payout_salary_task_entry(period_key, row_num, worker)
        if not entry:
            return await show_payout_screen(query, context, screen="tasks_entries", notice="❌ Допзадача не найдена.")
        payout["editing_task_row"] = row_num
        await show_text_screen(
            query,
            context,
            (
                "<b>📅 Изменить дату допзадачи</b>\n\n"
                f"Текущая дата: {escape_html(entry.get('Дата', ''))}\n"
                f"Месяц: {escape_html(format_period_label(period_key))}\n\n"
                "Введите новую дату в формате <code>дд.мм</code> или <code>дд.мм.гггг</code>."
            ),
            reply_markup=back_markup(f"payout_task_edit:{row_num}"),
            parse_mode="HTML",
        )
        return PAYOUT_TASK_EDIT_DATE
    if data.startswith("payout_task_del:"):
        row_num = int(data.split(":")[1])
        entry = await get_payout_salary_task_entry(period_key, row_num, worker)
        if not entry:
            return await show_payout_screen(query, context, screen="tasks_entries", notice="❌ Допзадача не найдена.")
        description = str(entry.get("Описание", "")).strip() or "Без описания"
        await show_text_screen(
            query,
            context,
            (
                "<b>🗑 Удалить допзадачу?</b>\n\n"
                f"📅 {escape_html(entry.get('Дата', ''))}\n"
                f"📝 {escape_html(description)}\n"
                f"💰 {format_money_spaced(entry.get('Сумма', 0))}"
            ),
            reply_markup=build_payout_delete_markup(f"payout_task_del_yes:{row_num}", "tasks_entries"),
            parse_mode="HTML",
        )
        return PAYOUT_SCREEN
    if data.startswith("payout_task_del_yes:"):
        row_num = int(data.split(":")[1])
        entry = await get_payout_salary_task_entry(period_key, row_num, worker)
        if not entry:
            return await show_payout_screen(query, context, screen="tasks_entries", notice="❌ Допзадача не найдена.")
        await run_blocking(delete_salary_task_row, row_num)
        return await show_payout_screen(query, context, screen="tasks_entries", notice="✅ Допзадача удалена.")

    return PAYOUT_SCREEN


async def payout_correction_amount_back_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "payout_screen:overview":
        get_payout_context(context).pop("correction_draft", None)
        return await show_payout_overview_screen(query, context)
    return PAYOUT_CORRECTION_AMOUNT


async def payout_correction_amount_handler(update: Update, context):
    if not is_payout_editor(update):
        await update.message.reply_text("⛔ Только редактор выплат может менять итог.")
        return PAYOUT_CORRECTION_AMOUNT
    raw_value = update.message.text.strip()
    normalized = raw_value.replace(" ", "")
    amount = parse_numeric_value(normalized)
    if amount is None:
        await update.message.reply_text(
            "❌ Введите сумму числом, например -500 или 300.",
            reply_markup=back_markup("payout_screen:overview"),
        )
        return PAYOUT_CORRECTION_AMOUNT

    payout = get_payout_context(context)
    payout["correction_draft"] = amount
    await update.message.reply_text(
        (
            "📝 Комментарий к ручной корректировке.\n\n"
            "Можно коротко указать причину: аванс, штраф, доплата, округление.\n"
            "Можно оставить пустым или нажать «Пропустить»."
        ),
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Пропустить", callback_data="payout_correction_skip")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="payout_screen:overview")],
            ]
        ),
    )
    return PAYOUT_CORRECTION_NOTE


async def payout_correction_note_back_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    payout = get_payout_context(context)
    if query.data == "payout_screen:overview":
        payout.pop("correction_draft", None)
        return await show_payout_overview_screen(query, context)
    if query.data != "payout_correction_skip":
        return PAYOUT_CORRECTION_NOTE

    period_key = payout.get("period") or get_default_payout_period_key()
    worker = get_selected_payout_worker(context)
    amount = payout.pop("correction_draft", 0)
    await run_blocking(
        upsert_payout,
        period_key,
        worker,
        {
            "correction": amount,
            "correction_note": "",
        },
    )
    return await show_payout_overview_screen(
        query,
        context,
        period_key=period_key,
        notice="✅ Ручная корректировка сохранена.",
    )


async def payout_correction_note_handler(update: Update, context):
    if not is_payout_editor(update):
        await update.message.reply_text("⛔ Только редактор выплат может менять итог.")
        return PAYOUT_CORRECTION_NOTE
    payout = get_payout_context(context)
    period_key = payout.get("period") or get_default_payout_period_key()
    worker = get_selected_payout_worker(context)
    amount = payout.pop("correction_draft", 0)
    note = update.message.text.strip()
    await run_blocking(
        upsert_payout,
        period_key,
        worker,
        {
            "correction": amount,
            "correction_note": note,
        },
    )
    return await render_payout_screen_target(
        update.message,
        context,
        screen="overview",
        period_key=period_key,
        notice="✅ Ручная корректировка сохранена.",
    )


async def payout_travel_edit_amount_back_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    if query.data.startswith("tr_edit_entry:"):
        return await show_travel_edit_card(query, context, int(query.data.split(":")[1]))
    if query.data.startswith("payout_travel_edit:"):
        return await show_payout_travel_edit_card(query, context, int(query.data.split(":")[1]))
    return PAYOUT_TRAVEL_EDIT_AMOUNT


async def payout_travel_edit_amount_handler(update: Update, context):
    travel_edit = context.user_data.get("travel_edit", {})
    if travel_edit.get("mode") == "general":
        if not is_payout_editor(update):
            await update.message.reply_text("⛔ Только редактор выплат может менять проезд.")
            return PAYOUT_TRAVEL_EDIT_AMOUNT
        try:
            amount = int(update.message.text.strip())
            if amount <= 0:
                raise ValueError
        except ValueError:
            row_num = travel_edit.get("row_num")
            await update.message.reply_text(
                "❌ Введите сумму целым числом, например 96.",
                reply_markup=back_markup(f"tr_edit_entry:{row_num}"),
            )
            return PAYOUT_TRAVEL_EDIT_AMOUNT

        row_num = travel_edit.get("row_num")
        entry = await get_travel_entry_by_row(row_num)
        if not entry or str(entry.get("Кто", "")).strip() != str(context.user_data.get("travel_who", "")).strip():
            return await render_travel_edit_entries_screen(
                update.message,
                context,
                notice="❌ Запись проезда не найдена.",
            )
        await run_blocking(update_travel_row, row_num, entry.get("Дата", ""), entry.get("Кто", ""), amount)
        context.user_data.pop("travel_edit", None)
        return await render_travel_edit_entries_screen(
            update.message,
            context,
            notice="✅ Проезд обновлён.",
        )

    if not is_payout_editor(update):
        await update.message.reply_text("⛔ Только редактор выплат может менять проезд.")
        return PAYOUT_TRAVEL_EDIT_AMOUNT
    try:
        amount = int(update.message.text.strip())
        if amount <= 0:
            raise ValueError
    except ValueError:
        row_num = get_payout_context(context).get("editing_travel_row")
        await update.message.reply_text(
            "❌ Введите сумму целым числом, например 96.",
            reply_markup=back_markup(f"payout_travel_edit:{row_num}"),
        )
        return PAYOUT_TRAVEL_EDIT_AMOUNT

    payout = get_payout_context(context)
    row_num = payout.get("editing_travel_row")
    period_key = payout.get("period") or get_default_payout_period_key()
    return_screen = payout.get("screen", "travels_entries")
    entry = await get_payout_travel_entry(period_key, row_num, get_selected_payout_worker(context))
    if not entry:
        return await render_payout_screen_target(
            update.message,
            context,
            screen=return_screen,
            period_key=period_key,
            notice="❌ Запись проезда не найдена.",
        )
    await run_blocking(update_travel_row, row_num, entry.get("Дата", ""), entry.get("Кто", ""), amount)
    return await render_payout_screen_target(
        update.message,
        context,
        screen=return_screen,
        period_key=period_key,
        notice="✅ Проезд обновлён.",
    )


async def payout_travel_edit_date_back_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    if query.data.startswith("tr_edit_entry:"):
        return await show_travel_edit_card(query, context, int(query.data.split(":")[1]))
    if query.data.startswith("payout_travel_edit:"):
        return await show_payout_travel_edit_card(query, context, int(query.data.split(":")[1]))
    return PAYOUT_TRAVEL_EDIT_DATE


async def payout_travel_edit_date_handler(update: Update, context):
    travel_edit = context.user_data.get("travel_edit", {})
    if travel_edit.get("mode") == "general":
        if not is_payout_editor(update):
            await update.message.reply_text("⛔ Только редактор выплат может менять проезд.")
            return PAYOUT_TRAVEL_EDIT_DATE
        parsed, error = validate_manual_date_input(update.message.text)
        row_num = travel_edit.get("row_num")
        if error:
            await update.message.reply_text(error, reply_markup=back_markup(f"tr_edit_entry:{row_num}"))
            return PAYOUT_TRAVEL_EDIT_DATE

        date_str = format_date(parsed)
        period_error = get_period_restriction_error(date_str, context.user_data.get("travel_allowed_period"))
        if period_error:
            await update.message.reply_text(period_error, reply_markup=back_markup(f"tr_edit_entry:{row_num}"))
            return PAYOUT_TRAVEL_EDIT_DATE

        entry = await get_travel_entry_by_row(row_num)
        if not entry or str(entry.get("Кто", "")).strip() != str(context.user_data.get("travel_who", "")).strip():
            return await render_travel_edit_entries_screen(
                update.message,
                context,
                notice="❌ Запись проезда не найдена.",
            )
        await run_blocking(update_travel_row, row_num, date_str, entry.get("Кто", ""), entry.get("Сумма", ""))
        context.user_data["travel_date"] = date_str
        context.user_data.pop("travel_edit", None)
        return await render_travel_edit_entries_screen(
            update.message,
            context,
            notice="✅ Дата проезда обновлена.",
        )

    if not is_payout_editor(update):
        await update.message.reply_text("⛔ Только редактор выплат может менять проезд.")
        return PAYOUT_TRAVEL_EDIT_DATE
    parsed, error = validate_manual_date_input(update.message.text)
    row_num = get_payout_context(context).get("editing_travel_row")
    if error:
        await update.message.reply_text(error, reply_markup=back_markup(f"payout_travel_edit:{row_num}"))
        return PAYOUT_TRAVEL_EDIT_DATE

    payout = get_payout_context(context)
    period_key = payout.get("period") or get_default_payout_period_key()
    return_screen = payout.get("screen", "travels_entries")
    date_str = format_date(parsed)
    period_error = get_period_restriction_error(date_str, period_key)
    if period_error:
        await update.message.reply_text(period_error, reply_markup=back_markup(f"payout_travel_edit:{row_num}"))
        return PAYOUT_TRAVEL_EDIT_DATE

    entry = await get_payout_travel_entry(period_key, row_num, get_selected_payout_worker(context))
    if not entry:
        return await render_payout_screen_target(
            update.message,
            context,
            screen=return_screen,
            period_key=period_key,
            notice="❌ Запись проезда не найдена.",
        )
    await run_blocking(update_travel_row, row_num, date_str, entry.get("Кто", ""), entry.get("Сумма", ""))
    payout["travels_date"] = date_str
    return await render_payout_screen_target(
        update.message,
        context,
        screen=return_screen,
        period_key=period_key,
        notice="✅ Дата проезда обновлена.",
    )


async def payout_task_edit_description_back_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    if query.data.startswith("payout_task_edit:"):
        return await show_payout_salary_task_edit_card(query, context, int(query.data.split(":")[1]))
    return PAYOUT_TASK_EDIT_DESCRIPTION


async def payout_task_edit_description_handler(update: Update, context):
    if not is_payout_editor(update):
        await update.message.reply_text("⛔ Только редактор выплат может менять допзадачи.")
        return PAYOUT_TASK_EDIT_DESCRIPTION
    description = str(update.message.text or "").strip()
    row_num = get_payout_context(context).get("editing_task_row")
    if not description:
        await update.message.reply_text(
            "❌ Напишите короткое описание задачи.",
            reply_markup=back_markup(f"payout_task_edit:{row_num}"),
        )
        return PAYOUT_TASK_EDIT_DESCRIPTION

    payout = get_payout_context(context)
    period_key = payout.get("period") or get_default_payout_period_key()
    return_screen = payout.get("screen", "tasks_entries")
    entry = await get_payout_salary_task_entry(period_key, row_num, get_selected_payout_worker(context))
    if not entry:
        return await render_payout_screen_target(
            update.message,
            context,
            screen=return_screen,
            period_key=period_key,
            notice="❌ Допзадача не найдена.",
        )
    await run_blocking(
        update_salary_task_row,
        row_num,
        {
            "date": entry.get("Дата", ""),
            "who": entry.get("Кто", ""),
            "description": description,
            "amount": entry.get("Сумма", ""),
            "added_by": entry.get("Кто добавил", ""),
        },
    )
    return await render_payout_screen_target(
        update.message,
        context,
        screen=return_screen,
        period_key=period_key,
        notice="✅ Описание обновлено.",
    )


async def payout_task_edit_amount_back_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    if query.data.startswith("payout_task_edit:"):
        return await show_payout_salary_task_edit_card(query, context, int(query.data.split(":")[1]))
    return PAYOUT_TASK_EDIT_AMOUNT


async def payout_task_edit_amount_handler(update: Update, context):
    if not is_payout_editor(update):
        await update.message.reply_text("⛔ Только редактор выплат может менять допзадачи.")
        return PAYOUT_TASK_EDIT_AMOUNT
    try:
        amount = normalize_number_text(update.message.text)
    except ValueError:
        row_num = get_payout_context(context).get("editing_task_row")
        await update.message.reply_text(
            "❌ Введите сумму числом, например 500 или 750,5.",
            reply_markup=back_markup(f"payout_task_edit:{row_num}"),
        )
        return PAYOUT_TASK_EDIT_AMOUNT

    payout = get_payout_context(context)
    row_num = payout.get("editing_task_row")
    period_key = payout.get("period") or get_default_payout_period_key()
    return_screen = payout.get("screen", "tasks_entries")
    entry = await get_payout_salary_task_entry(period_key, row_num, get_selected_payout_worker(context))
    if not entry:
        return await render_payout_screen_target(
            update.message,
            context,
            screen=return_screen,
            period_key=period_key,
            notice="❌ Допзадача не найдена.",
        )
    await run_blocking(
        update_salary_task_row,
        row_num,
        {
            "date": entry.get("Дата", ""),
            "who": entry.get("Кто", ""),
            "description": entry.get("Описание", ""),
            "amount": amount,
            "added_by": entry.get("Кто добавил", ""),
        },
    )
    return await render_payout_screen_target(
        update.message,
        context,
        screen=return_screen,
        period_key=period_key,
        notice="✅ Сумма обновлена.",
    )


async def payout_task_edit_date_back_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    if query.data.startswith("payout_task_edit:"):
        return await show_payout_salary_task_edit_card(query, context, int(query.data.split(":")[1]))
    return PAYOUT_TASK_EDIT_DATE


async def payout_task_edit_date_handler(update: Update, context):
    if not is_payout_editor(update):
        await update.message.reply_text("⛔ Только редактор выплат может менять допзадачи.")
        return PAYOUT_TASK_EDIT_DATE
    parsed, error = validate_manual_date_input(update.message.text)
    row_num = get_payout_context(context).get("editing_task_row")
    if error:
        await update.message.reply_text(error, reply_markup=back_markup(f"payout_task_edit:{row_num}"))
        return PAYOUT_TASK_EDIT_DATE

    payout = get_payout_context(context)
    period_key = payout.get("period") or get_default_payout_period_key()
    return_screen = payout.get("screen", "tasks_entries")
    date_str = format_date(parsed)
    period_error = get_period_restriction_error(date_str, period_key)
    if period_error:
        await update.message.reply_text(period_error, reply_markup=back_markup(f"payout_task_edit:{row_num}"))
        return PAYOUT_TASK_EDIT_DATE

    entry = await get_payout_salary_task_entry(period_key, row_num, get_selected_payout_worker(context))
    if not entry:
        return await render_payout_screen_target(
            update.message,
            context,
            screen=return_screen,
            period_key=period_key,
            notice="❌ Допзадача не найдена.",
        )
    await run_blocking(
        update_salary_task_row,
        row_num,
        {
            "date": date_str,
            "who": entry.get("Кто", ""),
            "description": entry.get("Описание", ""),
            "amount": entry.get("Сумма", ""),
            "added_by": entry.get("Кто добавил", ""),
        },
    )
    payout["tasks_date"] = date_str
    return await render_payout_screen_target(
        update.message,
        context,
        screen=return_screen,
        period_key=period_key,
        notice="✅ Дата обновлена.",
    )


async def show_reports_section_menu(query, context):
    keyboard = [
        [InlineKeyboardButton("📅 Отчёт за день", callback_data="report_day")],
        [InlineKeyboardButton("📆 Отчёт за период", callback_data="report_period")],
        [InlineKeyboardButton("➕ Задача / доплата", callback_data="report_salary_task")],
    ]
    user_id = getattr(getattr(query, "from_user", None), "id", None)
    if user_id in get_payout_viewer_ids():
        payout_buttons = [
            [InlineKeyboardButton(get_salary_button_label(worker), callback_data=f"report_salary_worker:{worker}")]
            for worker in get_paid_workers()
        ]
        keyboard[2:2] = payout_buttons
    keyboard.append([InlineKeyboardButton("🏠 В меню", callback_data="back_main")])
    await show_text_screen(query, context, "📊 Отчёты\n\nВыберите отчёт:", reply_markup=InlineKeyboardMarkup(keyboard))
    return REPORT_MENU_SECTION


async def show_service_problem_points(query, context):
    await show_loading_state(query, context, "Загружаю ревизию...")
    records = await run_blocking(get_all_revisions)
    period = latest_revision_period(records)
    if not period:
        await show_text_screen(
            query,
            context,
            "⚠️ Проблемные точки\n\n❌ Пока нет ни одной ревизии.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Назад", callback_data="back_service_menu")],
                [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
            ]),
        )
        return SERVICE_MENU_SECTION

    await show_text_screen(
        query,
        context,
        build_revision_problem_points_text(period, records),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🛒 Закупка и остатки", callback_data="procurement")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_service_menu")],
            [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
        ]),
        parse_mode="HTML",
    )
    return SERVICE_MENU_SECTION

# ============ КОМАНДЫ ============
async def cancel(update: Update, context):
    if not is_allowed_user(update):
        return await deny_private_access(update)
    context.user_data.clear()
    await update.message.reply_text("❌ Действие отменено.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def cmd_add_center(update: Update, context):
    if not is_allowed_user(update):
        return await deny_private_access(update)
    raw = " ".join(context.args).strip() if context.args else ""
    if not raw:
        await update.effective_message.reply_text(
            "Использование:\n"
            "<code>/add_center Название;Город;Контакт;Телефон;Email;Адрес;Специализация;Заметки</code>\n\n"
            "Обязательно только название. Остальные поля можно оставить пустыми, разделители оставь:\n"
            "<code>/add_center Кофесервис;Москва;;+7...</code>",
            parse_mode="HTML",
        )
        return
    parts = [p.strip() for p in raw.split(";")]
    headers_fields = ["Название", "Город", "Контактное лицо", "Телефон", "Email", "Адрес", "Специализация", "Заметки"]
    entry = {field: (parts[i] if i < len(parts) else "") for i, field in enumerate(headers_fields)}
    if not entry["Название"]:
        await update.effective_message.reply_text("❌ Название не может быть пустым.")
        return
    try:
        centers = await run_blocking(get_all_repair_centers)
        entry["id"] = next_prefixed_id(centers, "SC")
        await run_blocking(add_repair_center_row, entry)
    except Exception:
        logger.exception("cmd_add_center failed: raw=%s", raw)
        await update.effective_message.reply_text("❌ Не удалось добавить сервисный центр.")
        return
    await update.effective_message.reply_text(
        f"✅ Сервисный центр добавлен: {entry['id']} · {entry['Название']}"
    )


async def cmd_add_machine(update: Update, context):
    if not is_allowed_user(update):
        return await deny_private_access(update)
    raw = " ".join(context.args).strip() if context.args else ""
    if not raw:
        await update.effective_message.reply_text(
            "Использование:\n"
            "<code>/add_machine Точка;Бренд;Модель;Серийный;ДатаПокупки;Гарантия;Заметки</code>\n\n"
            "Обязательны: Точка и Бренд. Точки: " + ", ".join(POINTS) + "\n"
            "Пример: <code>/add_machine Сити;Saeco;Aulika EVO;SN-12345</code>",
            parse_mode="HTML",
        )
        return
    parts = [p.strip() for p in raw.split(";")]
    headers_fields = ["Точка", "Бренд", "Модель", "Серийный номер", "Дата покупки", "Гарантия до", "Заметки"]
    entry = {field: (parts[i] if i < len(parts) else "") for i, field in enumerate(headers_fields)}
    if not entry["Точка"] or not entry["Бренд"]:
        await update.effective_message.reply_text("❌ Точка и Бренд обязательны.")
        return
    if entry["Точка"] not in POINTS:
        await update.effective_message.reply_text(
            f"❌ Точка «{entry['Точка']}» не из списка. Допустимые: {', '.join(POINTS)}"
        )
        return
    entry["Статус"] = REPAIR_MACHINE_WORKING
    try:
        machines = await run_blocking(get_all_repair_machines)
        entry["id"] = next_prefixed_id(machines, "M")
        await run_blocking(add_repair_machine_row, entry)
    except Exception:
        logger.exception("cmd_add_machine failed: raw=%s", raw)
        await update.effective_message.reply_text("❌ Не удалось добавить аппарат.")
        return
    await update.effective_message.reply_text(
        f"✅ Аппарат добавлен: {entry['id']} · {entry['Точка']} · {entry['Бренд']} {entry['Модель']}".rstrip()
    )


async def cmd_ids(update: Update, context):
    if not is_allowed_user(update):
        return await deny_private_access(update)

    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    if not (chat and user and message):
        return

    lines = [
        "🪪 <b>Идентификаторы</b>",
        "",
        f"👤 user_id: <code>{user.id}</code>",
        f"💬 chat_id: <code>{chat.id}</code>",
        f"🧩 тип чата: <code>{escape_html(chat.type)}</code>",
    ]

    chat_title = getattr(chat, "title", None)
    if chat_title:
        lines.append(f"🏷 чат: {escape_html(chat_title)}")

    lines.extend(
        [
            "",
            f"✅ user в ALLOWED_USER_IDS: {'да' if is_allowed_user(update) else 'нет'}",
        ]
    )

    if chat.type in {"group", "supergroup"}:
        lines.append(
            f"✅ chat в ALLOWED_GROUP_CHAT_IDS: {'да' if is_allowed_group_chat(update) else 'нет'}"
        )
        lines.extend(
            [
                "",
                "Для <code>.env</code>:",
                f"<code>ALLOWED_USER_IDS={user.id}</code>",
                f"<code>ALLOWED_GROUP_CHAT_IDS={chat.id}</code>",
            ]
        )

    await message.reply_text("\n".join(lines), parse_mode="HTML")

async def cmd_shortages(update: Update, context):
    if not is_allowed_user(update):
        return await deny_private_access(update)
    try:
        data = await run_blocking(get_sheet1_records)
        if not data:
            await update.message.reply_text("📭 Пока нет данных.")
            return
        shortages = {}
        for row in data:
            point = row.get("Точка", "")
            shortage = row.get("Нехватка", "")
            date = row.get("Дата", "")
            if shortage and shortage.strip() and shortage.strip() != "-":
                if point not in shortages:
                    shortages[point] = []
                shortages[point].append({"date": date, "items": shortage})
        if not shortages:
            await update.message.reply_text("✅ Нехваток нет! Всё в порядке.")
            return
        msg = "⚠️ Нехватки по точкам:\n\n"
        for point, items in shortages.items():
            msg += f"📍 {point}\n"
            last = items[-1]
            msg += f"   📅 {last['date']}\n"
            msg += f"   ❗ {last['items']}\n\n"
        await update.message.reply_text(msg)
    except Exception:
        logger.exception("Failed to build shortages report")
        await update.message.reply_text("❌ Не удалось получить данные. Попробуйте позже.")

async def cmd_reports(update: Update, context):
    if not is_allowed_user(update):
        return await deny_private_access(update)
    try:
        data = await run_blocking(get_sheet1_records)
        if not data:
            await update.message.reply_text("📭 Пока нет данных.")
            return
        today = now_local().strftime("%d.%m.%Y")
        today_reports = [r for r in data if r.get("Дата", "") == today]
        if not today_reports:
            await update.message.reply_text(f"📭 За {today} отчётов пока нет.")
            return
        msg = f"📋 Отчёты за {today}:\n\n"
        for r in today_reports:
            point = r.get("Точка", "?")
            worker = r.get("Кто", "?")
            water = r.get("Вода(бут)", "?")
            purchases = r.get("Закупки", "-")
            purchase_sum = r.get("Сумма закупок", 0)
            shortage = r.get("Нехватка", "-")
            msg += f"📍 {point} ({worker})\n"
            msg += f"   💧 Вода: {water}\n"
            if purchases and purchases != "-":
                msg += f"   🛒 Закупки: {purchases} = {purchase_sum}₽\n"
            else:
                msg += f"   🛒 Закупок нет\n"
            if shortage and shortage != "-":
                msg += f"   ⚠️ Нехватка: {shortage}\n"
            else:
                msg += f"   ✅ Нехваток нет\n"
            msg += "\n"
        await update.message.reply_text(msg)
    except Exception:
        logger.exception("Failed to build today reports command output")
        await update.message.reply_text("❌ Не удалось получить отчёты. Попробуйте позже.")


async def cmd_service_duplicates(update: Update, context):
    if not is_allowed_user(update):
        return await deny_private_access(update)

    message = update.effective_message
    if not message:
        return

    if not is_private_chat(update):
        await message.reply_text("⚪ Эту команду лучше запускать в личке бота.")
        return

    if not is_payout_editor(update):
        await message.reply_text("⛔ Команда доступна только Матвею.")
        return

    filters, error = parse_service_duplicates_command_args(getattr(context, "args", []))
    if error:
        await message.reply_text(f"{error}\n\n{build_service_duplicates_command_help()}")
        return

    status_message = await message.reply_text("🔎 Ищу дубли обслуживания...")
    try:
        groups = await run_blocking(
            find_service_duplicate_groups,
            filters,
            filters.get("limit", 10),
        )
        if not groups:
            await status_message.edit_text(
                build_service_duplicate_report_text(
                    groups,
                    filters,
                    limit=filters.get("limit", 10),
                )
            )
            return

        review = create_service_duplicate_review(
            context.application.bot_data,
            message.chat_id,
            getattr(getattr(update, "effective_user", None), "id", None),
            groups,
            filters=filters,
            limit=filters.get("limit", 10),
        )
        await status_message.edit_text(
            build_service_duplicate_review_text(review, mode="overview"),
            reply_markup=build_service_duplicate_review_overview_markup(
                review["id"],
                has_candidates=bool(review.get("candidate_rows")),
            ),
        )
    except Exception:
        logger.exception("Failed to build service duplicates report")
        await status_message.edit_text("❌ Не удалось собрать отчёт по дублям. Попробуйте позже.")


async def cmd_delete_service_rows(update: Update, context):
    if not is_allowed_user(update):
        return await deny_private_access(update)

    message = update.effective_message
    if not message:
        return

    if not is_private_chat(update):
        await message.reply_text("⚪ Эту команду лучше запускать в личке бота.")
        return

    if not is_payout_editor(update):
        await message.reply_text("⛔ Команда доступна только Матвею.")
        return

    row_numbers, confirm, error = parse_delete_service_rows_command_args(getattr(context, "args", []))
    if error:
        await message.reply_text(f"{error}\n\n{build_delete_service_rows_command_help()}")
        return

    status_message = await message.reply_text("🧮 Проверяю строки на удаление...")
    try:
        if not confirm:
            preview_text, _, _ = await run_blocking(build_delete_service_rows_preview, row_numbers)
            await status_message.edit_text(preview_text)
            return

        result = await run_blocking(delete_service_rows_by_numbers, row_numbers)
        try:
            await refresh_group_service_today_posts(context.application, force=True)
        except Exception:
            logger.exception("Failed to refresh group service-today post after service row delete command")
        await status_message.edit_text(build_delete_service_rows_result_text(result))
    except Exception:
        logger.exception("Failed to delete service rows via command")
        await status_message.edit_text("❌ Не удалось удалить строки. Попробуйте позже.")


async def service_duplicate_callback_handler(update: Update, context):
    query = update.callback_query
    if not query:
        return

    if not is_allowed_user(update):
        await deny_callback_access(query)
        return

    if not is_private_chat(update):
        await deny_callback_access(query)
        return

    if not is_payout_editor(update):
        await deny_callback_access(query)
        return

    await query.answer()
    data = str(query.data or "")
    if not data.startswith("svcdup:"):
        return

    parts = data.split(":")
    if len(parts) < 3:
        return

    action = parts[1]
    review_id = parts[2]
    user_id = getattr(getattr(query, "from_user", None), "id", None)
    chat_id = getattr(getattr(query, "message", None), "chat_id", None)
    review = get_service_duplicate_review(context.application.bot_data, review_id, user_id=user_id, chat_id=chat_id)

    if not review:
        await query.edit_message_text("⚪ Подборка дублей уже недоступна. Запусти /service_duplicates ещё раз.")
        return

    if action == "cancel":
        drop_service_duplicate_review(context.application.bot_data, review_id)
        await query.edit_message_text("❌ Проверка дублей закрыта.")
        return

    if action == "back":
        await query.edit_message_text(
            build_service_duplicate_review_text(review, mode="overview"),
            reply_markup=build_service_duplicate_review_overview_markup(
                review_id,
                has_candidates=bool(review.get("candidate_rows")),
            ),
        )
        return

    if action == "select":
        await query.edit_message_text(
            build_service_duplicate_review_text(review, mode="select"),
            reply_markup=build_service_duplicate_selection_markup(review),
        )
        return

    if action == "select_all":
        if review.get("selected_rows", []) == review.get("candidate_rows", []):
            await query.answer("Все строки уже выбраны.", show_alert=False)
            return
        review["selected_rows"] = list(review.get("candidate_rows", []))
        await query.edit_message_text(
            build_service_duplicate_review_text(review, mode="select"),
            reply_markup=build_service_duplicate_selection_markup(review),
        )
        return

    if action == "clear":
        if not review.get("selected_rows"):
            await query.answer("Сейчас ничего не выбрано.", show_alert=False)
            return
        review["selected_rows"] = []
        await query.edit_message_text(
            build_service_duplicate_review_text(review, mode="select"),
            reply_markup=build_service_duplicate_selection_markup(review),
        )
        return

    if action == "toggle":
        if len(parts) < 4 or not parts[3].isdigit():
            await query.edit_message_text("⚪ Не удалось определить строку.")
            return
        row_num = int(parts[3])
        if row_num not in review.get("candidate_rows", []):
            await query.edit_message_text("⚪ Эта строка уже недоступна. Запусти /service_duplicates ещё раз.")
            return
        selected = set(int(row) for row in review.get("selected_rows", []))
        if row_num in selected:
            selected.remove(row_num)
        else:
            selected.add(row_num)
        review["selected_rows"] = sorted(selected, reverse=True)
        await query.edit_message_text(
            build_service_duplicate_review_text(review, mode="select"),
            reply_markup=build_service_duplicate_selection_markup(review),
        )
        return

    if action == "confirm_all":
        await query.edit_message_text(
            build_service_duplicate_review_text(review, mode="confirm_all"),
            reply_markup=build_service_duplicate_confirm_markup(review_id, "delete_all"),
        )
        return

    if action == "confirm_selected":
        if not review.get("selected_rows"):
            await query.answer("Нет выбранных строк.", show_alert=True)
            return
        await query.edit_message_text(
            build_service_duplicate_review_text(review, mode="confirm_selected"),
            reply_markup=build_service_duplicate_confirm_markup(review_id, "delete_selected"),
        )
        return

    if action not in {"delete_all", "delete_selected"}:
        return

    row_numbers = (
        list(review.get("candidate_rows", []))
        if action == "delete_all"
        else list(review.get("selected_rows", []))
    )
    if not row_numbers:
        await query.answer("Нет строк для удаления.", show_alert=True)
        return

    try:
        result = await run_blocking(delete_service_rows_by_numbers, row_numbers)
        drop_service_duplicate_review(context.application.bot_data, review_id)
        try:
            await refresh_group_service_today_posts(context.application, force=True)
        except Exception:
            logger.exception("Failed to refresh group service-today post after duplicate cleanup")
        await query.edit_message_text(build_delete_service_rows_result_text(result))
    except Exception:
        logger.exception("Failed to delete duplicate service rows from review %s", review_id)
        await query.edit_message_text("❌ Не удалось удалить строки. Попробуй ещё раз позже.")


async def main_menu_handler(update: Update, context):
    query = update.callback_query
    if not is_allowed_user(update):
        await deny_callback_access(query)
        return ConversationHandler.END
    await query.answer()
    d = query.data
    if d == "back_main":
        return await start(update, context)
    elif d == "info":
        return await info_menu(update, context)
    elif d == "service":
        return await show_service_section_menu(query, context)
    elif d == "service_start":
        return await service_who(update, context)
    elif d == "revision":
        return await show_revision_menu(query, context)
    elif d == "procurement":
        revision = get_revision_context(context)
        revision.clear()
        revision["action"] = "procurement"
        return await show_revision_period_menu(query, context)
    elif d == "repair":
        return await show_repair_menu(query, context)
    elif d == "rent":
        return await show_rent_menu(query, context)
    elif d == "reports":
        return await show_reports_section_menu(query, context)
    elif d == "service_today":
        return await service_today_notice(update, context)
    elif d == "service_today_repair":
        return await service_today_group_details(update, context, "repair")
    elif d == "service_today_urgent":
        return await service_today_group_details(update, context, "urgent")
    elif d == "service_today_need":
        return await service_today_group_details(update, context, "need_today")
    elif d == "service_today_monitor":
        return await service_today_group_details(update, context, "monitor")
    elif d == "service_fix":
        return await show_service_fix_menu(query, context)
    elif d == "delete_service":
        context.user_data["delete"] = {}
        return await show_delete_date_menu(query, context)
    elif d == "travel":
        return await show_travel_menu(query, context)
    elif d == "report_day":
        return await report_day(update, context)
    elif d == "report_period":
        return await report_period_menu(update, context)
    return MAIN_MENU


async def service_section_handler(update: Update, context):
    query = update.callback_query
    if not is_allowed_user(update):
        await deny_callback_access(query)
        return ConversationHandler.END
    await query.answer()
    d = query.data
    if d == "back_main":
        return await start(update, context)
    if d == "cleanup_cards":
        deleted = await cleanup_tracked_messages(context, context.bot, query.message.chat_id)
        await show_text_screen(
            query,
            context,
            f"🧹 Удалено карточек: {deleted}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ К обслуживанию", callback_data="back_service_menu")],
                [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
            ]),
        )
        return SERVICE_MENU_SECTION
    if d == "back_service_menu":
        return await show_service_section_menu(query, context)
    if d == "service_start":
        return await service_who(update, context)
    if d == "service_today":
        return await service_today_notice(update, context)
    if d == "service_today_repair":
        return await service_today_group_details(update, context, "repair")
    if d == "service_today_urgent":
        return await service_today_group_details(update, context, "urgent")
    if d == "service_today_need":
        return await service_today_group_details(update, context, "need_today")
    if d == "service_today_monitor":
        return await service_today_group_details(update, context, "monitor")
    if d == "repair":
        return await show_repair_menu(query, context)
    if d == "info":
        return await info_menu(update, context)
    if d == "service_problem_points":
        return await show_service_problem_points(query, context)
    if d == "service_fix":
        return await show_service_fix_menu(query, context)
    if d == "back_service_fix":
        return await show_service_fix_menu(query, context)
    if d == "service_fix_latest":
        return await edit_last_service(query, context)
    if d == "service_fix_by_date":
        context.user_data["delete"] = {}
        return await show_delete_date_menu(query, context)
    if d == "edit_last_service":
        return await edit_last_service(query, context)
    if d == "delete_service":
        context.user_data["delete"] = {}
        return await show_delete_date_menu(query, context)
    if d == "travel":
        return await show_travel_menu(query, context)
    if d == "procurement":
        revision = get_revision_context(context)
        revision.clear()
        revision["action"] = "procurement"
        return await show_revision_period_menu(query, context)
    return SERVICE_MENU_SECTION


async def report_section_handler(update: Update, context):
    query = update.callback_query
    if not is_allowed_user(update):
        await deny_callback_access(query)
        return ConversationHandler.END
    await query.answer()
    d = query.data
    if d == "back_main":
        return await start(update, context)
    if d == "back_reports_menu":
        return await show_reports_section_menu(query, context)
    if d == "report_day":
        return await report_day(update, context)
    if d == "report_period":
        return await report_period_menu(update, context)
    if d.startswith("report_salary_worker:"):
        if not is_payout_viewer(update):
            await deny_callback_access(query)
            return REPORT_MENU_SECTION
        worker = d.split(":", 1)[1]
        return await show_salary_report_screen(query, context, worker)
    if d == "payout_open":
        if not is_payout_viewer(update):
            await deny_callback_access(query)
            return REPORT_MENU_SECTION
        return await show_payout_overview_screen(query, context)
    if d == "report_salary_task":
        return await start_salary_task_flow(query, context)
    if d.startswith("salary_task_add_"):
        worker = d.replace("salary_task_add_", "", 1)
        return await start_salary_task_flow(query, context, worker)
    return REPORT_MENU_SECTION


async def salary_task_worker_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "back_main":
        clear_salary_task_context(context)
        return await start(update, context)
    if data == "back_reports_menu":
        clear_salary_task_context(context)
        return await show_reports_section_menu(query, context)
    if data.startswith("salary_task_worker_"):
        try:
            worker = get_paid_workers()[int(data.rsplit("_", 1)[1])]
        except (ValueError, IndexError):
            return await show_salary_task_worker_menu(query, context)
        salary_task = get_salary_task_context(context)
        salary_task["who"] = worker
        return await show_salary_task_date_menu(query, context)
    return SALARY_TASK_WORKER


async def salary_task_date_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    data = query.data
    salary_task = get_salary_task_context(context)

    if data == "back_main":
        clear_salary_task_context(context)
        return await start(update, context)
    if data == "back_salary_task_worker":
        if salary_task.get("return_mode") == "payout":
            screen = salary_task.get("return_screen", "overview")
            period_key = salary_task.get("return_period")
            payout_worker = salary_task.get("return_worker", "")
            if payout_worker:
                get_payout_context(context)["worker"] = payout_worker
            clear_salary_task_context(context)
            return await show_payout_screen(query, context, screen=screen, period_key=period_key)
        paid_workers = get_paid_workers()
        if len(paid_workers) == 1 and salary_task.get("who") == paid_workers[0]:
            clear_salary_task_context(context)
            return await show_reports_section_menu(query, context)
        salary_task.pop("date", None)
        return await show_salary_task_worker_menu(query, context)
    if data == "salary_task_date_today":
        salary_task["date"] = today()
        error = get_period_restriction_error(salary_task["date"], salary_task.get("return_period"))
        if error and salary_task.get("return_mode") == "payout":
            return await show_salary_task_date_menu(query, context, notice=error)
        return await show_salary_task_description_prompt(query, context)
    if data == "salary_task_date_yesterday":
        salary_task["date"] = yesterday()
        error = get_period_restriction_error(salary_task["date"], salary_task.get("return_period"))
        if error and salary_task.get("return_mode") == "payout":
            return await show_salary_task_date_menu(query, context, notice=error)
        return await show_salary_task_description_prompt(query, context)
    if data == "salary_task_date_daybefore":
        salary_task["date"] = day_before_yesterday()
        error = get_period_restriction_error(salary_task["date"], salary_task.get("return_period"))
        if error and salary_task.get("return_mode") == "payout":
            return await show_salary_task_date_menu(query, context, notice=error)
        return await show_salary_task_description_prompt(query, context)
    if data == "salary_task_date_custom":
        await show_text_screen(
            query,
            context,
            "🧰 Задача / доплата\n\nВведите дату в формате дд.мм или дд.мм.гггг.",
            reply_markup=back_markup("back_salary_task_date"),
        )
        return SALARY_TASK_DATE_CUSTOM
    return SALARY_TASK_DATE


async def salary_task_date_custom_back_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "back_salary_task_date":
        return await show_salary_task_date_menu(query, context)
    return SALARY_TASK_DATE_CUSTOM


async def salary_task_date_custom_handler(update: Update, context):
    parsed, error = validate_manual_date_input(update.message.text)
    if error:
        await update.message.reply_text(error, reply_markup=back_markup("back_salary_task_date"))
        return SALARY_TASK_DATE_CUSTOM

    salary_task = get_salary_task_context(context)
    salary_task["date"] = format_date(parsed)
    period_error = get_period_restriction_error(salary_task["date"], salary_task.get("return_period"))
    if period_error and salary_task.get("return_mode") == "payout":
        await update.message.reply_text(period_error, reply_markup=back_markup("back_salary_task_date"))
        return SALARY_TASK_DATE_CUSTOM
    return await show_salary_task_description_prompt(update.message, context)


async def salary_task_description_handler(update: Update, context):
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        if query.data == "back_salary_task_date":
            return await show_salary_task_date_menu(query, context)
        return SALARY_TASK_DESCRIPTION

    description = str(update.message.text or "").strip()
    if not description:
        await update.message.reply_text(
            "❌ Напишите короткое описание задачи.",
            reply_markup=back_markup("back_salary_task_date"),
        )
        return SALARY_TASK_DESCRIPTION

    salary_task = get_salary_task_context(context)
    salary_task["description"] = description
    return await show_salary_task_amount_prompt(update.message, context)


async def salary_task_amount_handler(update: Update, context):
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        if query.data == "back_salary_task_description":
            return await show_salary_task_description_prompt(query, context)
        return SALARY_TASK_AMOUNT

    try:
        amount = normalize_number_text(update.message.text)
    except ValueError:
        await update.message.reply_text(
            "❌ Введите сумму числом, например 500 или 750,5.",
            reply_markup=back_markup("back_salary_task_description"),
        )
        return SALARY_TASK_AMOUNT

    salary_task = get_salary_task_context(context)
    salary_task["amount"] = amount
    return await show_salary_task_confirm_screen(update.message, context)


async def salary_task_confirm_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "back_main":
        clear_salary_task_context(context)
        return await start(update, context)
    if query.data == "back_salary_task_amount":
        return await show_salary_task_amount_prompt(query, context)
    if query.data != "salary_task_save":
        return SALARY_TASK_CONFIRM

    salary_task = get_salary_task_context(context)
    entry = {
        "date": salary_task.get("date", ""),
        "who": salary_task.get("who", ""),
        "description": salary_task.get("description", ""),
        "amount": salary_task.get("amount", ""),
        "added_by": get_actor_label(update),
    }
    try:
        await run_blocking(add_salary_task_row, entry)
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(
                query,
                context,
                retry_callback="salary_task_save",
                back_callback="back_salary_task_amount",
            )
            return SALARY_TASK_CONFIRM
        raise
    logger.info(
        "salary task saved: user_id=%s worker=%s date=%s amount=%s",
        getattr(update.effective_user, "id", None),
        entry["who"],
        entry["date"],
        entry["amount"],
    )
    if salary_task.get("return_mode") == "payout":
        screen = salary_task.get("return_screen", "overview")
        period_key = salary_task.get("return_period")
        get_payout_context(context)["tasks_date"] = entry["date"]
        clear_salary_task_context(context)
        return await show_payout_screen(query, context, screen=screen, period_key=period_key, notice="✅ Допзадача добавлена.")
    return await show_salary_task_saved_screen(query, context, entry)

# ============ ИНФОРМАЦИЯ ============
async def service_today_notice(update: Update, context):
    query = update.callback_query

    try:
        await show_loading_state(query, context, "Загружаю данные по точкам...")
        snapshot = await run_blocking(build_service_today_snapshot)
        groups = snapshot["groups"]
        text = snapshot["text"]
    except Exception as e:
        text = f"❌ Ошибка: {e}"
        groups = None

    kb = []
    merged_need = merge_service_items(groups["urgent"], groups["need_today"]) if groups else []
    if groups and merged_need:
        kb.append([InlineKeyboardButton(
            f"🔴 Подробно: нужно сегодня ({len(merged_need)})",
            callback_data="service_today_need",
        )])
    if groups and groups["monitor"]:
        kb.append([InlineKeyboardButton(
            f"🟡 Подробно: на контроле ({len(groups['monitor'])})",
            callback_data="service_today_monitor",
        )])
    if groups and groups["repair"]:
        kb.append([InlineKeyboardButton(
            f"🛠 На ремонте ({len(groups['repair'])})",
            callback_data="service_today_repair",
        )])
    kb.extend([
        [InlineKeyboardButton("📝 Начать обслуживание", callback_data="service_start", style="primary")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_service_menu")],
        [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
    ])
    await show_text_screen(query, context, text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    return SERVICE_MENU_SECTION


async def service_today_group_details(update: Update, context, group_key):
    query = update.callback_query
    group_titles = {
        "repair": "🛠 Подробно: точки на ремонте",
        "urgent": "🚨 Подробно: срочно пополнить",
        "need_today": "🔴 Подробно: нужно сегодня",
        "monitor": "🟡 Подробно: на контроле",
    }
    empty_texts = {
        "repair": "Сейчас нет точек в списке «На ремонте».",
        "urgent": "Сейчас нет точек в списке «Срочно пополнить».",
        "need_today": "Сейчас нет точек в списке «Нужно сегодня».",
        "monitor": "Сейчас нет точек в списке «На контроле».",
    }

    try:
        await show_loading_state(query, context, "Загружаю карточки точек...")
        snapshot = await run_blocking(build_service_today_snapshot)
        records = snapshot["records"]
        repair_points = snapshot["repair_points"]
        photos = await run_blocking(get_all_photos)
        groups = snapshot["groups"]
        if group_key == "need_today":
            items = merge_service_items(groups.get("urgent", []), groups.get("need_today", []))
        else:
            items = groups.get(group_key, [])
    except Exception as e:
        await show_text_screen(
            query,
            context,
            f"❌ Ошибка: {e}",
            reply_markup=back_markup("service_today", "⬅️ Назад к сводке"),
        )
        return SERVICE_MENU_SECTION

    title = group_titles[group_key]
    if not items:
        await show_text_screen(
            query,
            context,
            f"{title}\n\n{empty_texts[group_key]}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Назад к сводке", callback_data="service_today")],
                [InlineKeyboardButton("⬅️ К обслуживанию", callback_data="back_service_menu")],
                [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
            ]),
        )
        return SERVICE_MENU_SECTION

    await safe_delete_callback_message(query)
    header_message = await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=f"{title}\n\nТочек: {len(items)}",
    )
    remember_cleanup_message(context, header_message)

    for item in items:
        point = item["point"]
        if group_key == "repair":
            repair_item = item.get("repair_item") or repair_points.get(point)
            repair = (repair_item or {}).get("repair") or {}
            repair_id = repair.get("id", "")
            data = await run_blocking(get_repair_card_data, repair_id)
            card_text = build_repair_card_text(data) if data else f"🛠 {point}\n\n❌ Карточка ремонта не найдена."
            message = await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=card_text,
                parse_mode="HTML",
            )
            remember_cleanup_message(context, message)
            continue

        record, photo = get_point_latest_record_and_photo(point, records, photos)
        card_text = build_point_card_text(point, record)
        reason = item.get("reason")
        if reason:
            card_text += f"\n\n🔔 Почему здесь: {reason}"

        if photo and photo.get("File_ID"):
            message = await context.bot.send_photo(
                chat_id=query.message.chat_id,
                photo=photo["File_ID"],
                caption=card_text,
            )
            remember_cleanup_message(context, message)
        else:
            message = await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=card_text,
            )
            remember_cleanup_message(context, message)

    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="👆 Подробности по точкам",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🧹 Удалить карточки", callback_data="cleanup_cards")],
            [InlineKeyboardButton("⬅️ Назад к сводке", callback_data="service_today")],
            [InlineKeyboardButton("⬅️ К обслуживанию", callback_data="back_service_menu")],
            [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
        ]),
    )
    schedule_card_messages_cleanup(context, query.message.chat_id, query.from_user.id)
    return SERVICE_MENU_SECTION


async def info_menu(update: Update, context):
    query = update.callback_query
    keyboard = [[InlineKeyboardButton("📋 Все точки", callback_data="info_all")]]
    row = []
    for i, p in enumerate(POINTS):
        row.append(InlineKeyboardButton(p, callback_data=f"infop_{p}"))
        if len(row) == 2 or i == len(POINTS) - 1:
            keyboard.append(row)
            row = []
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_service_menu")])
    await show_text_screen(query, context, "📋 Информация по точкам:", reply_markup=InlineKeyboardMarkup(keyboard))
    return INFO_MENU

async def info_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    d = query.data
    if d == "back_main":
        return await start(update, context)
    elif d == "cleanup_cards":
        deleted = await cleanup_tracked_messages(context, context.bot, query.message.chat_id)
        await show_text_screen(
            query,
            context,
            f"🧹 Удалено карточек: {deleted}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ К информации", callback_data="back_info")],
                [InlineKeyboardButton("⬅️ К обслуживанию", callback_data="back_service_menu")],
                [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
            ]),
        )
        return INFO_MENU
    elif d == "back_service_menu":
        return await show_service_section_menu(query, context)
    elif d == "back_info":
        return await info_menu(update, context)
    elif d == "info_all":
        return await info_all(update, context)
    elif d.startswith("infop_"):
        return await info_point(update, context)
    return INFO_MENU

async def info_all(update: Update, context):
    query = update.callback_query
    try:
        await show_loading_state(query, context, "Загружаю информацию по точкам...")
        records = await run_blocking(get_all_services)
        photos = await run_blocking(get_all_photos)
    except Exception as e:
        await show_text_screen(query, context, f"❌ Ошибка: {e}")
        return INFO_MENU

    photo_items = []
    no_photo_text = ""
    summary_points = []

    for point in POINTS:
        pr = [r for r in records if r.get("Точка") == point]
        pp = [p for p in photos if p.get("Точка") == point]
        ordered_records = sorted_by_date(pr)
        ordered_photos = sorted_by_date(pp)
        last = ordered_records[0] if ordered_records else None
        last_ph = None
        if last and ordered_photos:
            last_ph = next((p for p in ordered_photos if p.get("Дата") == last.get("Дата")), ordered_photos[0])
        elif ordered_photos:
            last_ph = ordered_photos[0]

        summary_points.append((point, last))
        cap = build_point_card_text(point, last)

        if last_ph and last_ph.get("File_ID"):
            photo_items.append({"file_id": last_ph["File_ID"], "caption": cap})
        elif last:
            no_photo_text += cap + "\n\n"

    summary_text = build_info_all_summary(summary_points)

    if photo_items:
        await safe_delete_callback_message(query)
        summary_message = await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=summary_text,
            parse_mode="HTML",
        )
        remember_cleanup_message(context, summary_message)
        for item in photo_items:
            message = await context.bot.send_photo(
                chat_id=query.message.chat_id,
                photo=item["file_id"],
                caption=item["caption"],
            )
            remember_cleanup_message(context, message)
        if no_photo_text:
            message = await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"📄 Точки без фото\n\n{no_photo_text.strip()}",
            )
            remember_cleanup_message(context, message)
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="👆 Карточки по точкам",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🧹 Удалить карточки", callback_data="cleanup_cards")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="back_info")],
            ]),
        )
        schedule_card_messages_cleanup(context, query.message.chat_id, query.from_user.id)
    else:
        details = no_photo_text.strip()
        text = summary_text if not details else (
            f"{summary_text}\n\n<b>📄 Подробности</b>\n\n{escape_html(details)}"
        )
        await show_text_screen(
            query,
            context,
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Назад", callback_data="back_info")],
            ]),
        )
    return INFO_MENU

async def info_point(update: Update, context):
    query = update.callback_query
    point = query.data.replace("infop_", "")
    try:
        await show_loading_state(query, context, "Загружаю карточку точки...")
        records = await run_blocking(get_all_services)
        photos = await run_blocking(get_all_photos)
    except Exception as e:
        await show_text_screen(query, context, f"❌ Ошибка: {e}")
        return INFO_MENU

    pr = [r for r in records if r.get("Точка") == point]
    pp = [p for p in photos if p.get("Точка") == point]
    ordered_records = sorted_by_date(pr)
    ordered_photos = sorted_by_date(pp)
    last_ph = None
    if ordered_records and ordered_photos:
        last_ph = next((p for p in ordered_photos if p.get("Дата") == ordered_records[0].get("Дата")), ordered_photos[0])
    elif ordered_photos:
        last_ph = ordered_photos[0]

    if ordered_records:
        last = ordered_records[0]
        text = build_point_card_text(point, last, include_history=True, history_records=ordered_records)
    else:
        text = f"📍 {point}\n\n❌ Нет данных"

    kb = [[InlineKeyboardButton("⬅️ Назад", callback_data="back_info")]]
    if last_ph and last_ph.get("File_ID"):
        await safe_delete_callback_message(query)
        await context.bot.send_photo(chat_id=query.message.chat_id, photo=last_ph["File_ID"], caption=text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await show_text_screen(query, context, text, reply_markup=InlineKeyboardMarkup(kb))
    return INFO_MENU
# ============ ОБСЛУЖИВАНИЕ ============
async def service_who(update: Update, context):
    query = update.callback_query
    current_svc = context.user_data.get("svc", {})
    if current_svc.get("edit_mode"):
        context.user_data["svc"] = {
            key: value
            for key, value in current_svc.items()
            if key not in {"pidx", "sidx", "purchase_input_mode", "purchase_custom_flow"}
        }
    else:
        context.user_data["svc"] = {}
    kb = [[InlineKeyboardButton(w, callback_data=f"sw_{w}")] for w in get_worker_names()]
    kb.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_service_menu")])
    kb.append([InlineKeyboardButton("🏠 В меню", callback_data="back_main")])
    await query.edit_message_text(
        f"{build_service_progress_text(1)}\n\n🔧 Кто обслуживает?",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return SERVICE_WHO

async def service_who_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "back_service_menu":
        return await show_service_section_menu(query, context)
    if query.data == "back_main":
        return await start(update, context)
    who = query.data.replace("sw_", "")
    svc = context.user_data["svc"]
    previous_who = svc.get("who", "")
    previous_workers = normalize_salary_workers(svc.get("salary_workers", [])) if "salary_workers" in svc else None
    svc["who"] = who
    svc["date"] = today()
    if previous_workers is None or previous_workers == default_service_salary_workers(previous_who):
        svc["salary_workers"] = default_service_salary_workers(who)
    return await show_service_date_menu(query, context)


async def service_date_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    svc = context.user_data.get("svc", {})

    if query.data == "back_service_who":
        if svc.get("return_mode") == "payout" and svc.get("locked_who"):
            payout_worker = svc.get("return_worker", "")
            if payout_worker:
                get_payout_context(context)["worker"] = payout_worker
            return await show_payout_screen(
                query,
                context,
                screen=svc.get("return_screen", "overview"),
                period_key=svc.get("return_period"),
            )
        return await service_who(update, context)

    if query.data == "svc_date_custom":
        await show_text_screen(
            query,
            context,
            "📅 Введите дату в формате дд.мм:\n"
            "Не старше 1 года и не позже сегодня.",
            reply_markup=back_markup("back_service_date"),
        )
        return SERVICE_DATE_CUSTOM

    if query.data == "svc_date_today":
        context.user_data["svc"]["date"] = today()
    elif query.data == "svc_date_yesterday":
        context.user_data["svc"]["date"] = yesterday()
    elif query.data == "svc_date_daybefore":
        context.user_data["svc"]["date"] = day_before_yesterday()

    error = get_period_restriction_error(context.user_data["svc"].get("date"), svc.get("allowed_period"))
    if error:
        return await show_service_date_menu(query, context, notice=error)

    return await show_service_points(query, context)


async def service_date_custom_back_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "back_service_date":
        return await show_service_date_menu(query, context)
    return SERVICE_DATE_CUSTOM


async def service_date_custom_handler(update: Update, context):
    entered_date = update.message.text.strip()
    parsed, error_text = validate_manual_date_input(entered_date)
    if not parsed:
        await update.message.reply_text(
            error_text,
            reply_markup=back_markup("back_service_date"),
        )
        return SERVICE_DATE_CUSTOM

    context.user_data["svc"]["date"] = format_date(parsed)
    allowed_period = context.user_data.get("svc", {}).get("allowed_period")
    error = get_period_restriction_error(context.user_data["svc"]["date"], allowed_period)
    if error:
        await update.message.reply_text(
            error,
            reply_markup=back_markup("back_service_date"),
        )
        return SERVICE_DATE_CUSTOM
    who = context.user_data.get("svc", {}).get("who", "")
    repair_points = await run_blocking(get_active_repair_record_map)
    title = f"🔧 {who}\n📅 {context.user_data['svc']['date']}\n\nВыберите точку:"
    if repair_points:
        title += f"\n\n🛠 На ремонте: {', '.join(repair_points.keys())}"
    await update.message.reply_text(title, reply_markup=build_service_points_markup(repair_points))
    return SERVICE_POINT


async def delete_date_handler(update: Update, context):
    query = update.callback_query
    await query.answer()

    if query.data == "back_main":
        return await start(update, context)
    if query.data == "back_service_fix":
        return await show_service_fix_menu(query, context)
    if query.data == "back_service_menu":
        return await show_service_section_menu(query, context)

    all_entries = await run_blocking(get_all_services_with_rows)

    if query.data == "del_date_custom":
        await show_text_screen(
            query,
            context,
            "📅 Введите дату в формате дд.мм:\n"
            "Не старше 1 года и не позже сегодня.",
            reply_markup=back_markup("back_delete_date"),
        )
        return DELETE_DATE_CUSTOM

    if query.data == "del_date_latest":
        context.user_data["delete"]["date"] = latest_service_date(all_entries)
    elif query.data == "del_date_today":
        context.user_data["delete"]["date"] = today()
    elif query.data == "del_date_yesterday":
        context.user_data["delete"]["date"] = yesterday()
    elif query.data == "del_date_daybefore":
        context.user_data["delete"]["date"] = day_before_yesterday()

    context.user_data["delete"].pop("mode", None)
    context.user_data["delete"].pop("point", None)
    context.user_data["delete"].pop("entry", None)
    return await show_fix_action_menu(query, context)


async def delete_date_custom_back_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "back_delete_date":
        return await show_delete_date_menu(query, context)
    return DELETE_DATE_CUSTOM


async def delete_date_custom_handler(update: Update, context):
    entered_date = update.message.text.strip()
    parsed, error_text = validate_manual_date_input(entered_date)
    if not parsed:
        await update.message.reply_text(
            error_text,
            reply_markup=back_markup("back_delete_date"),
        )
        return DELETE_DATE_CUSTOM

    context.user_data["delete"]["date"] = format_date(parsed)
    context.user_data["delete"].pop("mode", None)
    context.user_data["delete"].pop("point", None)
    context.user_data["delete"].pop("entry", None)
    selected_date = context.user_data["delete"]["date"]
    all_entries = await run_blocking(get_all_services_with_rows)
    entries = [entry for entry in all_entries if entry.get("Дата") == selected_date]

    if not entries:
        await update.message.reply_text(
            f"✏️ Исправить запись\n\nЗа {selected_date} записей обслуживания нет.",
            reply_markup=back_markup("back_delete_date"),
        )
        return DELETE_POINT

    points = order_points({entry.get("Точка", "") for entry in entries if entry.get("Точка")})
    kb = [
        [InlineKeyboardButton("✏️ Изменить запись", callback_data="fix_action_edit")],
        [InlineKeyboardButton("🗑 Удалить одну запись", callback_data="fix_action_delete_one", style="danger")],
        [InlineKeyboardButton(f"🗑 Удалить все записи за день ({len(entries)})", callback_data="fix_action_delete_day", style="danger")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_delete_date")],
    ]
    await update.message.reply_text(
        f"✏️ Исправить запись\n\n📅 {selected_date}\n"
        f"Записей: {len(entries)}\n"
        f"Точки: {', '.join(points)}",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return DELETE_POINT


async def delete_point_handler(update: Update, context):
    query = update.callback_query
    await query.answer()

    if query.data == "back_delete_date":
        return await show_delete_date_menu(query, context)
    if query.data == "back_fix_actions":
        return await show_fix_action_menu(query, context)
    if query.data == "fix_action_edit":
        context.user_data["delete"]["mode"] = "edit"
        context.user_data["delete"].pop("point", None)
        context.user_data["delete"].pop("entry", None)
        return await show_delete_point_menu(query, context)
    if query.data == "fix_action_delete_one":
        context.user_data["delete"]["mode"] = "delete_one"
        context.user_data["delete"].pop("point", None)
        context.user_data["delete"].pop("entry", None)
        return await show_delete_point_menu(query, context)
    if query.data == "fix_action_delete_day":
        context.user_data["delete"]["mode"] = "delete_day"
        context.user_data["delete"].pop("point", None)
        context.user_data["delete"].pop("entry", None)
        return await show_delete_day_confirm_menu(query, context)

    point = query.data.replace("del_point_", "")
    context.user_data["delete"]["point"] = point
    context.user_data["delete"].pop("entry", None)
    return await show_delete_entry_menu(query, context)


async def delete_entry_handler(update: Update, context):
    query = update.callback_query
    await query.answer()

    if query.data == "back_delete_point":
        return await show_delete_point_menu(query, context)

    row_num = int(query.data.replace("del_entry_", ""))
    selected_date = context.user_data.get("delete", {}).get("date")
    point = context.user_data.get("delete", {}).get("point")
    entries = await run_blocking(get_all_services_with_rows)
    entry = next(
        (
            item for item in entries
            if item["__row"] == row_num
            and item.get("Дата") == selected_date
            and item.get("Точка") == point
        ),
        None,
    )

    if not entry:
        await show_text_screen(
            query,
            context,
            "❌ Запись не найдена. Попробуйте ещё раз.",
            reply_markup=back_markup("back_delete_point"),
        )
        return DELETE_ENTRY

    context.user_data["delete"]["entry"] = entry
    context.user_data["delete"].pop("salary_workers_draft", None)
    if context.user_data["delete"].get("mode") == "edit":
        return await show_fix_entry_action_menu(query, context)
    return await show_delete_confirm_menu(query, context)


async def delete_confirm_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    delete_data = context.user_data.setdefault("delete", {})

    if query.data == "back_main":
        return await start(update, context)
    if query.data == "service_fix":
        return await show_service_fix_menu(query, context)
    if query.data == "back_service_fix":
        return await show_service_fix_menu(query, context)
    if query.data == "delete_service":
        context.user_data["delete"] = {}
        return await show_delete_date_menu(query, context)
    if query.data == "back_fix_actions":
        return await show_fix_action_menu(query, context)
    if query.data == "back_fix_entry_actions":
        delete_data.pop("salary_workers_draft", None)
        return await show_fix_entry_action_menu(query, context)
    if query.data == "back_delete_entry":
        return await show_delete_entry_menu(query, context)
    if query.data == "fix_entry_edit":
        return await begin_service_edit_from_entry(query, context)
    if query.data == "fix_entry_salary":
        return await show_fix_entry_salary_menu(query, context)
    if query.data == "fix_entry_delete":
        return await show_delete_confirm_menu(query, context)
    if query.data == "fix_entry_salary_reset":
        entry = delete_data.get("entry", {})
        delete_data["salary_workers_draft"] = default_service_salary_workers(entry.get("Кто", ""))
        return await show_fix_entry_salary_menu(query, context)
    if query.data == "fix_entry_salary_clear":
        delete_data["salary_workers_draft"] = []
        return await show_fix_entry_salary_menu(query, context)
    if query.data.startswith("fix_entry_salary_toggle_"):
        try:
            worker = get_paid_workers()[int(query.data.rsplit("_", 1)[1])]
        except (ValueError, IndexError):
            return await show_fix_entry_salary_menu(query, context)
        selected = normalize_salary_workers(delete_data.get("salary_workers_draft", []))
        if worker in selected:
            selected = [item for item in selected if item != worker]
        else:
            selected.append(worker)
        delete_data["salary_workers_draft"] = normalize_salary_workers(selected)
        return await show_fix_entry_salary_menu(query, context)
    if query.data == "fix_entry_salary_save":
        entry = delete_data.get("entry")
        if not entry:
            await show_text_screen(
                query,
                context,
                "❌ Не удалось определить запись.",
                reply_markup=back_markup("back_delete_entry"),
            )
            return DELETE_CONFIRM
        selected = normalize_salary_workers(delete_data.get("salary_workers_draft", []))
        payload = build_service_update_data_from_entry(entry, selected)
        try:
            await run_blocking(update_service_row, entry["__row"], payload)
            entry["Сумма обслуж"] = payload["service_sum"]
            entry["В ЗП"] = serialize_salary_workers(selected)
            try:
                await refresh_group_service_today_posts(context.application, force=True)
            except Exception:
                logger.exception("Failed to refresh group service-today post after salary workers update")
            delete_data.pop("salary_workers_draft", None)
            label = entry["В ЗП"] or "не считать"
            return await show_fix_entry_action_menu(
                query,
                context,
                notice=f"✅ В ЗП обновлено: {label} · {format_money(payload['service_sum'])}",
            )
        except APIError as error:
            if is_google_sheets_busy_error(error):
                await show_sheets_busy_notice(
                    query,
                    context,
                    retry_callback="fix_entry_salary_save",
                    back_callback="fix_entry_salary",
                )
                return DELETE_CONFIRM
            raise

    if query.data == "del_day_confirm_yes":
        selected_date = delete_data.get("date")
        try:
            deleted_services, deleted_photos = await run_blocking(delete_service_entries_for_date, selected_date)
            try:
                await refresh_group_service_today_posts(context.application, force=True)
            except Exception:
                logger.exception("Failed to refresh group service-today post after day delete")
            text = (
                f"✅ Все записи за {selected_date} удалены.\n\n"
                f"Удалено обслуживаний: {deleted_services}\n"
                f"Удалено фото: {deleted_photos}"
            )
        except Exception:
            logger.exception("Failed to delete service entries for date %s", selected_date)
            text = "❌ Не удалось удалить записи за день."

        kb = [
            [InlineKeyboardButton("✏️ Исправить ещё", callback_data="service_fix")],
            [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
        ]
        await show_text_screen(query, context, text, reply_markup=InlineKeyboardMarkup(kb))
        return DELETE_CONFIRM

    if query.data != "del_confirm_yes":
        logger.warning("delete_confirm_handler got unexpected callback %r — ignoring", query.data)
        return DELETE_CONFIRM

    entry = delete_data.get("entry")
    if not entry:
        await show_text_screen(
            query,
            context,
            "❌ Не удалось определить запись для удаления.",
            reply_markup=back_markup("back_delete_point"),
        )
        return DELETE_CONFIRM

    try:
        photos = await run_blocking(get_all_photos_with_rows)
        photo_entry = find_matching_photo_row(entry, photos)
        await run_blocking(delete_service_entry, entry["__row"], photo_entry["__row"] if photo_entry else None)
        try:
            await refresh_group_service_today_posts(context.application, force=True)
        except Exception:
            logger.exception("Failed to refresh group service-today post after service delete")
        text = (
            "✅ Запись удалена.\n\n"
            f"{build_service_entry_text(entry)}\n\n"
            "Теперь можно занести её заново через «Обслуживание»."
        )
    except Exception:
        logger.exception("Failed to delete single service entry")
        text = "❌ Не удалось удалить запись."

    kb = [
        [InlineKeyboardButton("✏️ Исправить ещё", callback_data="service_fix")],
        [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
    ]
    await show_text_screen(query, context, text, reply_markup=InlineKeyboardMarkup(kb))
    return DELETE_CONFIRM

async def service_point_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "back_service_date":
        return await show_service_date_menu(query, context)
    point = query.data.replace("sp_", "")
    repair_points = await run_blocking(get_active_repair_record_map)
    if point in repair_points:
        return await show_service_points(query, context, notice=f"⚪ Точка {point} сейчас на ремонте и не попадает в обслуживание.")
    context.user_data["svc"]["point"] = point
    return await show_service_photo_prompt(query, context)


async def service_photo_back_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "back_service_point":
        return await show_service_points(query, context)
    if query.data == "keep_service_photo":
        return await show_service_water_menu(query, context)
    return SERVICE_PHOTO

async def service_photo_handler(update: Update, context):
    if not update.message.photo:
        await update.message.reply_text("❌ Отправьте фото!")
        return SERVICE_PHOTO

    file_id = update.message.photo[-1].file_id
    svc = context.user_data["svc"]
    svc["photo"] = file_id

    try:
        cap = f"📸 {svc['point']}\n📅 {svc['date']}\n👤 {svc['who']}"
        await context.bot.send_photo(chat_id=PHOTO_CHAT_ID, photo=file_id, caption=cap)
    except Exception as e:
        logger.error(f"Photo send error: {e}")

    kb = [
        [InlineKeyboardButton(str(i), callback_data=f"swa_{i}") for i in range(6)],
        [InlineKeyboardButton("Другое", callback_data="swa_custom")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_service_photo")],
    ]
    await update.message.reply_text(f"💧 {svc['point']}\n\nСколько бутылок воды?", reply_markup=InlineKeyboardMarkup(kb))
    return SERVICE_WATER

async def service_water_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "back_service_photo":
        return await show_service_photo_prompt(query, context)
    if query.data == "swa_custom":
        await query.edit_message_text("💧 Введите количество:", reply_markup=back_markup("back_service_water"))
        return SERVICE_WATER_CUSTOM
    context.user_data["svc"]["water"] = query.data.replace("swa_", "")
    return await show_purchase_question(query, context)


async def service_water_custom_back_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "back_service_water":
        return await show_service_water_menu(query, context)
    return SERVICE_WATER_CUSTOM

async def service_water_custom_handler(update: Update, context):
    try:
        w = float(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text(
            "❌ Введите число (например: 2,4):",
            reply_markup=back_markup("back_service_water")
        )
        return SERVICE_WATER_CUSTOM
    context.user_data["svc"]["water"] = str(w).replace(".", ",")
    kb = [
        [InlineKeyboardButton("✅ Да", callback_data="spu_yes"),
         InlineKeyboardButton("❌ Нет", callback_data="spu_no")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_service_water")],
    ]
    await update.message.reply_text(f"🛒 Были закупки?", reply_markup=InlineKeyboardMarkup(kb))
    return SERVICE_PURCHASE

async def service_purchase_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "back_service_water":
        return await show_service_water_menu(query, context)
    if query.data == "spu_no":
        context.user_data["svc"]["plist"] = []
        context.user_data["svc"]["purchases"] = ""
        context.user_data["svc"]["purchase_sum"] = 0
        context.user_data["svc"]["purchase_parse_failed"] = False
        context.user_data["svc"].pop("pidx", None)
        context.user_data["svc"].pop("purchase_input_mode", None)
        context.user_data["svc"].pop("purchase_custom_flow", None)
        return await ask_shortage(query, context)

    if context.user_data["svc"].get("plist"):
        return await show_purch_select(query, context)

    context.user_data["svc"]["plist"] = []
    context.user_data["svc"].pop("purchase_input_mode", None)
    context.user_data["svc"].pop("purchase_custom_flow", None)
    return await show_purch_select(query, context)

def build_purchase_select_markup(context):
    sel = [s["name"] for s in context.user_data["svc"].get("plist", [])]
    kb = []
    for item in PURCHASE_ITEMS:
        c = "✅" if item in sel else "☐"
        kb.append([InlineKeyboardButton(f"{c} {item}", callback_data=f"sps_{item}")])
    for s in context.user_data["svc"].get("plist", []):
        if s["name"] not in PURCHASE_ITEMS:
            kb.append([InlineKeyboardButton(f"✅ {s['name']}", callback_data=f"sps_{s['name']}")])
    kb.append([InlineKeyboardButton("✅ Готово", callback_data="sps_done")])
    kb.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_service_purchase")])
    return InlineKeyboardMarkup(kb)


async def show_purch_select(query, context):
    await query.edit_message_text("🛒 Что купили?", reply_markup=build_purchase_select_markup(context))
    return SERVICE_PURCHASE_SELECT


async def show_purch_select_message(message, context):
    await message.reply_text("🛒 Что купили?", reply_markup=build_purchase_select_markup(context))
    return SERVICE_PURCHASE_SELECT


async def show_custom_purchase_name_prompt(query, context):
    context.user_data["svc"]["purchase_input_mode"] = "custom_name"
    context.user_data["svc"]["purchase_custom_flow"] = True
    await query.edit_message_text(
        "✏️ Что купили? Напишите название:",
        reply_markup=back_markup("back_service_purchase_select"),
    )
    return SERVICE_PURCHASE_OTHER_NAME


async def show_custom_purchase_qty_prompt(query, context):
    svc = context.user_data["svc"]
    idx = svc.get("pidx")
    pl = svc.get("plist", [])
    if idx is None or idx >= len(pl):
        return await show_purch_select(query, context)
    svc["purchase_input_mode"] = "custom_qty"
    await show_text_screen(
        query,
        context,
        f"🛒 {pl[idx]['name']} — введите количество (можно дробное, например 2,4):",
        reply_markup=back_markup("back_service_purchase_qty"),
    )
    return SERVICE_PURCHASE_OTHER_NAME


async def show_custom_purchase_qty_prompt_message(message, context):
    svc = context.user_data["svc"]
    idx = svc.get("pidx")
    pl = svc.get("plist", [])
    if idx is None or idx >= len(pl):
        return await show_purch_select_message(message, context)
    svc["purchase_input_mode"] = "custom_qty"
    await message.reply_text(
        f"🛒 {pl[idx]['name']} — введите количество (можно дробное, например 2,4):",
        reply_markup=back_markup("back_service_purchase_qty"),
    )
    return SERVICE_PURCHASE_OTHER_NAME


async def show_purch_qty_prompt_message(message, context):
    pl = context.user_data["svc"]["plist"]
    idx = context.user_data["svc"]["pidx"]
    item = pl[idx]
    kb = [
        [InlineKeyboardButton(str(i), callback_data=f"spq_{i}") for i in range(1, 6)],
        [InlineKeyboardButton("Другое", callback_data="spq_custom")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_service_purchase_select")],
    ]
    await message.reply_text(f"🛒 {item['name']} — сколько?", reply_markup=InlineKeyboardMarkup(kb))
    return SERVICE_PURCHASE_QTY


def finalize_purchase_summary(svc):
    purchases, total = build_purchase_summary(svc.get("plist", []))
    svc["purchases"] = purchases
    svc["purchase_sum"] = total
    svc["purchase_parse_failed"] = False


def remove_pending_custom_purchase(svc):
    idx = svc.get("pidx")
    pl = svc.get("plist", [])
    if idx is not None and 0 <= idx < len(pl) and pl[idx].get("is_custom") and pl[idx].get("sum") is None:
        pl.pop(idx)
        svc["plist"] = pl
    svc.pop("pidx", None)
    svc.pop("purchase_custom_flow", None)


async def show_purchase_question_message(message, context):
    point = context.user_data.get("svc", {}).get("point", "")
    kb = [
        [InlineKeyboardButton("✅ Да", callback_data="spu_yes"),
         InlineKeyboardButton("❌ Нет", callback_data="spu_no")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_service_water")],
    ]
    await message.reply_text(
        f"{build_service_progress_text(6)}\n\n🛒 {point}\n\nБыли закупки?",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return SERVICE_PURCHASE

async def service_purch_select_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "back_service_purchase":
        return await show_purchase_question(query, context)
    d = query.data.replace("sps_", "")

    if d == "done":
        svc = context.user_data["svc"]
        pl = svc.get("plist", [])
        if not pl:
            if svc.get("purchase_parse_failed") and str(svc.get("purchases", "")).strip():
                return await ask_shortage(query, context)
            svc["purchases"] = ""
            svc["purchase_sum"] = 0
            return await ask_shortage(query, context)
        next_idx = get_next_unfilled_purchase_index(pl)
        if next_idx is None:
            finalize_purchase_summary(svc)
            return await ask_shortage(query, context)
        svc["pidx"] = next_idx
        return await ask_purch_qty(query, context)

    if d == "Другое":
        context.user_data["svc"]["purchase_custom_flow"] = True
        return await show_custom_purchase_name_prompt(query, context)

    pl = context.user_data["svc"].get("plist", [])
    names = [s["name"] for s in pl]
    if d in names:
        pl = [s for s in pl if s["name"] != d]
    else:
        pl.append({"name": d, "qty": None, "sum": None})
    context.user_data["svc"]["plist"] = pl
    return await show_purch_select(query, context)

async def service_purch_other_handler(update: Update, context):
    text = update.message.text.strip()
    svc = context.user_data["svc"]
    pl = svc.get("plist", [])
    idx = svc.get("pidx")
    mode = svc.get("purchase_input_mode")

    if mode == "qty":
        try:
            qty = normalize_number_text(text)
        except ValueError:
            await update.message.reply_text(
                "❌ Введите количество числом, например 2,4:",
                reply_markup=back_markup("back_service_purchase_qty"),
            )
            return SERVICE_PURCHASE_OTHER_NAME

        pl[idx]["qty"] = qty
        svc.pop("purchase_input_mode", None)
        await update.message.reply_text(
            f"💰 {pl[idx]['name']} ({format_number(qty)}) — введите стоимость (₽):",
            reply_markup=back_markup("back_service_purchase_qty"),
        )
        return SERVICE_PURCHASE_SUM

    if mode == "custom_name":
        if not text:
            await update.message.reply_text(
                "❌ Напишите название товара:",
                reply_markup=back_markup("back_service_purchase_select"),
            )
            return SERVICE_PURCHASE_OTHER_NAME

        pl.append({"name": text, "qty": None, "sum": None, "is_custom": True})
        svc["plist"] = pl
        svc["pidx"] = len(pl) - 1
        return await show_custom_purchase_qty_prompt_message(update.message, context)

    if mode == "custom_qty":
        try:
            qty = normalize_number_text(text)
        except ValueError:
            await update.message.reply_text(
                "❌ Введите количество числом, например 2,4:",
                reply_markup=back_markup("back_service_purchase_qty"),
            )
            return SERVICE_PURCHASE_OTHER_NAME

        pl[idx]["qty"] = qty
        svc.pop("purchase_input_mode", None)
        await update.message.reply_text(
            f"💰 {pl[idx]['name']} ({format_number(qty)}) — введите стоимость (₽):",
            reply_markup=back_markup("back_service_purchase_qty"),
        )
        return SERVICE_PURCHASE_SUM

    return await show_purch_select_message(update.message, context)


async def service_purch_other_back_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    svc = context.user_data["svc"]
    mode = svc.get("purchase_input_mode")
    svc.pop("purchase_input_mode", None)
    if mode == "qty":
        return await ask_purch_qty(query, context)
    if mode == "custom_qty":
        remove_pending_custom_purchase(svc)
        return await show_custom_purchase_name_prompt(query, context)
    if mode == "custom_name":
        svc.pop("purchase_custom_flow", None)
    return await show_purch_select(query, context)


async def ask_purch_qty(query, context):
    pl = context.user_data["svc"]["plist"]
    idx = context.user_data["svc"]["pidx"]
    if idx >= len(pl):
        finalize_purchase_summary(context.user_data["svc"])
        return await ask_shortage(query, context)
    item = pl[idx]
    kb = [
        [InlineKeyboardButton(str(i), callback_data=f"spq_{i}") for i in range(1, 6)],
        [InlineKeyboardButton("Другое", callback_data="spq_custom")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_service_purchase_select")],
    ]
    await query.edit_message_text(f"🛒 {item['name']} — сколько?", reply_markup=InlineKeyboardMarkup(kb))
    return SERVICE_PURCHASE_QTY

async def service_purch_qty_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "back_service_purchase_select":
        return await show_purch_select(query, context)
    d = query.data.replace("spq_", "")
    if d == "custom":
        context.user_data["svc"]["purchase_input_mode"] = "qty"
        await query.edit_message_text(
            "🛒 Введите количество (можно дробное, например 2,4):",
            reply_markup=back_markup("back_service_purchase_qty")
        )
        return SERVICE_PURCHASE_OTHER_NAME
    qty = normalize_number_text(d)
    pl = context.user_data["svc"]["plist"]
    idx = context.user_data["svc"]["pidx"]
    pl[idx]["qty"] = qty
    await query.edit_message_text(
        f"💰 {pl[idx]['name']} ({format_number(qty)}) — введите стоимость (₽):",
        reply_markup=back_markup("back_service_purchase_qty")
    )
    return SERVICE_PURCHASE_SUM


async def service_purch_sum_back_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "back_service_purchase_qty":
        if context.user_data["svc"].get("purchase_custom_flow"):
            return await show_custom_purchase_qty_prompt(query, context)
        return await ask_purch_qty(query, context)
    return SERVICE_PURCHASE_SUM

async def service_purch_sum_handler(update: Update, context):
    svc = context.user_data["svc"]
    try:
        price = float(update.message.text.replace(",", "."))
        if price < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "❌ Введите число:",
            reply_markup=back_markup("back_service_purchase_qty")
        )
        return SERVICE_PURCHASE_SUM
    pl = svc["plist"]
    idx = svc["pidx"]
    pl[idx]["sum"] = price
    if svc.get("purchase_custom_flow"):
        svc.pop("purchase_custom_flow", None)
        svc.pop("pidx", None)
        return await show_purch_select_message(update.message, context)

    next_idx = get_next_unfilled_purchase_index(pl)
    if next_idx is None:
        finalize_purchase_summary(svc)
        return await ask_shortage_message(update.message, context)

    svc["pidx"] = next_idx
    return await show_purch_qty_prompt_message(update.message, context)
# ============ НЕХВАТКА ============
async def ask_shortage(query, context):
    kb = [[InlineKeyboardButton("✅ Да", callback_data="ssh_yes"),
           InlineKeyboardButton("❌ Нет", callback_data="ssh_no")],
          [InlineKeyboardButton("⬅️ Назад", callback_data="back_service_shortage_prev")]]
    await query.edit_message_text(
        f"{build_service_progress_text(7)}\n\n❓ Есть нехватка расходников?",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return SERVICE_SHORTAGE


async def ask_shortage_message(message, context):
    kb = [[InlineKeyboardButton("✅ Да", callback_data="ssh_yes"),
           InlineKeyboardButton("❌ Нет", callback_data="ssh_no")],
          [InlineKeyboardButton("⬅️ Назад", callback_data="back_service_shortage_prev")]]
    await message.reply_text(
        f"{build_service_progress_text(7)}\n\n❓ Есть нехватка расходников?",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return SERVICE_SHORTAGE


def build_short_qty_markup(item_name):
    options = get_shortage_options(item_name)
    labels = [value.replace(".", ",") for value in options]
    callbacks = [value for value in options]
    kb = [[
        InlineKeyboardButton(label, callback_data=f"ssqv_{value}")
        for label, value in zip(labels, callbacks)
    ]]
    kb.append([InlineKeyboardButton("Другое", callback_data="ssq_custom")])
    kb.append([InlineKeyboardButton("🚫 Запаса для пополнения нет", callback_data="ssq_no_reserve")])
    kb.append([InlineKeyboardButton("Пропустить", callback_data="ssq_skip")])
    kb.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_service_shortage_select")])
    return InlineKeyboardMarkup(kb)


def build_short_qty_prompt_text(item):
    unit = get_supply_unit(item["name"])
    return f"📦 {item['name']} — сколько осталось в запасе для пополнения ({unit})?"


async def show_short_qty_prompt_message(message, context):
    svc = context.user_data["svc"]
    sl = svc.get("slist", [])
    idx = svc.get("sidx", 0)
    if idx >= len(sl):
        finalize_shortage_summary(svc)
        return await show_confirm_msg(message, context)
    item = ensure_shortage_item_shape(sl[idx])
    await message.reply_text(
        build_short_qty_prompt_text(item),
        reply_markup=build_short_qty_markup(item["name"]),
    )
    return SERVICE_SHORTAGE_QTY


async def ask_short_next_visit(query, context):
    svc = context.user_data["svc"]
    item = ensure_shortage_item_shape(svc["slist"][svc["sidx"]])
    kb = [
        [InlineKeyboardButton("✅ Хватит до следующего приезда", callback_data="ssn_enough")],
        [InlineKeyboardButton("🚨 Не хватит до следующего приезда", callback_data="ssn_not_enough")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_service_shortage_next_visit")],
    ]
    await query.edit_message_text(
        f"🚫 {item['name']} — запаса для пополнения нет.\n\nХватит ли до следующего приезда?",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return SERVICE_SHORTAGE_NEXT_VISIT


def finalize_shortage_summary(svc):
    shortage, shortage_qty = build_shortage_summary_and_details(svc.get("slist", []))
    svc["shortage"] = shortage
    svc["shortage_qty"] = shortage_qty
    svc["shortage_parse_failed"] = False


async def service_shortage_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "back_service_shortage_prev":
        return await back_to_purchase_stage(query, context)
    if query.data == "ssh_no":
        context.user_data["svc"]["shortage"] = ""
        context.user_data["svc"]["shortage_qty"] = ""
        context.user_data["svc"]["slist"] = []
        context.user_data["svc"]["shortage_parse_failed"] = False
        return await show_confirm(query, context)
    if context.user_data["svc"].get("slist"):
        return await show_short_select(query, context)
    context.user_data["svc"]["slist"] = []
    return await show_short_select(query, context)


async def show_short_select(query, context):
    sel = [s["name"] for s in context.user_data["svc"].get("slist", [])]
    kb = []
    row = []
    for i, item in enumerate(SUPPLIES):
        c = "✅" if item in sel else "☐"
        row.append(InlineKeyboardButton(f"{c} {item}", callback_data=f"sss_{item}"))
        if len(row) == 2 or i == len(SUPPLIES) - 1:
            kb.append(row)
            row = []
    kb.append([InlineKeyboardButton("✅ Готово", callback_data="sss_done")])
    kb.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_service_shortage")])
    await query.edit_message_text("⚠️ Чего не хватает?", reply_markup=InlineKeyboardMarkup(kb))
    return SERVICE_SHORTAGE_SELECT


async def service_short_select_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "back_service_shortage":
        return await ask_shortage(query, context)
    d = query.data.replace("sss_", "")
    if d == "done":
        svc = context.user_data["svc"]
        sl = svc.get("slist", [])
        if not sl:
            if svc.get("shortage_parse_failed") and (
                str(svc.get("shortage", "")).strip() or str(svc.get("shortage_qty", "")).strip()
            ):
                return await show_confirm(query, context)
            svc["shortage"] = ""
            svc["shortage_qty"] = ""
            return await show_confirm(query, context)
        svc["sidx"] = 0
        return await ask_short_qty(query, context)

    sl = context.user_data["svc"].get("slist", [])
    names = [s["name"] for s in sl]
    if d in names:
        sl = [s for s in sl if s["name"] != d]
    else:
        sl.append({
            "name": d,
            "reserve_qty": None,
            "no_reserve": False,
            "next_visit_status": None,
            "skipped": False,
        })
    context.user_data["svc"]["slist"] = sl
    return await show_short_select(query, context)


async def ask_short_qty(query, context):
    svc = context.user_data["svc"]
    sl = svc.get("slist", [])
    idx = svc.get("sidx", 0)
    if idx >= len(sl):
        finalize_shortage_summary(svc)
        return await show_confirm(query, context)

    item = ensure_shortage_item_shape(sl[idx])
    await query.edit_message_text(
        build_short_qty_prompt_text(item),
        reply_markup=build_short_qty_markup(item["name"]),
    )
    return SERVICE_SHORTAGE_QTY


async def service_short_qty_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    svc = context.user_data["svc"]
    sl = svc["slist"]
    idx = svc.get("sidx", 0)

    if query.data == "back_service_shortage_select":
        if idx > 0:
            svc["sidx"] = idx - 1
            return await ask_short_qty(query, context)
        return await show_short_select(query, context)

    current_item = ensure_shortage_item_shape(sl[idx])

    if query.data == "ssq_skip":
        current_item["reserve_qty"] = None
        current_item["no_reserve"] = False
        current_item["next_visit_status"] = None
        current_item["skipped"] = True
        svc["sidx"] = idx + 1
        return await ask_short_qty(query, context)

    if query.data == "ssq_custom":
        await query.edit_message_text(
            f"📦 {current_item['name']} — введите, сколько осталось в запасе для пополнения ({get_supply_unit(current_item['name'])}):",
            reply_markup=back_markup("back_service_shortage_qty"),
        )
        return SERVICE_SHORTAGE_QTY_CUSTOM

    if query.data == "ssq_no_reserve":
        current_item["reserve_qty"] = None
        current_item["no_reserve"] = True
        current_item["next_visit_status"] = None
        current_item["skipped"] = False
        return await ask_short_next_visit(query, context)

    if query.data.startswith("ssqv_"):
        value = query.data.replace("ssqv_", "")
        current_item["reserve_qty"] = normalize_number_text(value)
        current_item["no_reserve"] = False
        current_item["next_visit_status"] = None
        current_item["skipped"] = False
        svc["sidx"] = idx + 1
        return await ask_short_qty(query, context)

    return SERVICE_SHORTAGE_QTY


async def service_short_qty_custom_back_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "back_service_shortage_qty":
        return await ask_short_qty(query, context)
    return SERVICE_SHORTAGE_QTY_CUSTOM


async def service_short_qty_custom_handler(update: Update, context):
    try:
        val = normalize_number_text(update.message.text)
    except ValueError:
        svc = context.user_data["svc"]
        current_item = svc["slist"][svc["sidx"]]
        await update.message.reply_text(
            f"❌ Введите количество числом для «{current_item['name']}», например 0,5 или 120:",
            reply_markup=back_markup("back_service_shortage_qty"),
        )
        return SERVICE_SHORTAGE_QTY_CUSTOM

    svc = context.user_data["svc"]
    sl = svc["slist"]
    idx = svc["sidx"]
    current_item = ensure_shortage_item_shape(sl[idx])
    current_item["reserve_qty"] = val
    current_item["no_reserve"] = False
    current_item["next_visit_status"] = None
    current_item["skipped"] = False
    svc["sidx"] = idx + 1

    if svc["sidx"] >= len(sl):
        finalize_shortage_summary(svc)
        return await show_confirm_msg(update.message, context)

    return await show_short_qty_prompt_message(update.message, context)


async def service_short_next_visit_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    svc = context.user_data["svc"]

    if query.data == "back_service_shortage_next_visit":
        return await ask_short_qty(query, context)

    current_item = ensure_shortage_item_shape(svc["slist"][svc["sidx"]])
    if query.data == "ssn_enough":
        current_item["next_visit_status"] = "enough"
    elif query.data == "ssn_not_enough":
        current_item["next_visit_status"] = "not_enough"
    else:
        return SERVICE_SHORTAGE_NEXT_VISIT

    svc["sidx"] = svc["sidx"] + 1
    return await ask_short_qty(query, context)


# ============ ПОДТВЕРЖДЕНИЕ ============
async def show_confirm(query, context):
    svc = context.user_data["svc"]
    svc.pop("duplicate_match_rows", None)
    text = f"{build_service_progress_text(8)}\n\n{build_confirm_text(svc)}"
    kb = [[InlineKeyboardButton("✅ Подтвердить", callback_data="svc_ok"),
           InlineKeyboardButton("⬅️ Назад", callback_data="back_service_confirm")],
          [InlineKeyboardButton("❌ Отмена", callback_data="svc_cancel")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))
    return SERVICE_CONFIRM


async def show_confirm_msg(message, context):
    svc = context.user_data["svc"]
    svc.pop("duplicate_match_rows", None)
    text = f"{build_service_progress_text(8)}\n\n{build_confirm_text(svc)}"
    kb = [[InlineKeyboardButton("✅ Подтвердить", callback_data="svc_ok"),
           InlineKeyboardButton("⬅️ Назад", callback_data="back_service_confirm")],
          [InlineKeyboardButton("❌ Отмена", callback_data="svc_cancel")]]
    await message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))
    return SERVICE_CONFIRM


def build_confirm_text(svc):
    who = svc["who"]
    point = svc["point"]
    date = svc["date"]
    water = format_number(svc.get("water", "?"))
    purchases = svc.get("purchases", "")
    purchase_sum = svc.get("purchase_sum", 0)
    shortage = svc.get("shortage", "")
    shortage_qty = svc.get("shortage_qty", "")
    salary_workers = get_service_salary_workers_from_context(svc)
    service_total = calculate_service_sum_for_workers(salary_workers)

    lines = [
        "✅ Итог обслуживания:",
        "",
        f"📍 {point}",
        f"📅 {date}",
        f"👤 {who}",
        f"💧 Вода: {water} бут",
    ]

    if purchases:
        lines.append(f"🛒 Закупки: {purchases} — {format_money(purchase_sum)}")

    append_shortage_block(lines, shortage, shortage_qty)

    if salary_workers and salary_workers != default_service_salary_workers(who):
        lines.append("")
        lines.append(f"💸 В ЗП: {', '.join(salary_workers)}")

    if service_total:
        lines.append("")
        lines.append(f"💰 Обслуживание: {format_money(service_total)}")
        if purchase_sum:
            lines.append(f"💰 Закупки: {format_money(purchase_sum)}")
        lines.append(f"💰 Итого: {format_money(service_total + purchase_sum)}")
    elif purchase_sum:
        lines.append("")
        lines.append(f"💰 Закупки: {format_money(purchase_sum)}")

    return "\n".join(lines)

async def service_confirm_handler(update: Update, context):
    query = update.callback_query
    await query.answer()

    if query.data == "svc_dup_back":
        return await show_confirm(query, context)

    if query.data == "back_service_confirm":
        return await back_to_shortage_stage(query, context)

    if query.data == "svc_cancel":
        if context.user_data.get("svc", {}).get("return_mode") == "payout":
            payout = get_payout_context(context)
            payout_worker = context.user_data["svc"].get("return_worker", "")
            if payout_worker:
                payout["worker"] = payout_worker
            return_screen = context.user_data["svc"].get("return_screen", "overview")
            section = get_payout_screen_section(return_screen)
            if section in {"services", "purchases"}:
                payout[f"{section}_date"] = context.user_data["svc"].get("date")
                payout[f"{section}_point"] = context.user_data["svc"].get("point")
            return await show_payout_screen(
                query,
                context,
                screen=return_screen,
                period_key=context.user_data["svc"].get("return_period"),
                notice="❌ Добавление отменено.",
            )
        await query.edit_message_text("❌ Отменено")
        return await start(update, context)

    svc = context.user_data["svc"]
    service_sum = calculate_service_sum_for_workers(get_service_salary_workers_from_context(svc))
    payload = build_service_update_data(svc, service_sum)

    try:
        if query.data == "svc_ok":
            duplicates = await run_blocking(
                find_service_semantic_duplicates,
                payload,
                svc.get("service_row") if svc.get("edit_mode") else None,
            )
            if duplicates:
                svc["duplicate_match_rows"] = [entry.get("__row") for entry in duplicates]
                await show_text_screen(
                    query,
                    context,
                    build_service_duplicate_warning_text(svc, duplicates),
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [InlineKeyboardButton("✅ Всё равно сохранить", callback_data="svc_ok_force")],
                            [InlineKeyboardButton("✏️ Вернуться", callback_data="svc_dup_back")],
                            [InlineKeyboardButton("❌ Отмена", callback_data="svc_cancel")],
                        ]
                    ),
                )
                return SERVICE_CONFIRM

        if query.data not in {"svc_ok", "svc_ok_force"}:
            return SERVICE_CONFIRM

        if svc.get("edit_mode") and svc.get("service_row"):
            await run_blocking(update_service_row, svc["service_row"], payload)
        else:
            await run_blocking(add_service_row, payload)

        if svc.get("photo"):
            if svc.get("edit_mode") and svc.get("photo_row"):
                await run_blocking(
                    update_photo_row, svc["photo_row"], svc["date"], svc["point"], svc["who"], svc["photo"]
                )
            else:
                await run_blocking(add_photo_row, svc["date"], svc["point"], svc["who"], svc["photo"])

        logger.info(
            "service saved: user_id=%s point=%s date=%s mode=%s",
            getattr(update.effective_user, "id", None),
            svc["point"],
            svc["date"],
            "update" if svc.get("edit_mode") else "create",
        )
        success_text = "✅ Запись обновлена!" if svc.get("edit_mode") else "✅ Записано!"
        try:
            await refresh_group_service_today_posts(context.application, force=True)
        except Exception:
            logger.exception("Failed to refresh group service-today post after service save")
        if svc.get("return_mode") == "payout":
            payout = get_payout_context(context)
            payout_worker = svc.get("return_worker", "")
            if payout_worker:
                payout["worker"] = payout_worker
            return_screen = svc.get("return_screen", "overview")
            section = get_payout_screen_section(return_screen)
            if section in {"services", "purchases"}:
                payout[f"{section}_date"] = svc["date"]
                payout[f"{section}_point"] = svc["point"]
            return await show_payout_screen(
                query,
                context,
                screen=return_screen,
                period_key=svc.get("return_period"),
                notice=success_text,
            )
        await query.edit_message_text(f"{success_text}\n\n📍 {svc['point']} — {svc['who']}")

    except APIError as e:
        if is_google_sheets_busy_error(e):
            await show_sheets_busy_notice(
                query,
                context,
                retry_callback="svc_ok",
                back_callback="back_service_confirm",
            )
            return SERVICE_CONFIRM
        logger.exception("Failed to save service record")
        await query.edit_message_text("❌ Ошибка записи. Попробуйте позже.")
    except Exception:
        logger.exception("Failed to save service record")
        await query.edit_message_text("❌ Ошибка записи. Попробуйте позже.")

    if svc.get("return_mode") == "payout":
        payout = get_payout_context(context)
        payout_worker = svc.get("return_worker", "")
        if payout_worker:
            payout["worker"] = payout_worker
        return_screen = svc.get("return_screen", "overview")
        section = get_payout_screen_section(return_screen)
        if section in {"services", "purchases"}:
            payout[f"{section}_date"] = svc.get("date")
            payout[f"{section}_point"] = svc.get("point")
        return await show_payout_screen(
            query,
            context,
            screen=return_screen,
            period_key=svc.get("return_period"),
        )
    return await start(update, context)


# ============ ИМПОРТ ИЗ РАБОЧЕЙ ГРУППЫ ============
def build_group_report_saved_markup(save_result):
    log_row = save_result.get("log_row")
    current_worker = save_result.get("who", "")
    revision_meta = save_result.get("revision")
    service_row = str(save_result.get("service_row", "")).strip()
    keyboard = []
    row = []
    worker_names = get_worker_names()
    for idx, worker in enumerate(worker_names):
        prefix = "✅" if worker == current_worker else "👤"
        row.append(InlineKeyboardButton(f"{prefix} {worker}", callback_data=f"grp_report_worker_{idx}_{log_row}"))
        if len(row) == 2 or idx == len(worker_names) - 1:
            keyboard.append(row)
            row = []
    if service_row:
        keyboard.append([
            InlineKeyboardButton("✏️ Редактировать обслуживание", callback_data=f"grp_report_edit_service_{log_row}")
        ])
    if revision_meta:
        keyboard.append([
            InlineKeyboardButton("📦 Редактировать ревизию", callback_data=f"grp_report_edit_revision_{log_row}")
        ])
        keyboard.append([
            InlineKeyboardButton("📦 Не считать ревизией", callback_data=f"grp_report_remove_revision_{log_row}")
        ])
    keyboard.append([InlineKeyboardButton("🗑 Удалить всё", callback_data=f"grp_report_delete_{log_row}")])
    return InlineKeyboardMarkup(keyboard)


def build_group_report_save_result_from_record(record):
    revision = None
    if str(record.get("Revision_Period", "")).strip() and str(record.get("Revision_Location", "")).strip():
        revision = {
            "period": record.get("Revision_Period", ""),
            "location": record.get("Revision_Location", ""),
        }
    return {
        "log_row": record.get("__row"),
        "who": record.get("Кто", ""),
        "service_row": record.get("Service_Row", ""),
        "revision": revision,
    }


def build_group_report_action_expired_text():
    return (
        "⏳ Окно быстрых действий уже истекло.\n\n"
        "Обслуживание можно поправить через «Исправить записи», "
        "а ревизию — через раздел «Ревизия»."
    )


def build_group_report_delete_result_text(record):
    service_row = str(record.get("Service_Row", "")).strip()
    has_revision = any(str(record.get(key, "")).strip() for key in ("Revision_Row", "Revision_Period", "Revision_Location"))
    point = record.get("Точка", "—")
    date_value = record.get("Дата", "—")
    who = record.get("Кто", "—")

    if not service_row and has_revision:
        return (
            "🗑 Ревизия убрана из учёта.\n\n"
            f"📍 {point}\n"
            f"📅 {date_value}\n"
            f"👤 {who}"
        )

    if service_row and has_revision:
        return (
            "🗑 Обслуживание и ревизия убраны из базы.\n\n"
            f"📍 {point}\n"
            f"📅 {date_value}\n"
            f"👤 {who}"
        )

    return (
        "🗑 Сохранение сообщения отменено.\n\n"
        f"📍 {point}\n"
        f"📅 {date_value}\n"
        f"👤 {who}"
    )


def is_group_report_revision_only_record(record):
    service_row = str(record.get("Service_Row", "")).strip()
    has_revision = any(str(record.get(key, "")).strip() for key in ("Revision_Row", "Revision_Period", "Revision_Location"))
    return not service_row and has_revision


def parse_group_report_log_row_from_callback_data(data, action_name):
    prefix = f"grp_report_{action_name}_"
    if not str(data or "").startswith(prefix):
        return None
    raw_id = str(data).replace(prefix, "", 1).strip()
    if not raw_id.isdigit():
        return None
    return int(raw_id)


async def resolve_group_report_quick_action_record(query, context, action_name):
    log_row_num = parse_group_report_log_row_from_callback_data(query.data, action_name)
    if not log_row_num:
        return None, None

    record = await run_blocking(get_group_report_log_entry_by_row, log_row_num)
    if not record:
        await query.edit_message_text("⚪ Сообщение уже недоступно.")
        if query.message:
            schedule_single_message_cleanup(
                context.application,
                query.message.chat_id,
                query.message.message_id,
                GROUP_REPORT_FEEDBACK_AUTO_DELETE_SECONDS,
            )
        return None, MAIN_MENU

    if record.get("Статус") != "saved":
        await query.edit_message_text("⚪ Сообщение уже неактуально.")
        if query.message:
            schedule_single_message_cleanup(
                context.application,
                query.message.chat_id,
                query.message.message_id,
                GROUP_REPORT_FEEDBACK_AUTO_DELETE_SECONDS,
            )
        return None, MAIN_MENU

    if not is_group_report_action_window_open(record):
        await query.edit_message_text(build_group_report_action_expired_text())
        if query.message:
            schedule_single_message_cleanup(
                context.application,
                query.message.chat_id,
                query.message.message_id,
                GROUP_REPORT_FEEDBACK_AUTO_DELETE_SECONDS,
            )
        return None, MAIN_MENU

    return record, log_row_num


async def group_report_edit_service_entry_handler(update: Update, context):
    query = update.callback_query
    if not is_allowed_user(update) or not is_allowed_group_report_chat(update):
        await deny_callback_access(query)
        return ConversationHandler.END

    await query.answer()
    cleanup_expired_group_report_drafts(context.application.bot_data)

    record, fallback_state = await resolve_group_report_quick_action_record(query, context, "edit_service")
    if not record:
        return fallback_state or MAIN_MENU

    try:
        entry = await run_blocking(find_group_report_service_entry, record)
        if not entry:
            await query.edit_message_text("⚪ Не удалось найти запись обслуживания для редактирования.")
            if query.message:
                schedule_single_message_cleanup(
                    context.application,
                    query.message.chat_id,
                    query.message.message_id,
                    GROUP_REPORT_FEEDBACK_AUTO_DELETE_SECONDS,
                )
            return MAIN_MENU

        context.user_data["delete"] = {"entry": entry}
        return await begin_service_edit_from_entry(query, context)
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(
                query,
                context,
                retry_callback=str(query.data),
            )
            return MAIN_MENU
        logger.exception("Failed to open group report service edit for log row %s", log_row_num)
        await query.edit_message_text("❌ Не удалось открыть редактирование обслуживания.")
        return MAIN_MENU
    except Exception:
        logger.exception("Failed to open group report service edit for log row %s", log_row_num)
        await query.edit_message_text("❌ Не удалось открыть редактирование обслуживания.")
        return MAIN_MENU


async def group_report_edit_revision_entry_handler(update: Update, context):
    query = update.callback_query
    if not is_allowed_user(update) or not is_allowed_group_report_chat(update):
        await deny_callback_access(query)
        return ConversationHandler.END

    await query.answer()
    cleanup_expired_group_report_drafts(context.application.bot_data)

    record, fallback_state = await resolve_group_report_quick_action_record(query, context, "edit_revision")
    if not record:
        return fallback_state or MAIN_MENU

    try:
        revision_entry = await run_blocking(find_group_report_revision_entry, record)
        if not revision_entry:
            await query.edit_message_text("⚪ Для этого сообщения ревизия уже не учитывается.")
            if query.message:
                schedule_single_message_cleanup(
                    context.application,
                    query.message.chat_id,
                    query.message.message_id,
                    GROUP_REPORT_FEEDBACK_AUTO_DELETE_SECONDS,
                )
            return MAIN_MENU

        revision = get_revision_context(context)
        revision.clear()
        revision["action"] = "edit"
        revision["period"] = revision_entry.get("Период", "")
        revision["location"] = revision_entry.get("Локация", "")
        revision["existing_record"] = revision_entry
        if is_group_report_revision_only_record(record):
            revision["group_report_undo_log_row"] = log_row_num
        return await show_revision_edit_action_menu(query, context)
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(
                query,
                context,
                retry_callback=str(query.data),
            )
            return MAIN_MENU
        logger.exception("Failed to open group report revision edit for log row %s", log_row_num)
        await query.edit_message_text("❌ Не удалось открыть редактирование ревизии.")
        return MAIN_MENU
    except Exception:
        logger.exception("Failed to open group report revision edit for log row %s", log_row_num)
        await query.edit_message_text("❌ Не удалось открыть редактирование ревизии.")
        return MAIN_MENU


def remove_group_report_revision_by_log_row(log_row_num):
    record = next((entry for entry in get_group_report_logs_with_rows() if entry["__row"] == log_row_num), None)
    if not record:
        return "missing", None

    if record.get("Статус") != "saved":
        return record.get("Статус") or "missing", record

    has_revision = any(str(record.get(key, "")).strip() for key in ("Revision_Row", "Revision_Period", "Revision_Location"))
    if not has_revision:
        return "no_revision", record

    restore_group_report_revision(record)
    updated = {
        "chat_id": record.get("Chat_ID", ""),
        "source_key": record.get("Source_Key", ""),
        "source_message_id": record.get("Source_Message_ID", ""),
        "media_group_id": record.get("Media_Group_ID", ""),
        "who": record.get("Кто", ""),
        "point": record.get("Точка", ""),
        "date": record.get("Дата", ""),
        "fingerprint": record.get("Fingerprint", ""),
        "service_row": record.get("Service_Row", ""),
        "photo_rows": record.get("Photo_Rows", ""),
        "revision_row": "",
        "revision_period": "",
        "revision_location": "",
        "revision_mode": "",
        "revision_backup": "",
        "status": "saved",
        "created_at": record.get("Создано", ""),
    }
    update_group_report_log(log_row_num, updated)
    return "removed", record


def reassign_group_report_worker(log_row_num, worker):
    record = get_group_report_log_entry_by_row(log_row_num)
    if not record:
        return "missing", None

    if record.get("Статус") != "saved":
        return record.get("Статус") or "missing", record

    entry = find_group_report_service_entry(record)
    revision_entry = find_group_report_revision_entry(record)
    if not entry and not revision_entry:
        return "missing_service", record

    if entry:
        payload = build_service_update_data_from_entry(entry, default_service_salary_workers(worker))
        payload["who"] = worker
        payload["service_sum"] = calculate_service_sum_for_workers(payload.get("salary_workers", []))
        update_service_row(entry["__row"], payload)

        photo_rows = parse_logged_row_numbers(record.get("Photo_Rows", ""))
        if photo_rows:
            photos_by_row = {
                photo["__row"]: photo
                for photo in get_all_photos_with_rows()
                if photo.get("__row") in photo_rows
            }
            for row_num in photo_rows:
                photo = photos_by_row.get(row_num)
                if not photo:
                    continue
                update_photo_row(
                    row_num,
                    photo.get("Дата", entry.get("Дата", "")),
                    photo.get("Точка", entry.get("Точка", "")),
                    worker,
                    photo.get("File_ID", ""),
                )

    if revision_entry:
        revision_payload = {
            "period": revision_entry.get("Период", ""),
            "location": revision_entry.get("Локация", ""),
            "who": worker,
            "filled_at": revision_entry.get("Дата заполнения", ""),
            "values": build_revision_values_from_record(revision_entry),
        }
        update_revision_row(revision_entry["__row"], revision_payload)

    updated = {
        "chat_id": record.get("Chat_ID", ""),
        "source_key": record.get("Source_Key", ""),
        "source_message_id": record.get("Source_Message_ID", ""),
        "media_group_id": record.get("Media_Group_ID", ""),
        "who": worker,
        "point": record.get("Точка", ""),
        "date": record.get("Дата", ""),
        "fingerprint": record.get("Fingerprint", ""),
        "service_row": record.get("Service_Row", ""),
        "photo_rows": record.get("Photo_Rows", ""),
        "revision_row": record.get("Revision_Row", ""),
        "revision_period": record.get("Revision_Period", ""),
        "revision_location": record.get("Revision_Location", ""),
        "revision_mode": record.get("Revision_Mode", ""),
        "revision_backup": record.get("Revision_Backup", ""),
        "status": "saved",
        "created_at": record.get("Создано", ""),
    }
    update_group_report_log(log_row_num, updated)
    return "updated", updated


async def send_group_report_feedback_message(application, chat_id, source_message_id, text, reply_markup=None):
    send_kwargs = {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": reply_markup,
    }
    if source_message_id:
        send_kwargs["reply_to_message_id"] = source_message_id

    try:
        sent = await application.bot.send_message(**send_kwargs)
    except BadRequest:
        if not source_message_id:
            raise
        logger.exception(
            "Failed to send group feedback as reply, retrying without reply: chat=%s source=%s",
            chat_id,
            source_message_id,
        )
        sent = await application.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
        )
    schedule_single_message_cleanup(
        application,
        sent.chat_id,
        sent.message_id,
        GROUP_REPORT_FEEDBACK_AUTO_DELETE_SECONDS,
    )
    return sent


async def send_group_report_saved_message(application, draft, save_result):
    await send_group_report_feedback_message(
        application,
        draft["chat_id"],
        draft["source_message_id"],
        build_group_report_saved_text(draft, save_result),
        reply_markup=build_group_report_saved_markup(save_result),
    )


async def send_group_travel_saved_message(application, draft, log_row):
    await send_group_report_feedback_message(
        application,
        draft["chat_id"],
        draft["source_message_id"],
        build_group_travel_saved_text(draft),
    )


async def send_revision_restock_saved_message(application, draft, save_result):
    await send_group_report_feedback_message(
        application,
        draft["chat_id"],
        draft["source_message_id"],
        build_revision_restock_saved_text(draft, save_result),
        reply_markup=build_revision_restock_saved_markup(save_result),
    )


async def send_revision_message_saved_message(application, draft, save_result):
    await send_group_report_feedback_message(
        application,
        draft["chat_id"],
        draft["source_message_id"],
        build_revision_message_saved_text(draft, save_result),
        reply_markup=build_group_report_saved_markup(save_result),
    )


async def run_group_sheet_write_with_retry(save_callable, draft, operation_label):
    delay_seconds = 1.0
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            return await run_blocking(save_callable, draft)
        except APIError as error:
            if not is_google_sheets_busy_error(error) or attempt >= max_attempts:
                raise
            logger.warning(
                "Google Sheets busy during %s, retrying attempt %s/%s: point=%s date=%s who=%s",
                operation_label,
                attempt + 1,
                max_attempts,
                draft.get("point", ""),
                draft.get("date", ""),
                draft.get("who", ""),
            )
            await asyncio.sleep(delay_seconds)
            delay_seconds *= 2


async def process_group_report_message(message, application, photo_ids=None):
    if not message or not getattr(message, "chat", None):
        return
    if message.chat.type not in {"group", "supergroup", "private"}:
        return
    if getattr(message.from_user, "is_bot", False):
        return

    body_text = message.caption or message.text or ""
    restock_parsed = parse_revision_restock_message_text(body_text)
    if restock_parsed:
        draft = {
            **restock_parsed,
            "point": restock_parsed["location"],
            "who": get_service_report_author(message),
            "date": get_message_local_date(message),
            "period": get_period_key_for_date(get_message_local_date(message)),
            "chat_id": message.chat_id,
            "source_message_id": message.message_id,
            "media_group_id": getattr(message, "media_group_id", "") or "",
            "source_key": build_group_report_source_key(message),
        }
        draft["fingerprint"] = build_revision_restock_fingerprint(draft)

        try:
            async with GROUP_REPORT_SAVE_LOCK:
                existing, duplicate = await run_blocking(
                    find_group_report_duplicate,
                    draft["chat_id"],
                    draft["source_key"],
                    draft["fingerprint"],
                )
                if existing and existing.get("Статус") in {"saved", "ignored", "deleted"}:
                    status = existing.get("Статус")
                    if status == "saved":
                        text = "⚪ Это пополнение уже сохранено."
                    elif status == "deleted":
                        text = "⚪ Это пополнение уже было отмечено как «не учитывать»."
                    else:
                        text = "⚪ Это пополнение уже обработано."
                    await send_group_report_feedback_message(
                        application,
                        draft["chat_id"],
                        draft["source_message_id"],
                        text,
                    )
                    return

                if duplicate:
                    await send_group_report_feedback_message(
                        application,
                        draft["chat_id"],
                        draft["source_message_id"],
                        "⚪ Похоже, это дубль пополнения — оно уже сохранено.",
                    )
                    return

                save_result = await run_group_sheet_write_with_retry(
                    save_revision_restock_entry,
                    draft,
                    "revision restock save",
                )
            await send_revision_restock_saved_message(application, draft, save_result)
        except APIError as error:
            if is_google_sheets_busy_error(error):
                logger.exception(
                    "Google Sheets busy during revision restock save chat=%s source=%s",
                    draft["chat_id"],
                    draft["source_key"],
                )
                await show_sheets_busy_notice(message)
                return
            logger.exception(
                "Failed to auto-save revision restock chat=%s source=%s",
                draft["chat_id"],
                draft["source_key"],
            )
            await send_group_report_feedback_message(
                application,
                draft["chat_id"],
                draft["source_message_id"],
                "❌ Не удалось добавить пополнение в ревизию. Попробуйте ещё раз.",
            )
        except Exception:
            logger.exception(
                "Failed to auto-save revision restock chat=%s source=%s",
                draft["chat_id"],
                draft["source_key"],
            )
            await send_group_report_feedback_message(
                application,
                draft["chat_id"],
                draft["source_message_id"],
                "❌ Не удалось добавить пополнение в ревизию. Попробуйте ещё раз.",
            )
        return

    revision_parsed = parse_revision_snapshot_message_text(body_text)
    if revision_parsed:
        draft = {
            **revision_parsed,
            "point": revision_parsed["location"],
            "who": get_service_report_author(message),
            "date": get_message_local_date(message),
            "period": get_period_key_for_date(get_message_local_date(message)),
            "chat_id": message.chat_id,
            "source_message_id": message.message_id,
            "media_group_id": getattr(message, "media_group_id", "") or "",
            "source_key": build_group_report_source_key(message),
        }
        draft["fingerprint"] = build_revision_message_fingerprint(draft)

        try:
            async with GROUP_REPORT_SAVE_LOCK:
                existing, duplicate = await run_blocking(
                    find_group_report_duplicate,
                    draft["chat_id"],
                    draft["source_key"],
                    draft["fingerprint"],
                )
                if existing and existing.get("Статус") in {"saved", "ignored", "deleted"}:
                    status = existing.get("Статус")
                    if status == "saved":
                        text = "⚪ Эта ревизия уже сохранена."
                    elif status == "deleted":
                        text = "⚪ Эта ревизия уже была отмечена как «не учитывать»."
                    else:
                        text = "⚪ Эта ревизия уже обработана."
                    await send_group_report_feedback_message(
                        application,
                        draft["chat_id"],
                        draft["source_message_id"],
                        text,
                    )
                    return

                if duplicate:
                    await send_group_report_feedback_message(
                        application,
                        draft["chat_id"],
                        draft["source_message_id"],
                        "⚪ Похоже, это дубль ревизии — она уже сохранена.",
                    )
                    return

                save_result = await run_group_sheet_write_with_retry(
                    save_revision_message_entry,
                    draft,
                    "revision snapshot save",
                )
            await send_revision_message_saved_message(application, draft, save_result)
        except APIError as error:
            if is_google_sheets_busy_error(error):
                logger.exception(
                    "Google Sheets busy during revision snapshot save chat=%s source=%s",
                    draft["chat_id"],
                    draft["source_key"],
                )
                await show_sheets_busy_notice(message)
                return
            logger.exception(
                "Failed to auto-save revision snapshot chat=%s source=%s",
                draft["chat_id"],
                draft["source_key"],
            )
            await send_group_report_feedback_message(
                application,
                draft["chat_id"],
                draft["source_message_id"],
                "❌ Не удалось сохранить ревизию из сообщения. Попробуйте ещё раз.",
            )
        except Exception:
            logger.exception(
                "Failed to auto-save revision snapshot chat=%s source=%s",
                draft["chat_id"],
                draft["source_key"],
            )
            await send_group_report_feedback_message(
                application,
                draft["chat_id"],
                draft["source_message_id"],
                "❌ Не удалось сохранить ревизию из сообщения. Попробуйте ещё раз.",
            )
        return

    travel_parsed = parse_group_travel_message_text(body_text)
    if travel_parsed:
        draft = {
            **travel_parsed,
            "date": get_message_local_date(message),
            "who": get_service_report_author(message),
            "chat_id": message.chat_id,
            "source_message_id": message.message_id,
            "media_group_id": getattr(message, "media_group_id", "") or "",
            "source_key": build_group_report_source_key(message),
            "travel_amounts": list(travel_parsed.get("amounts", [])),
            "travel_items": list(travel_parsed.get("items", [])),
        }
        draft["fingerprint"] = build_group_travel_fingerprint(draft)

        try:
            async with GROUP_REPORT_SAVE_LOCK:
                existing, duplicate = await run_blocking(
                    find_group_report_duplicate,
                    draft["chat_id"],
                    draft["source_key"],
                    draft["fingerprint"],
                )
                if existing and existing.get("Статус") in {"saved", "ignored", "deleted"}:
                    status = existing.get("Статус")
                    if status == "saved":
                        text = "⚪ Этот проезд уже сохранён."
                    elif status == "deleted":
                        text = "⚪ Этот проезд уже был отмечен как «не учитывать»."
                    else:
                        text = "⚪ Этот проезд уже обработан."
                    await send_group_report_feedback_message(
                        application,
                        draft["chat_id"],
                        draft["source_message_id"],
                        text,
                    )
                    return

                if duplicate:
                    await send_group_report_feedback_message(
                        application,
                        draft["chat_id"],
                        draft["source_message_id"],
                        "⚪ Похоже, это дубль проезда — он уже сохранён.",
                    )
                    return

                log_row = await run_group_sheet_write_with_retry(
                    save_group_travel_entry,
                    draft,
                    "group travel save",
                )
            await send_group_travel_saved_message(application, draft, log_row)
        except APIError as error:
            if is_google_sheets_busy_error(error):
                logger.exception(
                    "Google Sheets busy during auto-save travel chat=%s source=%s",
                    draft["chat_id"],
                    draft["source_key"],
                )
                await show_sheets_busy_notice(message)
                return
            logger.exception(
                "Failed to auto-save travel report chat=%s source=%s",
                draft["chat_id"],
                draft["source_key"],
            )
            await send_group_report_feedback_message(
                application,
                draft["chat_id"],
                draft["source_message_id"],
                "❌ Не удалось сохранить проезд из сообщения. Попробуйте отправить ещё раз.",
            )
        except Exception:
            logger.exception(
                "Failed to auto-save travel report chat=%s source=%s",
                draft["chat_id"],
                draft["source_key"],
            )
            await send_group_report_feedback_message(
                application,
                draft["chat_id"],
                draft["source_message_id"],
                "❌ Не удалось сохранить проезд из сообщения. Попробуйте отправить ещё раз.",
            )
        return

    parsed = parse_service_report_message_text(body_text, has_photo=bool(photo_ids or getattr(message, "photo", None)))
    if not parsed:
        logger.info(
            "Skipped group report message chat=%s message=%s: parser returned None",
            message.chat_id,
            message.message_id,
        )
        return

    draft = {
        **parsed,
        "who": get_service_report_author(message),
        "user_id": getattr(getattr(message, "from_user", None), "id", None),
        "chat_id": message.chat_id,
        "source_message_id": message.message_id,
        "media_group_id": getattr(message, "media_group_id", "") or "",
        "source_key": build_group_report_source_key(message),
        "photo_ids": list(photo_ids or ([message.photo[-1].file_id] if getattr(message, "photo", None) else [])),
    }
    revision_data, revision_warnings = build_group_report_revision_data(draft)
    draft["revision"] = revision_data
    if revision_warnings:
        draft["warnings"] = list(draft.get("warnings", [])) + revision_warnings
    draft["fingerprint"] = build_group_report_fingerprint(draft)

    try:
        async with GROUP_REPORT_SAVE_LOCK:
            existing, duplicate = await run_blocking(
                find_group_report_duplicate,
                draft["chat_id"],
                draft["source_key"],
                draft["fingerprint"],
            )
            if existing and existing.get("Статус") in {"saved", "ignored", "deleted"}:
                status = existing.get("Статус")
                if status == "saved":
                    text = "⚪ Этот отчёт уже сохранён."
                elif status == "deleted":
                    text = "⚪ Этот отчёт уже был отмечен как «не учитывать»."
                else:
                    text = "⚪ Этот отчёт уже обработан."
                await send_group_report_feedback_message(
                    application,
                    draft["chat_id"],
                    draft["source_message_id"],
                    text,
                )
                return

            if duplicate:
                await send_group_report_feedback_message(
                    application,
                    draft["chat_id"],
                    draft["source_message_id"],
                    "⚪ Похоже, это дубль отчёта — он уже сохранён.",
                )
                return

            semantic_duplicates = await run_blocking(
                find_service_semantic_duplicates,
                build_group_report_payload(draft),
                None,
            )
            if semantic_duplicates:
                cleanup_expired_group_report_drafts(application.bot_data)
                draft_id = next_group_report_draft_id(application.bot_data)
                draft_copy = dict(draft)
                draft_copy["created_at_ts"] = datetime.now().timestamp()
                draft_copy["draft_mode"] = "service_duplicate_warning"
                get_group_report_drafts(application.bot_data)[draft_id] = draft_copy
                await send_group_report_feedback_message(
                    application,
                    draft["chat_id"],
                    draft["source_message_id"],
                    build_group_report_duplicate_warning_text(draft, semantic_duplicates),
                    reply_markup=build_group_report_duplicate_draft_markup(draft_id),
                )
                return

            save_result = await run_group_sheet_write_with_retry(
                save_group_report_entry,
                draft,
                "group report save",
            )
    except APIError as error:
        if is_google_sheets_busy_error(error):
            logger.exception(
                "Google Sheets busy during auto-save group report chat=%s source=%s",
                draft["chat_id"],
                draft["source_key"],
            )
            await show_sheets_busy_notice(message)
            return
        logger.exception(
            "Failed to auto-save group report chat=%s source=%s",
            draft["chat_id"],
            draft["source_key"],
        )
        await send_group_report_feedback_message(
            application,
            draft["chat_id"],
            draft["source_message_id"],
            "❌ Не удалось сохранить отчёт из сообщения. Попробуйте отправить ещё раз или поправьте вручную.",
        )
        return
    except Exception:
        logger.exception(
            "Failed to auto-save group report chat=%s source=%s",
            draft["chat_id"],
            draft["source_key"],
        )
        await send_group_report_feedback_message(
            application,
            draft["chat_id"],
            draft["source_message_id"],
            "❌ Не удалось сохранить отчёт из сообщения. Попробуйте отправить ещё раз или поправьте вручную.",
        )
        return

    try:
        await refresh_group_service_today_posts(application, force=True)
    except Exception:
        logger.exception("Failed to refresh group service-today post after group report save")

    last_error = None
    for attempt in range(4):
        try:
            await send_group_report_saved_message(application, draft, save_result)
            last_error = None
            break
        except (NetworkError, TimedOut) as e:
            last_error = e
            delay = 2 ** attempt  # 1, 2, 4, 8
            logger.warning(
                "send_group_report_saved_message network retry %d/4 in %ds: %s",
                attempt + 1, delay, type(e).__name__,
            )
            await asyncio.sleep(delay)
        except Exception as e:
            last_error = e
            break

    if last_error is not None:
        logger.exception(
            "Failed to send saved group report confirmation chat=%s source=%s",
            draft["chat_id"],
            draft["source_key"],
            exc_info=last_error,
        )
        fallback_result = dict(save_result or {})
        fallback_warnings = list(fallback_result.get("warnings", []))
        fallback_warnings.append("быстрые кнопки не показались, но запись сохранена")
        fallback_result["warnings"] = fallback_warnings
        try:
            await send_group_report_feedback_message(
                application,
                draft["chat_id"],
                draft["source_message_id"],
                build_group_report_saved_text(draft, fallback_result),
            )
        except Exception:
            logger.exception(
                "Failed to send fallback group report confirmation chat=%s source=%s",
                draft["chat_id"],
                draft["source_key"],
            )


async def finalize_group_report_media_group(application, media_key):
    await asyncio.sleep(1.2)
    store = application.bot_data.setdefault("group_report_media_groups", {})
    payload = store.pop(media_key, None)
    if not payload:
        return
    await process_group_report_message(
        payload["message"],
        application,
        photo_ids=payload.get("photo_ids", []),
    )


async def group_report_message_handler(update: Update, context):
    message = update.effective_message
    if not message:
        return
    if not is_allowed_user(update) or not is_allowed_group_report_chat(update):
        return
    if getattr(message.from_user, "is_bot", False):
        return

    if message.media_group_id:
        media_key = f"{message.chat_id}:{message.media_group_id}"
        store = context.application.bot_data.setdefault("group_report_media_groups", {})
        payload = store.setdefault(
            media_key,
            {
                "message": message,
                "photo_ids": [],
                "task_started": False,
            },
        )
        if getattr(message, "photo", None):
            payload["photo_ids"].append(message.photo[-1].file_id)
        if message.caption:
            payload["message"] = message
        if not payload["task_started"]:
            payload["task_started"] = True
            asyncio.create_task(finalize_group_report_media_group(context.application, media_key))
        return

    await process_group_report_message(message, context.application)


async def group_report_callback_handler(update: Update, context):
    query = update.callback_query
    if not is_allowed_user(update) or not is_allowed_group_report_chat(update):
        await deny_callback_access(query)
        return
    await query.answer()

    cleanup_expired_group_report_drafts(context.application.bot_data)
    data = str(query.data or "")
    if not data.startswith("grp_report_"):
        return

    payload = data.replace("grp_report_", "", 1)
    if "_" not in payload:
        return
    action, raw_id = payload.rsplit("_", 1)
    if not raw_id.isdigit():
        return

    quick_actions = {"delete", "edit_service", "edit_revision", "remove_revision"}
    if action in quick_actions or action.startswith("worker_"):
        log_row_num = int(raw_id)
        record = await run_blocking(get_group_report_log_entry_by_row, log_row_num)
        if not record:
            await query.edit_message_text("⚪ Сообщение уже недоступно.")
            if query.message:
                schedule_single_message_cleanup(
                    context.application,
                    query.message.chat_id,
                    query.message.message_id,
                    GROUP_REPORT_FEEDBACK_AUTO_DELETE_SECONDS,
                )
            return MAIN_MENU

        if record.get("Статус") != "saved":
            await query.edit_message_text("⚪ Сообщение уже неактуально.")
            if query.message:
                schedule_single_message_cleanup(
                    context.application,
                    query.message.chat_id,
                    query.message.message_id,
                    GROUP_REPORT_FEEDBACK_AUTO_DELETE_SECONDS,
                )
            return MAIN_MENU

        if not is_group_report_action_window_open(record):
            await query.edit_message_text(build_group_report_action_expired_text())
            if query.message:
                schedule_single_message_cleanup(
                    context.application,
                    query.message.chat_id,
                    query.message.message_id,
                    GROUP_REPORT_FEEDBACK_AUTO_DELETE_SECONDS,
                )
            return MAIN_MENU

        if action.startswith("worker_"):
            try:
                worker_index = int(action.replace("worker_", "", 1))
                worker = get_worker_names()[worker_index]
            except (ValueError, IndexError):
                await query.edit_message_text("⚪ Не удалось определить сотрудника.")
                if query.message:
                    schedule_single_message_cleanup(
                        context.application,
                        query.message.chat_id,
                        query.message.message_id,
                        GROUP_REPORT_FEEDBACK_AUTO_DELETE_SECONDS,
                    )
                return MAIN_MENU

            try:
                status, updated_record = await run_blocking(reassign_group_report_worker, log_row_num, worker)
            except Exception:
                logger.exception("Failed to reassign group report log row %s to %s", raw_id, worker)
                await query.edit_message_text("❌ Не удалось изменить сотрудника.")
                if query.message:
                    schedule_single_message_cleanup(
                        context.application,
                        query.message.chat_id,
                        query.message.message_id,
                        GROUP_REPORT_FEEDBACK_AUTO_DELETE_SECONDS,
                    )
                return MAIN_MENU

            if status == "updated" and updated_record:
                await query.edit_message_text(
                    "✅ Сообщение перезачтено.\n\n"
                    f"📍 {updated_record.get('point', '—')}\n"
                    f"📅 {updated_record.get('date', '—')}\n"
                    f"👤 {updated_record.get('who', '—')}",
                    reply_markup=build_group_report_saved_markup(build_group_report_save_result_from_record({
                        "__row": log_row_num,
                        "Кто": updated_record.get("who", ""),
                        "Service_Row": updated_record.get("service_row", ""),
                        "Revision_Period": updated_record.get("revision_period", ""),
                        "Revision_Location": updated_record.get("revision_location", ""),
                    })),
                )
                try:
                    await refresh_group_service_today_posts(context.application, force=True)
                except Exception:
                    logger.exception("Failed to refresh group service-today post after worker reassignment")
            else:
                await query.edit_message_text("⚪ Сообщение уже неактуально.")
            return MAIN_MENU

        if action == "edit_service":
            await query.edit_message_text(
                "⚪ Быстрое полное редактирование обслуживания из группового сообщения отключено.\n\n"
                "Для смены сотрудника используйте кнопки выше.\n"
                "Для остальных правок откройте «Исправить записи»."
            )
            if query.message:
                schedule_single_message_cleanup(
                    context.application,
                    query.message.chat_id,
                    query.message.message_id,
                    GROUP_REPORT_FEEDBACK_AUTO_DELETE_SECONDS,
                )
            return MAIN_MENU

        if action == "edit_revision":
            await query.edit_message_text(
                "⚪ Быстрое пошаговое редактирование ревизии из группового сообщения отключено.\n\n"
                "Если нужно поправить цифры, откройте раздел «Ревизия».\n"
                "Если ревизию не нужно учитывать вообще, нажмите «Не считать ревизией»."
            )
            if query.message:
                schedule_single_message_cleanup(
                    context.application,
                    query.message.chat_id,
                    query.message.message_id,
                    GROUP_REPORT_FEEDBACK_AUTO_DELETE_SECONDS,
                )
            return MAIN_MENU

        if action == "remove_revision":
            try:
                status, updated_record = await run_blocking(remove_group_report_revision_by_log_row, log_row_num)
            except Exception:
                logger.exception("Failed to remove revision from group report log row %s", raw_id)
                await query.edit_message_text("❌ Не удалось убрать ревизию из сообщения.")
                if query.message:
                    schedule_single_message_cleanup(
                        context.application,
                        query.message.chat_id,
                        query.message.message_id,
                        GROUP_REPORT_FEEDBACK_AUTO_DELETE_SECONDS,
                    )
                return MAIN_MENU

            if status == "removed" and updated_record:
                if is_group_report_revision_only_record(updated_record):
                    await query.edit_message_text(
                        "📦 Ревизия убрана из учёта.\n\n"
                        "Сообщение больше не влияет на ревизию.\n\n"
                        f"📍 {updated_record.get('Точка', '—')}\n"
                        f"📅 {updated_record.get('Дата', '—')}\n"
                        f"👤 {updated_record.get('Кто', '—')}"
                    )
                else:
                    await query.edit_message_text(
                        "📦 Ревизия убрана из учёта.\n\n"
                        "Обслуживание осталось сохранённым.\n\n"
                        f"📍 {updated_record.get('Точка', '—')}\n"
                        f"📅 {updated_record.get('Дата', '—')}\n"
                        f"👤 {updated_record.get('Кто', '—')}"
                    )
            elif status == "no_revision":
                await query.edit_message_text("⚪ Для этого сообщения ревизия уже не учитывается.")
            else:
                await query.edit_message_text("⚪ Сообщение уже неактуально.")

            if query.message:
                schedule_single_message_cleanup(
                    context.application,
                    query.message.chat_id,
                    query.message.message_id,
                    GROUP_REPORT_FEEDBACK_AUTO_DELETE_SECONDS,
                )
            return MAIN_MENU

        try:
            status, deleted_record = await run_blocking(delete_group_report_entry_by_log_row, log_row_num)
        except Exception:
            logger.exception("Failed to delete saved group report log row %s", raw_id)
            await query.edit_message_text("❌ Не удалось отменить сохранение сообщения.")
            if query.message:
                schedule_single_message_cleanup(
                    context.application,
                    query.message.chat_id,
                    query.message.message_id,
                    GROUP_REPORT_FEEDBACK_AUTO_DELETE_SECONDS,
                )
            return MAIN_MENU

        if status == "deleted" and deleted_record:
            try:
                await refresh_group_service_today_posts(context.application, force=True)
            except Exception:
                logger.exception("Failed to refresh group service-today post after group report delete")
            await query.edit_message_text(build_group_report_delete_result_text(deleted_record))
        else:
            await query.edit_message_text("⚪ Сообщение уже неактуально.")

        if query.message:
            schedule_single_message_cleanup(
                context.application,
                query.message.chat_id,
                query.message.message_id,
                GROUP_REPORT_FEEDBACK_AUTO_DELETE_SECONDS,
            )
        return MAIN_MENU

    draft_id = raw_id
    drafts = get_group_report_drafts(context.application.bot_data)
    draft = drafts.get(draft_id)

    if not draft:
        await query.edit_message_text("⚪ Черновик уже недоступен.")
        if query.message:
            schedule_single_message_cleanup(
                context.application,
                query.message.chat_id,
                query.message.message_id,
                GROUP_REPORT_FEEDBACK_AUTO_DELETE_SECONDS,
            )
        return MAIN_MENU

    if action == "ignore":
        drafts.pop(draft_id, None)
        await query.edit_message_text("❌ Черновик не учтён.")
        if query.message:
            schedule_single_message_cleanup(
                context.application,
                query.message.chat_id,
                query.message.message_id,
                GROUP_REPORT_FEEDBACK_AUTO_DELETE_SECONDS,
            )
        return MAIN_MENU

    try:
        save_result = await run_group_sheet_write_with_retry(
            save_group_report_entry,
            draft,
            "group report draft save",
        )
        drafts.pop(draft_id, None)
        try:
            await refresh_group_service_today_posts(context.application, force=True)
        except Exception:
            logger.exception("Failed to refresh group service-today post after draft save")
        text = build_group_report_saved_text(draft, save_result)
        if draft.get("draft_mode") == "service_duplicate_warning":
            text = text.replace(
                "✅ Отчёт сохранён",
                "✅ Отчёт сохранён как отдельное обслуживание",
                1,
            )
        await query.edit_message_text(
            text,
            reply_markup=build_group_report_saved_markup(save_result),
        )
    except APIError as error:
        if is_google_sheets_busy_error(error):
            logger.exception("Google Sheets busy during group report draft save %s", draft_id)
            await show_sheets_busy_notice(query)
            return MAIN_MENU
        logger.exception("Failed to save group report draft %s", draft_id)
        await query.edit_message_text("❌ Не удалось сохранить сообщение.")
    except Exception:
        logger.exception("Failed to save group report draft %s", draft_id)
        await query.edit_message_text("❌ Не удалось сохранить сообщение.")

    if query.message:
        schedule_single_message_cleanup(
            context.application,
            query.message.chat_id,
            query.message.message_id,
            GROUP_REPORT_FEEDBACK_AUTO_DELETE_SECONDS,
        )

    return MAIN_MENU
# ============ ПРОЕЗД ============
async def travel_who(update: Update, context):
    query = update.callback_query
    mode = context.user_data.get("travel_mode", "add")
    if mode == "add":
        title = "Кто едет?"
    elif mode == "edit":
        title = "Чьи записи исправить?"
    else:
        title = "По кому показать историю?"
    if mode == "add":
        context.user_data.pop("travel_date", None)
    kb = [[InlineKeyboardButton(w, callback_data=f"tw_{w}")] for w in get_worker_names()]
    kb.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_travel_menu")])
    kb.append([InlineKeyboardButton("🏠 В меню", callback_data="back_main")])
    await query.edit_message_text(
        f"{build_progress_text(TRAVEL_FLOW_STEPS, 1)}\n\n💰 Проезд\n\n{title}",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return TRAVEL_WHO


async def travel_menu_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "back_main":
        return await start(update, context)
    if data == "back_service_menu":
        return await show_service_section_menu(query, context)
    if data == "back_travel_menu":
        return await show_travel_menu(query, context)
    if data == "travel_add":
        context.user_data["travel_mode"] = "add"
        context.user_data.pop("travel_edit", None)
        context.user_data.pop("travel_who", None)
        context.user_data.pop("travel_date", None)
        context.user_data.pop("travel_allowed_period", None)
        context.user_data.pop("travel_return_mode", None)
        context.user_data.pop("travel_return_period", None)
        context.user_data.pop("travel_return_screen", None)
        context.user_data.pop("travel_return_worker", None)
        return await travel_who(update, context)
    if data == "travel_history_person":
        context.user_data["travel_mode"] = "history"
        context.user_data.pop("travel_edit", None)
        context.user_data.pop("travel_who", None)
        context.user_data.pop("travel_date", None)
        context.user_data.pop("travel_allowed_period", None)
        context.user_data.pop("travel_return_mode", None)
        context.user_data.pop("travel_return_period", None)
        context.user_data.pop("travel_return_screen", None)
        context.user_data.pop("travel_return_worker", None)
        return await travel_who(update, context)
    if data == "travel_history_all":
        context.user_data["travel_mode"] = "history"
        context.user_data.pop("travel_edit", None)
        context.user_data.pop("travel_who", None)
        context.user_data.pop("travel_allowed_period", None)
        context.user_data.pop("travel_return_mode", None)
        context.user_data.pop("travel_return_period", None)
        context.user_data.pop("travel_return_screen", None)
        context.user_data.pop("travel_return_worker", None)
        return await show_travel_history_period_menu(query, context, "all")
    if data == "travel_edit":
        if not is_payout_editor(update):
            await query.answer("⛔ Только редактор выплат может менять проезд.", show_alert=True)
            return TRAVEL_MENU
        context.user_data["travel_mode"] = "edit"
        context.user_data.pop("travel_edit", None)
        context.user_data.pop("travel_who", None)
        context.user_data.pop("travel_date", None)
        context.user_data.pop("travel_allowed_period", None)
        context.user_data.pop("travel_return_mode", None)
        context.user_data.pop("travel_return_period", None)
        context.user_data.pop("travel_return_screen", None)
        context.user_data.pop("travel_return_worker", None)
        return await travel_who(update, context)
    return TRAVEL_MENU


async def travel_who_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "back_travel_menu":
        return await show_travel_menu(query, context)
    if query.data == "back_main":
        return await start(update, context)
    who = query.data.replace("tw_", "")
    context.user_data["travel_who"] = who
    if context.user_data.get("travel_mode") == "history":
        return await show_travel_history_period_menu(query, context, "person")
    if context.user_data.get("travel_mode") == "edit":
        return await show_travel_history_period_menu(query, context, "edit")
    context.user_data.pop("travel_date", None)
    return await show_travel_date_menu(query, context)


async def travel_history_period_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    data = query.data
    mode = context.user_data.get("travel_history_mode", "person")

    if data == "back_main":
        return await start(update, context)
    if data == "back_travel_menu":
        return await show_travel_menu(query, context)
    if data == "back_travel_who":
        return await travel_who(update, context)
    if data == "back_travel_history_period":
        return await show_travel_history_period_menu(query, context, mode)

    if mode == "edit":
        if not is_payout_editor(update):
            await query.answer("⛔ Только редактор выплат может менять проезд.", show_alert=True)
            return TRAVEL_HISTORY_PERIOD
        if data == "tr_edit_back_dates":
            return await show_travel_edit_date_screen(query, context, back_callback="back_travel_history_period")
        if data == "tr_edit_back_entries":
            return await render_travel_edit_entries_screen(query, context)
        if data.startswith("tr_edit_date:"):
            context.user_data["travel_date"] = data.split(":", 1)[1]
            return await render_travel_edit_entries_screen(query, context)
        if data.startswith("tr_edit_entry:"):
            return await show_travel_edit_card(query, context, int(data.split(":")[1]))
        if data.startswith("tr_edit_amount:"):
            row_num = int(data.split(":")[1])
            entry = await get_travel_entry_by_row(row_num)
            if not entry:
                return await render_travel_edit_entries_screen(query, context, notice="❌ Запись проезда не найдена.")
            context.user_data["travel_edit"] = {"mode": "general", "row_num": row_num}
            await show_text_screen(
                query,
                context,
                (
                    "<b>💰 Изменить сумму проезда</b>\n\n"
                    f"📅 {escape_html(entry.get('Дата', ''))}\n"
                    f"Текущая сумма: {format_money_spaced(entry.get('Сумма', 0))}\n\n"
                    "Введите новую сумму числом, например <code>96</code>."
                ),
                reply_markup=back_markup(f"tr_edit_entry:{row_num}"),
                parse_mode="HTML",
            )
            return PAYOUT_TRAVEL_EDIT_AMOUNT
        if data.startswith("tr_edit_date_prompt:"):
            row_num = int(data.split(":")[1])
            entry = await get_travel_entry_by_row(row_num)
            if not entry:
                return await render_travel_edit_entries_screen(query, context, notice="❌ Запись проезда не найдена.")
            context.user_data["travel_edit"] = {"mode": "general", "row_num": row_num}
            period_key = context.user_data.get("travel_allowed_period")
            period_line = (
                f"Месяц: {escape_html(format_period_label(period_key))}\n\n"
                if period_key
                else ""
            )
            await show_text_screen(
                query,
                context,
                (
                    "<b>📅 Изменить дату проезда</b>\n\n"
                    f"Текущая дата: {escape_html(entry.get('Дата', ''))}\n"
                    f"{period_line}"
                    "Введите новую дату в формате <code>дд.мм</code> или <code>дд.мм.гггг</code>."
                ),
                reply_markup=back_markup(f"tr_edit_entry:{row_num}"),
                parse_mode="HTML",
            )
            return PAYOUT_TRAVEL_EDIT_DATE
        if data.startswith("tr_edit_delete:"):
            row_num = int(data.split(":")[1])
            entry = await get_travel_entry_by_row(row_num)
            if not entry:
                return await render_travel_edit_entries_screen(query, context, notice="❌ Запись проезда не найдена.")
            await show_text_screen(
                query,
                context,
                (
                    "<b>🗑 Удалить запись проезда?</b>\n\n"
                    f"📅 {escape_html(entry.get('Дата', ''))}\n"
                    f"💰 {format_money_spaced(entry.get('Сумма', 0))}\n"
                    f"🧾 Строка: #{entry.get('__row', '?')}"
                ),
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("✅ Да, удалить", callback_data=f"tr_edit_delete_yes:{row_num}", style="danger")],
                        [InlineKeyboardButton("⬅️ Назад", callback_data=f"tr_edit_entry:{row_num}")],
                    ]
                ),
                parse_mode="HTML",
            )
            return TRAVEL_HISTORY_PERIOD
        if data.startswith("tr_edit_delete_yes:"):
            row_num = int(data.split(":")[1])
            entry = await get_travel_entry_by_row(row_num)
            if not entry:
                return await render_travel_edit_entries_screen(query, context, notice="❌ Запись проезда не найдена.")
            await run_blocking(delete_travel_row, row_num)
            return await render_travel_edit_entries_screen(query, context, notice="✅ Запись проезда удалена.")

    if data not in {"tr_hist_current", "tr_hist_prev"}:
        return TRAVEL_HISTORY_PERIOD

    period_code = "rp_month_current" if data == "tr_hist_current" else "rp_month_prev"
    config = get_report_period_config(period_code)
    if not config:
        await show_text_screen(
            query,
            context,
            "❌ Не удалось определить месяц.",
            reply_markup=back_markup("back_travel_history_period"),
        )
        return TRAVEL_HISTORY_PERIOD

    context.user_data["travel_date"] = format_date(config["end_date"])
    context.user_data["travel_allowed_period"] = config.get("period_key")
    if mode == "all":
        return await show_travel_month_all_screen(query, context, back_callback="back_travel_history_period")
    if mode == "edit":
        return await show_travel_edit_date_screen(query, context, back_callback="back_travel_history_period")
    return await show_travel_month_person_screen(query, context, back_callback="back_travel_history_period")


async def travel_date_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    d = query.data
    allowed_period = context.user_data.get("travel_allowed_period")

    if d == "back_main":
        return await start(update, context)
    if d == "back_service_menu":
        return await show_service_section_menu(query, context)
    if d == "back_travel_who":
        if context.user_data.get("travel_return_mode") == "payout":
            payout_worker = context.user_data.get("travel_return_worker", "")
            if payout_worker:
                get_payout_context(context)["worker"] = payout_worker
            return await show_payout_screen(
                query,
                context,
                screen=context.user_data.get("travel_return_screen", "overview"),
                period_key=context.user_data.get("travel_return_period"),
            )
        return await travel_who(update, context)
    if d == "tr_date_today":
        context.user_data["travel_date"] = today()
        error = get_period_restriction_error(context.user_data["travel_date"], allowed_period)
        if error:
            return await show_travel_date_menu(query, context, notice=error)
        return await show_travel_action_menu(query, context)
    if d == "tr_date_yesterday":
        context.user_data["travel_date"] = yesterday()
        error = get_period_restriction_error(context.user_data["travel_date"], allowed_period)
        if error:
            return await show_travel_date_menu(query, context, notice=error)
        return await show_travel_action_menu(query, context)
    if d == "tr_date_daybefore":
        context.user_data["travel_date"] = day_before_yesterday()
        error = get_period_restriction_error(context.user_data["travel_date"], allowed_period)
        if error:
            return await show_travel_date_menu(query, context, notice=error)
        return await show_travel_action_menu(query, context)
    if d == "tr_date_custom":
        await show_text_screen(
            query,
            context,
            "💰 Проезд\n\nВведите дату в формате дд.мм",
            reply_markup=back_markup("back_travel_date"),
        )
        return TRAVEL_DATE_CUSTOM
    return TRAVEL_DATE


async def travel_date_custom_back_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "back_travel_date":
        return await show_travel_date_menu(query, context)
    return TRAVEL_DATE_CUSTOM


async def travel_date_custom_handler(update: Update, context):
    parsed, error = validate_manual_date_input(update.message.text)
    if error:
        await update.message.reply_text(error, reply_markup=back_markup("back_travel_date"))
        return TRAVEL_DATE_CUSTOM

    context.user_data["travel_date"] = format_date(parsed)
    period_error = get_period_restriction_error(
        context.user_data["travel_date"],
        context.user_data.get("travel_allowed_period"),
    )
    if period_error:
        await update.message.reply_text(period_error, reply_markup=back_markup("back_travel_date"))
        return TRAVEL_DATE_CUSTOM
    who = context.user_data.get("travel_who", "?")
    await update.message.reply_text(
        f"{build_progress_text(TRAVEL_FLOW_STEPS, 3)}\n\n💰 Проезд — {who}\n\n📅 {context.user_data['travel_date']}\n\n➕ Добавить поездки:",
        reply_markup=build_travel_action_markup(),
    )
    return TRAVEL_ACTION


async def show_travel_summary_screen(query, context, prefix_text=None):
    who = context.user_data.get("travel_who", "?")
    date_str = get_travel_selected_date(context)

    try:
        travels = await run_blocking(get_all_travels)
        summary = build_travel_day_summary(who, date_str, travels)
    except Exception:
        logger.exception("Failed to build travel summary")
        summary = "❌ Не удалось получить поездки за выбранную дату."

    text_parts = [f"💰 Проезд — {who}", "", f"📅 {date_str}"]
    if prefix_text:
        text_parts.extend(["", prefix_text])
    text_parts.extend(["", summary])

    await show_text_screen(query, context, "\n".join(text_parts), reply_markup=build_travel_action_markup())
    return TRAVEL_ACTION


async def travel_action_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    d = query.data
    who = context.user_data.get("travel_who", "?")
    date_str = get_travel_selected_date(context)
    payout_return_mode = context.user_data.get("travel_return_mode") == "payout"
    payout_return_screen = context.user_data.get("travel_return_screen", "overview")
    payout_return_period = context.user_data.get("travel_return_period")

    if d == "back_main":
        return await start(update, context)
    if d == "back_service_menu":
        return await show_service_section_menu(query, context)
    if d == "back_travel_who":
        if payout_return_mode:
            payout_worker = context.user_data.get("travel_return_worker", "")
            if payout_worker:
                get_payout_context(context)["worker"] = payout_worker
            return await show_payout_screen(query, context, screen=payout_return_screen, period_key=payout_return_period)
        return await travel_who(update, context)
    if d == "back_travel_date":
        return await show_travel_date_menu(query, context)
    if d == "back_travel_action":
        return await show_travel_action_menu(query, context)

    if d in {"tr_default", "tr_count_1", "tr_count_2", "tr_count_3", "tr_count_4"}:
        trip_count = 1 if d == "tr_default" else int(d.rsplit("_", 1)[1])
        amount = DEFAULT_FARE * trip_count
        try:
            await run_blocking(add_travel_row, date_str, who, amount)
        except Exception:
            logger.exception("Failed to save counted travel record")
            await show_text_screen(query, context, "❌ Ошибка записи поездки.", reply_markup=back_markup("back_travel_action"))
            return TRAVEL_ACTION

        label = f"✅ Записано: {trip_count} поездок — {format_money(amount)}"
        if payout_return_mode:
            payout_worker = context.user_data.get("travel_return_worker", "")
            if payout_worker:
                get_payout_context(context)["worker"] = payout_worker
            get_payout_context(context)["travels_date"] = date_str
            return await show_payout_screen(
                query,
                context,
                screen=payout_return_screen,
                period_key=payout_return_period,
                notice=label,
            )
        return await show_travel_summary_screen(query, context, label)

    if d == "tr_count_custom":
        await show_text_screen(
            query,
            context,
            "🔢 Введите количество поездок за выбранную дату:",
            reply_markup=back_markup("back_travel_action"),
        )
        return TRAVEL_TRIPS_CUSTOM

    if d == "tr_custom":
        await show_text_screen(
            query,
            context,
            "💰 Введите сумму затрат на проезд:",
            reply_markup=back_markup("back_travel_action"),
        )
        return TRAVEL_CUSTOM_SUM

    if d in {"tr_today", "tr_summary"}:
        return await show_travel_summary_screen(query, context)
    if d == "tr_month_person":
        return await show_travel_month_person_screen(query, context)
    if d == "tr_month_all":
        return await show_travel_month_all_screen(query, context, back_callback="back_travel_action")

    return TRAVEL_ACTION


async def travel_custom_back_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "back_travel_action":
        return await show_travel_action_menu(query, context)
    return TRAVEL_CUSTOM_SUM


async def travel_custom_handler(update: Update, context):
    try:
        amount = int(update.message.text.strip())
        if amount < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "❌ Введите сумму числом, например 96.",
            reply_markup=back_markup("back_travel_action")
        )
        return TRAVEL_CUSTOM_SUM

    who = context.user_data.get("travel_who", "?")
    date_str = get_travel_selected_date(context)
    payout_return_mode = context.user_data.get("travel_return_mode") == "payout"
    payout_return_screen = context.user_data.get("travel_return_screen", "overview")
    payout_return_period = context.user_data.get("travel_return_period")
    try:
        await run_blocking(add_travel_row, date_str, who, amount)
        if payout_return_mode:
            payout_worker = context.user_data.get("travel_return_worker", "")
            if payout_worker:
                get_payout_context(context)["worker"] = payout_worker
            get_payout_context(context)["travels_date"] = date_str
            return await render_payout_screen_target(
                update.message,
                context,
                screen=payout_return_screen,
                period_key=payout_return_period,
                notice=f"✅ Записано: {format_money(amount)}",
            )
        travels = await run_blocking(get_all_travels)
        summary = build_travel_day_summary(who, date_str, travels)
        await update.message.reply_text(
            f"✅ Записано: {format_money(amount)}\n\n💰 Проезд — {who}\n📅 {date_str}\n\n{summary}",
            reply_markup=build_travel_action_markup(),
        )
    except Exception:
        logger.exception("Failed to save custom travel record")
        await update.message.reply_text("❌ Ошибка записи поездки.")
    return TRAVEL_ACTION


async def travel_trips_custom_back_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "back_travel_action":
        return await show_travel_action_menu(query, context)
    return TRAVEL_TRIPS_CUSTOM


async def travel_trips_custom_handler(update: Update, context):
    try:
        trip_count = int(update.message.text.strip())
        if trip_count <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "❌ Введите количество поездок целым числом, например 2.",
            reply_markup=back_markup("back_travel_action"),
        )
        return TRAVEL_TRIPS_CUSTOM

    who = context.user_data.get("travel_who", "?")
    date_str = get_travel_selected_date(context)
    amount = DEFAULT_FARE * trip_count
    payout_return_mode = context.user_data.get("travel_return_mode") == "payout"
    payout_return_screen = context.user_data.get("travel_return_screen", "overview")
    payout_return_period = context.user_data.get("travel_return_period")
    try:
        await run_blocking(add_travel_row, date_str, who, amount)
        if payout_return_mode:
            payout_worker = context.user_data.get("travel_return_worker", "")
            if payout_worker:
                get_payout_context(context)["worker"] = payout_worker
            get_payout_context(context)["travels_date"] = date_str
            return await render_payout_screen_target(
                update.message,
                context,
                screen=payout_return_screen,
                period_key=payout_return_period,
                notice=f"✅ Записано: {trip_count} поездок — {format_money(amount)}",
            )
        travels = await run_blocking(get_all_travels)
        summary = build_travel_day_summary(who, date_str, travels)
        await update.message.reply_text(
            f"✅ Записано: {trip_count} поездок — {format_money(amount)}\n\n"
            f"💰 Проезд — {who}\n📅 {date_str}\n\n{summary}",
            reply_markup=build_travel_action_markup(),
        )
    except Exception:
        logger.exception("Failed to save travel trips count")
        await update.message.reply_text("❌ Ошибка записи поездки.")
    return TRAVEL_ACTION

# ============ ОТЧЁТ ЗА ДЕНЬ ============
async def report_day(update: Update, context):
    query = update.callback_query
    await query.answer()

    try:
        await show_loading_state(query, context, "Собираю отчёт за день...")
        services = await run_blocking(get_all_services)
        travels = await run_blocking(get_all_travels)
        salary_tasks = await run_blocking(get_all_salary_tasks)
        td = today()

        today_svc = [s for s in services if s.get("Дата") == td]
        today_trv = [t for t in travels if t.get("Дата") == td]
        today_tasks = [task for task in salary_tasks if task.get("Дата") == td]

        text = f"📊 Отчёт за {td}:\n\n"

        workers_today = set()
        for s in today_svc:
            workers_today.add(s.get("Кто", "?"))
            for salary_worker in get_service_salary_workers(s):
                workers_today.add(salary_worker)
        for t in today_trv:
            workers_today.add(t.get("Кто", "?"))
        for task in today_tasks:
            workers_today.add(task.get("Кто", "?"))

        if not workers_today:
            text += "Нет данных за сегодня"
        else:
            grand_total = 0
            for w in order_workers(workers_today):
                credited_svc = filter_services_for_salary_worker(today_svc, w)
                w_svc = [s for s in today_svc if s.get("Кто") == w]
                w_trv = [t for t in today_trv if t.get("Кто") == w]
                w_tasks = [task for task in today_tasks if task.get("Кто") == w]
                transferred_in = [s for s in credited_svc if s.get("Кто") != w]

                text += f"👤 {w}:\n"

                svc_sum = sum_service_amounts_for_worker(credited_svc, w)
                purch_sum = sum_amounts(w_svc, "Сумма закупок")
                task_sum = sum_amounts(w_tasks, "Сумма")
                if w_svc:
                    text += f"  🔧 Обслужено: {len(w_svc)} точек\n"
                    for s in w_svc:
                        point = s.get("Точка", "?")
                        transfer_note = build_service_salary_transfer_note(s)
                        text += f"    ✅ {point}{transfer_note}\n"
                if transferred_in:
                    text += f"  🔁 В ЗП зачтено от других: {len(transferred_in)}\n"
                    for s in credited_svc:
                        if s.get("Кто") == w:
                            continue
                        point = s.get("Точка", "?")
                        source_worker = s.get("Кто", "?")
                        service_amount = get_service_worker_amount(s, w)
                        if service_amount > 0:
                            text += f"    ↪️ {point} от {source_worker} — {format_money_spaced(service_amount)}\n"
                        else:
                            text += f"    ↪️ {point} от {source_worker}\n"

                trv_sum = int(sum_amounts(w_trv, "Сумма"))
                trv_count = len(w_trv)

                if purch_sum:
                    text += f"  🛒 Закупки: {format_money_spaced(purch_sum)}\n"
                if trv_count:
                    text += f"  🚌 Проезд: {format_money_spaced(trv_sum)} ({trv_count} поездок)\n"
                if task_sum:
                    text += f"  🧰 Допзадачи: {format_money_spaced(task_sum)}\n"

                w_total = svc_sum + purch_sum + trv_sum + task_sum
                if w in set(get_paid_workers()):
                    text += f"  💰 Итого {w}: {format_money_spaced(w_total)}\n"
                else:
                    if purch_sum or trv_sum or task_sum:
                        text += f"  💰 Расходы {w}: {format_money_spaced(purch_sum + trv_sum + task_sum)}\n"

                grand_total += w_total
                text += "\n"

            text += f"━━━━━━━━━━━━━━━━\n💰 Общие расходы: {format_money_spaced(grand_total)}"

    except Exception:
        logger.exception("Failed to build day report")
        text = "❌ Не удалось собрать отчёт за день."

    kb = [
        [InlineKeyboardButton("⬅️ К отчётам", callback_data="back_reports_menu")],
        [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))
    return REPORT_DAY

async def report_day_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "back_reports_menu":
        return await show_reports_section_menu(query, context)
    if query.data == "back_main":
        return await start(update, context)
    return REPORT_DAY

# ============ ОТЧЁТ ЗА ПЕРИОД ============
def get_previous_month_period_key():
    return shift_period(current_period_key(), -1)


def get_report_period_config(period_code):
    today_date = now_local().date()

    if period_code == "rp_week":
        start_date = today_date - timedelta(days=6)
        return {
            "start_date": start_date,
            "end_date": today_date,
            "title": f"Последние 7 дней ({format_date(start_date)}–{format_date(today_date)})",
        }

    period_key = current_period_key() if period_code in {"rp_month", "rp_month_current"} else get_previous_month_period_key()
    year, month = parse_period_key(period_key)
    if year is None:
        return None

    start_date = date(year, month, 1)
    end_date = date(year, month, month_last_day(start_date))
    if period_code in {"rp_month", "rp_month_current"}:
        end_date = min(end_date, today_date)

    return {
        "start_date": start_date,
        "end_date": end_date,
        "title": format_period_label(period_key),
        "period_key": period_key,
    }


def get_default_salary_period_code():
    return "rp_month_prev" if now_local().date().day <= 7 else "rp_month_current"


def get_default_payout_worker_name():
    paid_workers = get_paid_workers()
    return paid_workers[0] if paid_workers else ""


def get_salary_button_label(worker):
    config = get_report_period_config(get_default_salary_period_code())
    worker_name = str(worker or "").strip() or "Сотрудник"
    if not config:
        return f"💼 {worker_name}"
    month_name = str(config["title"]).split(" ", 1)[0]
    return f"💼 {worker_name} · {month_name}"


def is_date_in_report_range(date_str, start_date, end_date):
    dt = parse_date(date_str)
    if not dt:
        return False
    current_date = dt.date()
    return start_date <= current_date <= end_date


def order_workers(workers):
    known_workers = get_worker_names()
    known = [worker for worker in known_workers if worker in workers]
    extras = sorted(worker for worker in workers if worker not in known_workers)
    return known + extras


def sum_amounts(items, key):
    total = 0.0
    for item in items:
        amount = parse_numeric_value(item.get(key, ""))
        if amount is not None:
            total += amount
    return total


def filter_services_for_salary_worker(services, worker):
    return [service for service in services if worker in get_service_salary_workers(service)]


def get_service_worker_amount(service, worker):
    paid_worker_names = set(get_paid_workers())
    paid_workers = [item for item in get_service_salary_workers(service) if item in paid_worker_names]
    if worker not in paid_workers:
        return 0.0

    stored_sum = parse_numeric_value(service.get("Сумма обслуж", ""))
    if stored_sum is None:
        return float(SERVICE_PRICE)

    if not paid_workers:
        return 0.0
    return float(stored_sum) / len(paid_workers)


def sum_service_amounts_for_worker(services, worker):
    return sum(get_service_worker_amount(service, worker) for service in services)


def build_service_salary_transfer_note(service):
    actor = str(service.get("Кто", "")).strip()
    credited = get_service_salary_workers(service)
    default_workers = default_service_salary_workers(actor)
    if credited == default_workers:
        return ""
    if credited:
        return f" → ЗП: {', '.join(credited)}"
    return " → не в ЗП"


def build_worker_service_day_lines(worker_services):
    if not worker_services:
        return []

    points_by_day = {}
    for service in worker_services:
        date_key = service.get("Дата", "")
        point = str(service.get("Точка", "")).strip() or "?"
        points_by_day.setdefault(date_key, []).append(point)

    lines = []
    ordered_dates = sorted(points_by_day.keys(), key=lambda value: parse_date(value) or datetime.min)
    for date_key in ordered_dates:
        points = order_points(set(points_by_day[date_key]))
        lines.append(f"• {date_key} — {', '.join(points)}")
    return lines


def build_worker_travel_day_lines(worker_travels):
    if not worker_travels:
        return []

    totals_by_day = {}
    counts_by_day = {}
    for travel in worker_travels:
        date_key = travel.get("Дата", "")
        amount = parse_numeric_value(travel.get("Сумма", ""))
        if amount is None:
            continue
        totals_by_day[date_key] = totals_by_day.get(date_key, 0.0) + amount
        counts_by_day[date_key] = counts_by_day.get(date_key, 0) + 1

    lines = []
    ordered_dates = sorted(totals_by_day.keys(), key=lambda value: parse_date(value) or datetime.min)
    for date_key in ordered_dates:
        trip_word = "поездка" if counts_by_day[date_key] == 1 else "поездок"
        lines.append(
            f"• {date_key} — {format_money_spaced(totals_by_day[date_key])} ({counts_by_day[date_key]} {trip_word})"
        )
    return lines


def build_worker_salary_task_lines(worker_tasks):
    if not worker_tasks:
        return []

    ordered_tasks = sorted(
        worker_tasks,
        key=lambda item: (parse_date(item.get("Дата", "")) or datetime.min, str(item.get("Описание", ""))),
    )
    lines = []
    for task in ordered_tasks:
        date_key = task.get("Дата", "")
        description = str(task.get("Описание", "")).strip() or "Без описания"
        amount_text = format_money_spaced(task.get("Сумма", ""))
        lines.append(f"• {date_key} — {description} ({amount_text})")
    return lines


def build_period_report_text(title, services, travels, salary_tasks=None):
    salary_tasks = salary_tasks or []
    workers_all = set()
    for service in services:
        who = str(service.get("Кто", "")).strip()
        if who:
            workers_all.add(who)
        workers_all.update(get_service_salary_workers(service))
    for item in [*travels, *salary_tasks]:
        who = str(item.get("Кто", "")).strip()
        if who:
            workers_all.add(who)
    lines = [f"📈 Отчёт — {title}", ""]

    if not workers_all:
        lines.append("Нет данных за выбранный период.")
        return "\n".join(lines)

    grand_service = 0.0
    grand_purchases = 0.0
    grand_travel = 0.0
    grand_salary_tasks = 0.0
    grand_total = 0.0

    for worker in order_workers(workers_all):
        credited_services = filter_services_for_salary_worker(services, worker)
        worker_services = [service for service in services if service.get("Кто") == worker]
        worker_travels = [travel for travel in travels if travel.get("Кто") == worker]
        worker_salary_tasks = [task for task in salary_tasks if task.get("Кто") == worker]
        transferred_in = [service for service in credited_services if service.get("Кто") != worker]
        transferred_out = [
            service
            for service in worker_services
            if get_service_salary_workers(service) != default_service_salary_workers(worker)
        ]

        service_sum = sum_service_amounts_for_worker(credited_services, worker)
        purchase_sum = sum_amounts(worker_services, "Сумма закупок")
        travel_sum = sum_amounts(worker_travels, "Сумма")
        salary_task_sum = sum_amounts(worker_salary_tasks, "Сумма")
        worker_total = service_sum + purchase_sum + travel_sum + salary_task_sum

        lines.append(f"👤 {worker}")

        if worker_services:
            lines.append(f"🔧 Обслуживаний: {len(worker_services)}")
            lines.extend(build_worker_service_day_lines(worker_services))
        else:
            lines.append("🔧 Обслуживаний: 0")

        if transferred_in:
            lines.append(f"🔁 Зачтено в ЗП от других: {len(transferred_in)}")
        if transferred_out:
            lines.append(f"↪️ Передано в ЗП другому: {len(transferred_out)}")

        travel_lines = build_worker_travel_day_lines(worker_travels)
        if travel_lines:
            lines.append("🚌 Проезд по дням")
            lines.extend(travel_lines)
        salary_task_lines = build_worker_salary_task_lines(worker_salary_tasks)
        if salary_task_lines:
            lines.append("🧰 Задачи и доплаты")
            lines.extend(salary_task_lines)

        lines.append(f"💰 Обслуживание: {format_money_spaced(service_sum)}")
        if purchase_sum:
            lines.append(f"🛒 Закупки: {format_money_spaced(purchase_sum)}")
        if travel_sum:
            lines.append(f"🚌 Проезд: {format_money_spaced(travel_sum)}")
        if salary_task_sum:
            lines.append(f"🧰 Допзадачи: {format_money_spaced(salary_task_sum)}")

        if worker in set(get_paid_workers()):
            lines.append(f"✅ К выплате: {format_money_spaced(worker_total)}")
        else:
            lines.append(f"💸 Расходы: {format_money_spaced(purchase_sum + travel_sum + salary_task_sum)}")

        lines.append("")

        grand_service += service_sum
        grand_purchases += purchase_sum
        grand_travel += travel_sum
        grand_salary_tasks += salary_task_sum
        grand_total += worker_total

    lines.extend(
        [
            "━━━━━━━━━━━━━━━━",
            f"🔧 Обслуживание: {format_money_spaced(grand_service)}",
            f"🛒 Закупки: {format_money_spaced(grand_purchases)}",
            f"🚌 Проезд: {format_money_spaced(grand_travel)}",
            f"🧰 Допзадачи: {format_money_spaced(grand_salary_tasks)}",
            f"💰 Общие расходы: {format_money_spaced(grand_total)}",
        ]
    )
    return "\n".join(lines)


def build_worker_salary_card_text(worker, title, services, travels, salary_tasks=None):
    salary_tasks = salary_tasks or []
    credited_services = filter_services_for_salary_worker(services, worker)
    worker_services = [service for service in services if service.get("Кто") == worker]
    worker_travels = [travel for travel in travels if travel.get("Кто") == worker]
    worker_salary_tasks = [task for task in salary_tasks if task.get("Кто") == worker]

    service_sum = sum_service_amounts_for_worker(credited_services, worker)
    purchase_sum = sum_amounts(worker_services, "Сумма закупок")
    travel_sum = sum_amounts(worker_travels, "Сумма")
    salary_task_sum = sum_amounts(worker_salary_tasks, "Сумма")
    total_sum = service_sum + purchase_sum + travel_sum + salary_task_sum

    lines = [f"💸 {worker} · {title}", ""]

    if not credited_services and not worker_travels and not worker_salary_tasks and not worker_services:
        lines.append("⚪ За выбранный период данных пока нет.")
        return "\n".join(lines)

    service_day_lines = build_worker_service_day_lines(credited_services)
    travel_day_lines = build_worker_travel_day_lines(worker_travels)
    salary_task_lines = build_worker_salary_task_lines(worker_salary_tasks)

    lines.append(f"🔧 Обслуживаний: {len(credited_services)}")
    lines.append(f"💰 Обслуживание: {format_money_spaced(service_sum)}")
    if purchase_sum:
        lines.append(f"🛒 Закупки: {format_money_spaced(purchase_sum)}")
    if travel_sum:
        lines.append(f"🚌 Проезд: {format_money_spaced(travel_sum)}")
    if salary_task_sum:
        lines.append(f"🧰 Допзадачи: {format_money_spaced(salary_task_sum)}")
    lines.append(f"✅ К выплате: {format_money_spaced(total_sum)}")

    if service_day_lines:
        lines.extend(["", "📍 Точки по датам"])
        lines.extend(service_day_lines)

    if travel_day_lines:
        lines.extend(["", "🚌 Проезд по датам"])
        lines.extend(travel_day_lines)

    if salary_task_lines:
        lines.extend(["", "🧰 Задачи и доплаты"])
        lines.extend(salary_task_lines)

    return "\n".join(lines)


def build_payout_sources():
    return {
        "services": get_all_services_with_rows(),
        "travels": get_all_travels_with_rows(),
        "salary_tasks": get_all_salary_tasks_with_rows(),
        "payouts": get_all_payouts_with_rows(),
    }


def is_payout_period_locked(settlement):
    return settlement.get("status") == PAYOUT_STATUS_PAID


def get_payout_status_icon(status):
    return "✅" if status == PAYOUT_STATUS_PAID else "⚪"


def get_payout_display_total(settlement):
    return settlement.get("display_total", settlement.get("total", 0))


def format_short_date_label(date_str):
    parsed = parse_date(date_str)
    if not parsed:
        return str(date_str)
    return parsed.strftime("%d.%m")


def get_unique_sorted_date_keys(items):
    date_keys = {
        str(item.get("Дата", "")).strip()
        for item in items
        if str(item.get("Дата", "")).strip()
    }
    return sorted(date_keys, key=lambda value: parse_date(value) or datetime.min)


def build_compact_date_summary(items, limit=8):
    date_labels = [format_short_date_label(value) for value in get_unique_sorted_date_keys(items)]
    if not date_labels:
        return ""
    if len(date_labels) <= limit:
        return ", ".join(date_labels)
    hidden_count = len(date_labels) - limit
    return f"{', '.join(date_labels[:limit])} ... +{hidden_count} дн."


def compute_payout_settlement(period_key, sources=None, worker=None):
    worker = str(worker or get_default_payout_worker_name()).strip()
    if not worker:
        raise RuntimeError("No paid workers configured")
    sources = sources or build_payout_sources()
    services = sources.get("services", [])
    travels = sources.get("travels", [])
    salary_tasks = sources.get("salary_tasks", [])
    payouts = sources.get("payouts", [])

    payout_record = find_payout_record(payouts, period_key, worker)
    payout_entry = build_payout_entry_from_record(payout_record)

    credited_services = sorted(
        [
            service
            for service in services
            if is_date_in_period_key(service.get("Дата", ""), period_key)
            and worker in get_service_salary_workers(service)
        ],
        key=lambda item: (
            parse_date(item.get("Дата", "")) or datetime.min,
            str(item.get("Точка", "")),
            int(item.get("__row", 0)),
        ),
    )
    worker_services = sorted(
        [
            service
            for service in services
            if str(service.get("Кто", "")).strip() == worker
            and is_date_in_period_key(service.get("Дата", ""), period_key)
        ],
        key=lambda item: (
            parse_date(item.get("Дата", "")) or datetime.min,
            str(item.get("Точка", "")),
            int(item.get("__row", 0)),
        ),
    )
    purchase_entries = [
        service
        for service in worker_services
        if parse_numeric_value(service.get("Сумма закупок", "")) or str(service.get("Закупки", "")).strip()
    ]
    worker_travels = sorted(
        [
            travel
            for travel in travels
            if str(travel.get("Кто", "")).strip() == worker
            and is_date_in_period_key(travel.get("Дата", ""), period_key)
        ],
        key=lambda item: (
            parse_date(item.get("Дата", "")) or datetime.min,
            int(item.get("__row", 0)),
        ),
    )
    worker_salary_tasks = sorted(
        [
            task
            for task in salary_tasks
            if str(task.get("Кто", "")).strip() == worker
            and is_date_in_period_key(task.get("Дата", ""), period_key)
        ],
        key=lambda item: (
            parse_date(item.get("Дата", "")) or datetime.min,
            str(item.get("Описание", "")),
            int(item.get("__row", 0)),
        ),
    )

    live_service_sum = sum_service_amounts_for_worker(credited_services, worker)
    live_purchase_sum = sum_amounts(worker_services, "Сумма закупок")
    live_travel_sum = sum_amounts(worker_travels, "Сумма")
    live_salary_task_sum = sum_amounts(worker_salary_tasks, "Сумма")
    correction = payout_entry.get("correction", 0)
    live_total = live_service_sum + live_purchase_sum + live_travel_sum + live_salary_task_sum + correction

    is_paid = payout_entry.get("status") == PAYOUT_STATUS_PAID and payout_record is not None
    display_service_sum = payout_entry.get("service_sum", 0) if is_paid else live_service_sum
    display_service_count = payout_entry.get("service_count", 0) if is_paid else len(credited_services)
    display_purchase_sum = payout_entry.get("purchase_sum", 0) if is_paid else live_purchase_sum
    display_travel_sum = payout_entry.get("travel_sum", 0) if is_paid else live_travel_sum
    display_travel_count = payout_entry.get("travel_count", 0) if is_paid else len(worker_travels)
    display_salary_task_sum = payout_entry.get("salary_task_sum", 0) if is_paid else live_salary_task_sum
    display_salary_task_count = payout_entry.get("salary_task_count", 0) if is_paid else len(worker_salary_tasks)
    display_total = payout_entry.get("total", 0) if is_paid else live_total

    return {
        "period_key": period_key,
        "period_label": format_period_label(period_key),
        "worker": worker,
        "services": credited_services,
        "service_count": len(credited_services),
        "service_sum": live_service_sum,
        "purchases": purchase_entries,
        "purchase_sum": live_purchase_sum,
        "travels": worker_travels,
        "travel_count": len(worker_travels),
        "travel_sum": live_travel_sum,
        "salary_tasks": worker_salary_tasks,
        "salary_task_count": len(worker_salary_tasks),
        "salary_task_sum": live_salary_task_sum,
        "correction": correction,
        "correction_note": payout_entry.get("correction_note", ""),
        "total": live_total,
        "status": payout_entry.get("status", PAYOUT_STATUS_PENDING),
        "paid_date": payout_entry.get("paid_date", ""),
        "paid_by": payout_entry.get("paid_by", ""),
        "display_service_sum": display_service_sum,
        "display_service_count": display_service_count,
        "display_purchase_sum": display_purchase_sum,
        "display_travel_sum": display_travel_sum,
        "display_travel_count": display_travel_count,
        "display_salary_task_sum": display_salary_task_sum,
        "display_salary_task_count": display_salary_task_count,
        "display_total": display_total,
        "has_snapshot": is_paid,
        "has_post_close_changes": is_paid and abs(live_total - display_total) > 0.009,
    }


def compute_kirill_settlement(period_key, sources=None, worker=None):
    return compute_payout_settlement(period_key, sources=sources, worker=worker)


def get_payout_screen_section(screen):
    screen = str(screen or "")
    for section in ("services", "purchases", "travels", "tasks"):
        if screen == section or screen.startswith(f"{section}_"):
            return section
    return "overview"


def get_payout_section_title(section):
    return {
        "services": "🔧 Обслуживания",
        "purchases": "🛒 Закупки",
        "travels": "🚌 Проезд",
        "tasks": "🧰 Допзадачи",
    }.get(section, "💼 Выплата")


def get_payout_section_records(settlement, section):
    return {
        "services": settlement.get("services", []),
        "purchases": settlement.get("purchases", []),
        "travels": settlement.get("travels", []),
        "tasks": settlement.get("salary_tasks", []),
    }.get(section, [])


def get_payout_section_item_amount(item, section, settlement):
    if section == "services":
        return get_service_worker_amount(item, settlement["worker"])
    if section == "purchases":
        return parse_numeric_value(item.get("Сумма закупок", "")) or 0
    if section == "travels":
        return parse_numeric_value(item.get("Сумма", "")) or 0
    if section == "tasks":
        return parse_numeric_value(item.get("Сумма", "")) or 0
    return 0


def build_payout_point_token(section, date_str, point):
    raw = f"{section}|{date_str}|{point}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:10]


def build_payout_date_groups(settlement, section):
    grouped = {}
    for item in get_payout_section_records(settlement, section):
        date_key = str(item.get("Дата", "")).strip()
        if not date_key:
            continue
        bucket = grouped.setdefault(date_key, {"date": date_key, "items": [], "amount": 0, "count": 0})
        bucket["items"].append(item)
        bucket["count"] += 1
        bucket["amount"] += get_payout_section_item_amount(item, section, settlement)

    return sorted(
        grouped.values(),
        key=lambda bucket: parse_date(bucket["date"]) or datetime.min,
        reverse=True,
    )


def build_payout_point_groups(settlement, section, date_str):
    grouped = {}
    for item in get_payout_section_records(settlement, section):
        if str(item.get("Дата", "")).strip() != str(date_str).strip():
            continue
        point = str(item.get("Точка", "")).strip() or "Без точки"
        bucket = grouped.setdefault(point, {"point": point, "items": [], "amount": 0})
        bucket["items"].append(item)
        bucket["amount"] += get_payout_section_item_amount(item, section, settlement)

    ordered = []
    for point in order_points(set(grouped.keys())):
        ordered.append(grouped[point])
    return ordered


def build_payout_service_entry_compact_text(entry, settlement):
    lines = [f"<b>#{entry['__row']}</b>"]
    lines.append(escape_html(build_service_entry_text(entry)))
    service_sum = get_service_worker_amount(entry, settlement["worker"])
    purchase_sum = parse_numeric_value(entry.get("Сумма закупок", "")) or 0
    if service_sum:
        lines.append(f"💰 В выплату: {format_money_spaced(service_sum)}")
    if purchase_sum:
        lines.append(f"🛒 Сумма закупок: {format_money_spaced(purchase_sum)}")
    return "\n".join(lines)


def build_payout_travel_entry_compact_text(entry):
    return "\n".join(
        [
            f"<b>#{entry['__row']}</b>",
            f"📅 {escape_html(entry.get('Дата', ''))}",
            f"💰 {format_money_spaced(entry.get('Сумма', 0))}",
        ]
    )


def build_payout_salary_task_entry_compact_text(entry):
    description = str(entry.get("Описание", "")).strip() or "Без описания"
    lines = [
        f"<b>#{entry['__row']}</b>",
        f"📅 {escape_html(entry.get('Дата', ''))}",
        f"📝 {escape_html(description)}",
        f"💰 {format_money_spaced(entry.get('Сумма', 0))}",
    ]
    added_by = str(entry.get("Кто добавил", "")).strip()
    if added_by:
        lines.append(f"👤 Добавил: {escape_html(added_by)}")
    return "\n".join(lines)


def build_payout_date_menu_text(section, settlement):
    title = get_payout_section_title(section)
    lines = [f"<b>{title} · {escape_html(settlement['worker'])} — {escape_html(settlement['period_label'])}</b>", ""]
    groups = build_payout_date_groups(settlement, section)
    if not groups:
        empty_text = {
            "services": "⚪ За выбранный месяц обслуживаний в выплату не найдено.",
            "purchases": "⚪ За выбранный месяц закупок нет.",
            "travels": "⚪ За выбранный месяц записей проезда нет.",
            "tasks": "⚪ За выбранный месяц допзадач нет.",
        }.get(section, "⚪ За выбранный месяц записей нет.")
        lines.append(empty_text)
        return "\n".join(lines)

    lines.append("Сначала выберите дату:")
    lines.append("")
    for bucket in groups:
        lines.append(
            f"• {escape_html(bucket['date'])} — {bucket['count']} зап. · {format_money_spaced(bucket['amount'])}"
        )
    lines.extend(
        [
            "",
            "━━━━━━━━━━━━━━━━",
        ]
    )
    if section == "services":
        lines.append(
            f"Итого: {settlement['service_count']} × {format_money_spaced(SERVICE_PRICE)} = "
            f"{format_money_spaced(settlement['service_sum'])}"
        )
    elif section == "purchases":
        lines.append(f"Итого: {format_money_spaced(settlement['purchase_sum'])}")
    elif section == "travels":
        lines.append(
            f"Итого: {settlement['travel_count']} записей — {format_money_spaced(settlement['travel_sum'])}"
        )
    else:
        lines.append(f"Итого: {format_money_spaced(settlement['salary_task_sum'])}")
    return "\n".join(lines)


def build_payout_point_menu_text(section, settlement, date_str):
    title = get_payout_section_title(section)
    lines = [
        f"<b>{title} · {escape_html(settlement['worker'])} — {escape_html(settlement['period_label'])}</b>",
        "",
        f"📅 {escape_html(date_str)}",
        "",
    ]
    groups = build_payout_point_groups(settlement, section, date_str)
    if not groups:
        lines.append("⚪ За выбранную дату записей по точкам не найдено.")
        return "\n".join(lines)
    lines.append("Теперь выберите точку:")
    lines.append("")
    for bucket in groups:
        lines.append(
            f"• {escape_html(bucket['point'])} — {len(bucket['items'])} зап. · {format_money_spaced(bucket['amount'])}"
        )
    return "\n".join(lines)


def build_payout_entries_menu_text(section, settlement, date_str, point=None):
    title = get_payout_section_title(section)
    lines = [
        f"<b>{title} · {escape_html(settlement['worker'])} — {escape_html(settlement['period_label'])}</b>",
        "",
        f"📅 {escape_html(date_str)}",
    ]
    if point:
        lines.append(f"📍 {escape_html(point)}")
    lines.append("")

    items = [
        item for item in get_payout_section_records(settlement, section)
        if str(item.get("Дата", "")).strip() == str(date_str).strip()
        and (point is None or (str(item.get("Точка", "")).strip() or "Без точки") == point)
    ]
    items.sort(key=lambda item: int(item.get("__row", 0)), reverse=True)

    if not items:
        lines.append("⚪ Записей не найдено.")
        return "\n".join(lines)

    if section == "purchases":
        lines.append("Удаление строки удалит всю запись обслуживания целиком.")
        lines.append("")

    for idx, item in enumerate(items, start=1):
        lines.append(f"{idx}.")
        if section in {"services", "purchases"}:
            lines.append(build_payout_service_entry_compact_text(item, settlement))
        elif section == "travels":
            lines.append(build_payout_travel_entry_compact_text(item))
        else:
            lines.append(build_payout_salary_task_entry_compact_text(item))
        if idx != len(items):
            lines.append("")
            lines.append("────────")
            lines.append("")
    return "\n".join(lines)


def build_payout_date_menu_markup(section, settlement, can_edit):
    keyboard = []
    for bucket in build_payout_date_groups(settlement, section):
        label = (
            f"{bucket['date']} · {len(bucket['items'])} зап. · "
            f"{format_money_spaced(bucket['amount'])}"
        )
        keyboard.append(
            [InlineKeyboardButton(label, callback_data=f"payout_date:{section}:{bucket['date']}")]
        )
    if can_edit:
        add_callback = {
            "services": "payout_service_add",
            "travels": "payout_travel_add",
            "tasks": "payout_task_add",
        }.get(section)
        if add_callback:
            add_title = {
                "services": "➕ Добавить обслуживание",
                "travels": "➕ Добавить проезд",
                "tasks": "➕ Добавить допзадачу",
            }[section]
            keyboard.append([InlineKeyboardButton(add_title, callback_data=add_callback)])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="payout_screen:overview")])
    return InlineKeyboardMarkup(keyboard)


def build_payout_point_menu_markup(section, settlement, date_str):
    keyboard = []
    for bucket in build_payout_point_groups(settlement, section, date_str):
        label = (
            f"{bucket['point']} · {len(bucket['items'])} зап. · "
            f"{format_money_spaced(bucket['amount'])}"
        )
        token = build_payout_point_token(section, date_str, bucket["point"])
        keyboard.append(
            [InlineKeyboardButton(label, callback_data=f"payout_point:{section}:{date_str}:{token}")]
        )
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"payout_screen:{section}")])
    return InlineKeyboardMarkup(keyboard)


def build_payout_entries_menu_markup(section, settlement, can_edit, date_str, point=None):
    keyboard = []
    items = [
        item for item in get_payout_section_records(settlement, section)
        if str(item.get("Дата", "")).strip() == str(date_str).strip()
        and (point is None or (str(item.get("Точка", "")).strip() or "Без точки") == point)
    ]
    items.sort(key=lambda item: int(item.get("__row", 0)), reverse=True)

    if can_edit:
        for item in items:
            if section == "services":
                label = f"#{item['__row']} · {item.get('Кто', '?')} · {format_number(item.get('Вода(бут)', 0))} бут"
                keyboard.append(
                    [
                        InlineKeyboardButton(f"✏️ {label}", callback_data=f"payout_service_edit:{item['__row']}"),
                        InlineKeyboardButton("🗑", callback_data=f"payout_service_del:{item['__row']}"),
                    ]
                )
            elif section == "purchases":
                label = (
                    f"#{item['__row']} · {item.get('Точка', '?')} · "
                    f"{format_money_spaced(item.get('Сумма закупок', 0))}"
                )
                keyboard.append(
                    [
                        InlineKeyboardButton(f"✏️ {label}", callback_data=f"payout_purchase_edit:{item['__row']}"),
                        InlineKeyboardButton("🗑", callback_data=f"payout_purchase_del:{item['__row']}"),
                    ]
                )
            elif section == "travels":
                label = f"#{item['__row']} · {format_money_spaced(item.get('Сумма', 0))}"
                keyboard.append(
                    [
                        InlineKeyboardButton(f"✏️ {label}", callback_data=f"payout_travel_edit:{item['__row']}"),
                        InlineKeyboardButton("🗑", callback_data=f"payout_travel_del:{item['__row']}"),
                    ]
                )
            elif section == "tasks":
                description = str(item.get("Описание", "")).strip() or "Без описания"
                label = f"#{item['__row']} · {description[:20]}"
                keyboard.append(
                    [
                        InlineKeyboardButton(f"✏️ {label}", callback_data=f"payout_task_edit:{item['__row']}"),
                        InlineKeyboardButton("🗑", callback_data=f"payout_task_del:{item['__row']}"),
                    ]
                )

        add_callback = {
            "services": "payout_service_add",
            "travels": "payout_travel_add",
            "tasks": "payout_task_add",
        }.get(section)
        if add_callback:
            add_title = {
                "services": "➕ Добавить обслуживание",
                "travels": "➕ Добавить проезд",
                "tasks": "➕ Добавить допзадачу",
            }[section]
            keyboard.append([InlineKeyboardButton(add_title, callback_data=add_callback)])

    back_screen = {
        "services": "services_points",
        "purchases": "purchases_points",
        "travels": "travels",
        "tasks": "tasks",
    }[section]
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"payout_screen:{back_screen}")])
    return InlineKeyboardMarkup(keyboard)


def build_payout_month_menu_text(worker):
    return f"<b>💼 {escape_html(worker)} — итоги и выплата</b>\n\nВыберите месяц:"


def build_payout_month_menu_markup(period_keys, sources, worker):
    keyboard = []
    for period_key in period_keys:
        settlement = compute_payout_settlement(period_key, sources=sources, worker=worker)
        label = (
            f"{get_payout_status_icon(settlement['status'])} "
            f"{format_period_label(period_key)} · {format_money_spaced(get_payout_display_total(settlement))}"
        )
        keyboard.append([InlineKeyboardButton(label, callback_data=f"payout_month:{period_key}")])
    keyboard.append([InlineKeyboardButton("⬅️ К отчётам", callback_data="back_reports_menu")])
    keyboard.append([InlineKeyboardButton("🏠 В меню", callback_data="back_main")])
    return InlineKeyboardMarkup(keyboard)


def build_payout_overview_text(settlement, notice=None):
    service_dates_summary = build_compact_date_summary(settlement.get("services", []))
    lines = [f"<b>💼 {escape_html(settlement['worker'])} — итоги и выплата</b>"]
    if notice:
        lines.extend([escape_html(notice), ""])
    lines.extend(
        [
            f"📅 {escape_html(settlement['period_label'])}",
            "",
        ]
    )
    if settlement["status"] == PAYOUT_STATUS_PAID:
        lines.append(f"✅ Переведено: {format_money_spaced(settlement['display_total'])}")
        if settlement.get("paid_date") or settlement.get("paid_by"):
            paid_bits = [part for part in [settlement.get("paid_date", ""), settlement.get("paid_by", "")] if part]
            lines.append(f"🧾 {' · '.join(escape_html(part) for part in paid_bits)}")
    else:
        lines.append(f"⚪ К переводу: {format_money_spaced(settlement['display_total'])}")

    lines.extend(
        [
            "",
            (
                f"🔧 Обслуживания: {settlement['display_service_count']} × "
                f"{format_money_spaced(SERVICE_PRICE)} = {format_money_spaced(settlement['display_service_sum'])}"
            ),
        ]
    )
    if service_dates_summary:
        lines.append(f"📅 Даты обслуживаний: {escape_html(service_dates_summary)}")
    lines.extend(
        [
            f"🛒 Закупки: {format_money_spaced(settlement['display_purchase_sum'])}",
            (
                f"🚌 Проезд: {format_money_spaced(settlement['display_travel_sum'])} "
                f"({settlement['display_travel_count']} записей)"
            ),
        ]
    )
    if settlement["display_salary_task_sum"]:
        lines.append(
            f"🧰 Допзадачи: {format_money_spaced(settlement['display_salary_task_sum'])}"
            f" ({settlement['display_salary_task_count']})"
        )
    lines.append(
        f"✏️ Ручная корректировка (плюс/минус к итогу): {format_money_spaced(settlement['correction'])}"
    )
    if settlement.get("correction_note"):
        lines.append(f"📝 {escape_html(settlement['correction_note'])}")
    lines.extend(
        [
            "━━━━━━━━━━━━━━━━",
            f"💰 Итого: {format_money_spaced(settlement['display_total'])}",
        ]
    )
    if settlement.get("has_post_close_changes"):
        lines.extend(
            [
                "",
                "ℹ️ После закрытия месяца исходные записи изменились.",
                "Вверху показана зафиксированная сумма перевода.",
            ]
        )
    return "\n".join(lines)


def build_payout_overview_markup(can_edit, can_manage_payment, settlement):
    keyboard = [
        [InlineKeyboardButton("🔧 Обслуживания", callback_data="payout_screen:services")],
        [InlineKeyboardButton("🛒 Закупки", callback_data="payout_screen:purchases")],
        [InlineKeyboardButton("🚌 Проезд", callback_data="payout_screen:travels")],
        [InlineKeyboardButton("🧰 Допзадачи", callback_data="payout_screen:tasks")],
    ]
    if can_edit:
        keyboard.append([InlineKeyboardButton("✏️ Ручная корректировка", callback_data="payout_correction")])
    if settlement["status"] == PAYOUT_STATUS_PAID:
        if can_manage_payment:
            keyboard.append([InlineKeyboardButton("↩️ Снять отметку", callback_data="payout_unmark_paid")])
    elif can_manage_payment:
        keyboard.append([InlineKeyboardButton("✅ Перевёл", callback_data="payout_mark_paid")])
    keyboard.append([InlineKeyboardButton("🔄 Сменить месяц", callback_data="payout_screen:months")])
    keyboard.append([InlineKeyboardButton("⬅️ К отчётам", callback_data="back_reports_menu")])
    keyboard.append([InlineKeyboardButton("🏠 В меню", callback_data="back_main")])
    return InlineKeyboardMarkup(keyboard)


def build_payout_services_text(settlement):
    lines = [f"<b>🔧 Обслуживания · {escape_html(settlement['worker'])} — {escape_html(settlement['period_label'])}</b>", ""]
    if not settlement["services"]:
        lines.append("⚪ За выбранный месяц обслуживаний в выплату не найдено.")
        return "\n".join(lines)

    grouped = {}
    for service in settlement["services"]:
        grouped.setdefault(service.get("Точка", "") or "Без точки", []).append(service)

    for point in order_points(set(grouped.keys())):
        items = grouped.get(point, [])
        point_sum = sum(get_service_worker_amount(item, settlement["worker"]) for item in items)
        lines.append(
            f"📍 {escape_html(point)} "
            f"({len(items)} × {format_money_spaced(SERVICE_PRICE)} = {format_money_spaced(point_sum)})"
        )
        for item in items:
            row = f"• {escape_html(format_short_date_label(item.get('Дата', '')))}"
            actor = str(item.get("Кто", "")).strip()
            if actor and actor != settlement["worker"]:
                row += f" — {escape_html(actor)} → в ЗП {escape_html(settlement['worker'])}"
            lines.append(row)
        lines.append("")

    if lines[-1] == "":
        lines.pop()
    lines.extend(
        [
            "━━━━━━━━━━━━━━━━",
            (
                f"Итого: {settlement['service_count']} × {format_money_spaced(SERVICE_PRICE)} = "
                f"{format_money_spaced(settlement['service_sum'])}"
            ),
        ]
    )
    return "\n".join(lines)


def build_payout_services_markup(settlement, can_edit):
    keyboard = []
    if can_edit:
        for item in settlement["services"]:
            label = f"{format_short_date_label(item.get('Дата', ''))} · {item.get('Точка', '?')}"
            keyboard.append(
                [
                    InlineKeyboardButton(f"✏️ {label}", callback_data=f"payout_service_edit:{item['__row']}"),
                    InlineKeyboardButton("🗑", callback_data=f"payout_service_del:{item['__row']}"),
                ]
            )
        keyboard.append([InlineKeyboardButton("➕ Добавить обслуживание", callback_data="payout_service_add")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="payout_screen:overview")])
    return InlineKeyboardMarkup(keyboard)


def build_payout_purchases_text(settlement):
    lines = [f"<b>🛒 Закупки · {escape_html(settlement['worker'])} — {escape_html(settlement['period_label'])}</b>", ""]
    if not settlement["purchases"]:
        lines.append("⚪ За выбранный месяц закупок нет.")
        return "\n".join(lines)

    lines.append("Удаление строки удалит всю запись обслуживания целиком.")
    lines.append("")
    for item in settlement["purchases"]:
        point = str(item.get("Точка", "")).strip() or "Без точки"
        purchases = str(item.get("Закупки", "")).strip() or "Без списка"
        lines.append(
            f"• {escape_html(format_short_date_label(item.get('Дата', '')))} — "
            f"{escape_html(point)} — {format_money_spaced(item.get('Сумма закупок', 0))}"
        )
        lines.append(f"↳ {escape_html(purchases)}")
    lines.extend(["", "━━━━━━━━━━━━━━━━", f"Итого: {format_money_spaced(settlement['purchase_sum'])}"])
    return "\n".join(lines)


def build_payout_purchases_markup(settlement, can_edit):
    keyboard = []
    if can_edit:
        for item in settlement["purchases"]:
            label = (
                f"{format_short_date_label(item.get('Дата', ''))} · "
                f"{item.get('Точка', '?')} · {format_money_spaced(item.get('Сумма закупок', 0))}"
            )
            keyboard.append(
                [
                    InlineKeyboardButton(f"✏️ {label}", callback_data=f"payout_purchase_edit:{item['__row']}"),
                    InlineKeyboardButton("🗑", callback_data=f"payout_purchase_del:{item['__row']}"),
                ]
            )
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="payout_screen:overview")])
    return InlineKeyboardMarkup(keyboard)


def build_payout_travels_text(settlement):
    lines = [f"<b>🚌 Проезд · {escape_html(settlement['worker'])} — {escape_html(settlement['period_label'])}</b>", ""]
    if not settlement["travels"]:
        lines.append("⚪ За выбранный месяц записей проезда нет.")
        return "\n".join(lines)

    for item in settlement["travels"]:
        lines.append(
            f"• {escape_html(format_short_date_label(item.get('Дата', '')))} — "
            f"{format_money_spaced(item.get('Сумма', 0))}"
        )
    lines.extend(
        [
            "",
            "━━━━━━━━━━━━━━━━",
            f"Итого: {settlement['travel_count']} записей — {format_money_spaced(settlement['travel_sum'])}",
        ]
    )
    return "\n".join(lines)


def build_payout_travels_markup(settlement, can_edit):
    keyboard = []
    if can_edit:
        for item in settlement["travels"]:
            label = f"{format_short_date_label(item.get('Дата', ''))} · {format_money_spaced(item.get('Сумма', 0))}"
            keyboard.append(
                [
                    InlineKeyboardButton(f"✏️ {label}", callback_data=f"payout_travel_edit:{item['__row']}"),
                    InlineKeyboardButton("🗑", callback_data=f"payout_travel_del:{item['__row']}"),
                ]
            )
        keyboard.append([InlineKeyboardButton("➕ Добавить проезд", callback_data="payout_travel_add")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="payout_screen:overview")])
    return InlineKeyboardMarkup(keyboard)


def build_payout_salary_tasks_text(settlement):
    lines = [f"<b>🧰 Допзадачи · {escape_html(settlement['worker'])} — {escape_html(settlement['period_label'])}</b>", ""]
    if not settlement["salary_tasks"]:
        lines.append("⚪ За выбранный месяц допзадач нет.")
        return "\n".join(lines)

    for item in settlement["salary_tasks"]:
        description = str(item.get("Описание", "")).strip() or "Без описания"
        lines.append(
            f"• {escape_html(format_short_date_label(item.get('Дата', '')))} — "
            f"{escape_html(description)} ({format_money_spaced(item.get('Сумма', 0))})"
        )
    lines.extend(
        [
            "",
            "━━━━━━━━━━━━━━━━",
            f"Итого: {format_money_spaced(settlement['salary_task_sum'])}",
        ]
    )
    return "\n".join(lines)


def build_payout_salary_tasks_markup(can_edit):
    keyboard = []
    if can_edit:
        keyboard.append([InlineKeyboardButton("➕ Добавить допзадачу", callback_data="payout_task_add")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="payout_screen:overview")])
    return InlineKeyboardMarkup(keyboard)


async def show_salary_report_screen(query, context, worker):
    if worker not in get_paid_workers():
        await show_text_screen(
            query,
            context,
            f"❌ Сотрудник {worker} не настроен для расчёта выплат.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ К отчётам", callback_data="back_reports_menu")],
                [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
            ]),
        )
        return REPORT_MENU_SECTION
    user_id = getattr(getattr(query, "from_user", None), "id", None)
    if user_id not in get_payout_viewer_ids():
        await show_text_screen(
            query,
            context,
            "⛔ Этот экран доступен не всем.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ К отчётам", callback_data="back_reports_menu")],
                [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
            ]),
        )
        return REPORT_MENU_SECTION
    clear_payout_context(context)
    get_payout_context(context)["worker"] = worker
    return await show_payout_overview_screen(query, context)


async def report_period_menu(update: Update, context):
    query = update.callback_query
    await query.answer()
    kb = [
        [InlineKeyboardButton("За неделю", callback_data="rp_week")],
        [InlineKeyboardButton("Текущий месяц", callback_data="rp_month_current")],
        [InlineKeyboardButton("Прошлый месяц", callback_data="rp_month_prev")],
        [InlineKeyboardButton("⬅️ К отчётам", callback_data="back_reports_menu")],
        [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
    ]
    await query.edit_message_text("📈 Отчёт за период:", reply_markup=InlineKeyboardMarkup(kb))
    return REPORT_PERIOD

async def report_period_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    d = query.data

    if d == "back_main":
        return await start(update, context)
    if d == "back_reports_menu":
        return await show_reports_section_menu(query, context)
    if d == "back_report_period":
        return await report_period_menu(update, context)

    try:
        await show_loading_state(query, context, "Собираю отчёт за период...")
        services = await run_blocking(get_all_services)
        travels = await run_blocking(get_all_travels)
        salary_tasks = await run_blocking(get_all_salary_tasks)
        config = get_report_period_config(d)
        if not config:
            text = "❌ Не удалось определить период."
        else:
            p_svc = [
                service for service in services
                if is_date_in_report_range(service.get("Дата", ""), config["start_date"], config["end_date"])
            ]
            p_trv = [
                travel for travel in travels
                if is_date_in_report_range(travel.get("Дата", ""), config["start_date"], config["end_date"])
            ]
            p_tasks = [
                task for task in salary_tasks
                if is_date_in_report_range(task.get("Дата", ""), config["start_date"], config["end_date"])
            ]
            text = build_period_report_text(config["title"], p_svc, p_trv, p_tasks)

    except Exception:
        logger.exception("Failed to build period report")
        text = "❌ Не удалось собрать отчёт за период."

    kb = [[InlineKeyboardButton("⬅️ Назад", callback_data="back_report_period")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))
    return REPORT_PERIOD


def build_home_midmonth_reminder_text():
    return (
        "🏠 Напоминание: заполните промежуточную ревизию запасов дома.\n\n"
        "Это поможет заранее понять, чего будет не хватать для закупки."
    )


def build_month_close_revision_reminder_text():
    return (
        "📦 Напоминание: скоро конец месяца.\n\n"
        "Заполните ревизию по точкам за текущий месяц за 1–2 дня до закрытия,"
        " чтобы спокойно проверить остатки и спланировать закупку."
    )


def get_service_today_post_state(state):
    return state.setdefault("service_today_posts", {})


def get_group_reminder_message_state(state):
    return state.setdefault("group_reminder_messages", {})


async def delete_bot_message(application, chat_id, message_id):
    if not message_id:
        return
    try:
        await application.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except BadRequest as e:
        err = str(e).lower()
        if "message to delete not found" in err or "message can't be deleted" in err:
            return
        raise


async def cleanup_expired_service_today_posts(application, state, current_dt):
    posts = get_service_today_post_state(state)

    for chat_key, payload in list(posts.items()):
        date_str = payload.get("date", "")
        message_id = payload.get("message_id")
        dt = parse_date(date_str)
        if not dt:
            posts.pop(chat_key, None)
            save_reminder_state(state, application)
            continue

        post_date = dt.date()
        if post_date >= current_dt.date():
            continue

        should_delete = current_dt.hour >= SERVICE_TODAY_GROUP_DELETE_HOUR or post_date <= current_dt.date() - timedelta(days=2)
        if not should_delete:
            continue

        chat_id = int(chat_key)
        try:
            await delete_bot_message(application, chat_id, message_id)
        except Exception:
            logger.exception("Failed to delete expired service-today post in chat %s", chat_id)
        posts.pop(chat_key, None)
        save_reminder_state(state, application)


async def cleanup_expired_group_reminders(application, state, current_dt):
    reminders = get_group_reminder_message_state(state)

    for message_key, payload in list(reminders.items()):
        date_str = payload.get("date", "")
        message_id = payload.get("message_id")
        chat_id = payload.get("chat_id")
        dt = parse_date(date_str)
        if not dt or not chat_id:
            reminders.pop(message_key, None)
            save_reminder_state(state, application)
            continue

        reminder_date = dt.date()
        if reminder_date >= current_dt.date():
            continue

        should_delete = current_dt.hour >= SERVICE_TODAY_GROUP_DELETE_HOUR or reminder_date <= current_dt.date() - timedelta(days=2)
        if not should_delete:
            continue

        try:
            await delete_bot_message(application, chat_id, message_id)
        except Exception:
            logger.exception("Failed to delete reminder message %s in chat %s", message_key, chat_id)
        reminders.pop(message_key, None)
        save_reminder_state(state, application)


async def refresh_group_service_today_posts(application, force=False):
    if not ALLOWED_GROUP_CHAT_IDS:
        return

    current_dt = now_local()
    if current_dt.hour < SERVICE_TODAY_GROUP_POST_HOUR:
        return

    snapshot = await run_blocking(build_service_today_snapshot)
    text = snapshot["text"]
    text_hash = hashlib.md5(text.encode("utf-8")).hexdigest()
    date_str = format_date(current_dt)

    state = load_reminder_state(application)
    posts = get_service_today_post_state(state)

    for chat_id in sorted(ALLOWED_GROUP_CHAT_IDS):
        chat_key = str(chat_id)
        payload = posts.get(chat_key, {})
        message_id = payload.get("message_id")
        is_current = payload.get("date") == date_str and message_id

        if not is_current:
            try:
                sent = await application.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode="HTML",
                )
                posts[chat_key] = {
                    "date": date_str,
                    "message_id": sent.message_id,
                    "hash": text_hash,
                }
                # Persist immediately so a deploy/restart right after send
                # doesn't lose the current message_id and create a duplicate.
                save_reminder_state(state, application)
            except Exception:
                logger.exception("Failed to send service-today post to chat %s", chat_id)
            continue

        if not force and payload.get("hash") == text_hash:
            continue

        try:
            await application.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode="HTML",
            )
            posts[chat_key]["hash"] = text_hash
            save_reminder_state(state, application)
        except BadRequest as e:
            err = str(e)
            if "message is not modified" in err.lower():
                posts[chat_key]["hash"] = text_hash
                save_reminder_state(state, application)
                continue
            if "message to edit not found" not in err.lower():
                logger.warning("Failed to edit service-today post in chat %s: %s", chat_id, e)
            try:
                sent = await application.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode="HTML",
                )
                posts[chat_key] = {
                    "date": date_str,
                    "message_id": sent.message_id,
                    "hash": text_hash,
                }
                save_reminder_state(state, application)
            except Exception:
                logger.exception("Failed to re-send service-today post to chat %s", chat_id)
        except Exception:
            logger.exception("Failed to refresh service-today post in chat %s", chat_id)


async def maybe_send_group_reminder(application, reminder_key, text):
    state = load_reminder_state(application)
    sent = state.setdefault("sent", {})
    reminders = get_group_reminder_message_state(state)

    for chat_id in sorted(ALLOWED_GROUP_CHAT_IDS):
        chat_key = f"{chat_id}:{reminder_key}"
        if chat_key in sent:
            continue
        try:
            sent_message = await application.bot.send_message(chat_id=chat_id, text=text)
            sent[chat_key] = now_local().strftime("%d.%m.%Y %H:%M")
            reminders[chat_key] = {
                "chat_id": chat_id,
                "message_id": sent_message.message_id,
                "date": today(),
            }
            save_reminder_state(state, application)
        except Exception:
            logger.exception("Failed to send reminder %s to chat %s", reminder_key, chat_id)


async def process_group_reminders(application):
    if not ALLOWED_GROUP_CHAT_IDS:
        return

    current_date = now_local().date()
    current_dt = now_local()
    period_key = build_period_key(current_date.year, current_date.month)
    state = load_reminder_state(application)

    await cleanup_expired_service_today_posts(application, state, current_dt)
    await cleanup_expired_group_reminders(application, state, current_dt)
    await refresh_group_service_today_posts(application)

    if should_send_midmonth_home_reminder(current_date) and current_dt.hour >= HOME_REVISION_REMINDER_HOUR:
        await maybe_send_group_reminder(
            application,
            f"home_mid:{period_key}",
            build_home_midmonth_reminder_text(),
        )

    if should_send_month_close_revision_reminder(current_date) and current_dt.hour >= MONTH_CLOSE_REVISION_REMINDER_HOUR:
        await maybe_send_group_reminder(
            application,
            f"month_close:{period_key}",
            build_month_close_revision_reminder_text(),
        )


async def reminder_loop(application):
    while True:
        try:
            await process_group_reminders(application)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Reminder loop iteration failed")
        await asyncio.sleep(900)


async def on_app_startup(application):
    await run_blocking(get_user_directory)
    load_reminder_state(application)
    if ALLOWED_GROUP_CHAT_IDS:
        try:
            await process_group_reminders(application)
        except Exception:
            logger.exception("Initial reminder sync failed on startup")
        application.bot_data["reminder_loop_task"] = asyncio.create_task(reminder_loop(application))


async def on_app_shutdown(application):
    task = application.bot_data.pop("reminder_loop_task", None)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

# ============ ЗАПУСК ============
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")
    if not SPREADSHEET_ID:
        raise RuntimeError("SPREADSHEET_ID is not set")
    if PHOTO_CHAT_ID is None:
        raise RuntimeError("PHOTO_CHAT_ID is not set")
    persistence = PicklePersistence(filepath=resolve_runtime_path(PERSISTENCE_FILE))
    # TimeWeb's IPv4 route to Telegram's 149.154.166.0/24 flaps; default 5s
    # connect timeout in httpx isn't enough when both IPv4 and IPv6 moment
    # simultaneously. Larger timeouts + longer pool absorb short outages.
    request_kwargs = dict(connect_timeout=30.0, read_timeout=30.0, write_timeout=30.0, pool_timeout=30.0)
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .persistence(persistence)
        .request(HTTPXRequest(**request_kwargs))
        .get_updates_request(HTTPXRequest(**request_kwargs))
        .post_init(on_app_startup)
        .post_shutdown(on_app_shutdown)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CallbackQueryHandler(
                group_report_edit_service_entry_handler,
                pattern=r"^grp_report_edit_service_\d+$",
            ),
            CallbackQueryHandler(
                group_report_edit_revision_entry_handler,
                pattern=r"^grp_report_edit_revision_\d+$",
            ),
        ],
        allow_reentry=True,
        name="coffee_bot_conv",
        persistent=True,
        states={
            MAIN_MENU: [CallbackQueryHandler(main_menu_handler)],
            SERVICE_MENU_SECTION: [CallbackQueryHandler(service_section_handler)],
            REPORT_MENU_SECTION: [CallbackQueryHandler(report_section_handler)],
            PAYOUT_SCREEN: [CallbackQueryHandler(payout_handler)],
            RENT_MENU_SECTION: [CallbackQueryHandler(rent_menu_handler)],
            REPAIR_MENU_SECTION: [CallbackQueryHandler(repair_menu_handler)],
            INFO_MENU: [CallbackQueryHandler(info_handler)],
            SERVICE_WHO: [CallbackQueryHandler(service_who_handler)],
            SERVICE_DATE: [CallbackQueryHandler(service_date_handler)],
            SERVICE_DATE_CUSTOM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, service_date_custom_handler),
                CallbackQueryHandler(service_date_custom_back_handler),
            ],
            SERVICE_POINT: [CallbackQueryHandler(service_point_handler)],
            SERVICE_PHOTO: [
                MessageHandler(filters.PHOTO, service_photo_handler),
                CallbackQueryHandler(service_photo_back_handler),
            ],
            SERVICE_WATER: [CallbackQueryHandler(service_water_handler)],
            SERVICE_WATER_CUSTOM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, service_water_custom_handler),
                CallbackQueryHandler(service_water_custom_back_handler),
            ],
            SERVICE_PURCHASE: [CallbackQueryHandler(service_purchase_handler)],
            SERVICE_PURCHASE_SELECT: [CallbackQueryHandler(service_purch_select_handler)],
            SERVICE_PURCHASE_QTY: [CallbackQueryHandler(service_purch_qty_handler)],
            SERVICE_PURCHASE_OTHER_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, service_purch_other_handler),
                CallbackQueryHandler(service_purch_other_back_handler),
            ],
            SERVICE_PURCHASE_SUM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, service_purch_sum_handler),
                CallbackQueryHandler(service_purch_sum_back_handler),
            ],
            SERVICE_SHORTAGE: [CallbackQueryHandler(service_shortage_handler)],
            SERVICE_SHORTAGE_SELECT: [CallbackQueryHandler(service_short_select_handler)],
            SERVICE_SHORTAGE_QTY: [CallbackQueryHandler(service_short_qty_handler)],
            SERVICE_SHORTAGE_QTY_CUSTOM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, service_short_qty_custom_handler),
                CallbackQueryHandler(service_short_qty_custom_back_handler),
            ],
            SERVICE_SHORTAGE_NEXT_VISIT: [CallbackQueryHandler(service_short_next_visit_handler)],
            SERVICE_CONFIRM: [CallbackQueryHandler(service_confirm_handler)],
            TRAVEL_MENU: [CallbackQueryHandler(travel_menu_handler)],
            TRAVEL_WHO: [CallbackQueryHandler(travel_who_handler)],
            TRAVEL_HISTORY_PERIOD: [CallbackQueryHandler(travel_history_period_handler)],
            TRAVEL_DATE: [CallbackQueryHandler(travel_date_handler)],
            TRAVEL_DATE_CUSTOM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, travel_date_custom_handler),
                CallbackQueryHandler(travel_date_custom_back_handler),
            ],
            TRAVEL_ACTION: [CallbackQueryHandler(travel_action_handler)],
            TRAVEL_CUSTOM_SUM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, travel_custom_handler),
                CallbackQueryHandler(travel_custom_back_handler),
            ],
            TRAVEL_TRIPS_CUSTOM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, travel_trips_custom_handler),
                CallbackQueryHandler(travel_trips_custom_back_handler),
            ],
            RENT_PAYMENT_RECEIPT: [
                MessageHandler(filters.ALL & ~filters.COMMAND, rent_payment_receipt_handler),
                CallbackQueryHandler(rent_payment_receipt_back_handler),
            ],
            REPAIR_NEW_POINT: [CallbackQueryHandler(repair_new_point_handler)],
            REPAIR_NEW_MACHINE: [CallbackQueryHandler(repair_new_machine_handler)],
            REPAIR_NEW_MACHINE_QUICK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, repair_new_machine_quick_handler),
                CallbackQueryHandler(repair_new_machine_handler),
            ],
            REPAIR_NEW_REASON: [CallbackQueryHandler(repair_new_reason_handler)],
            REPAIR_NEW_REASON_CUSTOM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, repair_new_reason_custom_handler),
                CallbackQueryHandler(repair_new_reason_handler),
            ],
            REPAIR_NEW_DESCRIPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, repair_new_description_handler),
                CallbackQueryHandler(repair_new_description_handler),
            ],
            REPAIR_NEW_PHOTO: [
                MessageHandler(filters.ALL & ~filters.COMMAND, repair_new_photo_handler),
                CallbackQueryHandler(repair_new_photo_handler),
            ],
            REPAIR_NEW_DATE: [CallbackQueryHandler(repair_new_date_handler)],
            REPAIR_NEW_DATE_CUSTOM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, repair_new_date_custom_handler),
                CallbackQueryHandler(repair_new_date_custom_handler),
            ],
            REPAIR_STATUS_UPDATE: [CallbackQueryHandler(repair_status_update_handler)],
            REPAIR_SET_SERVICE: [CallbackQueryHandler(repair_service_handler)],
            REPAIR_SET_SERVICE_MANUAL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, repair_service_manual_handler),
                CallbackQueryHandler(repair_service_manual_handler),
            ],
            REPAIR_SET_DATE_BROKEN: [CallbackQueryHandler(repair_broken_date_handler)],
            REPAIR_SET_DATE_BROKEN_CUSTOM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, repair_broken_date_custom_handler),
                CallbackQueryHandler(repair_broken_date_custom_handler),
            ],
            REPAIR_SET_DATE_SENT: [CallbackQueryHandler(repair_date_sent_handler)],
            REPAIR_SET_DATE_SENT_CUSTOM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, repair_date_sent_custom_handler),
                CallbackQueryHandler(repair_date_sent_custom_handler),
            ],
            REPAIR_SET_DATE_PLAN: [CallbackQueryHandler(repair_date_plan_handler)],
            REPAIR_SET_DATE_PLAN_CUSTOM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, repair_date_plan_custom_handler),
                CallbackQueryHandler(repair_date_plan_custom_handler),
            ],
            REPAIR_EXPENSE_TYPE: [CallbackQueryHandler(repair_expense_type_handler)],
            REPAIR_EXPENSE_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, repair_expense_amount_handler),
                CallbackQueryHandler(repair_expense_amount_handler),
            ],
            REPAIR_EXPENSE_DESCRIPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, repair_expense_description_handler),
                CallbackQueryHandler(repair_expense_description_handler),
            ],
            REPAIR_EXPENSE_PAID: [CallbackQueryHandler(repair_expense_paid_handler)],
            REPAIR_DOC_UPLOAD: [
                MessageHandler(filters.ALL & ~filters.COMMAND, repair_doc_upload_handler),
                CallbackQueryHandler(repair_doc_upload_handler),
            ],
            REPAIR_HISTORY_POINT: [CallbackQueryHandler(repair_menu_handler)],
            REPAIR_HISTORY_MACHINE: [CallbackQueryHandler(repair_menu_handler)],
            REPORT_DAY: [CallbackQueryHandler(report_day_handler)],
            REPORT_PERIOD: [CallbackQueryHandler(report_period_handler)],
            SALARY_TASK_WORKER: [CallbackQueryHandler(salary_task_worker_handler)],
            SALARY_TASK_DATE: [CallbackQueryHandler(salary_task_date_handler)],
            SALARY_TASK_DATE_CUSTOM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, salary_task_date_custom_handler),
                CallbackQueryHandler(salary_task_date_custom_back_handler),
            ],
            SALARY_TASK_DESCRIPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, salary_task_description_handler),
                CallbackQueryHandler(salary_task_description_handler),
            ],
            SALARY_TASK_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, salary_task_amount_handler),
                CallbackQueryHandler(salary_task_amount_handler),
            ],
            SALARY_TASK_CONFIRM: [CallbackQueryHandler(salary_task_confirm_handler)],
            PAYOUT_CORRECTION_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, payout_correction_amount_handler),
                CallbackQueryHandler(payout_correction_amount_back_handler),
            ],
            PAYOUT_CORRECTION_NOTE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, payout_correction_note_handler),
                CallbackQueryHandler(payout_correction_note_back_handler),
            ],
            PAYOUT_TRAVEL_EDIT_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, payout_travel_edit_amount_handler),
                CallbackQueryHandler(payout_travel_edit_amount_back_handler),
            ],
            PAYOUT_TRAVEL_EDIT_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, payout_travel_edit_date_handler),
                CallbackQueryHandler(payout_travel_edit_date_back_handler),
            ],
            PAYOUT_TASK_EDIT_DESCRIPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, payout_task_edit_description_handler),
                CallbackQueryHandler(payout_task_edit_description_back_handler),
            ],
            PAYOUT_TASK_EDIT_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, payout_task_edit_amount_handler),
                CallbackQueryHandler(payout_task_edit_amount_back_handler),
            ],
            PAYOUT_TASK_EDIT_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, payout_task_edit_date_handler),
                CallbackQueryHandler(payout_task_edit_date_back_handler),
            ],
            DELETE_DATE: [CallbackQueryHandler(delete_date_handler)],
            DELETE_DATE_CUSTOM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, delete_date_custom_handler),
                CallbackQueryHandler(delete_date_custom_back_handler),
            ],
            DELETE_POINT: [CallbackQueryHandler(delete_point_handler)],
            DELETE_ENTRY: [CallbackQueryHandler(delete_entry_handler)],
            DELETE_CONFIRM: [CallbackQueryHandler(delete_confirm_handler)],
            REVISION_MENU: [CallbackQueryHandler(revision_menu_handler)],
            REVISION_PERIOD: [CallbackQueryHandler(revision_period_handler)],
            REVISION_LOCATION: [CallbackQueryHandler(revision_location_handler)],
            REVISION_EXISTING: [CallbackQueryHandler(revision_existing_handler)],
            REVISION_ITEM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, revision_item_text_input_handler),
                CallbackQueryHandler(revision_item_handler),
            ],
            REVISION_ITEM_CUSTOM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, revision_item_custom_handler),
                CallbackQueryHandler(revision_item_custom_back_handler),
            ],
            REVISION_CONFIRM: [CallbackQueryHandler(revision_confirm_handler)],
            REVISION_EDIT_ACTION: [CallbackQueryHandler(revision_edit_action_handler)],
            REVISION_EDIT_ITEM_SELECT: [CallbackQueryHandler(revision_edit_item_select_handler)],
            REVISION_VIEW_MODE: [CallbackQueryHandler(revision_view_mode_handler)],
            REVISION_VIEW_LOCATION: [CallbackQueryHandler(revision_view_location_handler)],
            REVISION_VIEW_ITEM: [CallbackQueryHandler(revision_view_item_handler)],
            REVISION_COMPARE_LOCATION: [CallbackQueryHandler(revision_compare_location_handler)],
            REVISION_DELETE_CONFIRM: [CallbackQueryHandler(revision_delete_confirm_handler)],
            REVISION_IMPORT_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, revision_import_text_handler),
                CallbackQueryHandler(revision_import_text_back_handler),
            ],
            REVISION_IMPORT_CONFIRM: [CallbackQueryHandler(revision_import_confirm_handler)],
            REVISION_PROCUREMENT_REPORT: [CallbackQueryHandler(revision_procurement_report_handler)],
        },
        fallbacks=[
            CommandHandler("start", start),
            CommandHandler("ids", cmd_ids),
            CommandHandler("service_duplicates", cmd_service_duplicates),
            CommandHandler("dupes", cmd_service_duplicates),
            CommandHandler("delete_service_rows", cmd_delete_service_rows),
            CommandHandler("delrows", cmd_delete_service_rows),
            CommandHandler("add_center", cmd_add_center),
            CommandHandler("add_machine", cmd_add_machine),
        ],
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("ids", cmd_ids))
    app.add_handler(CommandHandler("shortages", cmd_shortages))
    app.add_handler(CommandHandler("reports", cmd_reports))
    app.add_handler(CommandHandler("service_duplicates", cmd_service_duplicates))
    app.add_handler(CommandHandler("dupes", cmd_service_duplicates))
    app.add_handler(CommandHandler("delete_service_rows", cmd_delete_service_rows))
    app.add_handler(CommandHandler("delrows", cmd_delete_service_rows))
    app.add_handler(CallbackQueryHandler(service_duplicate_callback_handler, pattern=r"^svcdup:"))
    app.add_handler(
        CallbackQueryHandler(
            group_report_callback_handler,
            pattern=r"^grp_report_",
        )
    )
    app.add_handler(MessageHandler((filters.PHOTO | (filters.TEXT & ~filters.COMMAND)), group_report_message_handler))
    app.add_error_handler(global_error_handler)
    logger.info("🤖 Бот запущен")
    # Keep retrying bootstrap requests when Telegram is temporarily unreachable.
    app.run_polling(bootstrap_retries=-1)

if __name__ == "__main__":
    main()
