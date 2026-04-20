import asyncio
import calendar
import hashlib
import json
import logging
import os
import re
from html import escape as escape_html
import gspread
from gspread.exceptions import APIError, WorksheetNotFound
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from google.oauth2.service_account import Credentials
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    ReplyKeyboardRemove,
)
from telegram.error import BadRequest
from telegram.helpers import escape_markdown
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, filters
)

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
PHOTO_CHAT_ID = int(os.getenv("PHOTO_CHAT_ID", "-5289856494"))
CREDENTIALS_FILE = os.getenv("CREDENTIALS_FILE", "credentials.json").strip()
GROUP_REPORT_DRAFT_TTL_SECONDS = int(os.getenv("GROUP_REPORT_DRAFT_TTL_SECONDS", "86400"))
GROUP_REPORT_FEEDBACK_AUTO_DELETE_SECONDS = int(os.getenv("GROUP_REPORT_FEEDBACK_AUTO_DELETE_SECONDS", "300"))
CARD_MESSAGES_AUTO_CLEANUP_SECONDS = int(os.getenv("CARD_MESSAGES_AUTO_CLEANUP_SECONDS", "600"))
SHEETS_BOOK_CACHE_TTL_SECONDS = int(os.getenv("SHEETS_BOOK_CACHE_TTL_SECONDS", "60"))
BOT_TIMEZONE = ZoneInfo("Europe/Moscow")
REMINDER_STATE_FILE = os.getenv("REMINDER_STATE_FILE", "reminder_state.json").strip()
SERVICE_TODAY_GROUP_POST_HOUR = int(os.getenv("SERVICE_TODAY_GROUP_POST_HOUR", "9"))
SERVICE_TODAY_GROUP_DELETE_HOUR = int(os.getenv("SERVICE_TODAY_GROUP_DELETE_HOUR", "2"))
HOME_REVISION_REMINDER_HOUR = int(os.getenv("HOME_REVISION_REMINDER_HOUR", "12"))
MONTH_CLOSE_REVISION_REMINDER_HOUR = int(os.getenv("MONTH_CLOSE_REVISION_REMINDER_HOUR", "12"))

USERS = {
    1395822345: "Матвей",
    611556433: "Владислав",
    5075547917: "Начальник",
    874403512: "Кирилл",
}
ALLOWED_USER_IDS = parse_env_id_set("ALLOWED_USER_IDS", USERS.keys())
ALLOWED_GROUP_CHAT_IDS = parse_env_id_set("ALLOWED_GROUP_CHAT_IDS")

PAID_WORKERS = ["Кирилл"]
SERVICE_PRICE = 250
DEFAULT_FARE = 48
WORKERS = ["Кирилл", "Матвей", "Владислав", "Начальник"]
POINTS = ["Беломорский", "Гагарина", "Гиппо", "Южный", "Сити", "Макси", "Бел2"]
REVISION_LOCATIONS = POINTS + ["Дома"]
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
 REVISION_PROCUREMENT_REPORT) = range(84)

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)
GROUP_REPORT_SAVE_LOCK = asyncio.Lock()


