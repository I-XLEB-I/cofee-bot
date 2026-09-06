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

    async def test_dated_revision_routes_to_revision_only_save(self):
        message = self.make_message(
            "04.09 Южный\nКофе - 4.1\nМолоко - 4.5\nВоды - 0.8"
        )
        application = SimpleNamespace(bot_data={})
        saved_drafts = []

        async def fake_run_blocking(callable_value, *args):
            if callable_value is bot.find_group_report_duplicate:
                return None, None
            if callable_value is bot.save_revision_message_entry:
                saved_drafts.append(args[0])
                return {
                    "log_row": 1,
                    "service_row": "",
                    "who": "Александр",
                    "revision": {
                        "period": "09.2026",
                        "location": "Южный",
                    },
                    "warnings": [],
                }
            raise AssertionError(f"Unexpected blocking call: {callable_value}")

        with (
            patch(
                "bot.now_local",
                return_value=datetime(2026, 9, 6, 12, 0, tzinfo=bot.BOT_TIMEZONE),
            ),
            patch("bot.get_service_report_author", return_value="Александр"),
            patch("bot.GROUP_REPORT_SAVE_MIN_INTERVAL_SECONDS", 0),
            patch("bot.run_blocking", side_effect=fake_run_blocking),
            patch("bot.parse_service_report_message_text") as service_parser,
            patch(
                "bot.send_revision_message_saved_message",
                new_callable=AsyncMock,
            ) as send_saved,
        ):
            await bot.process_group_report_message(message, application)

        service_parser.assert_not_called()
        send_saved.assert_awaited_once()
        self.assertEqual(len(saved_drafts), 1)
        self.assertEqual(saved_drafts[0]["period"], "09.2026")
        self.assertEqual(saved_drafts[0]["date"], "04.09.2026")

    async def test_multi_point_revision_routes_to_batch_without_service(self):
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
        application = SimpleNamespace(bot_data={})
        with patch(
            "bot.now_local",
            return_value=datetime(2026, 9, 6, 12, 0, tzinfo=bot.BOT_TIMEZONE),
        ):
            snapshots = bot.parse_revision_snapshot_messages_text(REVISION_PACKAGE)
        saved_drafts = []

        async def fake_run_blocking(callable_value, *args):
            if callable_value is bot.find_group_report_duplicate:
                return None, None
            if callable_value is bot.save_revision_message_entry:
                draft = args[0]
                saved_drafts.append(draft)
                return {
                    "log_row": len(saved_drafts),
                    "service_row": "",
                    "who": "Александр",
                    "revision": {
                        "period": draft["period"],
                        "location": draft["point"],
                    },
                    "warnings": [],
                }
            raise AssertionError(f"Unexpected blocking call: {callable_value}")

        with (
            patch(
                "bot.now_local",
                return_value=datetime(2026, 9, 6, 12, 0, tzinfo=bot.BOT_TIMEZONE),
            ),
            patch("bot.get_service_report_author", return_value="Александр"),
            patch("bot.GROUP_REPORT_SAVE_MIN_INTERVAL_SECONDS", 0),
            patch("bot.run_blocking", side_effect=fake_run_blocking),
            patch(
                "bot.send_group_report_feedback_message",
                new_callable=AsyncMock,
            ) as send_feedback,
        ):
            await bot.process_revision_snapshot_batch(message, application, snapshots)

        self.assertEqual(len(saved_drafts), 5)
        self.assertEqual(len({draft["source_key"] for draft in saved_drafts}), 5)
        self.assertTrue(all(draft["period"] == "09.2026" for draft in saved_drafts))
        result_text = send_feedback.await_args.args[3]
        self.assertIn("Сохранено/обновлено: 5", result_text)


if __name__ == "__main__":
    unittest.main()
