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


if __name__ == "__main__":
    unittest.main()
