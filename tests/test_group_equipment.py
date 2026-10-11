import base64
import asyncio
from functools import wraps
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest
from telegram.ext import ApplicationHandlerStop
import group_equipment as support
from owner_ai_client import OwnerAiClientConfig, query_owner_ai


def async_test(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return run


def message(text='@coffee_bot ошибка на Макси', **kwargs):
    values=dict(text=text, caption=None, reply_to_message=None, entities=(), caption_entities=(),
                from_user=NS(id=123, is_bot=False), chat_id=-1001, photo=[], message_id=9,
                reply_text=AsyncMock(return_value=NS(message_id=10, edit_text=AsyncMock())))
    values.update(kwargs)
    return NS(**values)


def setup():
    application=NS(add_handler=Mock(), create_task=Mock())
    host=NS(is_allowed_user=Mock(return_value=True), is_allowed_group_chat=Mock(return_value=True),
            owner_ai_api_configured=Mock(return_value=True), OWNER_AI_WORK_QUEUE=NS(submit=Mock(return_value=True)),
            answer_owner_ai_message=AsyncMock(), logger=Mock())
    support.register(application, host)
    assert application.add_handler.call_args.kwargs["group"] == -4  # before stock (-3) and reports (-1/0)
    handler=application.add_handler.call_args.args[0].callback
    context=NS(bot=NS(id=777,username='coffee_bot',get_file=AsyncMock()), application=application)
    return handler,host,context


@pytest.mark.parametrize('text,expected',[
    ('@coffee_bot ошибка',True),('@COFFEE_BOT ошибка',True),
    ('@coffee_bot_other ошибка',False),('обслужил Макси',False),('проблема у @other_bot',False)])
def test_only_explicit_mention(text,expected):
    assert support.addressed(message(text),NS(id=777,username='coffee_bot')) is expected


def test_reply_to_this_bot_only():
    for user,expected in [(777,True),(888,False)]:
        m=message('а что дальше?',reply_to_message=NS(from_user=NS(id=user)))
        assert support.addressed(m,NS(id=777,username='coffee_bot')) is expected


@async_test
async def test_ordinary_report_does_not_queue_ai_or_stop_accounting():
    handler,host,ctx=setup()
    await handler(NS(effective_message=message('Макси обслужил'),edited_message=None),ctx)
    host.OWNER_AI_WORK_QUEUE.submit.assert_not_called()


@async_test
@pytest.mark.parametrize('allowed,group,edited',[(False,True,False),(True,False,False),(True,True,True)])
async def test_denied_or_edited_question_cannot_fall_through_to_accounting(allowed,group,edited):
    handler,host,ctx=setup()
    host.is_allowed_user.return_value=allowed;host.is_allowed_group_chat.return_value=group
    with pytest.raises(ApplicationHandlerStop):
        await handler(NS(effective_message=message(),edited_message=object() if edited else None),ctx)
    host.OWNER_AI_WORK_QUEUE.submit.assert_not_called()


@async_test
async def test_question_stops_other_handlers_and_preserves_reply_identity():
    handler,host,ctx=setup()
    m=message(reply_to_message=NS(message_id=5,from_user=NS(id=777),photo=[]))
    with pytest.raises(ApplicationHandlerStop):
        await handler(NS(effective_message=m,edited_message=None),ctx)
    work=host.OWNER_AI_WORK_QUEUE.submit.call_args.args[1]
    await work()
    args=host.answer_owner_ai_message.call_args
    assert args.args[2]=='ошибка на Макси'
    assert args.kwargs['equipment_context']=={'response_message_id':10,'reply_message_id':5}


@async_test
async def test_photo_download_uses_inline_bytes_not_token_url():
    m=message(photo=[NS(file_size=4,width=320,height=320,file_id='file')])
    bot=NS(get_file=AsyncMock(return_value=NS(download_as_bytearray=AsyncMock(return_value=b'\xff\xd8\xff\xd9'))))
    assert await support.image_data(m,bot)=='data:image/jpeg;base64,'+base64.b64encode(b'\xff\xd8\xff\xd9').decode()


def test_equipment_client_uses_dedicated_large_contract():
    import json
    class Response:
        status=200
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,n):return b'{"version":"1","scope":"staff","answer":"ok"}'
    opener=Mock(return_value=Response())
    config=OwnerAiClientConfig(url='https://example.com/query',token='x'*32)
    context={'image_data':'data:image/jpeg;base64,'+'x'*20000}
    query_owner_ai(config,user_id=123,question='error',conversation_id='telegram:-1:123',
                   equipment_context=context,urlopen=opener)
    request=opener.call_args.args[0]
    assert request.full_url=='https://example.com/query/equipment'
    assert json.loads(request.data)['equipment_context']==context
    assert opener.call_args.kwargs['timeout']==100
