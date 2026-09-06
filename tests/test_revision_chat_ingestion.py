import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot

REVISION_PACKAGE = """04.09 Южный
Кофе - 4.1
Молоко - 4.5
Шоколад - 3
Мока - 4.2
Сироп - 4.1
Стакан - 340
Крышка Б - 180
Крышка Ч - 550
Палоч - 310
Трубочки - 340
Сахар - 200
Манжеты - 50
Салфетки влажные - 1.2
Салфетки сухие - 40
Мусорные пакеты - 1.5

Воды - 0.8

04.09 сити
Кофе - 6
Молоко - 4.3
Шоколад - 4.2
Мока - 4.5
Сироп - 4.7
Стакан - 100
Крышка Б - 150
Крышка Ч - 950
Палоч - 430
Трубочки - 420
Сахар - 390
Манжеты - 230
Салфетки влажные - 2
Салфетки сухие - 200
Мусорные пакеты - 1.5

Воды - 2.5

04.09 сити
Кофе - 6
Молоко - 4.3
Шоколад - 4.2
Мока - 4.5
Сироп - 4.7
Стакан - 100
Крышка Б - 150
Крышка Ч - 950
Палоч - 430
Трубочки - 420
Сахар - 390
Манжеты - 230
Салфетки влажные - 2
Салфетки сухие - 200
Мусорные пакеты - 1.5

Воды - 2.5

04.09 гагарина
Кофе - 8
Молоко - 3.5
Шоколад - 4
Мока - 4.2
Сироп - 0
Стакан - 400
Крышка Б - 450
Крышка Ч - 550
Палоч - 450
Трубочки - 320
Сахар - 400
Манжеты - 400
Салфетки влажные - 1.7
Салфетки сухие - 100
Мусорные пакеты - 2.5

Воды - 3.7

04.09 Беломорский
Кофе - 5.5
Молоко - 4.5
Шоколад - 5
Мока - 2.5
Сироп - 2.5
Стакан - 420
Крышка Б - 350
Крышка Ч - 750
Палоч - 580
Трубочки - 460
Сахар - 230
Манжеты - 650
Салфетки влажные - 1.5
Салфетки сухие - 200
Мусорные пакеты - 2.5

Воды - 2.4

04.09 гиппо
Кофе - 4.5
Молоко - 4.5
Шоколад - 3.3
Мока - 2
Сироп - 2.5
Стакан - 130
Крышка Б - 120
Крышка Ч - 510
Палоч - 300
Трубочки - 220
Сахар - 350
Манжеты - 210
Салфетки влажные - 1.5
Салфетки сухие - 200
Мусорные пакеты - 2.5

Воды - 2.1
(Автомат для воды скушал 50 рублей и отказался наливать воду)"""


