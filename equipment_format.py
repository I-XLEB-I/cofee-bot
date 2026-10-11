"""Trusted Telegram markup around escaped equipment text, never model HTML."""
import html
import re
from urllib.parse import urlsplit


def _source(line):
    match = re.fullmatch(r'(.+?):\s*(https?://\S+)', line.strip())
    if not match:
        return html.escape(line)
    title, url = match.groups()
    try:
        parsed = urlsplit(url)
        if not parsed.hostname or parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
            return html.escape(title)
    except ValueError:
        return html.escape(title)
    if 'руководство G23' in title:
        label = 'Инструкция Jofemar G23 · PDF'
    elif 'обслуживание PRO LV307' in title:
        label = 'Инструкция PRO LV307 · PDF'
    elif 'подключение к G23' in title:
        label = 'Подключение Vendista к G23'
    elif 'Vendista: ошибки' in title:
        label = 'Ошибки Vendista · база знаний'
    elif title.startswith('Интернет, внешняя публикация — '):
        label = 'Интернет · ' + title.split(' — ', 1)[1][:70]
    else:
        label = title[:80]
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'


def _draft(body):
    # Break the locally generated service paragraph into copyable lines.
    for marker in ('Терминал Vendista:', 'Ошибка:', 'Начало неисправности:', 'Проверки:',
                   'На ', 'Просим провести диагностику', 'Фото ошибки и шильдика'):
        body = re.sub(r'(?<=\.)\s+(?=' + re.escape(marker) + ')', '\n', body)
    return html.escape(body)


def format_equipment_answer_html(answer, *, expandable=True):
    quote = '<blockquote expandable>' if expandable else '<blockquote>'
    blocks = str(answer).strip().split('\n\n')
    rendered = []
    for i, block in enumerate(blocks):
        if block.startswith('Источники:\n'):
            links = '\n'.join(_source(line) for line in block.splitlines()[1:])
            rendered.append('<b>Источники и данные</b>\n' + quote + links + '</blockquote>')
        elif block.startswith('Сообщение для сервиса — черновик, не отправлено:\n'):
            body = block.partition('\n')[2]
            rendered.append('<b>Черновик для сервиса</b>\n' + quote + _draft(body) + '</blockquote>')
        elif block.startswith(('Возможные причины:\n', 'Что проверить:\n', 'Что сделать сейчас:\n')):
            heading, _, body = block.partition('\n')
            rendered.append(f'<b>{html.escape(heading.rstrip(":"))}</b>\n' + html.escape(body))
        elif block.startswith(('По сообщению/фото:', 'Как это связано:', 'Что это означает:', 'Мастер:', 'Учёл результаты:')):
            heading, _, body = block.partition(':')
            rendered.append(f'<b>{html.escape(heading)}:</b>{html.escape(body)}')
        elif block.startswith('Проверено:'):
            rendered.append('<i>' + html.escape(block.replace('Проверено:', 'Данные на:', 1)) + '</i>')
        elif block.startswith(('Telemetron:', 'Vendista:')):
            # Freshness warnings stay visible, outside collapsed citations.
            heading, _, body = block.partition(':')
            rendered.append(f'<b>{heading}:</b>{html.escape(body)}')
        elif block.startswith('⚠️'):
            rendered.append('<b>' + html.escape(block) + '</b>')
        elif i == 0 and ' · ' in block:
            rendered.append('<b>' + html.escape(block) + '</b>')
        else:
            rendered.append(html.escape(block))
    text = '\n\n'.join(rendered)
    # The backend asks the next relevant question. A generic "Подробнее"
    # footer on every turn invited repeated reports instead of a dialogue.
    return text
