"""Telegram dialogue orchestration; model proposals never call write functions."""

import json
import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

import revision_accounting
from revision_dialogue import (
    RevisionConflict,
    RevisionInputError,
    RevisionStore,
    is_revision_request,
    validate_proposal,
)

_store = None
_tasks = {}


def store_path(host):
    return Path(
        os.getenv("REVISION_DIALOGUE_DB", "")
        or (
            Path(host.resolve_runtime_path(host.PERSISTENCE_FILE)).parent
            / "revision_dialogue.sqlite3"
        )
    )


def get_store(host):
    global _store
    path = store_path(host)
    if os.getenv("RAILWAY_ENVIRONMENT_ID") and not os.path.ismount(path.parent):
        raise RevisionInputError(
            "Постоянное хранилище ревизий пока не готово. Используйте меню ревизии."
        )
    if _store is None:
        _store = RevisionStore(path)
    return _store


def buttons(draft, *, confirm=False, recover=False):
    tag = f"{draft['version']}:{draft['generation']}"
    if recover:
        rows = [
            [InlineKeyboardButton("Сверить начатую запись", callback_data=f"revchat:recover:{tag}")]
        ]
    else:
        rows = [
            [
                InlineKeyboardButton(
                    "Подтвердить запись" if confirm else "Сохранить ревизию",
                    callback_data=f"revchat:{'confirm' if confirm else 'preview'}:{tag}",
                )
            ],
            [InlineKeyboardButton("Отменить черновик", callback_data=f"revchat:cancel:{tag}")],
        ]
    return InlineKeyboardMarkup(rows)


async def send_draft(application, chat_id, draft, units):
    if draft["status"] in ("saving", "uncertain"):
        await application.bot.send_message(
            chat_id,
            "Запись начата, но её итог нужно сверить. "
            "Повторное обслуживание не создаётся автоматически.",
            reply_markup=buttons(draft, recover=True),
        )
        return
    if draft["status"] == "cancelled":
        await application.bot.send_message(chat_id, "Черновик отменён. Таблицы не изменены.")
        return
    if not draft["records"]:
        await application.bot.send_message(
            chat_id,
            draft.get("clarification")
            or "На какой точке считаем? Остатки можно присылать по частям.",
        )
        return
    for record in draft["records"]:
        lines = [f"Черновик · {record['location']} · {record['date']}"]
        lines += [
            f"{item}: {qty.replace('.', ',')} {units[item]}"
            for item, qty in record["values"].items()
        ]
        missing = [name for name in units if name not in record["values"]]
        if missing:
            lines.append("Ещё не указаны: " + ", ".join(missing) + ".")
        await application.bot.send_message(chat_id, "\n".join(lines))
    await application.bot.send_message(
        chat_id,
        draft.get("clarification")
        or "Можно дописать остатки, исправить число или попросить сохранить.",
        reply_markup=buttons(draft),
    )


async def send_preview(application, chat_id, preview):
    for record in preview["summaries"]:
        lines = [
            f"Сверка · {record['location']} · {record['date']}",
            f"Блок БДР: {record['bdr_date']}.",
        ]
        lines += [
            f"{c['item']}: {c['before'] or 'не заполнено'} → {c['after']} {c['unit']}"
            for c in record["changes"]
        ]
        if record["missing"]:
            lines.append(
                "Частичная ревизия. Неуказанные значения останутся прежними: "
                + ", ".join(record["missing"])
                + "."
            )
        if any(c["item"] == "Вода" for c in record["changes"]):
            lines.append("Вода сохраняется в учёте бота; в БДР отдельной строки воды нет.")
        lines.append(record["service"])
        await application.bot.send_message(chat_id, "\n".join(lines))


async def perform_save(host, application, chat_id, user_id, draft, *, recover=False):
    store = get_store(host)
    async with host.GROUP_REPORT_SAVE_LOCK:
        operation_id = (
            draft.get("operation_id")
            if recover
            else store.begin_operation(
                chat_id,
                user_id,
                version=draft["version"],
                generation=draft["generation"],
            )
        )
        try:
            result = await host.run_blocking(
                revision_accounting.save, host, store, operation_id, chat_id, user_id
            )
        except Exception as exc:
            host.logger.warning("revision_save_uncertain error_type=%s", type(exc).__name__)
            await application.bot.send_message(
                chat_id,
                "Не удалось подтвердить весь результат. Черновик и этапы записи сохранены. "
                "Нажмите «Сверить начатую запись»: бот проверит уже выполненное.",
                reply_markup=buttons(store.get(chat_id, user_id), recover=True),
            )
            return
    locations = ", ".join(r["location"] for r in result["summaries"])
    await application.bot.send_message(
        chat_id,
        f"Ревизия сохранена: {locations}. Значения в учёте бота и БДР прочитаны обратно и сверены. "
        "Для исправления укажите точку, дату и новое количество. "
        "Повторное обслуживание за ту же дату не начисляется.",
    )


