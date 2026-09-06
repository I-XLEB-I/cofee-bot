"""Bounded, value-only synchronization into the owner's existing BDR layout."""

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation


class BdrRevisionError(ValueError):
    """A date, layout, unit or live-value conflict needs owner attention."""


def key(value):
    return re.sub(r"[\s._-]+", "", str(value or "").casefold().replace("ё", "е"))


POINT_ALIASES = {
    "сити": "Сити",
    "южн": "Южный",
    "южный": "Южный",
    "белом": "Беломорский",
    "беломорский": "Беломорский",
    "бел1": "Беломорский",
    "гагарина": "Гагарина",
    "гаг": "Гагарина",
    "макси": "Макси",
    "гиппо": "Гиппо",
    "гип": "Гиппо",
    "дома": "Дома",
    "гараж": "Гараж",
    "бел2": "Бел2",
}
ITEM_ALIASES = {
    "кофе": "Кофе",
    "молоко": "Молоко",
    "шоколад": "Шоколад",
    "мока": "Мока",
    "сахар": "Сахар",
    "сироп": "Сиропы",
    "сиропы": "Сиропы",
    "стаканы": "Стаканы",
    "крышкич": "Крышки чёрн",
    "крышкичерн": "Крышки чёрн",
    "крышкиб": "Крышки бел",
    "крышкибел": "Крышки бел",
    "палочки": "Палочки",
    "трубочки": "Трубочки",
    "манжеты": "Манжеты",
    "салфвлажн": "Влажные салф",
    "влажныесалф": "Влажные салф",
    "салфсухие": "Салфетки сухие",
    "салфеткисухие": "Салфетки сухие",
    "пакеты": "Мус.пакеты",
    "муспакеты": "Мус.пакеты",
}


def number(value):
    if value in ("", None):
        return None
    try:
        result = Decimal(str(value).replace(",", ".").replace(" ", ""))
    except InvalidOperation as exc:
        raise BdrRevisionError(f"БДР: нечисловое количество «{value}»") from exc
    if not result.is_finite() or result < 0:
        raise BdrRevisionError("БДР: количество должно быть конечным и неотрицательным")
    return result


def converted(item, value):
    result = number(value)
    if result is not None and item == "Салфетки сухие":
        result /= 500
    return result


def cell_number(cell):
    entered = cell.get("userEnteredValue", {})
    if "formulaValue" in entered:
        raise BdrRevisionError("БДР: целевая ячейка содержит формулу — нужна ручная сверка")
    return number(entered.get("numberValue", entered.get("stringValue")))


@dataclass
class Block:
    date: object
    row: int
    columns: dict
    items: dict

    @property
    def period(self):
        return self.date.strftime("%m.%Y")


