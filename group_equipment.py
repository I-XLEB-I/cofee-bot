"""Explicit group-only technical help. Ordinary reports never reach the model."""
import base64
import re

from telegram.ext import ApplicationHandlerStop, MessageHandler, filters

MAX_IMAGE_BYTES = 256_000


def addressed(message, bot):
    reply = getattr(message, "reply_to_message", None)
    if getattr(getattr(reply, "from_user", None), "id", None) == bot.id:
        return True
    text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
    username = bot.username
    if username and re.search(r"(?<![\w@])@" + re.escape(username) + r"\b", text, re.I):
        return True
    entities = (getattr(message, "entities", None) or ()) + (getattr(message, "caption_entities", None) or ())
    return any(getattr(e, "type", None) == "text_mention" and
               getattr(getattr(e, "user", None), "id", None) == bot.id for e in entities)


async def image_data(message, bot):
    photos = getattr(message, "photo", None)
    if not photos:
        photos = getattr(getattr(message, "reply_to_message", None), "photo", None)
    if not photos:
        return None
    candidates = [p for p in photos if (p.file_size or 0) <= MAX_IMAGE_BYTES and p.width <= 1280]
    if not candidates:
        raise ValueError("Фото слишком большое. Пришли крупный план экрана как обычное фото.")
    photo = max(candidates, key=lambda p: p.width * p.height)
    file = await bot.get_file(photo.file_id)
    data = await file.download_as_bytearray()
    if len(data) > MAX_IMAGE_BYTES or not data.startswith(b"\xff\xd8\xff"):
        raise ValueError("Не удалось прочитать фото. Пришли крупный план экрана как обычное фото.")
    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")


def register(application, host):
    async def handler(update, context):
        message = update.effective_message
        if not message or not addressed(message, context.bot):
            return
        if not host.is_allowed_user(update) or not host.is_allowed_group_chat(update):
            raise ApplicationHandlerStop
        if getattr(message.from_user, "is_bot", False):
            raise ApplicationHandlerStop
        # Editing a diagnostic message must not create an accounting report.
        if getattr(update, "edited_message", None):
            raise ApplicationHandlerStop
        question = message.text or message.caption or "Разбери ошибку на фото."
        if context.bot.username:
            question = re.sub(r"(?<![\w@])@" + re.escape(context.bot.username) + r"\b", "", question, flags=re.I).strip()
        if not question:
            question = "Помоги разобраться с неисправностью аппарата или терминала."
        if not host.owner_ai_api_configured():
            await message.reply_text("Технический помощник временно не подключён.")
            raise ApplicationHandlerStop
        status = await message.reply_text("🔎 Разбираю неисправность и проверяю доступные данные…")

        async def answer():
            try:
                photo = await image_data(message, context.bot)
                equipment_context = {"response_message_id": status.message_id}
                reply = getattr(message, "reply_to_message", None)
                if reply is not None:
                    equipment_context["reply_message_id"] = reply.message_id
                if photo:
                    equipment_context["image_data"] = photo
                await host.answer_owner_ai_message(message, context, question, status=status,
                                                   equipment_context=equipment_context)
            except ValueError as exc:
                await status.edit_text(str(exc))
            except Exception as exc:
                host.logger.warning("equipment_photo_failed error_type=%s", type(exc).__name__)
                await status.edit_text("Не удалось обработать обращение. Пришли текст ошибки и название точки.")

        async def expired():
            await status.edit_text("Очередь диагностики заняла слишком много времени. Повтори вопрос позже.")

        accepted = host.OWNER_AI_WORK_QUEUE.submit(
            (message.chat_id, message.from_user.id), answer, expired,
            create_task=context.application.create_task,
        )
        if not accepted:
            await status.edit_text("Помощник занят предыдущими вопросами. Дождись ответа и повтори вопрос.")
        # Explicit technical questions must not pass into maintenance/payroll parsing.
        raise ApplicationHandlerStop

    application.add_handler(MessageHandler(
        filters.ChatType.GROUPS & (filters.TEXT | filters.PHOTO) & ~filters.COMMAND,
        handler,
    ), group=-4)