async def prepare_save(host, application, chat_id, user_id, draft):
    store = get_store(host)
    if store.next_message(chat_id, user_id):
        await application.bot.send_message(chat_id, "Сначала разберу уже принятые сообщения.")
        return
    try:
        preview = await host.run_blocking(
            revision_accounting.prepare, host, draft, chat_id, user_id
        )
        current = store.set_preview(
            chat_id, user_id, preview, version=draft["version"], generation=draft["generation"]
        )
        await send_preview(application, chat_id, preview)
        if preview["needs_confirmation"]:
            await application.bot.send_message(
                chat_id,
                "Проверьте итог перед записью.",
                reply_markup=buttons(current, confirm=True),
            )
        else:
            await perform_save(host, application, chat_id, user_id, current)
    except (RevisionInputError, RevisionConflict, host.bdr_revision.BdrRevisionError) as exc:
        await application.bot.send_message(chat_id, str(exc))
    except Exception as exc:
        host.logger.warning("revision_preview_failed error_type=%s", type(exc).__name__)
        await application.bot.send_message(
            chat_id,
            "Не удалось прочитать таблицы для сверки. "
            "Черновик сохранён; попробуйте сохранить позже.",
        )


async def drain(host, application, chat_id, user_id):
    store = get_store(host)
    while event := store.next_message(chat_id, user_id):
        draft = store.get(chat_id, user_id)
        if user_id not in host.get_allowed_user_ids():
            store.mark_message(chat_id, user_id, event["message_id"], "failed")
            continue
        try:
            if event["state"] == "parsed":
                proposal = json.loads(event["proposal"])
            else:
                store.mark_message(chat_id, user_id, event["message_id"], "processing")
                result = await host.run_blocking(
                    host.query_owner_ai,
                    host.get_owner_ai_client_config(),
                    user_id=user_id,
                    question=event["text"],
                    conversation_id=f"telegram:{chat_id}:{user_id}",
                    audience="private",
                    revision_context={
                        "message_date": event["message_date"],
                        "records": draft["records"],
                    },
                )
                proposal = validate_proposal(
                    result["revision"], host.REVISION_LOCATIONS, host.REVISION_ITEMS
                )
                store.mark_message(chat_id, user_id, event["message_id"], "parsed", proposal)
            current = store.apply_message(
                chat_id, user_id, event["message_id"], proposal, expected_version=draft["version"]
            )
        except Exception as exc:
            store.mark_message(chat_id, user_id, event["message_id"], "failed")
            host.logger.warning("revision_message_failed error_type=%s", type(exc).__name__)
            await application.bot.send_message(
                chat_id,
                "Не удалось надёжно разобрать последнее сообщение. Предыдущий черновик сохранён. "
                "Уточните сообщение или используйте обычное меню ревизии.",
            )
            continue
        if proposal["action"] == "other":
            # Answer normal questions without cancelling or mixing in accounting state.
            message = SimpleNamespace(
                from_user=SimpleNamespace(id=user_id),
                chat_id=chat_id,
                reply_text=lambda text: application.bot.send_message(chat_id, text),
            )
            await host.answer_owner_ai_message(
                message, SimpleNamespace(application=application), event["text"]
            )
        elif current.get("save_requested") and not store.next_message(chat_id, user_id):
            await prepare_save(host, application, chat_id, user_id, current)
        else:
            await send_draft(application, chat_id, current, host.REVISION_UNITS)


def start_worker(host, application, chat_id, user_id):
    key = chat_id, user_id
    if key not in _tasks or _tasks[key].done():
        _tasks[key] = application.create_task(drain(host, application, chat_id, user_id))