async def run_blocking(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


async def global_error_handler(update: object, context):
    logger.exception("Unhandled exception while processing update", exc_info=context.error)


def is_allowed_user(update):
    user = update.effective_user
    return bool(user and user.id in ALLOWED_USER_IDS)


def is_allowed_group_chat(update):
    chat = update.effective_chat
    return bool(
        chat
        and chat.type in {"group", "supergroup"}
        and ALLOWED_GROUP_CHAT_IDS
        and chat.id in ALLOWED_GROUP_CHAT_IDS
    )


async def deny_private_access(update):
    message = update.effective_message
    if message:
        await message.reply_text("⛔ Нет доступа.")
    return ConversationHandler.END


async def deny_callback_access(query):
    await query.answer("⛔ Нет доступа.", show_alert=True)

# ============ GOOGLE SHEETS ============
SERVICE_HEADERS = ["Дата", "Кто", "Точка", "Вода(бут)", "Нехватка", "Остатки", "Закупки", "Сумма закупок", "Сумма обслуж"]
TRAVEL_HEADERS = ["Дата", "Кто", "Сумма"]
PHOTO_HEADERS = ["Дата", "Точка", "Кто", "File_ID"]
REVISION_HEADERS = ["Период", "Локация", "Кто", "Дата заполнения"] + REVISION_ITEMS
GROUP_REPORT_LOG_HEADERS = [
    "Chat_ID", "Source_Key", "Source_Message_ID", "Media_Group_ID",
    "Кто", "Точка", "Дата", "Fingerprint", "Service_Row", "Photo_Rows",
    "Статус", "Создано",
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


def add_service_row(data):
    sheet = get_sheet().worksheet("Обслуживание")
    return append_row_and_get_index(sheet, [
        data["date"], data["who"], data["point"], data["water"],
        data.get("shortage", ""), data.get("shortage_qty", ""),
        data.get("purchases", ""), data.get("purchase_sum", 0),
        data.get("service_sum", 0)
    ])


def update_service_row(row_num, data):
    sheet = get_sheet().worksheet("Обслуживание")
    sheet.update(
        f"A{row_num}:I{row_num}",
        [[
            data["date"], data["who"], data["point"], data["water"],
            data.get("shortage", ""), data.get("shortage_qty", ""),
            data.get("purchases", ""), data.get("purchase_sum", 0),
            data.get("service_sum", 0)
        ]],
    )

def add_travel_row(date, who, amount):
    return append_row_and_get_index(get_sheet().worksheet("Проезд"), [date, who, amount])

def add_photo_row(date, point, who, file_id):
    return append_row_and_get_index(get_sheet().worksheet("Фото"), [date, point, who, file_id])


def update_photo_row(row_num, date, point, who, file_id):
    get_sheet().worksheet("Фото").update(
        f"A{row_num}:D{row_num}",
        [[date, point, who, file_id]],
    )

def get_all_services():
    return get_records("Обслуживание", SERVICE_HEADERS)

def get_all_travels():
    return get_records("Проезд", TRAVEL_HEADERS)

def get_all_photos():
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
    return get_records_with_rows("Обслуживание", SERVICE_HEADERS)


def get_all_photos_with_rows():
    return get_records_with_rows("Фото", PHOTO_HEADERS)


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
        entry.get("status", ""),
        entry.get("created_at", ""),
    ]
    return append_row_and_get_index(sheet, values)


def update_group_report_log(row_num, entry):
    sheet = get_or_create_worksheet("Импорт группы", GROUP_REPORT_LOG_HEADERS)
    sheet.update(
        f"A{row_num}:L{row_num}",
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
    get_or_create_worksheet("Ревизия", REVISION_HEADERS).append_row(build_revision_row_values(data))


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


def now_local():
    return datetime.now(BOT_TIMEZONE)


def parse_date(date_str):
    try:
        return datetime.strptime(str(date_str).strip(), "%d.%m.%Y")
    except (TypeError, ValueError):
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
    tg_user = getattr(message, "from_user", None)
    if not tg_user:
        return "Неизвестно"
    return USERS.get(tg_user.id) or tg_user.first_name or tg_user.username or str(tg_user.id)


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

    if is_revision_like_service_report(raw_text):
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
    service_sum = SERVICE_PRICE if draft.get("who") in PAID_WORKERS else 0
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
    }


def build_group_report_source_key(message):
    media_group_id = getattr(message, "media_group_id", None)
    if media_group_id:
        return f"media:{media_group_id}"
    return f"msg:{message.message_id}"


def build_group_report_fingerprint(draft):
    shortage_items = sorted(normalize_text_key(item) for item in draft.get("shortage_items", []))
    normalized_source = re.sub(r"\s+", " ", normalize_text_key(draft.get("source_text", ""))).strip()
    raw = "||".join([
        str(draft.get("chat_id", "")),
        normalize_text_key(draft.get("point", "")),
        normalize_text_key(draft.get("date", "")),
        normalize_text_key(draft.get("water", "")),
        "|".join(shortage_items),
        normalized_source,
        str(len(draft.get("photo_ids", []))),
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def find_group_report_log_entry(chat_id, source_key):
    for record in reversed(get_group_report_logs_with_rows()):
        if str(record.get("Chat_ID", "")) == str(chat_id) and str(record.get("Source_Key", "")) == str(source_key):
            return record
    return None


def find_group_report_duplicate(chat_id, source_key, fingerprint):
    matched_source = None
    matched_fingerprint = None
    for record in reversed(get_group_report_logs_with_rows()):
        if str(record.get("Chat_ID", "")) != str(chat_id):
            continue
        if not matched_source and str(record.get("Source_Key", "")) == str(source_key):
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


def build_group_report_saved_text(draft):
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

    warnings = draft.get("warnings", [])
    if warnings:
        lines.append("")
        lines.append("⚠️ Что стоит проверить позже:")
        lines.extend(f"• {warning}" for warning in warnings)

    lines.append("")
    lines.append("✏️ Если что-то не так, запись можно поправить через «Исправить записи».")
    return "\n".join(lines)


def build_group_travel_fingerprint(draft):
    amounts = [normalize_text_key(item) for item in draft.get("travel_amounts", [])]
    normalized_source = re.sub(r"\s+", " ", normalize_text_key(draft.get("source_text", ""))).strip()
    raw = "||".join([
        str(draft.get("chat_id", "")),
        normalize_text_key(draft.get("who", "")),
        normalize_text_key(draft.get("date", "")),
        "|".join(amounts),
        normalized_source,
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


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


def save_group_report_entry(draft):
    payload = build_group_report_payload(draft)
    service_row = add_service_row(payload)
    photo_rows = []
    for file_id in draft.get("photo_ids", []):
        photo_rows.append(add_photo_row(draft["date"], draft["point"], draft["who"], file_id))

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
            "status": "saved",
            "created_at": now_local().strftime("%d.%m.%Y %H:%M"),
        }
    )
    return log_row


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
            "status": "saved",
            "created_at": now_local().strftime("%d.%m.%Y %H:%M"),
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


def load_reminder_state():
    path = resolve_runtime_path(REMINDER_STATE_FILE)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_reminder_state(state):
    path = resolve_runtime_path(REMINDER_STATE_FILE)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


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


def normalize_revision_location_name(value):
    return REVISION_LOCATION_ALIASES.get(normalize_text_key(value))


def get_revision_import_item_spec(value):
    return REVISION_IMPORT_ITEM_SPECS.get(normalize_text_key(value))


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
    return USERS.get(tg_user.id) or tg_user.first_name or tg_user.username or str(tg_user.id)


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


def get_actor_label(update):
    tg_user = update.effective_user
    if not tg_user:
        return "Неизвестно"
    if tg_user.username:
        return f"@{tg_user.username}"
    return USERS.get(tg_user.id) or tg_user.first_name or str(tg_user.id)


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
    return "\n".join(lines)


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
            InlineKeyboardButton("✅ Вернули на точку", callback_data=f"repair_return_{repair_id}"),
        ],
        [
            InlineKeyboardButton("🗑 Удалить ремонт", callback_data=f"repair_delete_{repair_id}"),
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
            [InlineKeyboardButton("✏️ Ввести дату", callback_data="repair_sent_custom")],
            [InlineKeyboardButton("⏭ Пропустить", callback_data="repair_sent_skip")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="repair_sent_back_card")],
        ]
    )


def build_repair_broken_edit_markup(last_service_date=""):
    keyboard = [
        [InlineKeyboardButton(f"Сегодня ({today()})", callback_data="repair_broken_today")],
        [InlineKeyboardButton(f"Вчера ({yesterday()})", callback_data="repair_broken_yesterday")],
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


def build_repair_confirm_markup():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Создать", callback_data="repair_create_confirm"), InlineKeyboardButton("✏️ Изменить", callback_data="repair_create_edit")],
            [InlineKeyboardButton("❌ Отмена", callback_data="back_repair_menu")],
        ]
    )


def build_repair_delete_confirm_markup(repair_id):
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🗑 Да, удалить", callback_data=f"repair_delete_confirm_{repair_id}")],
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

    totals = {}
    for item in REVISION_ITEMS:
        total = 0.0
        has_values = False
        for record in period_records:
            numeric = parse_numeric_value(record.get(item, ""))
            if numeric is None:
                continue
            total += numeric
            has_values = True
        totals[item] = total if has_values else None

    total_rows = []
    for item in REVISION_ITEMS:
        value = totals[item]
        total_rows.append(
            (
                item,
                "—" if value is None else format_number(value),
                get_procurement_unit_short(get_revision_unit(item)),
            )
        )

    lines.append("")
    lines.append("<b>📊 Итого по сети</b>")
    lines.append(build_preformatted_block(total_rows))

    return "\n".join(lines)


