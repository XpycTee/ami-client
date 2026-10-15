"""
Юнит-тесты абстрактной базы AMIClientBase (ami/base.py).
"""
import asyncio

import pytest

from ami.base import AMIClientBase


class DummyClient(AMIClientBase):
    """Конкретная реализация базы для тестирования общих методов."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.requests = []
        self.close_called = False

    async def connect(self, username, password):
        return [{'Response': 'Success'}]

    async def ami_request(self, query):
        self.requests.append(dict(query))
        return [{'Response': 'Success'}]

    async def event_dispatch(self):
        return None

    async def close(self):
        self.close_called = True


class DummyNoClose(AMIClientBase):
    """База без метода close — проверка logoff без close."""

    async def connect(self, username, password):
        return []

    async def ami_request(self, query):
        return []

    async def event_dispatch(self):
        return None


# --------------------------------------------------------------------------
# Конструктор
# --------------------------------------------------------------------------

def test_init_ok():
    client = DummyClient('localhost', 5038)
    assert client.host == 'localhost'
    assert client.port == 5038
    assert client.running is False
    assert client._event_callbacks == {}
    assert client._loop_tasks == []
    assert client._queues == {}
    assert client._ssl_enabled is False
    assert client._cert_chain is None


def test_init_cert_ca_without_ssl_raises():
    with pytest.raises(AttributeError, match='ssl_enabled'):
        DummyClient('localhost', 5038, cert_ca='/tmp/ca.pem')


def test_init_cert_ca_with_ssl_ok():
    client = DummyClient('localhost', 5038, ssl_enabled=True, cert_ca='/tmp/ca.pem')
    assert client._cert_chain == '/tmp/ca.pem'
    assert client._ssl_enabled is True


# --------------------------------------------------------------------------
# register_callback
# --------------------------------------------------------------------------

async def _dummy_cb(event, client):
    return None


async def test_register_callback_new_event():
    client = DummyClient('localhost', 5038)
    await client.register_callback('Newchannel', _dummy_cb)
    assert client._event_callbacks == {'Newchannel': [_dummy_cb]}


async def test_register_callback_existing_event_appends():
    client = DummyClient('localhost', 5038)
    await client.register_callback('Newchannel', _dummy_cb)
    await client.register_callback('Newchannel', _dummy_cb)
    assert len(client._event_callbacks['Newchannel']) == 2


# --------------------------------------------------------------------------
# _login / logoff
# --------------------------------------------------------------------------

async def test_login_sends_credentials():
    client = DummyClient('localhost', 5038)
    resp = await client._login('hello', 'world')
    assert client.requests == [{'Action': 'Login', 'UserName': 'hello', 'Secret': 'world'}]
    assert resp == [{'Response': 'Success'}]


async def test_logoff():
    client = DummyClient('localhost', 5038)
    client.running = True
    resp = await client.logoff()
    assert client.requests == [{'Action': 'LogOff'}]
    assert resp == [{'Response': 'Success'}]
    assert client.running is False
    assert client.close_called is True
    assert client._loop_tasks == []


async def test_logoff_without_close_does_not_fail():
    client = DummyNoClose('localhost', 5038)
    client.running = True
    await client.logoff()
    assert client.running is False


async def test_logoff_gathers_pending_tasks():
    client = DummyClient('localhost', 5038)
    finished = asyncio.Event()

    async def worker():
        finished.set()

    task = asyncio.create_task(worker())
    client._loop_tasks.append(task)
    client.running = True

    await client.logoff()
    assert task.done()
    assert client._loop_tasks == []


# --------------------------------------------------------------------------
# channels / ping
# --------------------------------------------------------------------------

async def test_channels():
    client = DummyClient('localhost', 5038)
    await client.channels()
    assert client.requests == [{'Action': 'CoreShowChannels'}]


async def test_ping():
    client = DummyClient('localhost', 5038)
    await client.ping()
    assert client.requests == [{'Action': 'Ping'}]


# --------------------------------------------------------------------------
# originate
# --------------------------------------------------------------------------

async def test_originate_basic():
    client = DummyClient('localhost', 5038)
    await client.originate(originator='SIP/100', extension='200')
    data = client.requests[-1]
    assert data['Action'] == 'Originate'
    assert data['Channel'] == 'Local/SIP/100@from-internal'
    assert data['Exten'] == '200'
    assert data['Context'] == 'from-internal'
    assert data['Priority'] == 1
    assert data['Async'] is True
    assert data['Timeout'] == 15000
    assert data['Callerid'] == '200'


async def test_originate_custom_context_priority_async():
    client = DummyClient('localhost', 5038)
    await client.originate(
        originator='SIP/100', extension='200',
        context='myctx', priority=5, run_async=False, timeout=10,
    )
    data = client.requests[-1]
    assert data['Channel'] == 'Local/SIP/100@myctx'
    assert data['Context'] == 'myctx'
    assert data['Priority'] == 5
    assert data['Async'] is False
    assert data['Timeout'] == 10000


async def test_originate_caller_id_overrides_extension():
    client = DummyClient('localhost', 5038)
    await client.originate(originator='SIP/100', extension='200', caller_id='+79990000000')
    data = client.requests[-1]
    assert data['Callerid'] == '+79990000000'


async def test_originate_application_and_data():
    client = DummyClient('localhost', 5038)
    await client.originate(originator='SIP/100', extension='200',
                           application='Playback', app_data='hello')
    data = client.requests[-1]
    assert data['Application'] == 'Playback'
    assert data['Data'] == 'hello'


async def test_originate_application_without_data_raises():
    client = DummyClient('localhost', 5038)
    with pytest.raises(Exception, match='Data'):
        await client.originate(originator='SIP/100', extension='200', application='Playback')


async def test_originate_data_without_application_raises():
    client = DummyClient('localhost', 5038)
    with pytest.raises(Exception, match='Application'):
        await client.originate(originator='SIP/100', extension='200', app_data='hello')


async def test_originate_optional_params():
    client = DummyClient('localhost', 5038)
    await client.originate(
        originator='SIP/100', extension='200',
        account='acc', early_media=True, codecs=['alaw', 'ulaw'],
        other_channel_id='abc', variables=['var1=1', 'var2=2'],
    )
    data = client.requests[-1]
    assert data['Account'] == 'acc'
    assert data['EarlyMedia'] is True
    assert data['Codecs[0]'] == 'alaw'
    assert data['Codecs[1]'] == 'ulaw'
    assert data['OtherChannelId'] == 'abc'
    assert data['Variable[0]'] == 'var1=1'
    assert data['Variable[1]'] == 'var2=2'


# --------------------------------------------------------------------------
# redirect
# --------------------------------------------------------------------------

async def test_redirect_basic():
    client = DummyClient('localhost', 5038)
    await client.redirect(channel='SIP/100', extension=200)
    data = client.requests[-1]
    assert data['Action'] == 'Redirect'
    assert data['Channel'] == 'SIP/100'
    assert data['Exten'] == 200
    assert data['Context'] == 'from-internal'
    assert data['Priority'] == 1


async def test_redirect_extra_params():
    client = DummyClient('localhost', 5038)
    await client.redirect(
        channel='SIP/100', extension=200, context='ctx', priority=2,
        extra_channel='SIP/101', extra_extension=201,
        extra_context='ctx2', extra_priority=3,
    )
    data = client.requests[-1]
    assert data['ExtraChannel'] == 'SIP/101'
    assert data['ExtraExten'] == 201
    assert data['ExtraContext'] == 'ctx2'
    assert data['ExtraPriority'] == 3


# --------------------------------------------------------------------------
# transfers
# --------------------------------------------------------------------------

async def test_blind_transfer():
    client = DummyClient('localhost', 5038)
    await client.blind_transfer(channel='SIP/100', extension='200')
    assert client.requests[-1] == {
        'Action': 'BlindTransfer', 'Channel': 'SIP/100', 'Exten': '200', 'Context': 'from-internal',
    }


async def test_attended_transfer():
    client = DummyClient('localhost', 5038)
    await client.attended_transfer(channel='SIP/100', extension='200')
    assert client.requests[-1] == {
        'Action': 'Atxfer', 'Channel': 'SIP/100', 'Exten': '200', 'Context': 'from-internal',
    }
