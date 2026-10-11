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


def test_practical_reply_keeps_actions_visible_and_telemetry_details_collapsed():
    answer='''Гиппо · продолжение разбора

Что сделать сейчас:
1. Посмотри на экран. Если ошибка осталась, нужен мастер.

Мастер: Датчик внутри узла нагрева.

Текущее состояние по телеметрии не подтверждено.

Источники:
Jofemar: руководство G23: https://example.com/manual.pdf
Данные на: 11.10.2026 15:42 МСК.
Telemetron: последний отчёт неизвестен.
Vendista: последняя связь неизвестна.'''
    result=format_equipment_answer_html(answer)
    p=Markup();p.feed(result)
    assert not p.stack and len(p.quotes)==1 and 'expandable' in p.quotes[0]
    main=result.partition('<blockquote')[0]
    assert 'Что сделать сейчас' in main and 'Мастер:' in main
    assert 'Текущее состояние по телеметрии не подтверждено.' in main
    assert 'Telemetron:' not in main and 'Vendista:' not in main and '15:42' not in main
    assert 'Ответь на это сообщение' not in result


def test_group_memory_survives_reopening_and_is_shared_only_within_reply_chain(tmp_path):
    path=tmp_path/'cases.sqlite3'
    memory={'location':'Гиппо','model':'Jofemar Bluetec G23','problem':'датчик','checks':'вода есть'}
    EquipmentMemoryStore(path).save(1,-10,200,memory,now=100)
    reopened=EquipmentMemoryStore(path)
    assert reopened.load(1,-10,200,now=101)==memory
    assert reopened.load(2,-10,200,now=101)==memory
    assert reopened.load(1,-11,200,now=101) is None
    assert reopened.load(1,-10,201,now=101) is None
    assert reopened.load(1,-10,200,now=101+TTL) is None


def test_other_employee_replying_to_old_answer_gets_latest_checks(tmp_path):
    path=tmp_path/'cases.sqlite3'
    store=EquipmentMemoryStore(path)
    first={'location':'Гиппо','problem':'датчик','checks':'Вода есть'}
    latest={**first,'checks':'Вода есть; фото экрана проверено'}
    other={'location':'Гиппо','problem':'другая ошибка','checks':'OTHER_CASE'}
    store.save(1,-10,200,first,now=100)
    store.save(1,-10,300,other,now=101)
    store.save(2,-10,201,latest,reply_message_id=200,now=102)
    reopened=EquipmentMemoryStore(path)
    assert reopened.load(3,-10,200,now=103)==latest
    assert reopened.load(1,-10,201,now=103)==latest
    assert reopened.load(2,-10,300,now=103)==other
    assert reopened.load(2,-11,200,now=103) is None
    assert reopened.load(2,-10,200,now=103+TTL) is None


@pytest.mark.parametrize('new_location,start_new',[('Макси',False),('Гиппо',True)])
def test_point_switch_and_explicit_new_incident_do_not_replace_old_chain(tmp_path,new_location,start_new):
    store=EquipmentMemoryStore(tmp_path/'cases.sqlite3')
    old={'location':'Гиппо','checks':'OLD_CHECK'}
    new={'location':new_location,'checks':'NEW_CHECK'}
    store.save(1,-10,200,old)
    store.save(2,-10,201,new,reply_message_id=200,start_new=start_new)
    assert store.load(2,-10,200)==old
    assert store.load(1,-10,201)==new


def test_private_memory_remains_bound_to_employee_chat_and_message(tmp_path):
    store=EquipmentMemoryStore(tmp_path/'cases.sqlite3')
    memory={'checks':'PRIVATE_CHECK'}
    store.save(1,1,200,memory)
    assert store.load(1,1,200)==memory
    assert store.load(2,1,200) is None
    assert store.load(1,2,200) is None
    assert store.load(1,-10,200) is None


@pytest.mark.parametrize('question,expected',[
    ('На Гиппо другая ошибка E7',True),('Другой случай на Гиппо',True),
    ('Новая проблема',True),('Разберём другую проблему',True),
    ('Теперь показывает E7',False),('Другой сотрудник проверил воду',False)])
def test_explicit_new_case_words(question,expected):
    from equipment_memory import starts_new_case
    assert starts_new_case(question) is expected