class RevisionChatIngestionTests(unittest.TestCase):
    def setUp(self):
        self.now_patch = patch(
            "bot.now_local",
            return_value=datetime(2026, 9, 6, 12, 0, tzinfo=bot.BOT_TIMEZONE),
        )
        self.now_patch.start()

    def tearDown(self):
        self.now_patch.stop()

    def test_exact_revision_package_parses_five_unique_locations(self):
        snapshots = bot.parse_revision_snapshot_messages_text(REVISION_PACKAGE)

        self.assertEqual(
            [snapshot["location"] for snapshot in snapshots],
            ["Южный", "Сити", "Гагарина", "Беломорский", "Гиппо"],
        )
        by_location = {snapshot["location"]: snapshot for snapshot in snapshots}
        self.assertEqual(by_location["Южный"]["values"]["Вода"], "0,8")
        self.assertEqual(by_location["Сити"]["values"]["Кофе"], "6")
        self.assertEqual(by_location["Гиппо"]["values"]["Салфетки сухие"], "200")
        self.assertEqual(len(by_location["Сити"]["values"]), 16)
        self.assertTrue(
            any("повторный блок" in warning for warning in by_location["Сити"]["warnings"])
        )
        self.assertTrue(all(snapshot["date"] == "04.09.2026" for snapshot in snapshots))

    def test_dated_snapshot_is_recognized_without_slash(self):
        snapshot = bot.parse_revision_snapshot_message_text(
            "04.09 Южный\nКофе - 4.1\nМолоко - 4.5\nВоды - 0.8"
        )

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["location"], "Южный")
        self.assertEqual(snapshot["date"], "04.09.2026")

    def test_full_year_and_common_separators_are_supported(self):
        snapshot = bot.parse_revision_snapshot_message_text(
            "04.09.2026 сити\nКофе: 6\nМолоко = 4,3\nВоды — 2.5"
        )

        self.assertEqual(snapshot["values"], {"Кофе": "6", "Молоко": "4,3", "Вода": "2,5"})

    def test_water_purchase_without_repeated_word_water_is_counted(self):
        text = "01.09 Южный\n\nВоды - 2.2\n(Купил 2 бака 280₽)"

        self.assertEqual(bot.parse_revision_snapshot_messages_text(text), [])
        parsed = bot.parse_service_report_message_text(text)
        self.assertEqual(parsed["water"], "2,2")
        self.assertEqual(parsed["purchases"], "Вода 19л(2) 280₽")
        self.assertEqual(parsed["purchase_sum"], 280)

    def test_stock_quantity_without_purchase_words_is_not_an_expense(self):
        purchases, purchase_sum, warnings = bot.extract_service_report_purchases(
            "01.09 Южный\nВоды - 2.2"
        )

        self.assertEqual(purchases, "")
        self.assertEqual(purchase_sum, 0)
        self.assertEqual(warnings, [])

    def test_current_period_is_available_for_reading_before_open_day(self):
        with patch("bot.is_current_revision_period_available", return_value=False):
            periods = bot.get_revision_period_keys(include_current=True)
            text = bot.build_revision_period_menu_text("view")
            markup = bot.build_revision_period_markup(action="view")

        self.assertEqual(periods[0], "09.2026")
        self.assertNotIn("завершённый", text)
        self.assertEqual(
            markup.inline_keyboard[0][0].callback_data,
            "rev_period_09.2026",
        )


class RevisionChatRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.services = []
        self.revisions = []
        self.logs = []
        self.application = SimpleNamespace(bot_data={})

        def append_record(records, payload):
            records.append(payload)
            return len(records) + 1

        def find_duplicate(chat_id, source_key, fingerprint):
            for row, record in enumerate(self.logs, 2):
                if record["chat_id"] != chat_id:
                    continue
                logged = {
                    "__row": row,
                    "Chat_ID": record["chat_id"],
                    "Source_Key": record["source_key"],
                    "Source_Message_ID": record["source_message_id"],
                    "Статус": record["status"],
                    "Fingerprint": record["fingerprint"],
                    "Service_Row": record["service_row"],
                    "Revision_Row": record["revision_row"],
                    "Revision_Period": record["revision_period"],
                    "Revision_Location": record["revision_location"],
                    "Revision_Backup": record["revision_backup"],
                    "Revision_Mode": record["revision_mode"],
                }
                if record["source_key"] == source_key:
                    return logged, None
                if record["fingerprint"] == fingerprint:
                    return None, logged
            return None, None

        async def run_locally(function, *args):
            return function(*args)

        self.enterContext(
            patch(
                "bot.now_local",
                return_value=datetime(
                    2026,
                    9,
                    6,
                    12,
                    0,
                    tzinfo=bot.BOT_TIMEZONE,
                ),
            )
        )
        self.enterContext(patch("bot.get_service_report_author", return_value="Александр"))
        self.enterContext(patch("bot.BDR_SPREADSHEET_ID", ""))
        self.enterContext(patch("bot.get_paid_workers", return_value=["Александр"]))
        self.enterContext(patch("bot.get_user_directory_entries", return_value={}))
        self.enterContext(patch("bot.GROUP_REPORT_SAVE_MIN_INTERVAL_SECONDS", 0))
        self.enterContext(patch("bot.run_blocking", side_effect=run_locally))
        self.enterContext(patch("bot.find_group_report_duplicate", side_effect=find_duplicate))
        self.semantic_duplicates = self.enterContext(
            patch(
                "bot.find_service_semantic_duplicates",
                return_value=[],
            )
        )
        self.enterContext(
            patch(
                "bot.add_service_row",
                side_effect=lambda payload: append_record(self.services, payload),
            )
        )
        self.enterContext(
            patch(
                "bot.add_revision_row",
                side_effect=lambda payload: append_record(self.revisions, payload),
            )
        )
        self.enterContext(
            patch(
                "bot.append_group_report_log",
                side_effect=lambda payload: append_record(self.logs, payload),
            )
        )
        self.enterContext(patch("bot.find_revision_record", return_value=None))
        self.enterContext(patch("bot.auto_close_repair_for_point"))
        self.enterContext(patch("bot.request_group_service_today_refresh", new_callable=AsyncMock))
        self.send_saved = self.enterContext(
            patch("bot.send_group_report_saved_message", new_callable=AsyncMock)
        )
        self.send_revision = self.enterContext(
            patch("bot.send_revision_message_saved_message", new_callable=AsyncMock)
        )
        self.send_feedback = self.enterContext(
            patch("bot.send_group_report_feedback_message", new_callable=AsyncMock)
        )

    def make_message(self, text, message_id=101):
        return SimpleNamespace(
            caption=None,
            text=text,
            chat=SimpleNamespace(type="group"),
            chat_id=-1001,
            message_id=message_id,
            from_user=SimpleNamespace(is_bot=False, id=7),
            media_group_id=None,
            photo=None,
            edit_date=None,
        )

    async def test_dated_revision_saves_service_pay_and_inventory_together(self):
        message = self.make_message("04.09 Южный\nКофе - 4.1\nМолоко - 4.5\nВоды - 0.8")
        await bot.process_group_report_message(message, self.application)

        self.send_saved.assert_awaited_once()
        self.send_revision.assert_not_awaited()
        self.assertEqual(len(self.services), 1)
        self.assertEqual(self.services[0]["service_sum"], 250)
        self.assertEqual(self.services[0]["salary_workers"], ["Александр"])
        self.assertEqual(self.services[0]["water"], "0,8")
        self.assertEqual(self.services[0]["date"], "04.09.2026")
        self.assertEqual(self.revisions[0]["period"], "09.2026")
        self.assertEqual(self.revisions[0]["values"]["Кофе"], "4,1")
        self.assertEqual(self.logs[0]["service_row"], 2)
        self.assertEqual(self.logs[0]["revision_row"], 2)

    async def test_multi_point_revision_routes_to_batch(self):
        message = self.make_message(REVISION_PACKAGE)
        application = SimpleNamespace(bot_data={})

        with (
            patch(
                "bot.now_local",
                return_value=datetime(2026, 9, 6, 12, 0, tzinfo=bot.BOT_TIMEZONE),
            ),
            patch(
                "bot.process_revision_snapshot_batch",
                new_callable=AsyncMock,
            ) as process_batch,
            patch("bot.parse_service_report_message_text") as service_parser,
        ):
            await bot.process_group_report_message(message, application)

        service_parser.assert_not_called()
        process_batch.assert_awaited_once()
        snapshots = process_batch.await_args.args[2]
        self.assertEqual(len(snapshots), 5)

    async def test_batch_saves_each_unique_point_with_its_own_source_key(self):
        message = self.make_message(REVISION_PACKAGE)
        await bot.process_group_report_message(message, self.application)

        self.assertEqual(len(self.services), 5)
        self.assertEqual(len(self.revisions), 5)
        self.assertEqual(sum(service["service_sum"] for service in self.services), 1250)
        self.assertEqual(len({record["source_key"] for record in self.logs}), 5)
        self.assertTrue(all(revision["period"] == "09.2026" for revision in self.revisions))
        self.assertTrue(
            all(record["service_row"] and record["revision_row"] for record in self.logs)
        )
        result_text = self.send_feedback.await_args.args[3]
        self.assertIn("Сохранено/обновлено: 5", result_text)
        self.assertEqual(result_text.count("обслуживание и ревизия сохранены"), 5)

        # Retries of this message and a separately resent copy must not pay twice.
        await bot.process_group_report_message(message, self.application)
        await bot.process_group_report_message(
            self.make_message(REVISION_PACKAGE, 102), self.application
        )
        self.assertEqual(len(self.services), 5)
        self.assertEqual(len(self.revisions), 5)

    async def test_batch_purchase_belongs_only_to_its_point(self):
        message = self.make_message(
            "04.09 Южный\nКофе - 4\nМолоко - 5\nВоды - 2.2\n(Купил 2 бака 280₽)\n\n"
            "04.09 Сити\nКофе - 6\nМолоко - 4\nВоды - 2.5"
        )
        await bot.process_group_report_message(message, self.application)
        self.assertEqual([service["purchase_sum"] for service in self.services], [280, 0])

    async def test_warehouse_revision_does_not_create_service_or_salary(self):
        message = self.make_message("/Дома\nКофе - 4\nМолоко - 5\nВоды - 2")
        await bot.process_group_report_message(message, self.application)
        self.assertEqual(self.services, [])
        self.assertEqual(self.revisions[0]["location"], "Дома")
        self.assertEqual(self.logs[0]["service_row"], "")
        self.send_revision.assert_awaited_once()

    async def test_slash_revision_without_water_still_records_service(self):
        message = self.make_message("/Сити\nКофе: 6\nМолоко = 4,3\nСироп — 2.5")
        await bot.process_group_report_message(message, self.application)
        self.assertEqual(self.services[0]["water"], "")
        self.assertEqual(self.services[0]["service_sum"], 250)
        self.assertEqual(self.revisions[0]["values"]["Сиропы"], "2,5")

    async def test_batch_checks_existing_service_before_new_salary(self):
        self.semantic_duplicates.return_value = [{"__row": 345}]
        await bot.process_group_report_message(
            self.make_message(REVISION_PACKAGE), self.application
        )
        self.assertEqual(self.services, [])
        self.assertEqual(self.revisions, [])
        self.assertEqual(len(bot.get_group_report_drafts(self.application.bot_data)), 5)
        self.assertIn("нужно проверить возможный повтор", self.send_feedback.await_args.args[3])

    async def test_edit_of_combined_report_uses_linked_service_update(self):
        message = self.make_message("04.09 Южный\nКофе - 4\nМолоко - 5\nВоды - 2")
        await bot.process_group_report_message(message, self.application)
        message.text = message.text.replace("Кофе - 4", "Кофе - 6")
        message.edit_date = datetime(2026, 9, 6)
        with patch(
            "bot.update_group_report_entry_from_edit", return_value={"service_row": 2}
        ) as update:
            await bot.process_group_report_message(message, self.application)
        update.assert_called_once()
        self.assertEqual(update.call_args.args[0]["revision"]["values"]["Кофе"], "6")
        self.assertEqual(len(self.services), 1)

    def test_legacy_snapshot_edit_does_not_add_historical_salary(self):
        draft = {
            "point": "Южный",
            "revision": {"period": "09.2026", "values": {"Кофе": "6"}},
        }
        record = {"Service_Row": "", "Revision_Row": "43"}
        with (
            patch(
                "bot.update_revision_message_entry_from_edit",
                return_value={
                    "service_row": "",
                    "warnings": [],
                },
            ) as update_revision,
            patch("bot.update_group_report_entry_from_edit") as update_service,
        ):
            result = bot.update_group_service_or_legacy_revision(draft, record)
        update_revision.assert_called_once()
        self.assertEqual(update_revision.call_args.args[0]["values"], {"Кофе": "6"})
        update_service.assert_not_called()
        self.assertIn("не добавлены автоматически", result["warnings"][0])

    async def test_bdr_full_package_records_august_without_changing_service_date_or_pay(self):
        from test_bdr_revision import FakeBdrClient

        client = FakeBdrClient()
        with (
            patch("bot.BDR_SPREADSHEET_ID", "bdr"),
            patch("bot.get_sheet", return_value=SimpleNamespace(client=client)),
        ):
            message = self.make_message(REVISION_PACKAGE)
            await bot.process_group_report_message(message, self.application)
            await bot.process_group_report_message(message, self.application)
        self.assertEqual(len(self.revisions), 5)
        self.assertTrue(all(row["period"] == "08.2026" for row in self.revisions))
        self.assertTrue(all(row["date"] == "04.09.2026" for row in self.services))
        self.assertEqual(sum(row["service_sum"] for row in self.services), 1250)
        self.assertEqual(sum(len(batch["requests"]) for batch in client.writes), 75)
        self.assertEqual(client.rows[92]["values"][3]["userEnteredValue"]["numberValue"], 0.4)
        self.assertEqual(client.rows[92]["values"][4]["userEnteredValue"]["numberValue"], 0.08)
        self.assertEqual(client.rows[79]["values"][7], {})  # Maxi was not supplied.
        self.assertIn("formulaValue", client.rows[79]["values"][9]["userEnteredValue"])
        self.assertTrue(all('"bdr"' in row["revision_backup"] for row in self.logs))

    async def test_resending_pending_bdr_revision_reuses_service_and_original_log(self):
        from test_bdr_revision import FakeBdrClient

        client = FakeBdrClient()
        client.fail_write = True
        text = "04.09 Сити\nКофе - 6\nМолоко - 4\nВоды - 2.5"
        with (
            patch("bot.BDR_SPREADSHEET_ID", "bdr"),
            patch("bot.get_sheet", return_value=SimpleNamespace(client=client)),
        ):
            await bot.process_group_report_message(self.make_message(text), self.application)
            self.assertEqual(self.logs[0]["revision_mode"], "pending_bdr")
            self.assertEqual(self.revisions, [])
            client.fail_write = False
            with (
                patch(
                    "bot.find_group_report_service_entry",
                    return_value={
                        "__row": 2,
                        "Кто": "Александр",
                        "В ЗП": "Александр",
                    },
                ),
                patch("bot.update_service_row") as update_service,
                patch("bot.find_group_report_revision_entry", return_value=None),
                patch(
                    "bot.update_group_report_log",
                    side_effect=lambda row, payload: self.logs.__setitem__(row - 2, payload),
                ),
            ):
                await bot.process_group_report_message(
                    self.make_message(text, 102), self.application
                )
        self.assertEqual(len(self.services), 1)
        self.assertEqual(len(self.logs), 1)
        self.assertEqual(len(self.revisions), 1)
        self.assertEqual(self.logs[0]["revision_period"], "08.2026")
        self.assertEqual(self.logs[0]["source_key"], "msg:101")
        self.assertEqual(self.logs[0]["revision_mode"], "created")
        self.assertEqual(update_service.call_args.args[1]["service_sum"], 250)

    async def test_intentionally_removed_revision_is_not_treated_as_pending(self):
        message = self.make_message("04.09 Сити\nКофе - 6\nМолоко - 4\nВоды - 2.5")
        await bot.process_group_report_message(message, self.application)
        self.logs[0]["revision_row"] = ""
        self.logs[0]["revision_mode"] = ""
        await bot.process_group_report_message(message, self.application)
        self.assertEqual(len(self.revisions), 1)

    async def test_invalid_or_conflicting_dates_are_not_replaced_with_today(self):
        for text in (
            "31.09 Сити\nКофе - 6\nМолоко - 4\nВоды - 2.5",
            "04.09 Сити\nКофе - 6\nМолоко - 4\nВоды - 2.5\n"
            "04.08 Сити\nКофе - 5\nМолоко - 3\nВоды - 2",
        ):
            await bot.process_group_report_message(self.make_message(text), self.application)
        self.assertEqual(self.services, [])
        self.assertEqual(self.revisions, [])
        self.assertIn("уточните дату", self.send_feedback.await_args.args[3])

    def test_edit_preserves_explicitly_unpaid_service(self):
        draft = {
            "date": "04.09.2026",
            "who": "Александр",
            "point": "Сити",
            "chat_id": -1001,
            "source_key": "msg:101",
            "source_message_id": 101,
        }
        with (
            patch(
                "bot.find_group_report_service_entry",
                return_value={
                    "__row": 2,
                    "Кто": "Александр",
                    "В ЗП": "нет",
                },
            ),
            patch("bot.update_service_row") as update,
            patch("bot.find_group_report_revision_entry", return_value=None),
            patch("bot.update_group_report_log"),
        ):
            bot.update_group_report_entry_from_edit(draft, {"__row": 2})
        self.assertEqual(update.call_args.args[1]["service_sum"], 0)
        self.assertEqual(update.call_args.args[1]["salary_workers"], [])

    def test_batch_does_not_claim_inventory_saved_when_only_service_was_saved(self):
        text = bot.build_revision_snapshot_batch_result_text(
            [
                {
                    "status": "saved",
                    "draft": {
                        "point": "Сити",
                        "period": "08.2026",
                        "revision": {"values": {"Кофе": "6"}},
                    },
                    "save_result": {"service_row": 348, "revision": None},
                }
            ]
        )
        self.assertTrue(text.startswith("⚠️"))
        self.assertIn("Сохранено/обновлено: 0", text)
        self.assertIn("обслуживание сохранено, ревизия не сохранена", text)


if __name__ == "__main__":
    unittest.main()
