import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_bdr_revision import FakeBdrClient

import bot
import revision_accounting
import revision_bot
from revision_dialogue import RevisionConflict, RevisionStore
from revision_sheet_journal import entered


def proposal(location="Южный", values=None, action="update", previous=""):
    return {
        "action": action,
        "clarification": "",
        "records": [
            {
                "location": location,
                "previous_location": previous,
                "date": "",
                "values": values if values is not None else {"Кофе": "4.5"},
            }
        ],
    }


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "draft.sqlite3"
        self.store = RevisionStore(self.path)
        self.addCleanup(self.temp.cleanup)

    def add(self, mid=1, data=None):
        self.store.accept(12, 12, mid, "ревизия", "2026-09-04")
        draft = self.store.get(12, 12)
        data = data or proposal()
        self.store.mark_message(12, 12, mid, "parsed", data)
        return self.store.apply_message(12, 12, mid, data, expected_version=draft["version"])

    def test_multi_message_restart_correction_and_duplicate(self):
        first = self.add()
        self.assertFalse(self.store.accept(12, 12, 1, "повтор", "2026-09-04"))
        self.store = RevisionStore(self.path)
        corrected = self.add(2, proposal("", {"Молоко": "2.5"}))
        self.assertEqual(corrected["records"][0]["values"], {"Кофе": "4.5", "Молоко": "2.5"})
        again = self.store.apply_message(12, 12, 2, proposal(), expected_version=first["version"])
        self.assertEqual(again, corrected)
        self.assertIsNone(self.store.get(13, 13))

    def test_unassigned_values_are_bound_after_location_arrives(self):
        draft = self.add(data=proposal("", {"Кофе": "4.5"}))
        self.assertTrue(draft["unassigned"])
        draft = self.add(2, proposal("Сити", {}))
        self.assertEqual(draft["records"][0]["values"], {"Кофе": "4.5"})
        self.assertFalse(draft["unassigned"])
        self.assertTrue(draft["needs_review"])

    def test_two_points_never_receive_implicit_update(self):
        self.add()
        self.add(2, proposal("Сити", {"Кофе": "8"}))
        draft = self.add(3, proposal("", {"Кофе": "2"}))
        self.assertEqual([r["values"]["Кофе"] for r in draft["records"]], ["4.5", "8"])
        self.assertEqual(draft["unassigned"][0]["values"]["Кофе"], "2")

    def test_location_correction_preserves_values(self):
        self.add()
        draft = self.add(2, proposal("Сити", {}, previous="Южный"))
        self.assertEqual(
            draft["records"],
            [{"location": "Сити", "date": "2026-09-04", "values": {"Кофе": "4.5"}}],
        )

    def test_new_message_immediately_invalidates_confirmation(self):
        draft = self.add()
        draft = self.store.set_preview(
            12, 12, {"steps": []}, version=draft["version"], generation=draft["generation"]
        )
        self.store.accept(12, 12, 2, "исправь", "2026-09-04")
        with self.assertRaises(RevisionConflict):
            self.store.begin_operation(
                12, 12, version=draft["version"], generation=draft["generation"]
            )

    def test_double_confirmation_and_cancel_while_saving_are_rejected(self):
        draft = self.add()
        draft = self.store.set_preview(
            12, 12, {"steps": []}, version=draft["version"], generation=draft["generation"]
        )
        operation = self.store.begin_operation(
            12, 12, version=draft["version"], generation=draft["generation"]
        )
        with self.assertRaises(RevisionConflict):
            self.store.begin_operation(
                12, 12, version=draft["version"], generation=draft["generation"]
            )
        with self.assertRaises(RevisionConflict):
            self.store.cancel(12, 12, version=draft["version"], generation=draft["generation"])
        self.assertIsNone(self.store.operation(operation, 13, 13))

    def test_cancel_invalidates_inflight_model_and_pending_messages(self):
        draft = self.add()
        self.store.accept(12, 12, 2, "исправь", "2026-09-04")
        current = self.store.get(12, 12)
        self.store.cancel(12, 12, version=current["version"], generation=current["generation"])
        with self.assertRaises(RevisionConflict):
            self.store.apply_message(12, 12, 2, proposal(), expected_version=draft["version"])
        self.assertIsNone(self.store.next_message(12, 12))

    def test_interrupted_paid_call_is_not_retried_but_parsed_result_survives(self):
        self.store.accept(12, 12, 1, "ревизия", "2026-09-04")
        self.store.mark_message(12, 12, 1, "processing")
        restarted = RevisionStore(self.path)
        self.assertIsNone(restarted.next_message(12, 12))
        restarted.accept(12, 12, 2, "кофе 2", "2026-09-04")
        restarted.mark_message(12, 12, 2, "parsed", proposal())
        self.assertEqual(RevisionStore(self.path).next_message(12, 12)["state"], "parsed")


