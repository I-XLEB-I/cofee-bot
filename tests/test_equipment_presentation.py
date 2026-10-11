from html.parser import HTMLParser

import pytest
from equipment_format import format_equipment_answer_html
from equipment_memory import EquipmentMemoryStore, TTL

ANSWER = '''Гиппо · Jofemar Bluetec G23

Проверено: 11.10.2026 14:49 МСК.

По сообщению/фото: Ошибка датчика <температуры>

Возможные причины:
• Датчик или его цепь: нужна проверка мастером.

Что проверить:
• Сфотографировать экран.

Telemetron: свежесть не подтверждена.

Источники:
Jofemar: руководство G23 (копия руководства производителя): https://territoria-coffee.com/manual.pdf
Интернет, внешняя публикация — Опыт: Jofemar G23: https://infovend.ru/thread?q=1&x=2

Сообщение для сервиса — черновик, не отправлено:
Здравствуйте. Гиппо. Терминал Vendista: 31440. Ошибка: датчик температуры. Проверки: вода есть. Просим провести диагностику.'''


class Markup(HTMLParser):
    def __init__(self):
        super().__init__(); self.stack=[]; self.text=[]; self.links=[]; self.quotes=[]
    def handle_starttag(self, tag, attrs):
        assert tag in {'b', 'i', 'a', 'blockquote'}
        self.stack.append(tag)
        if tag == 'blockquote': self.quotes.append(dict(attrs))
        if tag == 'a': self.links.append(dict(attrs)['href'])
    def handle_endtag(self, tag):
        assert self.stack.pop() == tag
    def handle_data(self, data): self.text.append(data)


def test_equipment_message_has_short_links_collapsible_blocks_and_visible_warning():
    result = format_equipment_answer_html(ANSWER)
    p=Markup();p.feed(result)
    assert not p.stack
    assert len(p.quotes)==2 and all('expandable' in q for q in p.quotes)
    assert p.links == ['https://territoria-coffee.com/manual.pdf', 'https://infovend.ru/thread?q=1&x=2']
    visible=''.join(p.text)
    assert 'https://' not in visible and 'Ошибка датчика <температуры>' in visible
    assert 'Инструкция Jofemar G23 · PDF' in visible and 'черновик' in visible.lower()
    assert result.index('свежесть не подтверждена') < result.index('<blockquote')
    assert 'Терминал Vendista: 31440.\nОшибка:' in visible


def test_old_quote_fallback_and_injected_html_stay_safe():
    result=format_equipment_answer_html(ANSWER+'\n<script>bad()</script>', expandable=False)
    p=Markup();p.feed(result)
    assert all('expandable' not in q for q in p.quotes)
    assert '<script>' not in result


def test_memory_survives_reopening_and_is_bound_to_employee_chat_and_reply(tmp_path):
    path=tmp_path/'cases.sqlite3'
    memory={'location':'Гиппо','model':'Jofemar Bluetec G23','problem':'датчик','checks':'вода есть'}
    EquipmentMemoryStore(path).save(1,-10,200,memory,now=100)
    reopened=EquipmentMemoryStore(path)
    assert reopened.load(1,-10,200,now=101)==memory
    assert reopened.load(2,-10,200,now=101) is None
    assert reopened.load(1,-11,200,now=101) is None
    assert reopened.load(1,-10,201,now=101) is None
    assert reopened.load(1,-10,200,now=101+TTL) is None


def test_memory_never_stores_photo_or_unknown_fields(tmp_path):
    store=EquipmentMemoryStore(tmp_path/'cases.sqlite3')
    with pytest.raises(ValueError): store.save(1,-1,1,{'image_data':'secret'})
    with pytest.raises(ValueError): store.save(1,-1,1,{'checks':'a'*801})


import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch
import bot


class EquipmentReplyFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_bot_bridge_persists_response_and_restores_reply_context(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path=__import__('pathlib').Path(directory)/'cases.sqlite3'
            memory={'location':'Гиппо','model':'Jofemar Bluetec G23','problem':'датчик',
                    'checks':'Вода есть','last_answer':'Возможна неисправность датчика'}
            query=Mock(return_value={'scope':'staff','answer':ANSWER,'equipment_memory':memory})
            async def inline(fn,*args,**kwargs): return fn(*args,**kwargs)
            with patch.object(bot,'get_owner_ai_client_config',return_value=object()), \
                 patch.object(bot,'run_blocking',side_effect=inline), \
                 patch.object(bot,'build_owner_ai_maintenance_context',return_value={}), \
                 patch.object(bot,'query_owner_ai',query), \
                 patch.object(bot,'get_equipment_memory_store',side_effect=lambda:EquipmentMemoryStore(path)):
                m=NS(from_user=NS(id=1),chat_id=-10,reply_to_message=None)
                first=NS(message_id=200,edit_text=AsyncMock())
                await bot.answer_owner_ai_message(m,NS(),'Гиппо ошибка',status=first,
                    equipment_context={'response_message_id':200})
                self.assertIn('<blockquote expandable>',first.edit_text.call_args.args[0])
                m.reply_to_message=NS(message_id=200,text='Гиппо · Jofemar Bluetec G23')
                second=NS(message_id=201,edit_text=AsyncMock())
                await bot.answer_owner_ai_message(m,NS(),'Подробнее',status=second,
                    equipment_context={'response_message_id':201,'reply_message_id':200})
            sent=query.call_args.kwargs['equipment_context']
            self.assertEqual(sent['previous_case']['checks'],'Вода есть')
            self.assertEqual(EquipmentMemoryStore(path).load(1,-10,201),memory)