def test_legacy_snapshots_migrate_once_and_never_share_private_chats(tmp_path):
    import json,sqlite3
    path=tmp_path/'cases.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE equipment_cases (user_id INTEGER,chat_id INTEGER,message_id INTEGER,'
                   'updated REAL,body TEXT,PRIMARY KEY(user_id,chat_id,message_id))')
        db.executemany('INSERT INTO equipment_cases VALUES (?,?,?,?,?)',[
            (1,-10,200,100,json.dumps({'checks':'old'})),
            (2,-10,200,102,json.dumps({'checks':'latest'})),
            (1,1,200,102,json.dumps({'checks':'private'}))])
    store=EquipmentMemoryStore(path)
    assert store.load(3,-10,200,now=103)=={'checks':'latest'}
    assert store.load(2,1,200,now=103) is None
    assert store.load(1,1,200,now=103)=={'checks':'private'}
    assert EquipmentMemoryStore(path).load(3,-10,200,now=103)=={'checks':'latest'}
    with sqlite3.connect(path) as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='equipment_cases'").fetchone()


def test_case_and_reply_limits_expiry_cleanup(tmp_path):
    import sqlite3
    from equipment_memory import MAX_CASES, MAX_REPLY_LINKS
    path=tmp_path/'cases.sqlite3'
    store=EquipmentMemoryStore(path)
    for message in range(MAX_CASES+1):
        store.save(1,-10,message+1,{'location':'Гиппо'},now=100)
    assert store.load(2,-10,1,now=101) is None
    # A long active incident cannot grow reply links without limit.
    for message in range(MAX_CASES+2,MAX_CASES+MAX_REPLY_LINKS+3):
        store.save(2,-10,message,{'location':'Гиппо'},reply_message_id=message-1,now=102)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT count(*) FROM equipment_incidents').fetchone()[0]<=MAX_CASES
        assert db.execute('SELECT count(*) FROM equipment_reply_links').fetchone()[0]<=MAX_REPLY_LINKS
    assert store.load(1,-10,message,now=103+TTL) is None
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT count(*) FROM equipment_reply_links').fetchone()[0]==0


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
                m.from_user=NS(id=2)
                m.reply_to_message=NS(message_id=200,text='Гиппо · Jofemar Bluetec G23')
                second=NS(message_id=201,edit_text=AsyncMock())
                await bot.answer_owner_ai_message(m,NS(),'Подробнее',status=second,
                    equipment_context={'response_message_id':201,'reply_message_id':200})
                latest={**memory,'checks':'Вода есть; экран сфотографирован'}
                query.return_value={'scope':'staff','answer':ANSWER,'equipment_memory':latest}
                await bot.answer_owner_ai_message(m,NS(),'Экран сфотографирован',status=NS(message_id=202,edit_text=AsyncMock()),
                    equipment_context={'response_message_id':202,'reply_message_id':201})
                m.from_user=NS(id=3)
                await bot.answer_owner_ai_message(m,NS(),'Что дальше?',status=NS(message_id=203,edit_text=AsyncMock()),
                    equipment_context={'response_message_id':203,'reply_message_id':200})
                other={'location':'Гиппо','checks':'NEW_CHECK'}
                query.return_value={'scope':'staff','answer':ANSWER,'equipment_memory':other}
                await bot.answer_owner_ai_message(m,NS(),'На Гиппо другая ошибка',status=NS(message_id=204,edit_text=AsyncMock()),
                    equipment_context={'response_message_id':204,'reply_message_id':200})
            sent=query.call_args_list[-2].kwargs['equipment_context']
            self.assertEqual(sent['previous_case']['checks'],latest['checks'])
            self.assertEqual(EquipmentMemoryStore(path).load(1,-10,201),latest)
            self.assertEqual(EquipmentMemoryStore(path).load(1,-10,204),other)
            self.assertEqual(query.call_args.kwargs['user_id'],3)

    async def test_owner_scope_response_cannot_be_saved_in_group_memory(self):
        store=Mock()
        async def inline(fn,*args,**kwargs): return fn(*args,**kwargs)
        with patch.object(bot,'get_owner_ai_client_config',return_value=object()), \
             patch.object(bot,'run_blocking',side_effect=inline), \
             patch.object(bot,'build_owner_ai_maintenance_context',return_value={}), \
             patch.object(bot,'query_owner_ai',return_value={'scope':'owner','answer':'private','equipment_memory':{'checks':'private'}}), \
             patch.object(bot,'get_equipment_memory_store',return_value=store):
            status=NS(message_id=200,edit_text=AsyncMock())
            await bot.answer_owner_ai_message(NS(from_user=NS(id=1),chat_id=-10,reply_to_message=None),NS(),
                'Гиппо ошибка',status=status,equipment_context={'response_message_id':200})
            store.save.assert_not_called()
