"""Owner-only purchasing reminders, opt-in and quiet on unchanged state."""
import asyncio
import hashlib
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

logger = logging.getLogger(__name__)


def alert_text(items):
    due = [item for item in items if item['status'] in ('urgent', 'soon')]
    check = [item for item in items if item['status'] == 'check']
    lines = ['🛒 Пора проверить закупки' if due else '🛒 Для плана закупок нужны данные', '']
    for item in due:
        quantity = f" · к покупке ≈ {item['buy']:g} {item['unit']}" if item['buy'] is not None else ''
        lines.append(f"{item['name']}: заказать до {item['order_by']}{quantity}")
    if check:
        if due:
            lines.append('')
        lines.append(f'Для {len(check)} позиций пока нельзя рассчитать срок заказа — проверьте остатки и параметры в закупщике.')
    lines += ['', 'Расчёт ориентировочный. Откройте основания и проверьте неизвестные движения.']
    return '\n'.join(lines)


async def loop(application, workspace, url):
    while True:
        try:
            prefs = await asyncio.to_thread(workspace.store.notifications)
            uid = prefs.get('recipient')
            if prefs.get('enabled') and uid in workspace.owners():
                workspace.refresh()
                now = datetime.now(ZoneInfo('Europe/Moscow'))
                if 9 <= now.hour < 20:
                    data = await asyncio.to_thread(workspace.snapshot)
                    fetched = data.get('generated_at')
                    fresh = fetched and (now - datetime.fromisoformat(fetched)).total_seconds() < 3600
                    if fresh:
                        items = [i for i in data.get('items', []) if i['status'] in ('urgent', 'soon', 'check')]
                        if items:
                            signature = [(i['name'], i['status'], i['order_by']) for i in items]
                            event = hashlib.sha256(json.dumps([uid, signature], ensure_ascii=False).encode()).hexdigest()
                            if await asyncio.to_thread(workspace.store.reserve_alert, event):
                                try:
                                    await application.bot.send_message(uid, alert_text(items), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Открыть закупщик', web_app=WebAppInfo(url=url))]]))
                                except Exception:
                                    # Delivery may have succeeded; no blind resend after a timeout.
                                    await asyncio.to_thread(workspace.store.alert_result, event, 'uncertain')
                                    logger.warning('Procurement reminder delivery uncertain')
                                else:
                                    await asyncio.to_thread(workspace.store.alert_result, event, 'delivered')
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('Procurement notification iteration failed')
        await asyncio.sleep(900)
