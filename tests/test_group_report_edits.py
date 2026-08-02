import json
import unittest
from unittest.mock import Mock, patch

import bot


def revision_record(row, point, **values):
    record = {
        "__row": row,
        "Период": "07.2026",
        "Локация": point,
        "Кто": "Александр",
        "Дата заполнения": "31.07.2026",
    }
    record.update({item: "" for item in bot.REVISION_ITEMS})
    record.update(values)
    return record


class EditedRevisionTests(unittest.TestCase):
    def setUp(self):
        self.old_gippo = revision_record(31, "Гиппо", Кофе="6", Молоко="7")
        self.wrong_gippo = revision_record(31, "Гиппо", Кофе="7", Молоко="6,5")
        self.old_maxi = revision_record(32, "Макси", Кофе="6", Молоко="6")
        self.record = {
            "Revision_Row": "31",
            "Revision_Period": "07.2026",
            "Revision_Location": "Гиппо",
            "Revision_Mode": "updated",
            "Revision_Backup": bot.build_group_report_revision_backup(self.old_gippo),
        }
        self.draft = {
            "who": "Александр",
            "date": "31.07.2026",
            "point": "Макси",
        }
        self.revision = {
            "period": "07.2026",
            "location": "Макси",
            "values": {"Кофе": "7", "Молоко": "6,5", "Вода": "1,9"},
        }

    def test_point_edit_restores_source_and_updates_target(self):
        writes = []

        with (
            patch.object(
                bot,
                "find_group_report_revision_entry",
                return_value=self.wrong_gippo,
            ),
            patch.object(bot, "find_revision_record", return_value=self.old_maxi),
            patch.object(
                bot,
                "run_group_sheet_write_blocking_with_retry",
                side_effect=lambda operation, *_args, **_kwargs: operation(),
            ),
            patch.object(
                bot,
                "update_revision_row",
                side_effect=lambda row, payload: writes.append((row, payload)),
            ),
            patch.object(bot, "add_revision_row") as add_row,
            patch.object(bot, "clear_revision_row") as clear_row,
        ):
            result = bot.save_edited_revision_entry(
                self.draft,
                self.record,
                self.revision,
            )

        self.assertEqual([row for row, _payload in writes], [31, 32])
        restored = writes[0][1]
        self.assertEqual(restored["location"], "Гиппо")
        self.assertEqual(restored["values"]["Кофе"], "6")
        self.assertEqual(restored["values"]["Молоко"], "7")

        updated = writes[1][1]
        self.assertEqual(updated["location"], "Макси")
        self.assertEqual(updated["filled_at"], "31.07.2026")
        self.assertEqual(updated["values"]["Кофе"], "7")
        self.assertEqual(updated["values"]["Вода"], "1,9")

        self.assertEqual(result["row"], 32)
        self.assertEqual(result["mode"], "updated")
        self.assertTrue(result["relocated"])
        self.assertEqual(
            json.loads(result["backup"])["values"]["Кофе"],
            "6",
        )
        add_row.assert_not_called()
        clear_row.assert_not_called()

    def test_created_source_is_cleared_without_shifting_rows(self):
        record = {
            **self.record,
            "Revision_Mode": "created",
            "Revision_Backup": "",
        }

        with (
            patch.object(
                bot,
                "find_group_report_revision_entry",
                return_value=self.wrong_gippo,
            ),
            patch.object(bot, "find_revision_record", return_value=self.old_maxi),
            patch.object(
                bot,
                "run_group_sheet_write_blocking_with_retry",
                side_effect=lambda operation, *_args, **_kwargs: operation(),
            ),
            patch.object(bot, "clear_revision_row") as clear_row,
            patch.object(bot, "update_revision_row"),
        ):
            result = bot.save_edited_revision_entry(
                self.draft,
                record,
                self.revision,
            )

        clear_row.assert_called_once_with(31)
        self.assertEqual(result["row"], 32)
        self.assertEqual(result["mode"], "updated")

    def test_same_point_edit_keeps_original_backup(self):
        record = dict(self.record)
        draft = {**self.draft, "point": "Гиппо"}
        revision = {
            **self.revision,
            "location": "Гиппо",
            "values": {"Кофе": "5,5"},
        }

        with (
            patch.object(
                bot,
                "find_group_report_revision_entry",
                return_value=self.wrong_gippo,
            ),
            patch.object(
                bot,
                "run_group_sheet_write_blocking_with_retry",
                side_effect=lambda operation, *_args, **_kwargs: operation(),
            ),
            patch.object(bot, "update_revision_row") as update_row,
            patch.object(bot, "find_revision_record") as find_target,
            patch.object(bot, "clear_revision_row") as clear_row,
        ):
            result = bot.save_edited_revision_entry(draft, record, revision)

        update_row.assert_called_once()
        self.assertEqual(update_row.call_args.args[0], 31)
        self.assertEqual(result["backup"], record["Revision_Backup"])
        self.assertFalse(result["relocated"])
        find_target.assert_not_called()
        clear_row.assert_not_called()


class EditedMessageRegistrationTests(unittest.TestCase):
    def test_edited_messages_are_registered_before_conversations(self):
        application = Mock()

        bot.register_group_report_message_handlers(application)

        self.assertEqual(application.add_handler.call_count, 2)
        first_call, second_call = application.add_handler.call_args_list
        self.assertEqual(first_call.kwargs, {"group": -1})
        self.assertEqual(second_call.kwargs, {})
        self.assertIsInstance(first_call.args[0], bot.MessageHandler)
        self.assertIsInstance(second_call.args[0], bot.MessageHandler)
        self.assertIs(
            first_call.args[0].callback,
            bot.edited_group_report_message_handler,
        )
        self.assertIs(
            second_call.args[0].callback,
            bot.group_report_message_handler,
        )


if __name__ == "__main__":
    unittest.main()