def build_revision_item_text(period, item_name, records):
    period_records = [record for record in records if record.get("Период") == period]
    lines = [
        f"<b>🧾 {escape_html(item_name)} — {escape_html(format_period_label(period))}</b>",
        "",
    ]

    values_by_location = {record.get("Локация", ""): record.get(item_name, "") for record in period_records}
    point_total = 0.0
    point_has_values = False
    all_total = 0.0
    all_has_values = False
    home_value = values_by_location.get("Дома", "")
    home_num = parse_numeric_value(home_value)

    for location in POINTS:
        numeric = parse_numeric_value(values_by_location.get(location, ""))
        if numeric is None:
            continue
        point_total += numeric
        point_has_values = True
        all_total += numeric
        all_has_values = True

    if home_num is not None:
        all_total += home_num
        all_has_values = True

    summary_rows = [
        ("Итого по точкам", "—" if not point_has_values else format_number(point_total), "" if not point_has_values else get_procurement_unit_short(get_revision_unit(item_name))),
        ("Дома", "—" if home_num is None else format_number(home_num), "" if home_num is None else get_procurement_unit_short(get_revision_unit(item_name))),
        ("Итого с домом", "—" if not all_has_values else format_number(all_total), "" if not all_has_values else get_procurement_unit_short(get_revision_unit(item_name))),
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


def analyze_revision_thresholds(period, records):
    period_records = [record for record in records if record.get("Период") == period]
    by_location = {record.get("Локация", ""): record for record in period_records}
    home_record = by_location.get("Дома")
    home_has_data = bool(
        home_record and any(parse_numeric_value(home_record.get(item_name, "")) is not None for item_name in REVISION_ITEMS)
    )

    items = []
    point_critical = []
    point_warning = []

    for item_name, thresholds in REVISION_STOCK_THRESHOLDS.items():
        point_values = {}
        network_total = 0.0
        has_network_data = False
        for location in POINTS:
            record = by_location.get(location)
            value = parse_numeric_value(record.get(item_name, "")) if record else None
            if value is None:
                continue
            point_values[location] = value
            network_total += value
            has_network_data = True

        home_value = parse_numeric_value(home_record.get(item_name, "")) if home_record else None
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
    title = "🚨 Срочно по сети" if level == "critical" else "🟡 Скоро заказать"
    lines = [
        f"<b>{escape_html(title)}</b>",
        f"📅 {escape_html(format_period_label(period))}",
        "",
    ]

    if not items:
        lines.append("✅ По этой группе товаров сейчас пусто.")
        return "\n".join(lines)

    for item_data in items:
        rows = [(
            item_data["item"],
            format_number(round(item_data["network_total"], 2)),
            get_procurement_unit_short(item_data["unit"]),
        )]
        point_rows = [
            (
                point,
                format_number(value),
                get_procurement_unit_short(item_data["unit"]),
            )
            for point, value in sorted(item_data["point_values"].items(), key=lambda pair: pair[1])
        ]
        rows.extend(point_rows)
        home_value = "—" if item_data["home_value"] is None else format_number(item_data["home_value"])
        home_unit = "" if item_data["home_value"] is None else get_procurement_unit_short(item_data["unit"])
        rows.append(("Дома", home_value, home_unit))
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

    if analysis["home_has_data"]:
        focus_items = analysis["network_critical"] + analysis["network_warning"]
        home_rows = []
        seen = set()
        for item_data in focus_items:
            if item_data["item"] in seen:
                continue
            seen.add(item_data["item"])
            home_rows.append(
                {
                    "item": item_data["item"],
                    "unit": item_data["unit"],
                    "home_value": item_data["home_value"],
                }
            )

        if home_rows:
            parts.append("")
            parts.append("<b>🏠 Дома</b>")
            parts.append(
                build_preformatted_block(
                    [
                        (
                            item_data["item"],
                            "—" if item_data["home_value"] is None else format_number(round(item_data["home_value"], 2)),
                            "" if item_data["home_value"] is None else get_procurement_unit_short(item_data["unit"]),
                        )
                        for item_data in home_rows
                    ]
                )
            )
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
    flags = []
    if purchases:
        flags.append("закупки")
    if shortage:
        flags.append("нехватка")
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


def remember_cleanup_message(context, message):
    if not message:
        return
    tracked = context.user_data.setdefault("_cleanup_message_ids", [])
    tracked.append(message.message_id)


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
        return

    cleanup_tasks = application.bot_data.setdefault("_single_cleanup_tasks", {})
    task_key = f"{chat_id}:{message_id}"
    previous_task = cleanup_tasks.get(task_key)
    if previous_task and not previous_task.done():
        previous_task.cancel()

    async def _cleanup():
        try:
            await asyncio.sleep(delay_seconds)
            await delete_messages_by_ids(application.bot, chat_id, [message_id])
        except Exception:
            logger.exception("Failed to auto-clean single message %s in chat %s", message_id, chat_id)
        finally:
            current = cleanup_tasks.get(task_key)
            if current is asyncio.current_task():
                cleanup_tasks.pop(task_key, None)

    cleanup_tasks[task_key] = asyncio.create_task(_cleanup())


def schedule_card_messages_cleanup(context, chat_id, user_id):
    if CARD_MESSAGES_AUTO_CLEANUP_SECONDS <= 0:
        return

    tracked = list(context.user_data.get("_cleanup_message_ids", []))
    if not tracked:
        return

    task_key = f"{chat_id}:{user_id}"
    cleanup_tasks = context.application.bot_data.setdefault("_cleanup_tasks", {})
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
    total = len(SERVICE_FLOW_STEPS)
    index = max(1, min(current_step_index, total))
    step_name = SERVICE_FLOW_STEPS[index - 1]
    bar = "█" * index + "░" * (total - index)
    return f"📍 Шаг {index}/{total}: {step_name}\n{bar}"


async def show_loading_state(query, context, text):
    await show_text_screen(query, context, f"⏳ {text}")


async def show_sheets_busy_notice(target):
    text = "⏳ Google Sheets перегружен, попробуй через минуту."
    try:
        if hasattr(target, "answer"):
            await target.answer(text, show_alert=True)
            return
        if hasattr(target, "reply_text"):
            await target.reply_text(text)
            return
    except Exception:
        logger.exception("Failed to show Google Sheets busy notice")


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


async def show_service_date_menu(query, context):
    svc = context.user_data.get("svc", {})
    who = svc.get("who", "")
    selected_date = svc.get("date")
    date_line = f"📅 Выбрано: {selected_date}\n\n" if selected_date else ""
    kb = [
        [InlineKeyboardButton(f"Сегодня ({today()})", callback_data="svc_date_today")],
        [InlineKeyboardButton(f"Вчера ({yesterday()})", callback_data="svc_date_yesterday")],
        [InlineKeyboardButton("✏️ Другая дата", callback_data="svc_date_custom")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_service_who")],
    ]
    await show_text_screen(
        query,
        context,
        f"{build_service_progress_text(2)}\n\n🔧 {who}\n\n{date_line}За какую дату внести обслуживание?",
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


async def show_travel_date_menu(query, context):
    who = context.user_data.get("travel_who", "?")
    selected_date = context.user_data.get("travel_date")
    selected_line = f"📅 Выбрано: {selected_date}\n\n" if selected_date else ""
    keyboard = [
        [InlineKeyboardButton(f"Сегодня ({today()})", callback_data="tr_date_today")],
        [InlineKeyboardButton(f"Вчера ({yesterday()})", callback_data="tr_date_yesterday")],
        [InlineKeyboardButton("✏️ Другая дата", callback_data="tr_date_custom")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_travel_who")],
        [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
    ]
    await show_text_screen(
        query,
        context,
        f"💰 Проезд — {who}\n\n{selected_line}За какую дату добавить поездки?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return TRAVEL_DATE


async def show_travel_action_menu(query, context):
    who = context.user_data.get("travel_who", "?")
    selected_date = get_travel_selected_date(context)
    kb = [
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
        [InlineKeyboardButton("📋 Поездки за выбранную дату", callback_data="tr_summary")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_travel_date")],
        [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
    ]
    await show_text_screen(
        query,
        context,
        f"💰 Проезд — {who}\n\n📅 {selected_date}\nВыберите действие:",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return TRAVEL_ACTION


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
        [InlineKeyboardButton("✏️ Другая дата", callback_data="del_date_custom")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_service_menu")],
        [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
    ])
    await show_text_screen(
        query,
        context,
        f"✏️ Исправление записей\n\n{selected_line}За какую дату открыть записи?",
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
            f"✏️ Исправление записей\n\nЗа {selected_date} записей обслуживания нет.",
            reply_markup=back_markup("back_delete_date"),
        )
        return DELETE_POINT

    points = order_points({entry.get("Точка", "") for entry in entries if entry.get("Точка")})
    points_text = ", ".join(points)
    kb = [
        [InlineKeyboardButton("✏️ Изменить запись", callback_data="fix_action_edit")],
        [InlineKeyboardButton("🗑 Удалить одну запись", callback_data="fix_action_delete_one")],
        [InlineKeyboardButton(f"🗑 Удалить все записи за день ({len(entries)})", callback_data="fix_action_delete_day")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_delete_date")],
    ]
    await show_text_screen(
        query,
        context,
        f"✏️ Исправление записей\n\n📅 {selected_date}\n"
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
            f"✏️ Исправление записей\n\nЗа {selected_date} записей обслуживания нет.",
            reply_markup=back_markup("back_delete_date"),
        )
        return DELETE_POINT

    kb = [[InlineKeyboardButton(point, callback_data=f"del_point_{point}")] for point in points]
    kb.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_fix_actions")])
    await show_text_screen(
        query,
        context,
        f"✏️ Исправление записей\n\n📅 {selected_date}\nВыберите точку для {action_text}:",
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
            f"✏️ Исправление записей\n\n📅 {selected_date}\n📍 {point}\n\nЗаписей не найдено.",
            reply_markup=back_markup("back_delete_point"),
        )
        return DELETE_ENTRY

    kb = [[InlineKeyboardButton(build_service_entry_label(entry), callback_data=f"del_entry_{entry['__row']}")] for entry in entries]
    kb.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_delete_point")])
    await show_text_screen(
        query,
        context,
        f"✏️ Исправление записей\n\n📅 {selected_date}\n📍 {point}\nВыберите запись для {action_text}:",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return DELETE_ENTRY


async def show_fix_entry_action_menu(query, context):
    entry = context.user_data.get("delete", {}).get("entry")
    text = "✏️ Что сделать с записью?\n\n" + build_service_entry_text(entry)
    kb = [
        [InlineKeyboardButton("✏️ Изменить запись", callback_data="fix_entry_edit")],
        [InlineKeyboardButton("🗑 Удалить запись", callback_data="fix_entry_delete")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_delete_entry")],
        [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
    ]
    await show_text_screen(query, context, text, reply_markup=InlineKeyboardMarkup(kb))
    return DELETE_CONFIRM


async def show_delete_confirm_menu(query, context):
    entry = context.user_data.get("delete", {}).get("entry")
    text = "🗑 Удалить эту запись?\n\n" + build_service_entry_text(entry)
    kb = [
        [InlineKeyboardButton("🗑 Удалить", callback_data="del_confirm_yes")],
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
        [InlineKeyboardButton("🗑 Удалить всё за день", callback_data="del_day_confirm_yes")],
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
    }


async def begin_service_edit_from_entry(query, context):
    entry = context.user_data.get("delete", {}).get("entry")
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
    context.user_data["svc"] = {
        "edit_mode": True,
        "service_row": entry["__row"],
        "photo_row": photo_entry["__row"] if photo_entry else None,
        "photo": photo_entry.get("File_ID") if photo_entry else None,
        "who": entry.get("Кто", ""),
        "date": entry.get("Дата", ""),
        "point": entry.get("Точка", ""),
    }
    return await show_service_photo_prompt(query, context)


# ============ РЕВИЗИЯ ============
def get_revision_context(context):
    return context.user_data.setdefault("revision", {})


def build_revision_period_markup(show_all=False, action=None):
    periods = recent_completed_period_keys(8 if show_all else 2)
    keyboard = []
    if action == "fill":
        keyboard.append([InlineKeyboardButton("🏠 Заполнить промежуточную ревизию", callback_data="rev_home_check")])
    elif action == "view":
        keyboard.append([InlineKeyboardButton("🏠 Промежуточная ревизия запасов", callback_data="rev_home_view")])
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


def build_revision_location_markup(back_callback):
    keyboard = []
    row = []
    for i, location in enumerate(REVISION_LOCATIONS):
        row.append(InlineKeyboardButton(location, callback_data=f"revloc_{location}"))
        if len(row) == 2 or i == len(REVISION_LOCATIONS) - 1:
            keyboard.append(row)
            row = []
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
    keyboard.append([InlineKeyboardButton("Другое", callback_data="revi_custom")])
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
        f"📦 {item_name} — сколько осталось ({unit})?"
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
    for item in items_to_show:
        lines.append(f"• {item}: {format_revision_value(item, revision['values'].get(item, ''))}")
    return "\n".join(lines)


async def show_revision_menu(query, context):
    keyboard = [
        [InlineKeyboardButton("📝 Заполнить ревизию", callback_data="rev_fill")],
        [InlineKeyboardButton("📥 Импорт из текста", callback_data="rev_import")],
        [InlineKeyboardButton("✏️ Изменить ревизию", callback_data="rev_edit")],
        [InlineKeyboardButton("📋 Посмотреть ревизию", callback_data="rev_view")],
        [InlineKeyboardButton("🛒 Что нужно закупить", callback_data="rev_procurement")],
        [InlineKeyboardButton("📊 Сравнить с прошлым месяцем", callback_data="rev_compare")],
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
        reply_markup=build_revision_location_markup("back_revision_period"),
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
        [InlineKeyboardButton("🗑 Удалить ревизию", callback_data="rev_edit_delete")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_revision_location")],
    ]
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
        [InlineKeyboardButton("📍 По точке", callback_data="rev_view_location")],
        [InlineKeyboardButton("🧾 По товару", callback_data="rev_view_item")],
        [InlineKeyboardButton("🛒 Что нужно закупить", callback_data="rev_view_to_procurement")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_revision_period")],
    ]
    await show_text_screen(
        query,
        context,
        f"📋 {format_period_label(revision['period'])}\n\nЧто показать?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return REVISION_VIEW_MODE


async def show_revision_view_location_menu(query, context):
    await show_text_screen(
        query,
        context,
        "📍 Выберите точку:",
        reply_markup=build_revision_location_markup("back_revision_view_mode"),
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
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_revision_view_mode")])
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
    return REVISION_ITEM


async def ask_revision_item_message(message, context):
    revision = get_revision_context(context)
    if revision["idx"] >= len(revision["order"]):
        return await show_revision_confirm_message(message, context)
    item_name = revision["order"][revision["idx"]]
    await message.reply_text(
        build_revision_item_prompt_text(revision),
        reply_markup=build_revision_item_markup(item_name),
    )
    return REVISION_ITEM


async def show_revision_confirm(query, context):
    keyboard = [
        [InlineKeyboardButton("✅ Сохранить", callback_data="rev_confirm_save")],
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
    keyboard = [
        [InlineKeyboardButton("✅ Сохранить", callback_data="rev_confirm_save")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_revision_confirm")],
        [InlineKeyboardButton("❌ Отмена", callback_data="rev_confirm_cancel")],
    ]
    await message.reply_text(
        build_revision_confirm_text(get_revision_context(context)),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return REVISION_CONFIRM


async def show_revision_delete_confirm_menu(query, context):
    revision = get_revision_context(context)
    record = revision["existing_record"]
    keyboard = [
        [InlineKeyboardButton("🗑 Удалить ревизию", callback_data="rev_delete_yes")],
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


async def revision_view_mode_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    revision = get_revision_context(context)

    if query.data == "back_revision_period":
        return await show_revision_period_menu(query, context)
    if query.data == "back_revision_view_mode":
        return await show_revision_view_mode_menu(query, context)
    if query.data == "rev_view_summary":
        await show_loading_state(query, context, "Загружаю сводную ревизию...")
        records = await run_blocking(get_all_revisions)
        text = build_revision_summary_text(revision["period"], records)
        await show_text_screen(
            query,
            context,
            text,
            reply_markup=back_markup("back_revision_view_mode", "⬅️ Назад"),
            parse_mode="HTML",
        )
        return REVISION_VIEW_MODE
    if query.data == "rev_view_to_procurement":
        revision["action"] = "procurement"
        return await show_revision_procurement_screen(query, context, view="summary")
    if query.data == "rev_view_location":
        return await show_revision_view_location_menu(query, context)
    if query.data == "rev_view_item":
        return await show_revision_view_item_menu(query, context)
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
        [InlineKeyboardButton("🔧 Обслуживание", callback_data="service")],
        [InlineKeyboardButton("📦 Ревизия", callback_data="revision")],
        [InlineKeyboardButton("🛒 Закупка и остатки", callback_data="procurement")],
        [InlineKeyboardButton("🛠 Ремонт", callback_data="repair")],
        [InlineKeyboardButton("🏠 Аренда", callback_data="rent")],
        [InlineKeyboardButton("📊 Отчёты", callback_data="reports")],
    ]
    text = "☕ *Кофе\\-бот*\n\nВыберите действие:"
    if update.callback_query:
        await show_text_screen(
            update.callback_query,
            context,
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="MarkdownV2",
        )
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="MarkdownV2")
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
    await show_text_screen(
        query,
        context,
        build_machine_history_text(data["machine"], data),
        reply_markup=build_repair_history_back_markup(repair_ctx.get("history_point", "")),
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
    await show_text_screen(
        query,
        context,
        build_repair_centers_text(centers),
        reply_markup=back_markup("repair_refs"),
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
    await show_text_screen(
        query,
        context,
        build_repair_machines_text(machines),
        reply_markup=back_markup("repair_refs"),
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
    text = "🆕 Новый ремонт\n\nВыберите точку:"
    if notice:
        text = f"{notice}\n\n{text}"
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
            f"🆕 Новый ремонт — {point}\n\n"
            "На этой точке пока нет аппарата в реестре.\n"
            "Если знаешь, можно ввести модель вручную.\n"
            "Например: <b>Saeco Aulika</b>"
        )
        if notice:
            text = f"{notice}\n\n{text}"
        await show_text_screen(query, context, text, reply_markup=build_repair_no_machine_markup(), parse_mode="HTML")
        return REPAIR_NEW_MACHINE

    if len(machines) == 1:
        machine = machines[0]
        repair_ctx["machine_candidate"] = machine
        text = (
            f"🆕 Новый ремонт — {point}\n\n"
            f"На точке сейчас стоит:\n<b>{escape_html(get_machine_display_name(machine))}</b>\n\n"
            "Ремонтируем этот аппарат?"
        )
        if notice:
            text = f"{notice}\n\n{text}"
        await show_text_screen(query, context, text, reply_markup=build_repair_single_machine_markup(), parse_mode="HTML")
        return REPAIR_NEW_MACHINE

    text = f"🆕 Новый ремонт — {point}\n\nВыберите аппарат:"
    if notice:
        text = f"{notice}\n\n{text}"
    await show_text_screen(query, context, text, reply_markup=build_repair_machine_markup(point, machines))
    return REPAIR_NEW_MACHINE


async def show_repair_reason_step(query, context, notice=None):
    repair_ctx = get_repair_context(context)
    machine = repair_ctx.get("machine_record") or repair_ctx.get("machine_candidate")
    text = "🧩 Причина поломки\n\nВыберите причину:"
    if machine:
        text = f"☕ Аппарат: {escape_html(get_machine_display_name(machine))}\n\n{text}"
    if notice:
        text = f"{notice}\n\n{text}"
    await show_text_screen(query, context, text, reply_markup=build_repair_reason_markup(), parse_mode="HTML")
    return REPAIR_NEW_REASON


async def show_repair_description_step(query, context, notice=None):
    text = "📝 Коротко опиши, что случилось.\n\nМожно пропустить и добавить детали позже."
    if notice:
        text = f"{notice}\n\n{text}"
    await show_text_screen(query, context, text, reply_markup=build_repair_description_markup(), parse_mode="HTML")
    return REPAIR_NEW_DESCRIPTION


async def show_repair_photo_step(query, context, notice=None):
    text = "📎 Пришли фото поломки, если оно есть.\n\nМожно пропустить и добавить позже из карточки."
    if notice:
        text = f"{notice}\n\n{text}"
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
    text = "📅 Когда случилась поломка?"
    if notice:
        text = f"{notice}\n\n{text}"
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
            await run_blocking(update_repair_status_value, repair_id, REPAIR_STATUS_INSTALLED)
        except APIError as error:
            if is_google_sheets_busy_error(error):
                await show_sheets_busy_notice(query)
                return REPAIR_MENU_SECTION
            raise
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
        "📎 Пришли фото поломки, если оно есть.\n\nМожно пропустить и добавить позже из карточки.",
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
        "📅 Когда случилась поломка?",
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
        await run_blocking(update_repair_status_value, repair_id, new_status)
    except APIError as error:
        if is_google_sheets_busy_error(error):
            await show_sheets_busy_notice(query)
            return REPAIR_STATUS_UPDATE
        raise
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
        [InlineKeyboardButton("🔔 К обслуживанию сегодня", callback_data="service_today")],
        [InlineKeyboardButton("📝 Начать обслуживание", callback_data="service_start")],
        [InlineKeyboardButton("📋 Информация по точкам", callback_data="info")],
        [InlineKeyboardButton("⚠️ Проблемные точки", callback_data="service_problem_points")],
        [InlineKeyboardButton("✏️ Исправить записи", callback_data="delete_service")],
        [InlineKeyboardButton("💰 Проезд", callback_data="travel")],
        [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
    ]
    await show_text_screen(query, context, "🔧 Обслуживание\n\nВыберите действие:", reply_markup=InlineKeyboardMarkup(keyboard))
    return SERVICE_MENU_SECTION


async def show_reports_section_menu(query, context):
    keyboard = [
        [InlineKeyboardButton("📅 Отчёт за день", callback_data="report_day")],
        [InlineKeyboardButton("📆 Отчёт за период", callback_data="report_period")],
        [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
    ]
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

async def cmd_ids(update: Update, context):
    if not is_allowed_user(update):
        return await deny_private_access(update)

    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    if not (chat and user and message):
        return

    lines = [
        "🪪 Идентификаторы",
        "",
        f"👤 user_id: `{user.id}`",
        f"💬 chat_id: `{chat.id}`",
        f"🧩 тип чата: `{chat.type}`",
    ]

    chat_title = getattr(chat, "title", None)
    if chat_title:
        lines.append(f"🏷 чат: {escape_markdown(chat_title, version=2)}")

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
                "Для `.env`:",
                f"`ALLOWED_USER_IDS={user.id}`",
                f"`ALLOWED_GROUP_CHAT_IDS={chat.id}`",
            ]
        )

    await message.reply_text("\n".join(lines), parse_mode="MarkdownV2")

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
    elif d == "delete_service":
        context.user_data["delete"] = {}
        return await show_delete_date_menu(query, context)
    elif d == "travel":
        return await travel_who(update, context)
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
    if d == "info":
        return await info_menu(update, context)
    if d == "service_problem_points":
        return await show_service_problem_points(query, context)
    if d == "delete_service":
        context.user_data["delete"] = {}
        return await show_delete_date_menu(query, context)
    if d == "travel":
        return await travel_who(update, context)
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
    return REPORT_MENU_SECTION

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
        [InlineKeyboardButton("📝 Начать обслуживание", callback_data="service_start")],
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
            "edit_mode": True,
            "service_row": current_svc.get("service_row"),
            "photo_row": current_svc.get("photo_row"),
            "photo": current_svc.get("photo"),
        }
    else:
        context.user_data["svc"] = {}
    kb = [[InlineKeyboardButton(w, callback_data=f"sw_{w}")] for w in WORKERS]
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
    context.user_data["svc"]["who"] = who
    context.user_data["svc"]["date"] = today()
    return await show_service_date_menu(query, context)


async def service_date_handler(update: Update, context):
    query = update.callback_query
    await query.answer()

    if query.data == "back_service_who":
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
            f"✏️ Исправление записей\n\nЗа {selected_date} записей обслуживания нет.",
            reply_markup=back_markup("back_delete_date"),
        )
        return DELETE_POINT

    points = order_points({entry.get("Точка", "") for entry in entries if entry.get("Точка")})
    kb = [
        [InlineKeyboardButton("✏️ Изменить запись", callback_data="fix_action_edit")],
        [InlineKeyboardButton("🗑 Удалить одну запись", callback_data="fix_action_delete_one")],
        [InlineKeyboardButton(f"🗑 Удалить все записи за день ({len(entries)})", callback_data="fix_action_delete_day")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_delete_date")],
    ]
    await update.message.reply_text(
        f"✏️ Исправление записей\n\n📅 {selected_date}\n"
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
    if context.user_data["delete"].get("mode") == "edit":
        return await show_fix_entry_action_menu(query, context)
    return await show_delete_confirm_menu(query, context)


async def delete_confirm_handler(update: Update, context):
    query = update.callback_query
    await query.answer()

    if query.data == "back_main":
        return await start(update, context)
    if query.data == "delete_service":
        context.user_data["delete"] = {}
        return await show_delete_date_menu(query, context)
    if query.data == "back_fix_actions":
        return await show_fix_action_menu(query, context)
    if query.data == "back_delete_entry":
        return await show_delete_entry_menu(query, context)
    if query.data == "fix_entry_edit":
        return await begin_service_edit_from_entry(query, context)
    if query.data == "fix_entry_delete":
        return await show_delete_confirm_menu(query, context)

    if query.data == "del_day_confirm_yes":
        selected_date = context.user_data.get("delete", {}).get("date")
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
            [InlineKeyboardButton("✏️ Исправить ещё", callback_data="delete_service")],
            [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
        ]
        await show_text_screen(query, context, text, reply_markup=InlineKeyboardMarkup(kb))
        return DELETE_CONFIRM

    entry = context.user_data.get("delete", {}).get("entry")
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
        [InlineKeyboardButton("🗑 Удалить ещё", callback_data="delete_service")],
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
        pl = context.user_data["svc"].get("plist", [])
        if not pl:
            context.user_data["svc"]["purchases"] = ""
            context.user_data["svc"]["purchase_sum"] = 0
            return await ask_shortage(query, context)
        next_idx = get_next_unfilled_purchase_index(pl)
        if next_idx is None:
            finalize_purchase_summary(context.user_data["svc"])
            return await ask_shortage(query, context)
        context.user_data["svc"]["pidx"] = next_idx
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


async def service_shortage_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "back_service_shortage_prev":
        return await back_to_purchase_stage(query, context)
    if query.data == "ssh_no":
        context.user_data["svc"]["shortage"] = ""
        context.user_data["svc"]["shortage_qty"] = ""
        context.user_data["svc"]["slist"] = []
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
        sl = context.user_data["svc"].get("slist", [])
        if not sl:
            context.user_data["svc"]["shortage"] = ""
            context.user_data["svc"]["shortage_qty"] = ""
            return await show_confirm(query, context)
        context.user_data["svc"]["sidx"] = 0
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
    text = f"{build_service_progress_text(8)}\n\n{build_confirm_text(svc)}"
    kb = [[InlineKeyboardButton("✅ Подтвердить", callback_data="svc_ok"),
           InlineKeyboardButton("⬅️ Назад", callback_data="back_service_confirm")],
          [InlineKeyboardButton("❌ Отмена", callback_data="svc_cancel")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))
    return SERVICE_CONFIRM


async def show_confirm_msg(message, context):
    svc = context.user_data["svc"]
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

    if who in PAID_WORKERS:
        lines.append("")
        lines.append(f"💰 Обслуживание: {format_money(SERVICE_PRICE)}")
        if purchase_sum:
            lines.append(f"💰 Закупки: {format_money(purchase_sum)}")
        lines.append(f"💰 Итого: {format_money(SERVICE_PRICE + purchase_sum)}")
    elif purchase_sum:
        lines.append("")
        lines.append(f"💰 Закупки: {format_money(purchase_sum)}")

    return "\n".join(lines)

async def service_confirm_handler(update: Update, context):
    query = update.callback_query
    await query.answer()

    if query.data == "back_service_confirm":
        return await back_to_shortage_stage(query, context)

    if query.data == "svc_cancel":
        await query.edit_message_text("❌ Отменено")
        return await start(update, context)

    svc = context.user_data["svc"]
    service_sum = SERVICE_PRICE if svc["who"] in PAID_WORKERS else 0
    payload = build_service_update_data(svc, service_sum)

    try:
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

        success_text = "✅ Запись обновлена!" if svc.get("edit_mode") else "✅ Записано!"
        try:
            await refresh_group_service_today_posts(context.application, force=True)
        except Exception:
            logger.exception("Failed to refresh group service-today post after service save")
        await query.edit_message_text(f"{success_text}\n\n📍 {svc['point']} — {svc['who']}")

    except Exception:
        logger.exception("Failed to save service record")
        await query.edit_message_text("❌ Ошибка записи. Попробуйте позже.")

    return await start(update, context)


# ============ ИМПОРТ ИЗ РАБОЧЕЙ ГРУППЫ ============
async def send_group_report_feedback_message(application, chat_id, source_message_id, text, reply_markup=None):
    sent = await application.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_to_message_id=source_message_id,
        reply_markup=reply_markup,
    )
    schedule_single_message_cleanup(
        application,
        sent.chat_id,
        sent.message_id,
        GROUP_REPORT_FEEDBACK_AUTO_DELETE_SECONDS,
    )
    return sent


async def send_group_report_saved_message(application, draft, log_row):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Не учитывать", callback_data=f"grp_report_delete_{log_row}")]
    ])
    await send_group_report_feedback_message(
        application,
        draft["chat_id"],
        draft["source_message_id"],
        build_group_report_saved_text(draft),
        reply_markup=kb,
    )


async def send_group_travel_saved_message(application, draft, log_row):
    await send_group_report_feedback_message(
        application,
        draft["chat_id"],
        draft["source_message_id"],
        build_group_travel_saved_text(draft),
    )


async def process_group_report_message(message, application, photo_ids=None):
    if not message or not getattr(message, "chat", None):
        return
    if message.chat.type not in {"group", "supergroup"}:
        return
    if getattr(message.from_user, "is_bot", False):
        return

    body_text = message.caption or message.text or ""
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

                log_row = await run_blocking(save_group_travel_entry, draft)
            await send_group_travel_saved_message(application, draft, log_row)
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
        "chat_id": message.chat_id,
        "source_message_id": message.message_id,
        "media_group_id": getattr(message, "media_group_id", "") or "",
        "source_key": build_group_report_source_key(message),
        "photo_ids": list(photo_ids or ([message.photo[-1].file_id] if getattr(message, "photo", None) else [])),
    }
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

            log_row = await run_blocking(save_group_report_entry, draft)
        try:
            await refresh_group_service_today_posts(application, force=True)
        except Exception:
            logger.exception("Failed to refresh group service-today post after group report save")
        await send_group_report_saved_message(application, draft, log_row)
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
    if not is_allowed_user(update) or not is_allowed_group_chat(update):
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
    if not is_allowed_user(update) or not is_allowed_group_chat(update):
        await deny_callback_access(query)
        return
    await query.answer()

    cleanup_expired_group_report_drafts(context.application.bot_data)
    match = re.match(r"^grp_report_(save|ignore|delete)_(\d+)$", query.data)
    if not match:
        return

    action, raw_id = match.groups()

    if action == "delete":
        try:
            status, record = await run_blocking(delete_group_report_entry_by_log_row, int(raw_id))
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

        if status == "deleted" and record:
            try:
                await refresh_group_service_today_posts(context.application, force=True)
            except Exception:
                logger.exception("Failed to refresh group service-today post after group report delete")
            await query.edit_message_text(
                "🗑 Сообщение убрано из базы.\n\n"
                f"📍 {record.get('Точка', '—')}\n"
                f"📅 {record.get('Дата', '—')}\n"
                f"👤 {record.get('Кто', '—')}"
            )
        elif status == "saved":
            await query.edit_message_text("⚪ Сообщение уже недоступно для удаления.")
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

    payload = build_group_report_payload(draft)
    try:
        await run_blocking(add_service_row, payload)
        for file_id in draft.get("photo_ids", []):
            await run_blocking(add_photo_row, draft["date"], draft["point"], draft["who"], file_id)
        drafts.pop(draft_id, None)
        try:
            await refresh_group_service_today_posts(context.application, force=True)
        except Exception:
            logger.exception("Failed to refresh group service-today post after draft save")
        await query.edit_message_text(
            "✅ Сообщение сохранено в обслуживание.\n\n"
            f"📍 {draft['point']}\n"
            f"📅 {draft['date']}\n"
            f"👤 {draft['who']}"
        )
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
    kb = [[InlineKeyboardButton(w, callback_data=f"tw_{w}")] for w in WORKERS]
    kb.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_service_menu")])
    kb.append([InlineKeyboardButton("🏠 В меню", callback_data="back_main")])
    await query.edit_message_text("💰 Проезд\n\nКто едет?", reply_markup=InlineKeyboardMarkup(kb))
    return TRAVEL_WHO

async def travel_who_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "back_service_menu":
        return await show_service_section_menu(query, context)
    if query.data == "back_main":
        return await start(update, context)
    who = query.data.replace("tw_", "")
    context.user_data["travel_who"] = who
    context.user_data.pop("travel_date", None)
    return await show_travel_date_menu(query, context)


async def travel_date_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    d = query.data

    if d == "back_main":
        return await start(update, context)
    if d == "back_service_menu":
        return await show_service_section_menu(query, context)
    if d == "back_travel_who":
        return await travel_who(update, context)
    if d == "tr_date_today":
        context.user_data["travel_date"] = today()
        return await show_travel_action_menu(query, context)
    if d == "tr_date_yesterday":
        context.user_data["travel_date"] = yesterday()
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
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➡️ Продолжить", callback_data="tr_summary")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_travel_date")],
        ]
    )
    await update.message.reply_text(
        f"📅 Выбрано: {context.user_data['travel_date']}",
        reply_markup=kb,
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

    kb = [
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
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_travel_date")],
        [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
    ]
    await show_text_screen(query, context, "\n".join(text_parts), reply_markup=InlineKeyboardMarkup(kb))
    return TRAVEL_ACTION


async def travel_action_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    d = query.data
    who = context.user_data.get("travel_who", "?")
    date_str = get_travel_selected_date(context)

    if d == "back_main":
        return await start(update, context)
    if d == "back_service_menu":
        return await show_service_section_menu(query, context)
    if d == "back_travel_who":
        return await travel_who(update, context)
    if d == "back_travel_date":
        return await show_travel_date_menu(query, context)

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
    try:
        await run_blocking(add_travel_row, date_str, who, amount)
        travels = await run_blocking(get_all_travels)
        summary = build_travel_day_summary(who, date_str, travels)
        kb = InlineKeyboardMarkup(
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
                [InlineKeyboardButton("⬅️ Назад", callback_data="back_travel_date")],
                [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
            ]
        )
        await update.message.reply_text(
            f"✅ Записано: {format_money(amount)}\n\n💰 Проезд — {who}\n📅 {date_str}\n\n{summary}",
            reply_markup=kb,
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
    try:
        await run_blocking(add_travel_row, date_str, who, amount)
        travels = await run_blocking(get_all_travels)
        summary = build_travel_day_summary(who, date_str, travels)
        kb = InlineKeyboardMarkup(
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
                [InlineKeyboardButton("⬅️ Назад", callback_data="back_travel_date")],
                [InlineKeyboardButton("🏠 В меню", callback_data="back_main")],
            ]
        )
        await update.message.reply_text(
            f"✅ Записано: {trip_count} поездок — {format_money(amount)}\n\n"
            f"💰 Проезд — {who}\n📅 {date_str}\n\n{summary}",
            reply_markup=kb,
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
        td = today()

        today_svc = [s for s in services if s.get("Дата") == td]
        today_trv = [t for t in travels if t.get("Дата") == td]

        text = f"📊 Отчёт за {td}:\n\n"

        workers_today = set()
        for s in today_svc:
            workers_today.add(s.get("Кто", "?"))
        for t in today_trv:
            workers_today.add(t.get("Кто", "?"))

        if not workers_today:
            text += "Нет данных за сегодня"
        else:
            grand_total = 0
            for w in sorted(workers_today):
                w_svc = [s for s in today_svc if s.get("Кто") == w]
                w_trv = [t for t in today_trv if t.get("Кто") == w]

                text += f"👤 {w}:\n"

                svc_sum = 0
                purch_sum = 0
                if w_svc:
                    text += f"  🔧 Обслужено: {len(w_svc)} точек\n"
                    for s in w_svc:
                        ss = int(s.get("Сумма обслуж", 0))
                        ps = int(s.get("Сумма закупок", 0))
                        svc_sum += ss
                        purch_sum += ps
                        point = s.get("Точка", "?")
                        if ss > 0:
                            text += f"    ✅ {point} — {ss}₽\n"
                        else:
                            text += f"    ✅ {point}\n"

                trv_sum = sum(int(t.get("Сумма", 0)) for t in w_trv)
                trv_count = len(w_trv)

                if purch_sum:
                    text += f"  🛒 Закупки: {purch_sum}₽\n"
                if trv_count:
                    text += f"  🚌 Проезд: {trv_sum}₽ ({trv_count} поездок)\n"

                w_total = svc_sum + purch_sum + trv_sum
                if w in PAID_WORKERS:
                    text += f"  💰 Итого {w}: {w_total}₽\n"
                else:
                    if purch_sum or trv_sum:
                        text += f"  💰 Расходы {w}: {purch_sum + trv_sum}₽\n"

                grand_total += w_total
                text += "\n"

            text += f"━━━━━━━━━━━━━━━━\n💰 Общие расходы: {grand_total}₽"

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
async def report_period_menu(update: Update, context):
    query = update.callback_query
    await query.answer()
    kb = [
        [InlineKeyboardButton("За неделю", callback_data="rp_week")],
        [InlineKeyboardButton("За месяц", callback_data="rp_month")],
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

        now = now_local()
        if d == "rp_week":
            start_date = now - timedelta(days=7)
            period_name = "неделю"
        else:
            start_date = now - timedelta(days=30)
            period_name = "месяц"

        def in_period(date_str):
            try:
                dt = datetime.strptime(str(date_str), "%d.%m.%Y")
                return dt >= start_date
            except Exception:
                return False

        p_svc = [s for s in services if in_period(s.get("Дата", ""))]
        p_trv = [t for t in travels if in_period(t.get("Дата", ""))]

        text = f"📈 Отчёт за {period_name}:\n\n"

        workers_all = set()
        for s in p_svc:
            workers_all.add(s.get("Кто", "?"))
        for t in p_trv:
            workers_all.add(t.get("Кто", "?"))

        if not workers_all:
            text += "Нет данных"
        else:
            grand_total = 0
            for w in sorted(workers_all):
                w_svc = [s for s in p_svc if s.get("Кто") == w]
                w_trv = [t for t in p_trv if t.get("Кто") == w]

                svc_sum = sum(int(s.get("Сумма обслуж", 0)) for s in w_svc)
                purch_sum = sum(int(s.get("Сумма закупок", 0)) for s in w_svc)
                trv_sum = sum(int(t.get("Сумма", 0)) for t in w_trv)

                text += f"👤 {w}:\n"
                if w_svc:
                    text += f"  🔧 Обслуживаний: {len(w_svc)} ({svc_sum}₽)\n"
                if purch_sum:
                    text += f"  🛒 Закупки: {purch_sum}₽\n"
                if trv_sum:
                    text += f"  🚌 Проезд: {trv_sum}₽\n"

                w_total = svc_sum + purch_sum + trv_sum
                if w in PAID_WORKERS:
                    text += f"  💰 Итого к выплате: {w_total}₽\n"
                else:
                    text += f"  💰 Расходы: {purch_sum + trv_sum}₽\n"
                text += "\n"
                grand_total += w_total

            text += f"━━━━━━━━━━━━━━━━\n💰 Общие расходы: {grand_total}₽"

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
    changed = False

    for chat_key, payload in list(posts.items()):
        date_str = payload.get("date", "")
        message_id = payload.get("message_id")
        dt = parse_date(date_str)
        if not dt:
            posts.pop(chat_key, None)
            changed = True
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
        changed = True

    if changed:
        save_reminder_state(state)


async def cleanup_expired_group_reminders(application, state, current_dt):
    reminders = get_group_reminder_message_state(state)
    changed = False

    for message_key, payload in list(reminders.items()):
        date_str = payload.get("date", "")
        message_id = payload.get("message_id")
        chat_id = payload.get("chat_id")
        dt = parse_date(date_str)
        if not dt or not chat_id:
            reminders.pop(message_key, None)
            changed = True
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
        changed = True

    if changed:
        save_reminder_state(state)


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

    state = load_reminder_state()
    posts = get_service_today_post_state(state)
    changed = False

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
                changed = True
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
            changed = True
        except BadRequest as e:
            err = str(e)
            if "message is not modified" in err.lower():
                posts[chat_key]["hash"] = text_hash
                changed = True
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
                changed = True
            except Exception:
                logger.exception("Failed to re-send service-today post to chat %s", chat_id)
        except Exception:
            logger.exception("Failed to refresh service-today post in chat %s", chat_id)

    if changed:
        save_reminder_state(state)


async def maybe_send_group_reminder(application, reminder_key, text):
    state = load_reminder_state()
    sent = state.setdefault("sent", {})
    reminders = get_group_reminder_message_state(state)
    changed = False

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
            changed = True
        except Exception:
            logger.exception("Failed to send reminder %s to chat %s", reminder_key, chat_id)

    if changed:
        save_reminder_state(state)


async def process_group_reminders(application):
    if not ALLOWED_GROUP_CHAT_IDS:
        return

    current_date = now_local().date()
    current_dt = now_local()
    period_key = build_period_key(current_date.year, current_date.month)
    state = load_reminder_state()

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
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(on_app_startup)
        .post_shutdown(on_app_shutdown)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            MAIN_MENU: [CallbackQueryHandler(main_menu_handler)],
            SERVICE_MENU_SECTION: [CallbackQueryHandler(service_section_handler)],
            REPORT_MENU_SECTION: [CallbackQueryHandler(report_section_handler)],
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
            TRAVEL_WHO: [CallbackQueryHandler(travel_who_handler)],
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
            REVISION_ITEM: [CallbackQueryHandler(revision_item_handler)],
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
        ],
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("ids", cmd_ids))
    app.add_handler(CommandHandler("shortages", cmd_shortages))
    app.add_handler(CommandHandler("reports", cmd_reports))
    app.add_handler(CallbackQueryHandler(group_report_callback_handler, pattern=r"^grp_report_(save|ignore|delete)_\d+$"))
    app.add_handler(MessageHandler((filters.PHOTO | (filters.TEXT & ~filters.COMMAND)), group_report_message_handler))
    app.add_error_handler(global_error_handler)
    logger.info("🤖 Бот запущен")
    app.run_polling()

if __name__ == "__main__":
    main()
