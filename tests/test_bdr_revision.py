import copy
import json
import unittest
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

import bot
from bdr_revision import BdrRevisionError, apply_and_verify, read_layout

ITEMS = [
    "Кофе ",
    "Молоко",
    "Шоколад",
    "Мока",
    "Сахар",
    "Сироп",
    "Стаканы",
    "КрышкиЧ",
    "КрышкиБ",
    "Палочки",
    "Трубочки",
    "Манжеты",
    "Салф влажн",
    "Салф сухие",
    "Пакеты",
]
PRICES = [1819, 560, 700, 560, 0.8415, 406, 3.9, 1.7, 1.9, 0.92, 0.84, 2.3, 165, 135, 50]


def value_cell(value):
    if isinstance(value, (int, float)):
        return {"userEnteredValue": {"numberValue": value}, "formattedValue": str(value)}
    return {"userEnteredValue": {"stringValue": value}, "formattedValue": str(value)}


class FakeBdrClient:
    def __init__(self):
        self.rows = [{"values": [{} for _ in range(13)]} for _ in range(94)]
        header = [
            "31.8.2026",
            "Цена",
            "Сити",
            "Южн",
            "Белом",
            "Гагарина",
            "Макси",
            "Гиппо",
            "Дома",
            "Гараж",
            "Итого",
        ]
        for column, value in enumerate(header, 1):
            self.rows[78]["values"][column] = value_cell(value)
        for row, (item, price) in enumerate(zip(ITEMS, PRICES), 79):
            cells = self.rows[row]["values"]
            cells[1], cells[2] = value_cell(item), value_cell(price)
            cells[9] = {"userEnteredValue": {"formulaValue": "=2*100"}, "formattedValue": "200"}
            cells[11] = {"userEnteredValue": {"formulaValue": f"=SUM(D{row + 1}:K{row + 1})"}}
            cells[12] = {"userEnteredValue": {"formulaValue": f"=C{row + 1}*L{row + 1}"}}
        self.properties = {
            "sheetId": 353159931,
            "title": "Ревизия",
            "gridProperties": {"rowCount": 537, "columnCount": 26},
        }
        self.writes = []
        self.fail_write = False

    def fetch_sheet_metadata(self, spreadsheet_id, params):
        assert params["fields"].count("(") == params["fields"].count(")")
        sheet = {"properties": self.properties}
        if params.get("includeGridData"):
            sheet["data"] = [{"startRow": 0, "startColumn": 0, "rowData": self.rows}]
        return {"sheets": [copy.deepcopy(sheet)]}

    def batch_update(self, spreadsheet_id, body):
        if self.fail_write:
            raise BdrRevisionError("БДР временно недоступен")
        self.writes.append(copy.deepcopy(body))
        for request in body["requests"]:
            update = request["updateCells"]
            assert update["fields"] == "userEnteredValue"
            bounds = update["range"]
            row, column = bounds["startRowIndex"], bounds["startColumnIndex"]
            self.rows[row]["values"][column]["userEnteredValue"] = update["rows"][0]["values"][0][
                "userEnteredValue"
            ]


class BdrRevisionTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeBdrClient()
        self.layout = read_layout(self.client, "bdr")
        self.block = self.layout.nearest("04.09.2026")

    def test_early_september_report_belongs_to_august(self):
        self.assertEqual(self.block.period, "08.2026")
        self.assertEqual(self.block.date, date(2026, 8, 31))
        self.assertEqual(self.layout.nearest("22.08.2026"), self.block)
        self.assertEqual(self.layout.nearest("10.09.2026"), self.block)
        with self.assertRaisesRegex(BdrRevisionError, "10 дней"):
            self.layout.nearest("11.09.2026")

    def test_equidistant_blocks_require_clarification(self):
        self.layout.blocks.append(copy.copy(self.block))
        self.layout.blocks[-1].date = date(2026, 9, 10)
        with self.assertRaisesRegex(BdrRevisionError, "равноудалены"):
            self.layout.nearest("05.09.2026")

    def test_actual_august_headers_determine_point_columns(self):
        self.assertEqual(self.block.columns["Южный"], 4)
        self.assertEqual(self.block.columns["Беломорский"], 5)
        self.assertEqual(self.block.columns["Гиппо"], 8)
        self.assertEqual(self.block.items["Салфетки сухие"], 92)

    def test_convert_dry_napkins_skip_water_and_preserve_formulas_and_warehouses(self):
        before = copy.deepcopy(self.client.rows)
        requests, backup, checks = self.layout.plan(
            self.block,
            "Сити",
            {"Салфетки сухие": "200", "Вода": "2.5", "Сиропы": "0"},
        )
        apply_and_verify(self.client, "bdr", self.layout, requests, checks)
        self.assertEqual(len(requests), 2)
        self.assertEqual(checks[92, 3], Decimal("0.4"))
        self.assertEqual(backup, {"Салфетки сухие": "", "Сиропы": ""})
        for i, row in enumerate(self.client.rows):
            for j, cell in enumerate(row["values"]):
                if (i, j) not in checks:
                    self.assertEqual(cell, before[i]["values"][j])

    def test_idempotent_replay_does_not_write_again(self):
        requests, _, checks = self.layout.plan(self.block, "Сити", {"Кофе": "6"})
        apply_and_verify(self.client, "bdr", self.layout, requests, checks)
        layout = read_layout(self.client, "bdr")
        requests, _, checks = layout.plan(layout.nearest("04.09.2026"), "Сити", {"Кофе": "6"})
        self.assertEqual(requests, [])
        apply_and_verify(self.client, "bdr", layout, requests, checks)
        self.assertEqual(len(self.client.writes), 1)

    def test_manual_conflict_formula_or_validation_stops_the_entire_plan(self):
        for cell in (
            value_cell(99),
            {"userEnteredValue": {"formulaValue": "=6"}},
            {"dataValidation": {"condition": {"type": "ONE_OF_LIST"}}},
        ):
            with self.subTest(cell=cell):
                self.layout.cells[79, 3] = cell
                with self.assertRaises(BdrRevisionError):
                    self.layout.plan(self.block, "Сити", {"Молоко": "4", "Кофе": "6"})
                self.assertEqual(self.client.writes, [])

    def test_known_previous_value_can_be_updated(self):
        self.layout.cells[79, 3] = value_cell(5)
        requests, before, _ = self.layout.plan(
            self.block,
            "Сити",
            {"Кофе": "6"},
            {"Кофе": "5"},
        )
        self.assertEqual(len(requests), 1)
        self.assertEqual(before, {"Кофе": "5"})

    def test_missing_item_and_missing_point_do_not_get_guessed(self):
        with self.assertRaisesRegex(BdrRevisionError, "нет строки"):
            self.layout.plan(self.block, "Сити", {"Кофе Исп": "6"})
        with self.assertRaisesRegex(BdrRevisionError, "нет точки"):
            self.layout.plan(self.block, "Бел2", {"Кофе": "6"})

    def test_readback_mismatch_does_not_report_success(self):
        with self.assertRaisesRegex(BdrRevisionError, "проверка записи"):
            apply_and_verify(self.client, "bdr", self.layout, [], {(79, 3): Decimal(6)})

    def test_restoration_clears_only_originally_empty_cells(self):
        self.layout.cells[79, 3] = value_cell(6)
        requests, _, checks = self.layout.plan(
            self.block,
            "Сити",
            {"Кофе": ""},
            {"Кофе": "6"},
            clear=True,
        )
        self.assertEqual(
            requests[0]["updateCells"]["rows"][0]["values"], [{"userEnteredValue": {}}]
        )
        self.assertIsNone(checks[79, 3])

    def test_point_edit_restores_bdr_source_and_updates_target_in_one_batch(self):
        self.client.rows[79]["values"][3] = value_cell(6)
        source = {"__row": 43, "Период": "08.2026", "Локация": "Сити", "Кофе": "6"}
        log = {
            "Revision_Row": 43,
            "Revision_Period": "08.2026",
            "Revision_Location": "Сити",
            "Revision_Mode": "created",
            "Revision_Backup": json.dumps(
                {
                    "bdr": {"date": "31.08.2026", "location": "Сити", "values": {"Кофе": ""}},
                }
            ),
        }
        draft = {"sync_bdr": True, "date": "04.09.2026", "point": "Южный", "who": "Александр"}
        revision = {"period": "09.2026", "location": "Южный", "values": {"Кофе": "9"}}
        with (
            patch("bot.BDR_SPREADSHEET_ID", "bdr"),
            patch("bot.get_sheet", return_value=SimpleNamespace(client=self.client)),
            patch("bot.find_group_report_revision_entry", return_value=source),
            patch("bot.find_revision_record", return_value=None),
            patch("bot.update_revision_row") as update,
        ):
            result = bot.save_edited_revision_entry(draft, log, revision)
        self.assertEqual(len(self.client.writes), 1)
        self.assertEqual(self.client.rows[79]["values"][3]["userEnteredValue"], {})
        self.assertEqual(self.client.rows[79]["values"][4]["userEnteredValue"], {"numberValue": 9})
        self.assertEqual(result["row"], 43)
        self.assertEqual(result["period"], "08.2026")
        self.assertEqual(json.loads(result["backup"])["bdr"]["location"], "Южный")
        self.assertEqual(update.call_args.args[1]["filled_at"], "04.09.2026")

    def test_bdr_undo_conflict_stops_before_paid_service_deletion(self):
        self.client.rows[79]["values"][3] = value_cell(99)
        source = {"__row": 43, "Период": "08.2026", "Локация": "Сити", "Кофе": "6"}
        log = {
            "__row": 370,
            "Статус": "saved",
            "Service_Row": 348,
            "Revision_Period": "08.2026",
            "Revision_Location": "Сити",
            "Revision_Backup": json.dumps(
                {
                    "bdr": {
                        "date": "31.08.2026",
                        "location": "Сити",
                        "values": {"Кофе": ""},
                    }
                }
            ),
        }
        book = SimpleNamespace(client=self.client, worksheet=Mock())
        with (
            patch("bot.BDR_SPREADSHEET_ID", "bdr"),
            patch("bot.get_sheet", return_value=book),
            patch("bot.get_group_report_logs_with_rows", return_value=[log]),
            patch("bot.find_group_report_revision_entry", return_value=source),
            patch("bot.delete_revision_row") as delete_revision,
        ):
            with self.assertRaises(BdrRevisionError):
                bot.delete_group_report_entry_by_log_row(370)
        book.worksheet.assert_not_called()
        delete_revision.assert_not_called()
        self.assertEqual(self.client.writes, [])


if __name__ == "__main__":
    unittest.main()