class BdrLayout:
    def __init__(self, sheet):
        self.sheet_id = sheet["properties"]["sheetId"]
        self.title = sheet["properties"]["title"]
        self.cells = {}
        self.merges = sheet.get("merges", [])
        for grid in sheet.get("data", []):
            for i, row in enumerate(grid.get("rowData", []), grid.get("startRow", 0)):
                for j, cell in enumerate(row.get("values", []), grid.get("startColumn", 0)):
                    self.cells[i, j] = cell
        self.blocks = []
        active = None
        for row in sorted({r for r, _ in self.cells}):
            label = self.cells.get((row, 1), {}).get("formattedValue", "")
            if re.fullmatch(r"\d{1,2}\.\d{1,2}\.\d{4}", label):
                block_date = datetime.strptime(label, "%d.%m.%Y").date()
                columns = {}
                for (r, column), cell in self.cells.items():
                    location = POINT_ALIASES.get(key(cell.get("formattedValue")))
                    if r == row and location:
                        if location in columns:
                            raise BdrRevisionError(f"БДР: повторная колонка {location}")
                        columns[location] = column
                active = Block(block_date, row, columns, {})
                self.blocks.append(active)
            elif active:
                item = ITEM_ALIASES.get(key(label))
                if item:
                    if item in active.items:
                        raise BdrRevisionError(f"БДР: повторная строка {item}")
                    active.items[item] = row

    def nearest(self, report_date, max_days=10):
        try:
            report_date = datetime.strptime(report_date, "%d.%m.%Y").date()
        except ValueError as exc:
            raise BdrRevisionError("БДР: не определена дата отчёта") from exc
        distances = [(abs((b.date - report_date).days), b) for b in self.blocks]
        if not distances or min(d for d, _ in distances) > max_days:
            raise BdrRevisionError(
                f"БДР: нет блока в пределах {max_days} дней от {report_date:%d.%m.%Y}; "
                "уточните дату ревизии или добавьте нужный блок и повторите сообщение"
            )
        closest = [b for d, b in distances if d == min(d for d, _ in distances)]
        if len(closest) != 1:
            raise BdrRevisionError("БДР: две ближайшие даты равноудалены — уточните дату ревизии")
        return closest[0]

    def exact(self, block_date):
        matches = [b for b in self.blocks if b.date.strftime("%d.%m.%Y") == block_date]
        if len(matches) != 1:
            raise BdrRevisionError("БДР: исходный блок изменился — нужна ручная сверка")
        return matches[0]

    def plan(self, block, location, values, expected=None, *, clear=False):
        if location not in block.columns:
            raise BdrRevisionError(f"БДР: в блоке {block.date:%d.%m.%Y} нет точки {location}")
        column = block.columns[location]
        expected = expected or {}
        requests, before, checks = [], {}, {}
        for item, raw in values.items():
            if item == "Вода" or (raw in ("", None) and not clear):
                continue
            if item not in block.items:
                raise BdrRevisionError(f"БДР: нет строки «{item}» — запись остановлена")
            row = block.items[item]
            cell = self.cells.get((row, column), {})
            if cell.get("dataValidation") or cell.get("chipRuns"):
                raise BdrRevisionError("БДР: у целевой ячейки есть правило ввода — нужна сверка")
            if any(
                m.get("startRowIndex", 0) <= row < m.get("endRowIndex", 0)
                and m.get("startColumnIndex", 0) <= column < m.get("endColumnIndex", 0)
                for m in self.merges
            ):
                raise BdrRevisionError("БДР: целевая ячейка объединена — нужна сверка")
            current = cell_number(cell)
            desired = converted(item, raw)
            prior = converted(item, expected.get(item))
            if current is not None and current != desired and current != prior:
                raise BdrRevisionError(
                    f"БДР: {location}, {item}: уже записано {current}; "
                    "значение отличается от ревизии бота, нужна ручная сверка"
                )
            before[item] = (
                "" if current is None else str(current * (500 if item == "Салфетки сухие" else 1))
            )
            checks[row, column] = desired
            if current == desired:
                continue
            entered = {} if desired is None else {"numberValue": float(desired)}
            requests.append(
                {
                    "updateCells": {
                        "range": {
                            "sheetId": self.sheet_id,
                            "startRowIndex": row,
                            "endRowIndex": row + 1,
                            "startColumnIndex": column,
                            "endColumnIndex": column + 1,
                        },
                        "rows": [{"values": [{"userEnteredValue": entered}]}],
                        "fields": "userEnteredValue",
                    }
                }
            )
        return requests, before, checks


def read_layout(client, spreadsheet_id, title="Ревизия"):
    metadata = client.fetch_sheet_metadata(
        spreadsheet_id,
        params={
            "fields": "sheets(properties)",
        },
    )
    sheets = [s for s in metadata.get("sheets", []) if s["properties"]["title"] == title]
    if len(sheets) != 1:
        raise BdrRevisionError(f"БДР: лист «{title}» не найден")
    grid = sheets[0]["properties"]["gridProperties"]
    if grid["rowCount"] > 4000 or grid["columnCount"] < 13:
        raise BdrRevisionError("БДР: размер листа изменился — нужна сверка структуры")
    escaped = title.replace("'", "''")
    data = client.fetch_sheet_metadata(
        spreadsheet_id,
        params={
            "ranges": f"'{escaped}'!B1:M{grid['rowCount']}",
            "includeGridData": "true",
            "fields": "sheets(properties,merges,data(startRow,startColumn,rowData(values("
            "formattedValue,userEnteredValue,dataValidation,chipRuns,note))))",
        },
    )
    return BdrLayout(data["sheets"][0])


def apply_and_verify(client, spreadsheet_id, layout, requests, checks):
    if requests:
        client.batch_update(spreadsheet_id, {"requests": requests})
    # A readback is required even after an idempotent no-op, so a caller never
    # reports a successful sync based solely on the write response.
    actual = read_layout(client, spreadsheet_id, layout.title)
    for position, expected in checks.items():
        if cell_number(actual.cells.get(position, {})) != expected:
            raise BdrRevisionError("БДР: проверка записи не совпала; повторите исходное сообщение")
