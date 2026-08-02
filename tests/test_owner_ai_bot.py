import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot


class OwnerAiBotAccessTests(unittest.IsolatedAsyncioTestCase):
    def make_update(self):
        message = SimpleNamespace(
            reply_text=AsyncMock(),
            from_user=SimpleNamespace(id=874403512),
        )
        return SimpleNamespace(
            effective_message=message,
            effective_user=message.from_user,
            effective_chat=SimpleNamespace(
                id=-100123,
                type="supergroup",
            ),
        )

    async def test_allowed_group_requires_explicit_ai_command(self):
        update = self.make_update()
        context = SimpleNamespace(args=["Продажи", "сегодня"])

        with (
            patch.object(bot, "is_allowed_user", return_value=True),
            patch.object(bot, "is_private_chat", return_value=False),
            patch.object(bot, "is_allowed_group_chat", return_value=True),
            patch.object(bot, "owner_ai_api_configured", return_value=True),
            patch.object(
                bot,
                "answer_owner_ai_message",
                new=AsyncMock(),
            ) as answer,
        ):
            result = await bot.cmd_ai(update, context)

        self.assertEqual(result, bot.ConversationHandler.END)
        answer.assert_awaited_once_with(
            update.effective_message,
            context,
            "Продажи сегодня",
        )

    async def test_unknown_group_cannot_reach_ai(self):
        update = self.make_update()
        context = SimpleNamespace(args=["Продажи", "сегодня"])

        with (
            patch.object(bot, "is_allowed_user", return_value=True),
            patch.object(bot, "is_private_chat", return_value=False),
            patch.object(bot, "is_allowed_group_chat", return_value=False),
            patch.object(
                bot,
                "answer_owner_ai_message",
                new=AsyncMock(),
            ) as answer,
        ):
            result = await bot.cmd_ai(update, context)

        self.assertEqual(result, bot.ConversationHandler.END)
        answer.assert_not_awaited()
        update.effective_message.reply_text.assert_awaited_once()

    async def test_ai_request_uses_chat_scoped_conversation_id(self):
        status = SimpleNamespace(edit_text=AsyncMock())
        message = SimpleNamespace(
            reply_text=AsyncMock(return_value=status),
            from_user=SimpleNamespace(id=874403512),
            chat_id=-100123,
        )
        config = SimpleNamespace()

        with (
            patch.object(bot, "get_owner_ai_client_config", return_value=config),
            patch.object(
                bot,
                "owner_ai_question_needs_maintenance_context",
                return_value=False,
            ),
            patch.object(
                bot,
                "run_blocking",
                new=AsyncMock(
                    return_value={
                        "scope": "staff",
                        "answer": "Вчера 2 продажи.",
                    }
                ),
            ) as run,
        ):
            await bot.answer_owner_ai_message(
                message,
                SimpleNamespace(),
                "А вчера?",
            )

        run.assert_awaited_once_with(
            bot.query_owner_ai,
            config,
            user_id=874403512,
            question="А вчера?",
            conversation_id="telegram:-100123:874403512",
            maintenance_context=None,
        )
        edit_call = status.edit_text.await_args
        self.assertEqual(edit_call.kwargs["parse_mode"], "HTML")
        self.assertIs(
            edit_call.kwargs["link_preview_options"],
            bot.NO_LINK_PREVIEW,
        )
        self.assertEqual(
            edit_call.args[0],
            (
                "<b>🤖 ИИ-аналитик · сотрудник</b>"
                "\n\nВчера 2 продажи."
            ),
        )

    def test_owner_ai_html_formatting_is_readable_and_escaped(self):
        rendered = bot.format_owner_ai_answer_html(
            "☕ Популярность напитков\n"
            "Точка: Гиппо & <main>\n"
            "Период: 01.07.2026 — 31.07.2026\n\n"
            "Рейтинг по количеству:\n"
            "1. Капучино — 12 продаж\n"
            "<b>не доверенный HTML</b>"
        )

        self.assertIn("<b>☕ Популярность напитков</b>", rendered)
        self.assertIn(
            "<b>Точка:</b> Гиппо &amp; &lt;main&gt;",
            rendered,
        )
        self.assertIn(
            "<b>Период:</b> 01.07.2026 — 31.07.2026",
            rendered,
        )
        self.assertIn("<b>Рейтинг по количеству:</b>", rendered)
        self.assertIn("1. Капучино — 12 продаж", rendered)
        self.assertIn(
            "&lt;b&gt;не доверенный HTML&lt;/b&gt;",
            rendered,
        )
        self.assertNotIn("<main>", rendered)

    async def test_owner_ai_html_failure_falls_back_to_plain_text(self):
        status = SimpleNamespace(
            edit_text=AsyncMock(
                side_effect=[bot.BadRequest("bad html"), None]
            )
        )
        message = SimpleNamespace(
            reply_text=AsyncMock(return_value=status),
            from_user=SimpleNamespace(id=874403512),
            chat_id=-100123,
        )

        with (
            patch.object(
                bot,
                "get_owner_ai_client_config",
                return_value=SimpleNamespace(),
            ),
            patch.object(
                bot,
                "owner_ai_question_needs_maintenance_context",
                return_value=False,
            ),
            patch.object(
                bot,
                "run_blocking",
                new=AsyncMock(
                    return_value={
                        "scope": "staff",
                        "answer": "Точка: Гиппо <тест>",
                    }
                ),
            ),
        ):
            await bot.answer_owner_ai_message(
                message,
                SimpleNamespace(),
                "Продажи?",
            )

        self.assertEqual(status.edit_text.await_count, 2)
        first, second = status.edit_text.await_args_list
        self.assertEqual(first.kwargs["parse_mode"], "HTML")
        self.assertNotIn("parse_mode", second.kwargs)
        self.assertIn("Точка: Гиппо <тест>", second.args[0])

    async def test_causal_ai_question_includes_service_history(self):
        status = SimpleNamespace(edit_text=AsyncMock())
        message = SimpleNamespace(
            reply_text=AsyncMock(return_value=status),
            from_user=SimpleNamespace(id=1_395_822_345),
            chat_id=1_395_822_345,
        )
        config = SimpleNamespace()
        maintenance = {
            "points": [
                {
                    "point_name": "Сити",
                    "service_dates": ["2026-07-26"],
                }
            ]
        }

        async def fake_run(func, *args, **kwargs):
            if func is bot.build_owner_ai_maintenance_context:
                return maintenance
            self.assertIs(func, bot.query_owner_ai)
            self.assertEqual(kwargs["maintenance_context"], maintenance)
            return {"scope": "owner", "answer": "Это гипотеза."}

        with (
            patch.object(bot, "get_owner_ai_client_config", return_value=config),
            patch.object(bot, "run_blocking", new=fake_run),
        ):
            await bot.answer_owner_ai_message(
                message,
                SimpleNamespace(),
                "Почему у Сити мало продаж?",
            )

        status.edit_text.assert_awaited_once()

    async def test_sheet_failure_does_not_block_causal_ai_question(self):
        status = SimpleNamespace(edit_text=AsyncMock())
        message = SimpleNamespace(
            reply_text=AsyncMock(return_value=status),
            from_user=SimpleNamespace(id=1_395_822_345),
            chat_id=1_395_822_345,
        )

        async def fake_run(func, *args, **kwargs):
            if func is bot.build_owner_ai_maintenance_context:
                raise RuntimeError("Sheets unavailable")
            self.assertIsNone(kwargs["maintenance_context"])
            return {"scope": "owner", "answer": "Причина не подтверждена."}

        with (
            patch.object(
                bot,
                "get_owner_ai_client_config",
                return_value=SimpleNamespace(),
            ),
            patch.object(bot, "run_blocking", new=fake_run),
        ):
            await bot.answer_owner_ai_message(
                message,
                SimpleNamespace(),
                "Почему у Сити мало продаж?",
            )

        status.edit_text.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
