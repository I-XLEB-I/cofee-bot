"""Guarded Sheets batches with an atomic, server-enforced operation marker.

Each workbook is a separate transaction. A fixed developerMetadata ID makes
even a duplicated HTTP request fail atomically, instead of duplicating payroll.
Only the orchestrator may advance from a verified BDR batch to the bot ledger.
"""

import hashlib
import json
from functools import wraps
from threading import RLock

from gspread.http_client import HTTPClient

from revision_dialogue import RevisionConflict

SHEETS_ACCESS_LOCK = RLock()


class SerializedSheetsClient(HTTPClient):
    """All existing bot writers participate in a revision's critical section."""

    def request(self, *args, **kwargs):
        with SHEETS_ACCESS_LOCK:
            return super().request(*args, **kwargs)


def serialized_write(operation):
    @wraps(operation)
    def wrapped(*args, **kwargs):
        with SHEETS_ACCESS_LOCK:
            return operation(*args, **kwargs)

    return wrapped


def entered(value):
    if value in (None, ""):
        return {}
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return {"numberValue": value}
    # Never interpret user-provided strings as formulas.
    return {"stringValue": str(value)}


def address(cell):
    column = cell["column"] + 1
    letters = ""
    while column:
        column, digit = divmod(column - 1, 26)
        letters = chr(65 + digit) + letters
    title = cell["title"].replace("'", "''")
    return f"'{title}'!{letters}{cell['row'] + 1}"


def read_cells(client, spreadsheet_id, cells):
    if not cells:
        return {}
    # Compact adjacent cells into row ranges to stay below URL size limits.
    rows = {}
    for cell in cells:
        key = cell["title"], cell["row"]
        bounds = rows.setdefault(key, [cell, cell])
        if cell["column"] < bounds[0]["column"]:
            bounds[0] = cell
        if cell["column"] > bounds[1]["column"]:
            bounds[1] = cell
    ranges = [
        address(first) + ":" + address(last).rsplit("!", 1)[1] for first, last in rows.values()
    ]
    result = client.fetch_sheet_metadata(
        spreadsheet_id,
        params={
            "ranges": ranges,
            "includeGridData": "true",
            "fields": "sheets(properties(sheetId,title),merges,data(startRow,startColumn,"
            "rowData(values(userEnteredValue,dataValidation,chipRuns))))",
        },
    )
    actual = {}
    titles = {}
    merges = {}
    for sheet in result.get("sheets", []):
        sheet_id = sheet["properties"]["sheetId"]
        titles[sheet_id] = sheet["properties"]["title"]
        merges[sheet_id] = sheet.get("merges", [])
        for grid in sheet.get("data", []):
            for r, row in enumerate(grid.get("rowData", []), grid.get("startRow", 0)):
                for c, value in enumerate(row.get("values", []), grid.get("startColumn", 0)):
                    actual[sheet_id, r, c] = value
    output = {}
    for cell in cells:
        position = cell["sheet_id"], cell["row"], cell["column"]
        if titles.get(cell["sheet_id"]) != cell["title"]:
            raise RevisionConflict("Структура таблицы изменилась; нужна новая сверка.")
        value = actual.get(position, {})
        if (
            value.get("dataValidation")
            or value.get("chipRuns")
            or any(
                m.get("startRowIndex", 0) <= cell["row"] < m.get("endRowIndex", 0)
                and m.get("startColumnIndex", 0) <= cell["column"] < m.get("endColumnIndex", 0)
                for m in merges.get(cell["sheet_id"], [])
            )
        ):
            raise RevisionConflict("Целевая ячейка защищена структурой таблицы; нужна сверка.")
        raw = value.get("userEnteredValue", {})
        if "formulaValue" in raw and cell.get("write", True):
            raise RevisionConflict("Целевая ячейка содержит формулу; запись остановлена.")
        output[position] = raw
    return output


def prepare_step(client, spreadsheet_id, cells, operation_key):
    """Capture exact values after locating rows; no worksheet creation or edits."""
    positions = [(c["sheet_id"], c["row"], c["column"]) for c in cells]
    if len(positions) != len(set(positions)):
        raise RevisionConflict("Операция содержит пересекающиеся изменения.")
    actual = read_cells(client, spreadsheet_id, cells)
    prepared = []
    for cell, position in zip(cells, positions):
        before = actual[position]
        # Locating a row and capturing its values are separate reads. Verify
        # identity/empty slots against the earlier snapshot before previewing.
        if "expected" in cell and before != cell["expected"]:
            raise RevisionConflict("Таблица изменилась во время сверки. Повторите сохранение.")
        after = cell["after"] if cell.get("write", True) else before
        prepared.append(
            {k: v for k, v in {**cell, "before": before, "after": after}.items() if k != "expected"}
        )
    digest = hashlib.sha256(json.dumps(prepared, sort_keys=True).encode()).hexdigest()
    marker_id = 1 + int(hashlib.sha256(operation_key.encode()).hexdigest()[:8], 16) % 2_147_483_646
    return {
        "spreadsheet_id": spreadsheet_id,
        "cells": prepared,
        "state": "prepared",
        "marker_id": marker_id,
        "marker_value": f"{operation_key}:{digest}",
    }


def marker_present(client, step):
    metadata = client.fetch_sheet_metadata(
        step["spreadsheet_id"],
        params={
            "fields": "developerMetadata(metadataId,metadataKey,metadataValue)",
        },
    )
    found = [
        m for m in metadata.get("developerMetadata", []) if m["metadataId"] == step["marker_id"]
    ]
    if not found:
        return False
    if (
        len(found) != 1
        or found[0].get("metadataKey") != "coffee_revision_operation"
        or (found[0].get("metadataValue") != step["marker_value"])
    ):
        raise RevisionConflict("Идентификатор записи занят другой операцией; нужна сверка.")
    return True


def verify_step(client, step, target):
    actual = read_cells(client, step["spreadsheet_id"], step["cells"])
    for cell in step["cells"]:
        if actual[cell["sheet_id"], cell["row"], cell["column"]] != cell[target]:
            raise RevisionConflict("Значения в таблице изменились. Запись остановлена для сверки.")


def execute_step(client, step):
    """No blind retry. A later explicit recovery uses this same fixed marker."""
    if marker_present(client, step):
        verify_step(client, step, "after")
        return
    verify_step(client, step, "before")
    requests = []
    for cell in step["cells"]:
        if cell["before"] == cell["after"]:
            continue
        requests.append(
            {
                "updateCells": {
                    "start": {
                        "sheetId": cell["sheet_id"],
                        "rowIndex": cell["row"],
                        "columnIndex": cell["column"],
                    },
                    "rows": [{"values": [{"userEnteredValue": cell["after"]}]}],
                    "fields": "userEnteredValue",
                }
            }
        )
    requests.append(
        {
            "createDeveloperMetadata": {
                "developerMetadata": {
                    "metadataId": step["marker_id"],
                    "metadataKey": "coffee_revision_operation",
                    "metadataValue": step["marker_value"],
                    "visibility": "DOCUMENT",
                    "location": {"spreadsheet": True},
                }
            }
        }
    )
    client.batch_update(step["spreadsheet_id"], {"requests": requests})
    if not marker_present(client, step):
        raise RevisionConflict("Не удалось подтвердить отметку операции в таблице.")
    verify_step(client, step, "after")
