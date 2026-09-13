"""Revision reconciliation using the bot's existing units and payroll rules."""

from datetime import date
from decimal import Decimal

import bdr_revision
from revision_dialogue import RevisionConflict, RevisionInputError
from revision_sheet_journal import (
    entered,
    execute_step,
    prepare_step,
    serialized_write,
    verify_step,
)


def prepare(host, draft, chat_id, user_id):
    if draft.get("unassigned") or draft.get("clarification"):
        raise RevisionInputError(
            draft.get("clarification") or "Сначала укажите точку для остатков."
        )
    if not draft["records"] or any(not record["values"] for record in draft["records"]):
        raise RevisionInputError("Укажите точку и хотя бы один фактический остаток.")
    who = host.get_configured_user_name(user_id)
    if not who or user_id not in host.get_allowed_user_ids():
        raise RevisionInputError("Нет доступа к записи ревизии.")
    book = host.get_owner_ai_readonly_book()
    if book is None or not host.BDR_SPREADSHEET_ID:
        raise RevisionInputError("Не настроено чтение таблиц или связь с БДР.")
    client = book.client
    layout = bdr_revision.read_layout(client, host.BDR_SPREADSHEET_ID)
    tables = {}
    for title, headers in (
        ("Ревизия", host.REVISION_HEADERS),
        ("Обслуживание", host.SERVICE_HEADERS),
        ("Импорт группы", host.GROUP_REPORT_LOG_HEADERS),
    ):
        sheet = book.worksheet(title)
        rows = sheet.get_all_values(value_render_option="FORMULA")
        if not rows or rows[0][: len(headers)] != headers or len(rows) > 20000:
            raise RevisionConflict(f"Лист «{title}» изменился; нужна сверка структуры.")
        tables[title] = (sheet, [row + [""] * max(0, len(headers) - len(row)) for row in rows])
    cells, bdr_cells, summaries = [], [], []
    bdr_guards = {}
    needs_confirmation = bool(draft.get("needs_review"))
    op_key = f"{draft['id']}:{draft['version']}"
    allocations = {title: len(rows) for title, (_, rows) in tables.items()}

    def row_cells(title, row_index, after, *, write_columns=None):
        sheet, rows = tables[title]
        if row_index >= sheet.row_count:
            raise RevisionConflict(
                f"На листе «{title}» закончились строки; увеличьте размер листа."
            )
        before = rows[row_index] if row_index < len(rows) else [""] * len(after)
        for column, value in enumerate(after):
            write = write_columns is None or column in write_columns
            cells.append(
                {
                    "sheet_id": sheet.id,
                    "title": title,
                    "row": row_index,
                    "column": column,
                    "expected": entered(before[column]),
                    "after": entered(value),
                    "write": write,
                }
            )

    def allocate(title):
        row_index = allocations[title]
        allocations[title] += 1
        return row_index

    # Headers are guards too. This also prevents writing through a renamed layout.
    for title, (_, rows) in tables.items():
        row_cells(title, 0, rows[0], write_columns=set())

    for record in draft["records"]:
        location = record["location"]
        report_date = date.fromisoformat(record["date"]).strftime("%d.%m.%Y")
        block = layout.nearest(report_date)
        values = record["values"]
        revision_rows = tables["Ревизия"][1]
        matches = [
            (i, row)
            for i, row in enumerate(revision_rows[1:], 1)
            if row[:2] == [block.period, location]
        ]
        if len(matches) > 1:
            raise RevisionConflict(
                f"{location}: найдено несколько ревизий за {block.period}; нужна сверка."
            )
        previous = dict(zip(host.REVISION_HEADERS, matches[0][1])) if matches else None
        before_values = host.build_revision_values_from_record(previous) if previous else {}
        # Keep the established conflict policy, including dry napkins / 500 and
        # water excluded from BDR. No arithmetic is delegated to the model.
        _, bdr_before, checks = layout.plan(block, location, values, before_values)
        for row, column in [
            (block.row, 1),
            (block.row, block.columns[location]),
            *[(row, 1) for row, _ in checks],
        ]:
            bdr_guards[row, column] = {
                "sheet_id": layout.sheet_id,
                "title": layout.title,
                "row": row,
                "column": column,
                "expected": layout.cells.get((row, column), {}).get("userEnteredValue", {}),
                "write": False,
            }
        for (row, column), desired in checks.items():
            bdr_cells.append(
                {
                    "sheet_id": layout.sheet_id,
                    "title": layout.title,
                    "row": row,
                    "column": column,
                    "expected": layout.cells.get((row, column), {}).get("userEnteredValue", {}),
                    "after": {} if desired is None else {"numberValue": float(desired)},
                }
            )
        merged = {**before_values, **values}
        rev_index = matches[0][0] if matches else allocate("Ревизия")
        revision_payload = {
            "period": block.period,
            "location": location,
            "who": who,
            "filled_at": report_date,
            "values": merged,
        }
        after = host.build_revision_row_values(revision_payload)
        after[4:] = [
            float(Decimal(str(v).replace(",", "."))) if v not in ("", None) else ""
            for v in after[4:]
        ]
        row_cells(
            "Ревизия",
            rev_index,
            after,
            write_columns={0, 1, 2, 3} | {4 + host.REVISION_ITEMS.index(item) for item in values},
        )
        missing = [item for item in host.REVISION_ITEMS if item not in values]
        changes = [
            {
                "item": item,
                "before": str(before_values.get(item, "")),
                "after": value,
                "unit": host.REVISION_UNITS[item],
            }
            for item, value in values.items()
        ]
        service_row = ""
        salary = 0
        service_status = "Склад: обслуживание не создаётся."
        if location in host.POINTS:
            service_rows = tables["Обслуживание"][1]
            visits = [
                (i, row)
                for i, row in enumerate(service_rows[1:], 1)
                if row[:3] == [report_date, who, location]
            ]
            if len(visits) > 1:
                raise RevisionConflict(
                    f"{location}: несколько обслуживаний за день; сначала разберите дубли."
                )
            # A correction on the same point/day reuses the existing visit. It
            # never creates a second payment or changes somebody else's salary.
            if visits:
                service_index, service_values = visits[0]
                row_cells("Обслуживание", service_index, service_values, write_columns=set())
                service_status = "Обслуживание за эту дату уже есть; нового начисления не будет."
            else:
                service_index = allocate("Обслуживание")
                payload = host.build_group_report_payload(
                    {
                        "date": report_date,
                        "who": who,
                        "point": location,
                        "water": values.get("Вода", ""),
                    }
                )
                salary = payload["service_sum"]
                row_cells("Обслуживание", service_index, host.build_service_row_values(payload))
                service_status = f"Будет записано обслуживание. Начисление: {salary} ₽."
            service_row = service_index + 1
            needs_confirmation = True
        needs_confirmation = (
            needs_confirmation
            or bool(missing)
            or any(change["before"] not in ("", change["after"]) for change in changes)
        )
        backup = host.add_bdr_revision_backup(
            host.build_group_report_revision_backup(previous),
            {
                "date": block.date.strftime("%d.%m.%Y"),
                "location": location,
                "before": bdr_before,
            },
        )
        log = [
            str(chat_id),
            f"dialogue:{op_key}:{location}",
            "",
            "",
            who,
            location,
            report_date,
            op_key,
            service_row,
            "",
            rev_index + 1,
            block.period,
            location,
            "updated" if matches else "created",
            backup,
            "saved",
            report_date,
        ]
        row_cells("Импорт группы", allocate("Импорт группы"), log)
        summaries.append(
            {
                "location": location,
                "date": report_date,
                "period": block.period,
                "bdr_date": block.date.strftime("%d.%m.%Y"),
                "changes": changes,
                "missing": missing,
                "service": service_status,
                "salary": salary,
            }
        )
    steps = []
    if bdr_cells:
        steps.append(
            prepare_step(
                client,
                host.BDR_SPREADSHEET_ID,
                bdr_cells + list(bdr_guards.values()),
                op_key + ":bdr",
            )
        )
    steps.append(prepare_step(client, host.SPREADSHEET_ID, cells, op_key + ":ledger"))
    return {"steps": steps, "summaries": summaries, "needs_confirmation": needs_confirmation}


@serialized_write
def save(host, store, operation_id, chat_id, user_id):
    if user_id not in host.get_allowed_user_ids():
        raise RevisionInputError("Нет доступа к записи ревизии.")
    operation = store.operation(operation_id, chat_id, user_id)
    if operation is None:
        raise RevisionConflict("Операция не найдена.")
    payload = operation["payload"]
    client = host.get_sheet().client
    try:
        # Verify ALL untouched destinations before touching either workbook.
        # execute_step repeats the check immediately before its atomic batch.
        for step in payload["steps"]:
            if step["state"] == "prepared":
                verify_step(client, step, "before")
        for step in payload["steps"]:
            if step["state"] == "done":
                verify_step(client, step, "after")
                continue
            step["state"] = "attempting"
            store.checkpoint(operation_id, chat_id, user_id, payload)
            execute_step(client, step)
            step["state"] = "done"
            store.checkpoint(operation_id, chat_id, user_id, payload)
        store.checkpoint(operation_id, chat_id, user_id, payload, state="saved")
    except Exception:
        store.checkpoint(operation_id, chat_id, user_id, payload, state="uncertain")
        raise
    return payload