class FakeSheets:
    """In-memory model of the actual Sheets atomic-batch/metadata contract."""

    def __init__(self):
        self.books = {"ledger": {}, "bdr": {}}
        self.markers = {"ledger": {}, "bdr": {}}
        self.calls = []
        self.fail_after = None
        self.fail_before = None
        for index, (title, headers) in enumerate(
            [
                ("Ревизия", bot.REVISION_HEADERS),
                ("Обслуживание", bot.SERVICE_HEADERS),
                ("Импорт группы", bot.GROUP_REPORT_LOG_HEADERS),
            ],
            1,
        ):
            self.books["ledger"][index] = {
                "properties": {
                    "sheetId": index,
                    "title": title,
                    "gridProperties": {"rowCount": 1000, "columnCount": 26},
                },
                "data": [
                    {
                        "startRow": 0,
                        "startColumn": 0,
                        "rowData": [
                            {"values": [{"userEnteredValue": entered(v)} for v in headers]}
                        ],
                    }
                ],
            }
        bdr = FakeBdrClient()
        self.books["bdr"][bdr.properties["sheetId"]] = {
            "properties": bdr.properties,
            "data": [{"startRow": 0, "startColumn": 0, "rowData": bdr.rows}],
        }
        # Clear the fixture's intentionally protected warehouse cells.
        for row in bdr.rows[79:]:
            row["values"][9] = {}

    def fetch_sheet_metadata(self, spreadsheet_id, params):
        assert params["fields"].count("(") == params["fields"].count(")")
        return {
            "sheets": copy.deepcopy(list(self.books[spreadsheet_id].values())),
            "developerMetadata": copy.deepcopy(list(self.markers[spreadsheet_id].values())),
        }

    def batch_update(self, spreadsheet_id, body):
        if self.fail_before == spreadsheet_id:
            raise TimeoutError("before write")
        books, markers = copy.deepcopy(self.books), copy.deepcopy(self.markers)
        for request in body["requests"]:
            if "createDeveloperMetadata" in request:
                marker = request["createDeveloperMetadata"]["developerMetadata"]
                if marker["metadataId"] in markers[spreadsheet_id]:
                    raise ValueError("Metadata ID already exists; whole batch rejected")
                markers[spreadsheet_id][marker["metadataId"]] = marker
                continue
            change = request["updateCells"]
            assert change["fields"] == "userEnteredValue"
            start = change["start"]
            rows = books[spreadsheet_id][start["sheetId"]]["data"][0]["rowData"]
            while len(rows) <= start["rowIndex"]:
                rows.append({"values": []})
            cells = rows[start["rowIndex"]]["values"]
            while len(cells) <= start["columnIndex"]:
                cells.append({})
            cells[start["columnIndex"]]["userEnteredValue"] = change["rows"][0]["values"][0][
                "userEnteredValue"
            ]
        self.books, self.markers = books, markers
        self.calls.append((spreadsheet_id, copy.deepcopy(body)))
        if self.fail_after == spreadsheet_id:
            self.fail_after = None
            raise TimeoutError("response lost after commit")

    def worksheet(self, title):
        sheet = next(s for s in self.books["ledger"].values() if s["properties"]["title"] == title)

        def values(**kwargs):
            return [
                [next(iter(c.get("userEnteredValue", {}).values()), "") for c in r["values"]]
                for r in sheet["data"][0]["rowData"]
            ]

        return SimpleNamespace(
            id=sheet["properties"]["sheetId"], row_count=1000, get_all_values=values
        )

    def host(self):
        names = (
            "REVISION_HEADERS",
            "SERVICE_HEADERS",
            "GROUP_REPORT_LOG_HEADERS",
            "REVISION_ITEMS",
            "REVISION_UNITS",
            "POINTS",
            "build_revision_values_from_record",
            "build_revision_row_values",
            "build_service_row_values",
            "build_group_report_payload",
            "add_bdr_revision_backup",
            "build_group_report_revision_backup",
        )
        return SimpleNamespace(
            **{n: getattr(bot, n) for n in names},
            get_configured_user_name=lambda uid: "Сотрудник",
            get_allowed_user_ids=lambda: {12},
            get_owner_ai_readonly_book=lambda: SimpleNamespace(
                client=self, worksheet=self.worksheet
            ),
            get_sheet=lambda: SimpleNamespace(client=self),
            SPREADSHEET_ID="ledger",
            BDR_SPREADSHEET_ID="bdr",
        )


class AccountingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RevisionStore(Path(self.temp.name) / "draft.sqlite3")
        self.sheets = FakeSheets()
        self.host = self.sheets.host()
        self.patch = patch.object(bot, "get_paid_workers", return_value=["Сотрудник"])
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def prepare(self, location="Сити", values=None):
        self.store.accept(12, 12, 1, "ревизия", "2026-09-04")
        draft = self.store.apply_message(12, 12, 1, proposal(location, values), expected_version=0)
        preview = revision_accounting.prepare(self.host, draft, 12, 12)
        self.assertFalse(self.sheets.calls, "Preview must be read only")
        draft = self.store.set_preview(
            12, 12, preview, version=draft["version"], generation=draft["generation"]
        )
        opid = self.store.begin_operation(
            12, 12, version=draft["version"], generation=draft["generation"]
        )
        return opid, preview

    def test_payroll_water_napkins_and_resume_without_duplicates(self):
        opid, preview = self.prepare(values={"Кофе": "4.5", "Вода": "2.5", "Салфетки сухие": "200"})
        self.assertTrue(preview["needs_confirmation"])
        self.assertEqual(preview["summaries"][0]["salary"], 250)
        self.sheets.fail_after = "bdr"
        with self.assertRaises(TimeoutError):
            revision_accounting.save(self.host, self.store, opid, 12, 12)
        self.assertEqual(self.store.get(12, 12)["status"], "uncertain")
        self.assertEqual(len(self.sheets.worksheet("Обслуживание").get_all_values()), 1)
        revision_accounting.save(self.host, self.store, opid, 12, 12)
        revision_accounting.save(self.host, self.store, opid, 12, 12)
        self.assertEqual(len(self.sheets.calls), 2)
        self.assertEqual(len(self.sheets.worksheet("Обслуживание").get_all_values()), 2)
        self.assertEqual(self.store.get(12, 12)["status"], "saved")
        row = next(iter(self.sheets.books["bdr"].values()))["data"][0]["rowData"][92]["values"][3]
        self.assertEqual(row["userEnteredValue"], {"numberValue": 0.4})

    def test_lost_local_commit_response_recovered_without_second_payment(self):
        opid, _ = self.prepare()
        self.sheets.fail_after = "ledger"
        with self.assertRaises(TimeoutError):
            revision_accounting.save(self.host, self.store, opid, 12, 12)
        revision_accounting.save(self.host, self.store, opid, 12, 12)
        self.assertEqual(len(self.sheets.calls), 2)
        self.assertEqual(len(self.sheets.worksheet("Обслуживание").get_all_values()), 2)

    def test_conflict_before_first_write_leaves_both_workbooks_untouched(self):
        opid, preview = self.prepare()
        cell = preview["steps"][-1]["cells"][-1]
        rows = self.sheets.books["ledger"][cell["sheet_id"]]["data"][0]["rowData"]
        rows.append({"values": [{"userEnteredValue": entered("другой отчёт")}]})
        with self.assertRaises(RevisionConflict):
            revision_accounting.save(self.host, self.store, opid, 12, 12)
        self.assertFalse(self.sheets.calls)

    def test_warehouse_creates_no_service(self):
        opid, preview = self.prepare("Дома", {name: "1" for name in bot.REVISION_ITEMS})
        self.assertFalse(preview["needs_confirmation"])
        revision_accounting.save(self.host, self.store, opid, 12, 12)
        self.assertEqual(len(self.sheets.worksheet("Обслуживание").get_all_values()), 1)

    def test_same_day_correction_reuses_visit(self):
        opid, _ = self.prepare()
        revision_accounting.save(self.host, self.store, opid, 12, 12)
        self.store.accept(12, 12, 2, "исправь ревизию", "2026-09-04")
        current = self.store.get(12, 12)
        draft = self.store.apply_message(
            12, 12, 2, proposal("Сити", {"Кофе": "5"}), expected_version=current["version"]
        )
        preview = revision_accounting.prepare(self.host, draft, 12, 12)
        self.assertEqual(preview["summaries"][0]["salary"], 0)
        self.assertIn("уже есть", preview["summaries"][0]["service"])
        draft = self.store.set_preview(
            12, 12, preview, version=draft["version"], generation=draft["generation"]
        )
        second = self.store.begin_operation(
            12, 12, version=draft["version"], generation=draft["generation"]
        )
        revision_accounting.save(self.host, self.store, second, 12, 12)
        self.assertEqual(len(self.sheets.worksheet("Обслуживание").get_all_values()), 2)

    def test_marker_collision_rejects_entire_batch(self):
        opid, preview = self.prepare()
        step = preview["steps"][0]
        self.sheets.markers["bdr"][step["marker_id"]] = {
            "metadataId": step["marker_id"],
            "metadataValue": "foreign",
        }
        with self.assertRaises(RevisionConflict):
            revision_accounting.save(self.host, self.store, opid, 12, 12)
        self.assertFalse(self.sheets.calls)


class TelegramDialogueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RevisionStore(Path(self.temp.name) / "draft.sqlite3")
        self.application = SimpleNamespace(
            bot=SimpleNamespace(send_message=AsyncMock()), create_task=None
        )

    async def test_parsed_message_after_restart_needs_no_model_call(self):
        self.store.accept(12, 12, 1, "Ревизия на Южном", "2026-09-04")
        self.store.mark_message(12, 12, 1, "parsed", proposal())
        with (
            patch.object(revision_bot, "get_store", return_value=self.store),
            patch.object(bot, "get_allowed_user_ids", return_value={12}),
            patch.object(bot, "query_owner_ai") as model,
        ):
            await revision_bot.drain(bot, self.application, 12, 12)
        model.assert_not_called()
        self.assertEqual(self.store.get(12, 12)["records"][0]["values"]["Кофе"], "4.5")

    async def test_whole_dialogue_calls_model_once_per_message_and_never_writes_without_save(self):
        self.store.accept(12, 12, 1, "Ревизия на Южном кофе четыре с половиной", "2026-09-04")
        self.store.accept(12, 12, 2, "Молока два с половиной", "2026-09-04")
        with (
            patch.object(revision_bot, "get_store", return_value=self.store),
            patch.object(bot, "get_allowed_user_ids", return_value={12}),
            patch.object(bot, "get_owner_ai_client_config", return_value=object()),
            patch.object(
                bot,
                "query_owner_ai",
                side_effect=[
                    {"scope": "staff", "revision": proposal()},
                    {"scope": "staff", "revision": proposal("", {"Молоко": "2.5"})},
                ],
            ) as model,
            patch.object(revision_accounting, "prepare") as prepare,
        ):
            await revision_bot.drain(bot, self.application, 12, 12)
        self.assertEqual(model.call_count, 2)
        second_context = model.call_args_list[1].kwargs["revision_context"]["records"]
        self.assertEqual(second_context[0]["values"], {"Кофе": "4.5"})
        prepare.assert_not_called()
        self.assertEqual(
            self.store.get(12, 12)["records"][0]["values"], {"Кофе": "4.5", "Молоко": "2.5"}
        )

    async def test_new_message_during_preview_prevents_stale_auto_save(self):
        self.store.accept(12, 12, 1, "ревизия", "2026-09-04")
        draft = self.store.apply_message(12, 12, 1, proposal(), expected_version=0)

        def prepare(*args):
            self.store.accept(12, 12, 2, "пока не сохраняй", "2026-09-04")
            return {"needs_confirmation": False, "steps": [], "summaries": []}

        with (
            patch.object(revision_bot, "get_store", return_value=self.store),
            patch.object(revision_accounting, "prepare", side_effect=prepare),
            patch.object(revision_bot, "perform_save", new=AsyncMock()) as save,
        ):
            await revision_bot.prepare_save(bot, self.application, 12, 12, draft)
        save.assert_not_awaited()
        self.assertEqual(self.store.get(12, 12)["status"], "draft")

    async def test_group_message_never_enters_revision_or_initializes_store(self):
        message = SimpleNamespace(from_user=SimpleNamespace(id=12), chat_id=-100)
        with patch.object(revision_bot, "get_store") as store:
            accepted = await revision_bot.handle_message(bot, message, SimpleNamespace(), "ревизия")
        self.assertFalse(accepted)
        store.assert_not_called()


