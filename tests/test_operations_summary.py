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
    last_product="Капучино",
    contains_coffee=True,
    no_sales_since="2026-07-29T12:00:00+03:00",
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
        "last_sale_product_name": last_product,
        "last_sale_contains_coffee": contains_coffee,
        "last_sale_scope": "cashless",
        "no_sales_since_at": no_sales_since,
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
        self.assertEqual(
            normalized["points"][0]["last_sale_product_name"],
            "Капучино",
        )
        self.assertIs(
            normalized["points"][0]["last_sale_contains_coffee"],
            True,
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

        self.assertIn("Беломор", text)
        self.assertIn("❔ нет данных: Гагарина", text)
        self.assertIn("❔ <b>Гагарина</b> · нет данных о связи", text)
        self.assertIn("Часть оперативных данных пока недоступна", text)

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

        self.assertIn("🟡 <b>Макси</b> · требуется проверка", text)
        self.assertIn("Продаж нет 2 ч.", text)
        self.assertIn(
            "Пауза в продажах — повод проверить точку, "
            "а не подтверждённая ошибка.",
            text,
        )
        self.assertNotIn("🔴 <b>Макси</b> · нет связи", text)

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

        self.assertIn("12:10", text)
        self.assertNotIn("13:55", text)

    def test_provider_failure_keeps_existing_service_text(self):
        service_text = "<b>Обслуживание</b>\nСуществующие данные"
        unavailable = {
            "available": False,
            "incomplete_data": True,
            "points": [],
        }

        combined = bot.append_operations_notice(service_text, unavailable)

        self.assertIn(service_text, combined)
        self.assertIn("Оперативные данные временно недоступны", combined)

    def test_unconfigured_provider_does_not_change_existing_text(self):
        service_text = "<b>Обслуживание</b>"

        self.assertEqual(
            bot.append_operations_notice(service_text, None),
            service_text,
        )

    def test_compact_table_distinguishes_zero_from_missing_data(self):
        rows = [
            point_row(
                name,
                today=0 if name == "Сити" else None,
            )
            for name in bot.ACTIVE_OPERATIONAL_POINTS
        ]

        text = bot.build_operations_notice(
            bot.normalize_operations_digest({"points": rows}),
            reference=self.reference,
        )

        table = text.split("<pre>", 1)[1].split("</pre>", 1)[0]
        city_line = next(line for line in table.splitlines() if "Сити" in line)
        maxi_line = next(line for line in table.splitlines() if "Макси" in line)
        self.assertIn("  0", city_line)
        self.assertIn("  —", maxi_line)
        self.assertLessEqual(max(len(line) for line in table.splitlines()), 36)

    def test_coffee_free_last_drink_is_only_a_hypothesis(self):
        rows = [
            point_row(
                name,
                warnings=["no_sales"] if name == "Сити" else [],
                last_product=(
                    "Горячий шоколад" if name == "Сити" else "Капучино"
                ),
                contains_coffee=False if name == "Сити" else True,
            )
            for name in bot.ACTIVE_OPERATIONAL_POINTS
        ]

        text = bot.build_operations_notice(
            bot.normalize_operations_digest({"points": rows}),
            reference=self.reference,
        )

        self.assertIn("Горячий шоколад · без кофе", text)
        self.assertIn(
            "Возможна проблема с подачей кофе — нужна проверка.",
            text,
        )
        self.assertIn("не подтверждённая ошибка", text)

    def test_coffee_drink_does_not_claim_a_coffee_failure(self):
        rows = [
            point_row(
                name,
                warnings=["no_sales"] if name == "Гиппо" else [],
                last_product="Моккачино",
                contains_coffee=True,
            )
            for name in bot.ACTIVE_OPERATIONAL_POINTS
        ]

        text = bot.build_operations_notice(
            bot.normalize_operations_digest({"points": rows}),
            reference=self.reference,
        )

        self.assertIn("Моккачино · с кофе", text)
        self.assertIn(
            "отдельного признака сбоя подачи кофе нет",
            text,
        )
        self.assertNotIn(
            "Возможна проблема с подачей кофе",
            text,
        )

    def test_combined_notice_stays_within_telegram_text_limit(self):
        service_text = "<b>Обслуживание</b>\n" + ("Данные\n" * 200)
        rows = [
            point_row(name, warnings=["no_sales"])
            for name in bot.ACTIVE_OPERATIONAL_POINTS
        ]
        digest = bot.normalize_operations_digest({"points": rows})

        combined = bot.append_operations_notice(
            service_text,
            digest,
            reference=self.reference,
        )

        self.assertLessEqual(len(combined), 4096)


if __name__ == "__main__":
    unittest.main()
