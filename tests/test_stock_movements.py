import asyncio
import copy
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import bot
import stock_movement_bot as adapter
import stock_movements as stock
from revision_dialogue import RevisionConflict
from revision_sheet_journal import entered
from test_revision_dialogue import FakeSheets

EXAMPLE = "Макси обслужил. Довёз кофе 2 пачки, стаканы 100. Купил салфетки 2 упаковки за 180 ₽."


def test_combined_report_preserves_unknown_source_and_purchase_total():
    result = stock.parse(EXAMPLE, bot, "14.09.2026")
    assert result["remainder"] == "Макси обслужил"
    assert [x["kind"] for x in result["items"]] == ["delivery", "delivery", "purchase"]
    assert [x["balance_quantity"] for x in result["items"]] == ["2", "100", None]
    assert result["items"][0]["source"] == "не указан"
    assert result["items"][2]["purchase_total_rub"] == "180"


@pytest.mark.parametrize("text", ["Макси довез кофе", "Макси довез кофе -2", "Макси довез кофе 0",
    "Макси довез кофе 2, стаканы", "Макси довез кофе 2 и неизвестный товар 3",
    "Макси и Сити довез кофе 2", "Макси не довез кофе 2", "Завтра Макси довез кофе 2",
    "31.09 Макси довез кофе 2", "15.09 Макси довез кофе 2", "Макси довез стаканы 0.5"])
def test_ambiguous_or_incomplete_report_never_partially_writes(text):
    result = stock.parse(text, bot, "14.09.2026")
    assert result["error"] and result["items"] == []


@pytest.mark.parametrize("text", ["Сколько кофе довёз на Макси?", "Ревизия Макси\nКофе - 2",
    "04.09 Южный\nКофе - 4\nВоды - 2\n(Купил 2 бака 280₽)\n04.09 Сити\nКофе - 6"])
def test_questions_and_existing_revision_water_reports_keep_their_route(text):
    assert stock.parse(text, bot, "14.09.2026") is None


def test_dates_units_and_explicit_source():
    result = stock.parse("Вчера Макси довез из дома кофе 0.25 кг, стаканы 2 упаковки", bot, "14.09.2026")
    assert result["date"] == "2026-09-13"
    assert result["items"][0]["source"] == "Дома"
    assert result["items"][0]["balance_quantity"] == "0.25"
    assert result["items"][1]["balance_quantity"] is None


@pytest.fixture
def ledger(tmp_path):
    client = FakeSheets()
    client.books["ledger"][4] = {"properties": {"sheetId": 4, "title": stock.SHEET},
        "data": [{"rowData": [{"values": [{"userEnteredValue": entered(v)} for v in stock.HEADERS]}]}]}
    def get(*args, **kwargs):
        return [[next(iter(cell.get("userEnteredValue", {}).values()), "") for cell in row["values"]]
                for row in client.books["ledger"][4]["data"][0]["rowData"]]
    book = SimpleNamespace(id="ledger", client=client, worksheet=lambda title: SimpleNamespace(id=4, get=get))
    payload = {"source_key": "telegram:1:2", "date": "2026-09-14", "point": "Макси", "who": "Worker",
               "user_id": 1, "text": EXAMPLE, "items": stock.parse(EXAMPLE, bot, "14.09.2026")["items"]}
    return stock.MovementStore(tmp_path / "movements.sqlite3"), book, payload


def test_restart_retry_edit_cancel_touch_only_movement_row(ledger):
    store, book, payload = ledger
    before = copy.deepcopy(book.client.books)
    book.client.fail_after = "ledger"
    with pytest.raises(TimeoutError):
        store.save(book, payload)
    assert len(book.client.calls) == 1
    restarted = stock.MovementStore(store.path)
    assert restarted.save(book, payload) == {"version": 1, "duplicate": True}
    assert len(book.client.calls) == 1
    payload["items"][0]["quantity"] = "3"
    payload["items"][0]["balance_quantity"] = "3"
    assert restarted.save(book, payload)["version"] == 2
    assert len(book.worksheet(stock.SHEET).get()) == 2
    with pytest.raises(RevisionConflict):
        restarted.save(book, {**payload, "status": "cancelled"}, expected_version=1)
    assert restarted.save(book, {**payload, "status": "cancelled"}, expected_version=2)["version"] == 3
    assert book.worksheet(stock.SHEET).get()[1][4] == "Отменено"
    for sheet_id in (1, 2, 3):
        assert before["ledger"][sheet_id] == book.client.books["ledger"][sheet_id]
    assert before["bdr"] == book.client.books["bdr"]


def test_pending_write_before_send_is_resumed_once(ledger):
    store, book, payload = ledger
    book.client.fail_before = "ledger"
    with pytest.raises(TimeoutError):
        store.save(book, payload)
    book.client.fail_before = None
    assert stock.MovementStore(store.path).save(book, payload)["duplicate"]
    assert len(book.client.calls) == 1


def test_reposted_same_report_is_not_another_delivery(ledger):
    store, book, payload = ledger
    store.save(book, payload)
    result = store.save(book, {**payload, "source_key": "telegram:1:3"})
    assert result["duplicate_of"] == payload["source_key"]
    assert len(book.client.calls) == 1


def test_cancelled_report_cannot_reappear_on_update_replay(ledger):
    store, book, payload = ledger
    store.save(book, payload)
    store.save(book, {**payload, "status": "cancelled"})
    with pytest.raises(RevisionConflict):
        store.save(book, payload)


def test_legacy_restock_writer_cannot_change_physical_inventory():
    with pytest.raises(ValueError, match="отдельный журнал"):
        bot.save_revision_restock_entry({})


def test_another_author_or_formula_conflict_cannot_change_movement(ledger):
    store, book, payload = ledger
    store.save(book, payload)
    with pytest.raises(RevisionConflict):
        store.save(book, {**payload, "user_id": 2})
    book.client.books["ledger"][4]["data"][0]["rowData"][1]["values"][0]["userEnteredValue"] = {"formulaValue": "=TODAY()"}
    with pytest.raises(RevisionConflict):
        store.save(book, {**payload, "date": "2026-09-13"})


def test_combined_service_uses_sanitized_text_and_durable_journal(ledger):
    asyncio.run(combined_service_case(ledger))


async def combined_service_case(ledger):
    store, book, payload = ledger
    message = SimpleNamespace(text=EXAMPLE, caption=None, chat_id=1, message_id=2,
        from_user=SimpleNamespace(id=1, is_bot=False, first_name="Worker", username=None),
        date=datetime(2026, 9, 14, tzinfo=bot.BOT_TIMEZONE), edit_date=None, photo=None,
        media_group_id=None, forward_origin=None, reply_text=AsyncMock())
    application = SimpleNamespace(bot_data={})
    with patch.object(adapter, "get_store", return_value=store), patch.object(bot, "get_sheet", return_value=book), \
         patch.object(bot, "get_service_report_author", return_value="Worker"), \
         patch.object(bot, "process_group_service_report_draft", new_callable=AsyncMock) as service:
        service.return_value = {"status": "saved"}
        assert await adapter.handle(bot, message, application)
        draft = service.call_args.args[2]
        assert "дов" not in draft["source_text"].lower()
        assert not draft["revision"]
        assert draft["purchase_sum"] == 0
        assert draft["date"] == "14.09.2026"
        assert "Обслуживание сохранено" in message.reply_text.call_args.args[0]
