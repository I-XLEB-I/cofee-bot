"""Telegram adapter for the separate stock journal."""
import json
import os
import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationHandlerStop, CallbackQueryHandler, MessageHandler, filters

import stock_movements as stock
from revision_dialogue import RevisionConflict


def store_path(host):
    return Path(host.resolve_runtime_path(host.PERSISTENCE_FILE)).with_name("stock_movements.sqlite3")


def get_store(host):
    path = store_path(host)
    if os.getenv("RAILWAY_ENVIRONMENT_ID") and not os.path.ismount(path.parent):
        raise RevisionConflict("Постоянное хранилище движений пока недоступно.")
    return stock.MovementStore(path)


def source_key(message):
    return f"telegram:{message.chat_id}:{message.message_id}"


async def handle(host, message, application, photo_ids=None):
    text = message.caption or message.text or ""
    parsed = stock.parse(text, host, host.get_message_local_date(message))
    previous = None
    if (parsed is not None or getattr(message, "edit_date", None)) and store_path(host).exists():
        previous = get_store(host).latest(source_key(message))
    if parsed is None and previous is None:
        return False
    if parsed and parsed.get("error"):
        await message.reply_text("Движение пока не записано. " + parsed["error"])
        return True
    if previous and not parsed:
        await message.reply_text("В исправленном сообщении больше нет доставки или покупки. Для отмены используйте кнопку «Отменить движение» под его подтверждением.")
        return True
    if previous:
        old_payload = json.loads(previous["payload"])
        if old_payload.get("status") == "cancelled":
            await message.reply_text("Это движение отменено. Новую доставку отправьте отдельным сообщением.")
            return True
        edited = getattr(message, "edit_date", None)
        if edited and edited.timestamp() < old_payload.get("event_time", 0):
            return True
    payload = {"source_key": source_key(message), "date": parsed["date"], "point": parsed["point"],
               "who": host.get_service_report_author(message), "user_id": message.from_user.id,
               "items": parsed["items"], "text": text, "status": "saved",
               "event_time": (getattr(message, "edit_date", None) or message.date).timestamp(),
               "message_time": message.date.isoformat(), "photo_ids": list(photo_ids or [])}
    try:
        async with host.GROUP_REPORT_SAVE_LOCK:
            result = await host.run_blocking(lambda: get_store(host).save(host.get_sheet(), payload))
    except Exception:
        host.logger.exception("Movement write needs verification source=%s", payload["source_key"])
        await message.reply_text("Не удалось подтвердить запись движения. Нажмите «Сверить запись»: бот проверит начатую операцию перед повтором.",
                                 reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Сверить запись", callback_data=f"stock:retry:{message.message_id}:0")]]))
        return True
    if result.get("duplicate_of"):
        await message.reply_text("Такое сообщение за эту дату уже записано. Для исправления отредактируйте первое сообщение. Если это ещё одна доставка, добавьте в текст «ещё одна доставка».")
        return True
    # A normal service report still uses its established payroll path. Movement
    # clauses are removed before extracting physical inventory values.
    service_result = None
    remainder = parsed["remainder"]
    snapshot = host.parse_revision_snapshot_message_text(remainder)
    service = (host.build_service_report_from_revision_snapshot(snapshot, host.get_message_local_date(message))
               if snapshot and snapshot["location"] in host.POINTS and not snapshot.get("date_error")
               else host.parse_service_report_message_text(remainder, has_photo=bool(photo_ids or getattr(message, "photo", None))))
    if service is None and re.search(r"\bобслужил(?:а|и)?\b", remainder, re.I):
        service = {"point": parsed["point"], "date": datetime.fromisoformat(parsed["date"]).strftime("%d.%m.%Y"),
                   "water": host.extract_service_report_water(remainder) or "", "purchases": "", "purchase_sum": 0,
                   "shortage_items": host.extract_service_report_shortage_items(remainder), "warnings": [], "source_text": remainder}
    if service:
        # Preserve expense handling only where the old report format already
        # qualifies; a new movement alone must not generate wages/expenses.
        legacy = host.parse_service_report_message_text(text, has_photo=bool(photo_ids or getattr(message, "photo", None)))
        if legacy:
            service.update({key: legacy[key] for key in ("purchases", "purchase_sum")})
        draft = host.build_group_service_report_draft(service, message, photo_ids=photo_ids)
        service_result = await host.process_group_service_report_draft(message, application, draft, send_saved_feedback=False)
    lines = [f"{'Уже записано' if result['duplicate'] else 'Записано'} · {parsed['point']} · {parsed['date']}", stock.description(parsed["items"])]
    if service_result:
        lines.append("Обслуживание сохранено." if service_result.get("status") in ("saved", "already", "duplicate") else "Запись обслуживания требует отдельной проверки.")
    if any(item["balance_quantity"] is None and item["item"] in ("Кофе", "Молоко", "Мока", "Шоколад", "Стаканы") for item in parsed["items"]):
        lines.append("Для расчёта остатка уточните размер упаковки: кофе/порошки в кг, стаканы в штуках.")
    lines.append("Исправить — отредактируйте исходное сообщение.")
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("Отменить движение", callback_data=f"stock:cancel:{message.message_id}:{result['version']}")]])
    await message.reply_text("\n".join(lines), reply_markup=markup)
    return True