def test_processing_message_blocks_preview_and_commit(tmp_path):
    store = RevisionStore(tmp_path / "draft.sqlite3")
    store.accept(12, 12, 1, "ревизия", "2026-09-04")
    draft = store.apply_message(12, 12, 1, proposal(), expected_version=0)
    store.accept(12, 12, 2, "исправление", "2026-09-04")
    store.mark_message(12, 12, 2, "processing")
    draft = store.get(12, 12)
    with unittest.TestCase().assertRaises(RevisionConflict):
        store.set_preview(
            12, 12, {"steps": []}, version=draft["version"], generation=draft["generation"]
        )
    with unittest.TestCase().assertRaises(RevisionConflict):
        store.begin_operation(12, 12, version=draft["version"], generation=draft["generation"])


def test_new_preview_invalidates_old_confirmation(tmp_path):
    store = RevisionStore(tmp_path / "draft.sqlite3")
    store.accept(12, 12, 1, "ревизия", "2026-09-04")
    draft = store.apply_message(12, 12, 1, proposal(), expected_version=0)
    first = store.set_preview(
        12, 12, {"steps": [], "salary": 0}, version=draft["version"], generation=draft["generation"]
    )
    second = store.set_preview(
        12,
        12,
        {"steps": [], "salary": 250},
        version=first["version"],
        generation=first["generation"],
    )
    assert second["generation"] > first["generation"]
    with unittest.TestCase().assertRaises(RevisionConflict):
        store.begin_operation(12, 12, version=first["version"], generation=first["generation"])


def test_legacy_google_writer_joins_revision_critical_section():
    import threading

    from gspread.http_client import HTTPClient

    from revision_sheet_journal import SHEETS_ACCESS_LOCK, SerializedSheetsClient

    client = object.__new__(SerializedSheetsClient)
    entered_thread = threading.Event()

    def legacy_request():
        entered_thread.set()
        client.request("post", "https://sheets.googleapis.com/test")

    with patch.object(HTTPClient, "request", return_value=object()) as request:
        with SHEETS_ACCESS_LOCK:
            thread = threading.Thread(target=legacy_request)
            thread.start()
            assert entered_thread.wait(1)
            request.assert_not_called()
        thread.join(1)
        assert not thread.is_alive()
        request.assert_called_once()
