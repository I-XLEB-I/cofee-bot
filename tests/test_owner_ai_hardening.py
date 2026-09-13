import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import bot
from owner_ai_client import OwnerAiClientConfig, OwnerAiInputError, query_owner_ai
from owner_ai_queue import OwnerAiWorkQueue


class AiReadOnlyTests(unittest.TestCase):
    def test_changed_column_order_is_read_without_repairing_headers(self):
        sheet = Mock()
        sheet.get_all_values.return_value = [
            ["Комментарий", "Точка", "Дата"],
            ["not sent", "Сити", "12.09.2026"],
            ["", "Сити", "11.09.2026"],
        ]
        book = Mock()
        book.worksheet.return_value = sheet
        with patch.object(bot, "get_owner_ai_readonly_book", return_value=book):
            result = bot.build_owner_ai_maintenance_context()
        book.worksheet.assert_called_once_with("Обслуживание")
        self.assertEqual([c[0] for c in sheet.mock_calls], ["get_all_values"])
        city = next(p for p in result["points"] if p["point_name"] == "Сити")
        self.assertEqual(city["service_dates"], ["2026-09-12", "2026-09-11"])
        self.assertNotIn("not sent", str(result))

    def test_missing_or_ambiguous_headers_never_trigger_a_write(self):
        for headers in [[], ["Дата", "Точка", "Дата"], ["Точка", "Когда"]]:
            with self.subTest(headers=headers):
                sheet = Mock()
                sheet.get_all_values.return_value = [headers]
                book = Mock()
                book.worksheet.return_value = sheet
                with patch.object(bot, "get_owner_ai_readonly_book", return_value=book):
                    with self.assertRaises(ValueError):
                        bot.build_owner_ai_maintenance_context()
                self.assertEqual([c[0] for c in sheet.mock_calls], ["get_all_values"])

    def test_ai_google_session_has_readonly_scope_and_its_own_timeout(self):
        client = Mock()
        bot.get_owner_ai_readonly_book.cache_clear()
        try:
            with patch.object(bot, "SPREADSHEET_ID", "test-sheet"), \
                    patch.object(bot, "get_google_credentials") as credentials, \
                    patch.object(bot.gspread, "authorize", return_value=client):
                bot.get_owner_ai_readonly_book()
            credentials.assert_called_once_with([
                "https://www.googleapis.com/auth/spreadsheets.readonly"
            ])
            client.set_timeout.assert_called_once_with((3, 5))
            client.open_by_key.assert_called_once_with("test-sheet")
        finally:
            bot.get_owner_ai_readonly_book.cache_clear()


class AiPayloadTests(unittest.TestCase):
    def test_long_russian_question_and_history_fit_the_wire_contract(self):
        captured = {}

        def fake_open(request, timeout):
            captured["body"] = request.data
            response = Mock(status=200)
            response.read.return_value = b'{"version":"1","scope":"staff","answer":"ok"}'
            context = Mock()
            context.__enter__ = Mock(return_value=response)
            context.__exit__ = Mock(return_value=False)
            return context

        query_owner_ai(
            OwnerAiClientConfig(url="https://example.test/query", token="x" * 32),
            user_id=1, question="а" * 1200, reply_context="б" * 800,
            audience="group", conversation_id="telegram:-100:1",
            maintenance_context={"points": [
                {"point_name": name, "service_dates": ["2026-09-01"] * 12}
                for name in bot.ACTIVE_OPERATIONAL_POINTS
            ]}, urlopen=fake_open,
        )
        self.assertGreater(len(captured["body"]), 4096)
        self.assertLessEqual(len(captured["body"]), 16384)
        self.assertEqual(json.loads(captured["body"])["audience"], "group")

    def test_overflow_is_reported_before_network(self):
        network = Mock()
        with self.assertRaises(OwnerAiInputError):
            query_owner_ai(
                OwnerAiClientConfig(
                    url="https://example.test/query", token="x" * 32,
                    max_request_bytes=100,
                ), user_id=1, question="я" * 200, urlopen=network,
            )
        network.assert_not_called()