def register(application, host):
    async def incoming(update, context):
        if not host.is_allowed_user(update) or not host.is_allowed_group_report_chat(update):
            return
        message = update.effective_message
        if not message or getattr(message.from_user, "is_bot", False):
            return
        if await handle(host, message, context.application):
            raise ApplicationHandlerStop

    async def callback(update, context):
        query = update.callback_query
        if not host.is_allowed_user(update) or not host.is_allowed_group_report_chat(update):
            await host.deny_callback_access(query)
            return
        await query.answer()
        _, action, message_id, version = query.data.split(":")
        key = f"telegram:{query.message.chat_id}:{int(message_id)}"
        store = get_store(host)
        previous = store.latest(key)
        if not previous:
            await query.message.reply_text("Запись не найдена. Отправьте исходное сообщение ещё раз.")
            raise ApplicationHandlerStop
        payload = json.loads(previous["payload"])
        if int(payload["user_id"]) != update.effective_user.id:
            await query.message.reply_text("Изменить движение может его автор.")
            raise ApplicationHandlerStop
        try:
            async with host.GROUP_REPORT_SAVE_LOCK:
                if action == "cancel":
                    if payload.get("status") == "cancelled":
                        await query.message.reply_text("Движение уже отменено.")
                        raise ApplicationHandlerStop
                    payload["status"] = "cancelled"
                    await host.run_blocking(lambda: store.save(host.get_sheet(), payload, expected_version=int(version)))
                    await query.edit_message_text("Движение отменено. Ревизия и обслуживание не изменены.")
                elif action == "retry":
                    await host.run_blocking(lambda: store.recover(host.get_sheet().client))
            if action == "retry":
                replay = SimpleNamespace(text=payload["text"], caption=None, chat_id=query.message.chat_id,
                    message_id=int(message_id), from_user=update.effective_user, photo=None, media_group_id=None,
                    date=datetime.fromisoformat(payload["message_time"]), edit_date=None,
                    reply_text=query.message.reply_text, forward_origin=None)
                await handle(host, replay, context.application, payload.get("photo_ids"))
        except ApplicationHandlerStop:
            raise
        except Exception as exc:
            host.logger.exception("Movement action failed source=%s", key)
            await query.message.reply_text(str(exc) if isinstance(exc, RevisionConflict) else "Не удалось сверить движение. Исходная запись сохранена для проверки.")
        raise ApplicationHandlerStop

    application.add_handler(MessageHandler((filters.TEXT & ~filters.COMMAND) | filters.CAPTION, incoming), group=-3)
    application.add_handler(CallbackQueryHandler(callback, pattern=r"^stock:(cancel|retry):\d+:\d+$"), group=-3)
