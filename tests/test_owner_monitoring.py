import asyncio
from functools import wraps
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import Forbidden, RetryAfter, TimedOut

from owner_monitoring import MonitorStore, deliver_pending, recipient_id


def run_async(test):
    @wraps(test)
    def run(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))
    return run


def response(kind="new", stamp="2026-09-13T15:00:00+03:00"):
    return {"version": "1", "states": {"city": {"primary_problem": None if kind == "recovery" else "offline", "last_event_at": stamp}},
            "events": [{"point_code": "city", "kind": kind, "problem": "offline", "occurred_at": stamp, "text": f"{kind} Vendista"}]}


def record(store, kind="new", stamp="2026-09-13T15:00:00+03:00"):
    version, _ = store.cursor()
    store.record(version, response(kind, stamp))


def test_durable_cursor_dedup_and_concurrent_claim(tmp_path):
    path = tmp_path / "monitor.sqlite3"
    store = MonitorStore(path, 123)
    record(store)
    restarted = MonitorStore(path, 123)
    assert restarted.cursor()[1]["city"]["primary_problem"] == "offline"
    with pytest.raises(ValueError, match="concurrently"):
        restarted.record(0, response())
    record(restarted)
    item = store.next_delivery()
    assert store.claim(item["seq"])
    assert not restarted.claim(item["seq"])
    assert store.next_delivery() is None
    restarted = MonitorStore(path, 123)
    assert restarted.next_delivery() is None
    assert restarted.uncertain_problem(item["event"])


@run_async
async def test_new_message_repeat_edit_and_recovery_message(tmp_path):
    store = MonitorStore(tmp_path / "monitor.sqlite3", 123)
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)), edit_message_text=AsyncMock())
    record(store)
    await deliver_pending(bot, store)
    store = MonitorStore(store.path, 123)
    record(store, "repeat", "2026-09-13T16:00:00+03:00")
    await deliver_pending(bot, store)
    bot.edit_message_text.assert_awaited_once_with(chat_id=123, message_id=42, text="repeat Vendista")
    record(store, "recovery", "2026-09-13T16:05:00+03:00")
    await deliver_pending(bot, store)
    assert bot.send_message.await_count == 2
    assert store.card(response()["events"][0]) is None
    assert store.next_delivery() is None


@run_async
async def test_uncertain_send_is_not_repeated_after_restart_or_hourly_event(tmp_path):
    store = MonitorStore(tmp_path / "monitor.sqlite3", 123)
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=TimedOut()), edit_message_text=AsyncMock())
    record(store)
    await deliver_pending(bot, store)
    store = MonitorStore(store.path, 123)
    await deliver_pending(bot, store)
    record(store, "repeat", "2026-09-13T16:00:00+03:00")
    await deliver_pending(bot, store)
    assert bot.send_message.await_count == 1
    assert store.next_delivery() is None


@run_async
async def test_rate_limit_defers_retry_and_forbidden_is_not_retried(tmp_path):
    store = MonitorStore(tmp_path / "monitor.sqlite3", 123)
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=RetryAfter(60)), edit_message_text=AsyncMock())
    record(store)
    seq = store.next_delivery()["seq"]
    await deliver_pending(bot, store)
    assert store.next_delivery() is None
    store.status(seq, "ready")
    bot.send_message.side_effect = Forbidden("blocked")
    await deliver_pending(bot, store)
    await deliver_pending(bot, store)
    assert bot.send_message.await_count == 2
    assert store.next_delivery() is None


def test_group_destination_rejected_and_owner_cursors_isolated(tmp_path):
    path = tmp_path / "monitor.sqlite3"
    with pytest.raises(ValueError):
        MonitorStore(path, -100)
    first, second = MonitorStore(path, 123), MonitorStore(path, 456)
    record(first)
    assert second.cursor() == (0, {})
    assert second.next_delivery() is None


def test_recipient_must_be_unambiguous_existing_owner():
    assert recipient_id("", {123}) == 123
    assert recipient_id("", {123, 456}) is None
    assert recipient_id("456", {123, 456}) == 456
    assert recipient_id("-100", {123}) is None
    assert recipient_id("broken", {123}) is None


@run_async
async def test_obsolete_unsent_problem_is_not_sent_after_recovery(tmp_path):
    store = MonitorStore(tmp_path / "monitor.sqlite3", 123)
    bot = SimpleNamespace(send_message=AsyncMock(), edit_message_text=AsyncMock())
    record(store)
    record(store, "recovery", "2026-09-13T15:05:00+03:00")
    await deliver_pending(bot, store)
    bot.send_message.assert_not_awaited()
    assert store.next_delivery() is None
