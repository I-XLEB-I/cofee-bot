import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import bot

MSK = ZoneInfo("Europe/Moscow")


def point_row(
    name,
    *,
    state="online",
    warnings=None,
    yesterday=10,
    today=4,
    last_sale="2026-07-29T13:40:00+03:00",
    last_payment="2026-07-29T13:45:00+03:00",
):
    return {
        "point_code": f"code-{name}",
        "point_name": name,
        "operational_state": state,
        "warnings": warnings or [],
        "yesterday_sales_count": yesterday,
        "today_sales_count": today,
        "last_sale_at": last_sale,
        "last_successful_payment_at": last_payment,
    }


class OperationsSummaryTests(unittest.TestCase):
    def setUp(self):
        self.reference = datetime(2026, 7, 29, 14, 0, tzinfo=MSK)

    def test_normalizer_keeps_six_active_points_and_excludes_archived(self):
        payload = {
            "observed_at": self.reference.isoformat(),
            "points": [
                point_row(name) for name in bot.ACTIVE_OPERATIONAL_POINTS
            ]
            + [point_row("Бел2")],
        }

        normalized = bot.normalize_operations_digest(payload)

        self.assertEqual(
            [row["point_name"] for row in normalized["points"]],
            list(bot.ACTIVE_OPERATIONAL_POINTS),
        )
        self.assertNotIn(
            "Бел2",
            {row["point_name"] for row in normalized["points"]},
        )
        self.assertFalse(normalized["incomplete_data"])

    def test_missing_point_is_rendered_as_no_data(self):
        normalized = bot.normalize_operations_digest(
            {"points": [point_row("Беломорский")]}
        )

        text = bot.build_operations_notice(
            normalized,
            reference=self.reference,
        )

        self.assertIn("🟢 онлайн · <b>Беломор</b>", text)
        self.assertIn("⚪ нет данных · <b>Гагарина</b>", text)
        self.assertIn("Часть онлайн-данных пока недоступна", text)

    def test_no_sales_warning_does_not_call_point_offline(self):
        payload = {
            "points": [
                point_row(
                    name,
                    warnings=["no_sales"] if name == "Макси" else [],
                )
                for name in bot.ACTIVE_OPERATIONAL_POINTS
            ]
        }

        text = bot.build_operations_notice(
            bot.normalize_operations_digest(payload),
            reference=self.reference,
        )

        self.assertIn(
            "🟡 онлайн, давно без продаж · <b>Макси</b>",
            text,
        )
        self.assertNotIn("🔴 офлайн · <b>Макси</b>", text)

    def test_timestamp_uses_moscow_today_and_yesterday_labels(self):
        self.assertEqual(
            bot.format_operations_timestamp(
                "2026-07-29T10:15:00Z",
                reference=self.reference,
            ),
            "сегодня 13:15",
        )
        self.assertEqual(
            bot.format_operations_timestamp(
                "2026-07-28T22:30:00+03:00",
                reference=self.reference,
            ),
            "вчера 22:30",
        )

    def test_notice_uses_actual_sale_time_not_payment_time(self):
        payload = {
            "points": [
                point_row(
                    name,
                    last_sale="2026-07-29T12:10:00+03:00",
                    last_payment="2026-07-29T13:55:00+03:00",
                )
                for name in bot.ACTIVE_OPERATIONAL_POINTS
            ]
        }

        text = bot.build_operations_notice(
            bot.normalize_operations_digest(payload),
            reference=self.reference,
        )

        self.assertIn("посл. продажа сегодня 12:10", text)
        self.assertNotIn("сегодня 13:55", text)

    def test_provider_failure_keeps_existing_service_text(self):
        service_text = "<b>Обслуживание</b>\nСуществующие данные"
        unavailable = {
            "available": False,
            "incomplete_data": True,
            "points": [],
        }

        combined = bot.append_operations_notice(service_text, unavailable)

        self.assertIn(service_text, combined)
        self.assertIn("Онлайн-данные временно недоступны", combined)

    def test_unconfigured_provider_does_not_change_existing_text(self):
        service_text = "<b>Обслуживание</b>"

        self.assertEqual(
            bot.append_operations_notice(service_text, None),
            service_text,
        )


if __name__ == "__main__":
    unittest.main()
