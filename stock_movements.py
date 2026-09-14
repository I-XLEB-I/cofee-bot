"""Dated deliveries/purchases. Never change a physical inventory snapshot.

The parser accepts completed, quantified reports and abstains on ambiguous
clauses. A journal row represents one Telegram message; edits replace that
row under a durable, atomic Sheets operation marker.
"""
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from revision_dialogue import RevisionConflict
from revision_sheet_journal import entered, execute_step, prepare_step, serialized_write

SHEET = "Движения товаров"
HEADERS = ["Дата", "Точка", "Кто", "Товары и операция", "Статус", "Версия",
           "Source_Key", "User_ID", "Items_JSON", "Исходное сообщение", "Change_ID"]
TRIGGER = re.compile(r"\b(дов[её]з(?:ла|ли)?|прив[её]з(?:ла|ли)?|зав[её]з(?:ла|ли)?|купил(?:а|и)?)\b", re.I)
NUMBER = r"\d+(?:[.,]\d+)?"
UNITS = {"пач": "пачек", "уп": "упаковок", "шт": "шт", "кг": "кг",
         "г": "г", "рул": "рулонов", "бут": "бутылок", "л": "л", "стик": "стиков"}


def norm(text):
    return str(text).lower().replace("ё", "е").strip()


def digest(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:24]


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def parse(text, host, message_date):
    """Return None for non-movements, otherwise items, remainder, or an error.

    A single message may mix purchases and deliveries, but only one destination
    and one event date. The purchase total is informational, not a payroll write.
    Explicit non-standard units are retained and never silently converted.
    """
    text = str(text or "").strip()
    triggers = list(TRIGGER.finditer(text))
    if not triggers:
        return None
    # Existing water reimbursements (including multi-point revision reports)
    # keep their established accounting path.
    if all(norm(t.group()).startswith("куп") and re.match(
        r"\s*\d+(?:[.,]\d+)?\s*(?:бак(?:а|ов)?|бутыл(?:ка|ки|ок))\b",
        text[t.end():], re.I) for t in triggers):
        return None
    if "?" in text or re.match(r"^(?:сколько|когда|кто|что|почему|покажи|скажи|какие)\b", norm(text)):
        return None
    error = lambda reason: {"error": reason, "items": []}
    # Questions, intentions, negation and quotations must not become writes.
    if re.search(r"[?«»\"]|\b(?:не|если|например|пример|завтра|планирую|нужно|надо|хочу|сказал|написал)\b", norm(text)):
        return error("Нужен отчёт о фактической доставке или покупке: точка, товар и количество.")
    locations = set()
    for alias, location in host.REVISION_LOCATION_ALIASES.items():
        if host.contains_normalized_alias(norm(text), norm(alias)):
            locations.add(location)
    source = "не указан"
    if re.search(r"\bиз дома\b", norm(text)):
        source = "Дома"
        locations.discard("Дома")
    if len(locations) != 1:
        return error("Укажите одну точку для этих товаров. Доставки на разные точки отправьте отдельными сообщениями.")
    point = locations.pop()
    day = datetime.strptime(message_date, "%d.%m.%Y").date()
    dates = set()
    # Dates require a year or whitespace boundaries so 2.5 kg is not a date.
    raw_dates = re.findall(r"(?<![\w.,])(\d{1,2}\.\d{2}\.\d{4})(?![\w.,])", text)
    raw_dates += re.findall(r"(?<![\w.,])(\d{1,2}\.\d{2})(?![\w.,])", text[:triggers[0].start()])
    for raw in raw_dates:
        try:
            event = datetime.strptime(raw if len(raw) > 5 else f"{raw}.{day.year}", "%d.%m.%Y").date()
        except ValueError:
            return error("Не получилось прочитать дату доставки. Укажите её как ДД.ММ.ГГГГ.")
        dates.add(event)
    if "вчера" in norm(text):
        dates.add(day - timedelta(days=1))
    if "сегодня" in norm(text):
        dates.add(day)
    if len(dates) > 1 or (dates and next(iter(dates)) > day):
        return error("Укажите одну дату уже выполненной доставки или покупки.")
    event_date = next(iter(dates), day).isoformat()
    items, spans = [], []
    for index, trigger in enumerate(triggers):
        end = triggers[index + 1].start() if index + 1 < len(triggers) else len(text)
        chunk = text[trigger.end():end]
        # A following service/revision sentence belongs to the established flow.
        stop = re.search(r"(?:[.!;]\s+|\n)(?=[^\n]*(?:обслужил|ревизия|остаток|остатки|воды\s*[-:]))", chunk, re.I)
        if stop:
            end = trigger.end() + stop.start()
            chunk = text[trigger.end():end]
        spans.append((trigger.start(), end))
        kind = "purchase" if norm(trigger.group()).startswith("куп") else "delivery"
        purchase_total = None
        cost = re.search(r"\bза\s+(" + NUMBER + r")\s*(?:₽|руб(?:лей|ля|ль)?\.?|р\.?)(?!\w)", chunk, re.I)
        if cost:
            purchase_total = str(Decimal(cost.group(1).replace(",", ".")))
            chunk = chunk[:cost.start()] + chunk[cost.end():]
        chunk = re.sub(r"\b(?:из дома|на месте|сегодня|вчера)\b", " ", chunk, flags=re.I)
        chunk = re.sub(r"(?<!\w)\d{1,2}\.\d{2}\.\d{4}(?!\w)", " ", chunk)
        # Remove point names only from the movement clause, longest first.
        for alias in sorted(host.REVISION_LOCATION_ALIASES, key=len, reverse=True):
            chunk = re.sub(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", " ", chunk, flags=re.I)
        chunk = re.sub(r"^\s*(?:на|в)\s+", "", chunk)
        parts = re.split(r"(?<!\d),(?!\d)|,(?=\s)|[;\n]|\s+и\s+", chunk.strip(" .:;\n"))
        clause_items = []
        for part in parts:
            part = part.strip(" .:;\n")
            if not part:
                continue
            amounts = list(re.finditer(NUMBER, part))
            if len(amounts) != 1 or re.search(r"[-−]\s*\d", part):
                return error(f"Уточните товар и одно положительное количество: «{part[:100]}».")
            amount = Decimal(amounts[0].group().replace(",", "."))
            if not 0 < amount <= 100000:
                return error("Количество должно быть больше нуля и не превышать 100000.")
            label = (part[:amounts[0].start()] + " " + part[amounts[0].end():]).strip()
            unit = None
            unit_pattern = r"\b(пач\w*|упаков\w*|уп\.?|шт\w*|кг|грамм\w*|г|рулон\w*|рул\.?|бут\w*|литр\w*|л|стик\w*)\b"
            unit_match = re.search(unit_pattern, norm(label))
            if unit_match:
                raw_unit = unit_match.group()
                unit = next((value for key, value in UNITS.items() if raw_unit.startswith(key)), None)
                label = re.sub(unit_pattern, " ", label, flags=re.I).strip(" .-:")
            item = host.resolve_revision_restock_item_name(label)
            if norm(label) in ("салфетки", "салфеток"):
                item = "Салфетки (вид не указан)"
                unit = unit or "упаковок"
            if not item:
                return error(f"Не понял товар «{label[:80]}». Укажите его название и количество.")
            # Exact alias matching prevents accepting 'кофе и неизвестный товар'
            # as a single quantity of coffee; unresolved residue needs clarification.
            aliases = [a for a, name in host.REVISION_RESTOCK_ITEM_ALIASES.items() if name == item]
            aliases += [item, "мусорных пакетов" if item == "Мус.пакеты" else ""]
            if item.startswith("Салфетки ("):
                aliases += ["салфетки", "салфеток"]
            if norm(label) not in {norm(a) for a in aliases if a}:
                return error(f"Уточните название товара: «{label[:80]}».")
            unit = unit or host.REVISION_UNITS.get(item, "упаковок")
            balance_quantity = None
            if item in ("Кофе", "Молоко", "Мока", "Шоколад"):
                if unit == "пачек":
                    balance_quantity = str(amount)
                elif unit == "кг":
                    balance_quantity = str(amount)  # existing 1000 g inventory convention
                elif unit == "г":
                    balance_quantity = str(amount / 1000)
            elif item == "Стаканы" and unit == "шт":
                if amount != amount.to_integral_value():
                    return error("Количество стаканов в штуках должно быть целым.")
                balance_quantity = str(amount)
            clause_items.append({"kind": kind, "item": item, "quantity": str(amount),
                                 "unit": unit, "source": source if kind == "delivery" else "покупка на месте",
                                 "balance_quantity": balance_quantity})
        if not clause_items:
            return error("Укажите товары и их количество после «довёз» или «купил».")
        if purchase_total is not None:
            if kind != "purchase":
                return error("Стоимость укажите в отдельной фразе «купил … за … ₽».")
            # Exactly one total per purchase clause, not repeated on every item.
            clause_items[0]["purchase_total_rub"] = purchase_total
        items.extend(clause_items)
    remainder = text
    for start, end in reversed(spans):
        remainder = remainder[:start] + remainder[end:]
    return {"point": point, "date": event_date, "items": items, "remainder": remainder.strip(" .;\n")}


def description(items):
    lines = []
    for item in items:
        verb = "Доставка" if item["kind"] == "delivery" else "Покупка"
        line = f"{verb}: {item['item']} {item['quantity']} {item['unit']}"
        if item["kind"] == "delivery":
            line += f"; источник: {item['source']}"
        if "purchase_total_rub" in item:
            line += f"; за покупку: {item['purchase_total_rub']} ₽"
        lines.append(line)
    return "\n".join(lines)


class MovementStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, source TEXT, version INTEGER, payload TEXT, step TEXT, state TEXT)")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def latest(self, source):
        with self.connect() as db:
            row = db.execute("SELECT * FROM operations WHERE source=? ORDER BY version DESC LIMIT 1", (source,)).fetchone()
        return dict(row) if row else None

    @serialized_write
    def recover(self, client):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM operations WHERE state='prepared' ORDER BY rowid").fetchall()
        for row in rows:
            execute_step(client, json.loads(row["step"]))
            with self.connect() as db:
                db.execute("UPDATE operations SET state='saved' WHERE id=?", (row["id"],))

    @serialized_write
    def save(self, book, payload, *, expected_version=None):
        self.recover(book.client)
        sheet = book.worksheet(SHEET)  # Provisioned once, never lazily guessed/created.
        rows = sheet.get("A1:K20000", value_render_option="UNFORMATTED_VALUE")
        if not rows or rows[0] != HEADERS or len(rows) >= 20000:
            raise RevisionConflict("Структура журнала движений требует проверки.")
        matching = [(index, row + [""] * (11 - len(row))) for index, row in enumerate(rows[1:], 1)
                    if len(row) > 6 and row[6] == payload["source_key"]]
        if len(matching) > 1:
            raise RevisionConflict("В журнале есть повтор записи; нужна сверка.")
        if not matching:
            for row in rows[1:]:
                if (len(row) >= 10 and row[4] == "Учтено" and row[0] == payload["date"]
                    and row[1] == payload["point"] and str(row[7]) == str(payload["user_id"])
                    and norm(" ".join(str(row[9]).split())) == norm(" ".join(payload["text"].split()))):
                    return {"version": int(row[5]), "duplicate": True, "duplicate_of": row[6]}
        row_index, old = matching[0] if matching else (len(rows), [""] * 11)
        version = int(old[5] or 0)
        if old[4] == "Отменено" and payload.get("status", "saved") != "cancelled":
            raise RevisionConflict("Движение отменено. Новую доставку отправьте отдельным сообщением.")
        if old[7] and str(old[7]) != str(payload["user_id"]):
            raise RevisionConflict("Исправить запись может её автор.")
        if expected_version is not None and expected_version != version:
            raise RevisionConflict("Запись уже изменилась. Используйте последнее подтверждение.")
        current = [payload["date"], payload["point"], payload["who"], description(payload["items"]),
                   "Отменено" if payload.get("status") == "cancelled" else "Учтено", version + 1, payload["source_key"], str(payload["user_id"]),
                   dumps(payload["items"]), payload["text"], ""]
        if old[:5] == current[:5] and old[6:10] == current[6:10]:
            return {"version": version, "duplicate": True}
        change_id = "movement:" + digest(payload["source_key"] + ":" + str(version + 1) + dumps(current))
        current[10] = change_id
        cells = [{"sheet_id": sheet.id, "title": SHEET, "row": row_index, "column": col,
                  "expected": entered(old[col]), "after": entered(value)} for col, value in enumerate(current)]
        cells += [{"sheet_id": sheet.id, "title": SHEET, "row": 0, "column": col,
                   "expected": entered(value), "after": entered(value), "write": False}
                  for col, value in enumerate(HEADERS)]
        step = prepare_step(book.client, book.id, cells, change_id)
        with self.connect() as db:
            db.execute("INSERT INTO operations VALUES (?,?,?,?,?,?)", (change_id, payload["source_key"], version + 1, dumps(payload), dumps(step), "prepared"))
        execute_step(book.client, step)
        with self.connect() as db:
            db.execute("UPDATE operations SET state='saved' WHERE id=?", (change_id,))
        return {"version": version + 1, "duplicate": False}