async def handle_message(host, message, context, question):
    user_id = getattr(getattr(message, "from_user", None), "id", None)
    chat_id = getattr(message, "chat_id", None)
    if not user_id or chat_id != user_id:
        return False
    requested = is_revision_request(question)
    if not requested and not store_path(host).exists():
        return False
    try:
        store = get_store(host)
        draft = store.get(chat_id, user_id)
        if not requested and (not draft or draft["status"] in ("saved", "cancelled")):
            return False
        if draft and draft["status"] in ("saving", "uncertain"):
            await send_draft(context.application, chat_id, draft, host.REVISION_UNITS)
            return True
        if user_id not in host.get_allowed_user_ids():
            raise RevisionInputError("Нет доступа к записи ревизии.")
        if not host.owner_ai_api_configured():
            raise RevisionInputError("ИИ пока не подключён. Используйте меню ревизии.")
        day = datetime.strptime(host.get_message_local_date(message), "%d.%m.%Y").date().isoformat()
        accepted = store.accept(chat_id, user_id, message.message_id, question, day)
        if accepted:
            await message.reply_text("Сообщение сохранено в черновике. Разбираю остатки…")
        start_worker(host, context.application, chat_id, user_id)
    except (RevisionConflict, RevisionInputError) as exc:
        await message.reply_text(str(exc))
    return True


def register(application, host):
    async def cancel_command(update, context):
        if (
            not host.is_allowed_user(update)
            or not host.is_private_chat(update)
            or not store_path(host).exists()
        ):
            return
        chat_id, user_id = update.effective_chat.id, update.effective_user.id
        store = get_store(host)
        draft = store.get(chat_id, user_id)
        if not draft or draft["status"] in ("saved", "cancelled"):
            return
        try:
            store.cancel(chat_id, user_id, version=draft["version"], generation=draft["generation"])
            await update.effective_message.reply_text("Черновик отменён. Таблицы не изменены.")
        except RevisionConflict as exc:
            await update.effective_message.reply_text(str(exc))
        raise ApplicationHandlerStop

    async def callback(update, context):
        query = update.callback_query
        if not host.is_allowed_user(update) or not host.is_private_chat(update):
            await query.answer("Нет доступа", show_alert=True)
            raise ApplicationHandlerStop
        await query.answer()
        chat_id, user_id = update.effective_chat.id, update.effective_user.id
        try:
            _, action, version, generation = query.data.split(":")
            store = get_store(host)
            draft = store.get(chat_id, user_id)
            if not draft or (draft["version"], draft["generation"]) != (
                int(version),
                int(generation),
            ):
                raise RevisionConflict("Кнопка устарела. Перечитайте текущий черновик.")
            if action == "cancel":
                store.cancel(
                    chat_id, user_id, version=draft["version"], generation=draft["generation"]
                )
                await context.bot.send_message(chat_id, "Черновик отменён. Таблицы не изменены.")
            elif action in ("preview", "confirm", "recover"):

                async def work():
                    try:
                        if action == "preview":
                            await prepare_save(host, context.application, chat_id, user_id, draft)
                        else:
                            await perform_save(
                                host,
                                context.application,
                                chat_id,
                                user_id,
                                draft,
                                recover=action == "recover",
                            )
                    except (RevisionConflict, RevisionInputError) as exc:
                        await context.bot.send_message(chat_id, str(exc))

                context.application.create_task(work())
        except (RevisionConflict, RevisionInputError, ValueError) as exc:
            await context.bot.send_message(chat_id, str(exc))
        raise ApplicationHandlerStop

    async def edited(update, context):
        if (
            not host.is_allowed_user(update)
            or not host.is_private_chat(update)
            or not store_path(host).exists()
        ):
            return
        message = update.effective_message
        if get_store(host).has_message(
            message.chat_id, update.effective_user.id, message.message_id
        ):
            await message.reply_text(
                "Для исправления ревизии пришлите новое сообщение с точкой и количеством. "
                "Изменение старого сообщения не запускает повторную запись."
            )
            raise ApplicationHandlerStop

    application.add_handler(CallbackQueryHandler(callback, pattern=r"^revchat:"), group=-2)
    application.add_handler(CommandHandler("cancel", cancel_command), group=-2)
    application.add_handler(
        MessageHandler(filters.UpdateType.EDITED_MESSAGE & filters.ChatType.PRIVATE, edited),
        group=-2,
    )


def resume(host, application):
    if store_path(host).exists():
        for chat_id, user_id in get_store(host).resume_keys():
            start_worker(host, application, chat_id, user_id)
