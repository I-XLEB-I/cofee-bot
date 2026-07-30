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
        )


if __name__ == "__main__":
    unittest.main()