class AiWorkerTests(unittest.IsolatedAsyncioTestCase):
    def make_message(self, chat_id=-100):
        status = SimpleNamespace(edit_text=AsyncMock())
        return SimpleNamespace(
            reply_text=AsyncMock(return_value=status),
            from_user=SimpleNamespace(id=7), chat_id=chat_id,
        ), status

    async def test_group_never_publishes_owner_response_even_with_old_backend(self):
        message, status = self.make_message()

        async def run(func, *args, **kwargs):
            if func is bot.build_owner_ai_maintenance_context:
                return {"points": []}
            self.assertEqual(kwargs["audience"], "group")
            return {"scope": "owner", "answer": "PRIVATE_PAYROLL"}

        with patch.object(bot, "get_owner_ai_client_config", return_value=Mock()), \
                patch.object(bot, "run_blocking", new=run):
            await bot.answer_owner_ai_message(message, SimpleNamespace(), "Зарплата?")
        self.assertNotIn("PRIVATE_PAYROLL", str(status.edit_text.await_args_list))
        self.assertIn("личном чате", status.edit_text.await_args.args[0])

    async def test_slow_sheet_read_times_out_and_sales_still_answer(self):
        message, status = self.make_message(chat_id=7)

        async def run(func, *args, **kwargs):
            if func is bot.build_owner_ai_maintenance_context:
                await asyncio.Event().wait()
            self.assertIsNone(kwargs["maintenance_context"])
            return {"scope": "owner", "answer": "Продажи: 6"}

        with patch.object(bot, "get_owner_ai_client_config", return_value=Mock()), \
                patch.object(bot, "run_blocking", new=run), \
                patch.object(bot, "OWNER_AI_MAINTENANCE_TIMEOUT_SECONDS", .01):
            await asyncio.wait_for(
                bot.answer_owner_ai_message(message, SimpleNamespace(), "Продажи?"), .5
            )
        self.assertIn("<b>Продажи:</b> 6", status.edit_text.await_args.args[0])

    async def test_telegram_handler_finishes_while_ai_is_still_working(self):
        message, status = self.make_message()
        release, started = asyncio.Event(), asyncio.Event()
        workers = []

        async def slow_answer(*args, **kwargs):
            started.set()
            await release.wait()

        def create_task(coro):
            task = asyncio.create_task(coro)
            workers.append(task)
            return task

        context = SimpleNamespace(application=SimpleNamespace(create_task=create_task))
        with patch.object(bot, "owner_ai_api_configured", return_value=True), \
                patch.object(bot, "OWNER_AI_WORK_QUEUE", OwnerAiWorkQueue()), \
                patch.object(bot, "answer_owner_ai_message", new=slow_answer):
            await asyncio.wait_for(bot.enqueue_owner_ai_message(message, context, "Вопрос"), .5)
            await asyncio.wait_for(started.wait(), .5)
            self.assertFalse(workers[0].done())
            release.set()
            await asyncio.gather(*workers)
        message.reply_text.assert_awaited_once()

    async def test_queue_preserves_order_and_bounds_pending_work(self):
        queue = OwnerAiWorkQueue(capacity=3, per_conversation=2)
        release = asyncio.Event()
        events, workers = [], []

        def create_task(coro):
            task = asyncio.create_task(coro)
            workers.append(task)
            return task

        async def first():
            events.append("start")
            await release.wait()
            events.append("finish")

        async def second():
            events.append("second")

        expired = AsyncMock()
        self.assertTrue(queue.submit("a", first, expired, create_task=create_task))
        self.assertTrue(queue.submit("a", second, expired, create_task=create_task))
        self.assertFalse(queue.submit("a", second, expired, create_task=create_task))
        self.assertTrue(queue.submit("b", second, expired, create_task=create_task))
        self.assertFalse(queue.submit("c", second, expired, create_task=create_task))
        release.set()
        await asyncio.gather(*workers)
        self.assertEqual(events, ["start", "finish", "second", "second"])
        expired.assert_not_awaited()
        self.assertTrue(queue.submit("a", second, expired, create_task=create_task))
        await asyncio.gather(*workers)

    async def test_expired_work_does_not_call_provider(self):
        queue = OwnerAiWorkQueue(max_wait_seconds=0)
        operation, expired = AsyncMock(), AsyncMock()
        tasks = []

        def create_task(coro):
            task = asyncio.create_task(coro)
            tasks.append(task)
            return task

        with patch("owner_ai_queue.monotonic", return_value=1):
            queue.submit("a", operation, expired, create_task=create_task)
        with patch("owner_ai_queue.monotonic", return_value=2):
            await asyncio.gather(*tasks)
        operation.assert_not_awaited()
        expired.assert_awaited_once()
