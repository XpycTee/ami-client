"""
Юнит-тесты TCPClient (ami/client/tcp_client.py).

Сетевой слой заменяется фейковыми reader/writer (фикстура make_tcp_client),
а macOS-специфичные ветки — через monkeypatch sys.platform.
"""
import asyncio
import logging
import os
import ssl
import subprocess
import sys

import pytest

from ami.client.tcp_client import TCPClient
from conftest import FakeWriter


async def _stop_tcp(client):
    """Корректно останавливает клиент: останавливает фоновые задачи и закрывает writer."""
    client.running = False
    if 'events' in client._queues:
        await client._queues['events'].put(None)
    await asyncio.gather(*client._loop_tasks, return_exceptions=True)
    await client.close()


# --------------------------------------------------------------------------
# Конструктор
# --------------------------------------------------------------------------

def test_init_default_port():
    client = TCPClient('localhost')
    assert client.port == 5038
    assert client._reader is None
    assert client._writer is None
    assert client._encoding == 'windows-1251'


def test_init_ssl_switches_port():
    client = TCPClient('localhost', ssl_enabled=True)
    assert client.port == 5039


def test_init_ssl_with_explicit_port_keeps_it():
    client = TCPClient('localhost', port=9999, ssl_enabled=True)
    assert client.port == 9999


def test_init_cert_ca_without_ssl_raises():
    with pytest.raises(AttributeError, match='ssl_enabled'):
        TCPClient('localhost', cert_ca='/tmp/ca.pem')


# --------------------------------------------------------------------------
# connect
# --------------------------------------------------------------------------

async def test_connect_already_running_raises(make_tcp_client):
    client, _, _ = make_tcp_client()
    client.running = True
    with pytest.raises(RuntimeError, match='already connected'):
        await client.connect('hello', 'world')
    await client.close()


async def test_connect_success(make_tcp_client):
    lines = [
        b'Response: Success\r\n',
        b'Message: Authentication accepted\r\n',
        b'\r\n',
    ]
    client, reader, writer = make_tcp_client(lines=lines)
    resp = await client.connect('hello', 'world')
    assert resp == [{'Response': 'Success', 'Message': 'Authentication accepted'}]
    assert client._reader is reader
    assert client._writer is writer
    assert b'Action: Login' in writer.written
    assert b'UserName: hello' in writer.written
    assert b'Secret: world' in writer.written
    assert len(client._loop_tasks) >= 2  # message_loop + event_dispatch (+ _receiving)
    await _stop_tcp(client)


async def test_connect_login_error_sets_running_false(make_tcp_client):
    lines = [
        b'Response: Error\r\n',
        b'Message: Authentication failed\r\n',
        b'\r\n',
    ]
    client, _, _ = make_tcp_client(lines=lines)
    resp = await client.connect('hello', 'world')
    assert resp[0]['Response'] == 'Error'
    assert client.running is False
    await _stop_tcp(client)


async def test_connect_os_error_raises(make_tcp_client):
    async def fail_open(*args, **kwargs):
        raise OSError('connection refused')

    client, _, _ = make_tcp_client()
    client._open_connection = fail_open  # type: ignore[assignment]
    with pytest.raises(OSError):
        await client.connect('hello', 'world')
    assert client.running is False
    await client.close()


async def test_connect_ssl_verify_retries_with_keychain(make_tcp_client, monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    lines = [
        b'Response: Success\r\n',
        b'Message: Authentication accepted\r\n',
        b'\r\n',
    ]
    client, reader, writer = make_tcp_client(lines=lines, ssl_enabled=True)
    calls = {'open': 0}

    async def flaky_open(ssl_context=None):
        calls['open'] += 1
        if calls['open'] == 1:
            raise ssl.SSLCertVerificationError('verify failed')
        client._reader = reader
        client._writer = writer
        return reader, writer

    client._open_connection = flaky_open  # type: ignore[assignment]
    monkeypatch.setattr(client, '_load_macos_keychain_certs', lambda ctx: 1)

    resp = await client.connect('hello', 'world')
    assert resp[0]['Response'] == 'Success'
    assert calls['open'] == 2
    await _stop_tcp(client)


async def test_connect_ssl_verify_no_keychain_certs_raises(make_tcp_client, monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'darwin')

    async def fail_open(*args, **kwargs):
        raise ssl.SSLCertVerificationError('verify failed')

    client, _, _ = make_tcp_client(ssl_enabled=True)
    client._open_connection = fail_open  # type: ignore[assignment]
    monkeypatch.setattr(client, '_load_macos_keychain_certs', lambda ctx: 0)

    with pytest.raises(ssl.SSLCertVerificationError):
        await client.connect('hello', 'world')
    assert client.running is False
    await client.close()


async def test_connect_ssl_verify_with_cert_chain_does_not_retry(make_tcp_client, monkeypatch):
    monkeypatch.setattr(ssl.SSLContext, 'load_verify_locations', lambda *a, **kw: None)
    monkeypatch.setattr(sys, 'platform', 'darwin')

    async def fail_open(*args, **kwargs):
        raise ssl.SSLCertVerificationError('verify failed')

    client, _, _ = make_tcp_client(ssl_enabled=True, cert_ca='/tmp/ca.pem')
    client._open_connection = fail_open  # type: ignore[assignment]
    monkeypatch.setattr(client, '_load_macos_keychain_certs', lambda ctx: 1)

    with pytest.raises(ssl.SSLCertVerificationError):
        await client.connect('hello', 'world')
    assert client.running is False
    await client.close()


async def test_connect_ssl_retry_second_attempt_fails(make_tcp_client, monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'darwin')

    async def fail_open(*args, **kwargs):
        raise ssl.SSLCertVerificationError('verify failed')

    client, _, _ = make_tcp_client(ssl_enabled=True)
    client._open_connection = fail_open  # type: ignore[assignment]
    monkeypatch.setattr(client, '_load_macos_keychain_certs', lambda ctx: 1)

    with pytest.raises(ssl.SSLCertVerificationError):
        await client.connect('hello', 'world')
    assert client.running is False
    await client.close()


# --------------------------------------------------------------------------
# Статические хелперы
# --------------------------------------------------------------------------

def test_format_value():
    assert TCPClient._format_value(True) == 'true'
    assert TCPClient._format_value(False) == 'false'
    assert TCPClient._format_value(7) == '7'
    assert TCPClient._format_value('text') == 'text'


def test_dict_to_headers_skips_none_and_strips_indices():
    result = TCPClient._dict_to_headers({
        'Action': 'Ping',
        'Codecs[0]': 'alaw',
        'Async': True,
        'SkipMe': None,
    })
    assert 'Action: Ping\r\n' in result
    assert 'Codecs: alaw\r\n' in result
    assert 'Async: true\r\n' in result
    assert 'SkipMe' not in result
    assert result.endswith('\r\n\r\n')


def test_message_to_dict():
    result = TCPClient._message_to_dict(['Response: Success', 'Message: OK', 'Variable:'])
    assert result == {'Response': 'Success', 'Message': 'OK', 'Variable': ''}


# --------------------------------------------------------------------------
# _receiving
# --------------------------------------------------------------------------

async def test_receiving_collects_messages_and_sentinel(make_tcp_client):
    lines = [
        b'Event: Test\r\n',
        b'Channel: SIP/foo\r\n',
        b'\r\n',
        b'Response: Success\r\n',
        b'\r\n',
    ]
    client, reader, _ = make_tcp_client(lines=lines)
    client._reader = reader
    client._queues['messages'] = asyncio.Queue()
    client.running = True

    await client._receiving()
    assert client.running is False

    assert await client._queues['messages'].get() == ['Event: Test', 'Channel: SIP/foo']
    assert await client._queues['messages'].get() == ['Response: Success']
    assert await client._queues['messages'].get() is None
    assert client._queues['messages'].empty()


async def test_receiving_timeout_retries(make_tcp_client):
    class SlowReader:
        async def readline(self):
            await asyncio.sleep(10)
            return b''

    client, _, _ = make_tcp_client()
    client._reader = SlowReader()
    client._queues['messages'] = asyncio.Queue()
    client._read_timeout = 0.05
    client.running = True

    task = asyncio.create_task(client._receiving())
    await asyncio.sleep(0.1)  # переживёт хотя бы один TimeoutError → continue
    client.running = False
    await task


async def test_receiving_connection_error_stops(make_tcp_client):
    class BrokenReader:
        async def readline(self):
            raise ConnectionResetError('reset')

    client, _, _ = make_tcp_client()
    client._reader = BrokenReader()
    client._queues['messages'] = asyncio.Queue()
    client.running = True

    await client._receiving()
    assert client.running is False
    # sentinel добавлен, чтобы message_loop завершился
    assert await client._queues['messages'].get() is None


async def test_receiving_without_reader_returns(make_tcp_client):
    client, _, _ = make_tcp_client()
    client._reader = None
    client._queues['messages'] = asyncio.Queue()
    await client._receiving()  # не должно упасть


async def test_fail_pending():
    client = TCPClient('localhost')
    loop = asyncio.get_running_loop()
    f1 = loop.create_future()
    f2 = loop.create_future()
    client._pending = {'a': f1, 'b': f2}
    client._fail_pending(ConnectionError('closed'))
    assert f1.done() and isinstance(f1.exception(), ConnectionError)
    assert f2.done() and isinstance(f2.exception(), ConnectionError)
    assert client._pending == {}


# --------------------------------------------------------------------------
# Callbacks и диспетчеризация событий
# --------------------------------------------------------------------------

async def test_get_functions_combines_event_and_wildcard():
    client = TCPClient('localhost')

    async def cb1(event, client):
        pass

    async def cb2(event, client):
        pass

    await client.register_callback('Newchannel', cb1)
    await client.register_callback('*', cb2)
    assert client._get_functions('Newchannel') == [cb1, cb2]
    assert client._get_functions('Unknown') == [cb2]


async def test_run_callback_success():
    client = TCPClient('localhost')
    calls = []

    async def cb(event, client):
        calls.append(event)

    await client._run_callback(cb, {'Event': 'Test'})
    assert len(calls) == 1


async def test_run_callback_exception_logged(caplog):
    client = TCPClient('localhost')

    async def cb(event, client):
        raise ValueError('boom')

    with caplog.at_level(logging.ERROR):
        await client._run_callback(cb, {'Event': 'Test'})
    assert 'boom' in caplog.text


async def test_event_dispatch_runs_callback(make_tcp_client):
    client, _, _ = make_tcp_client()
    client._queues['events'] = asyncio.Queue()
    client.running = True
    received = asyncio.Event()

    async def cb(event, client):
        received.set()

    await client.register_callback('Newchannel', cb)
    task = asyncio.create_task(client.event_dispatch())
    await client._queues['events'].put({'Event': 'Newchannel'})
    await asyncio.wait_for(received.wait(), timeout=2)
    client.running = False
    await client._queues['events'].put(None)
    await task


# --------------------------------------------------------------------------
# message_loop
# --------------------------------------------------------------------------

async def test_message_loop_routes_event(make_tcp_client):
    lines = [
        b'Event: Newchannel\r\n',
        b'Channel: SIP/foo\r\n',
        b'\r\n',
    ]
    client, reader, _ = make_tcp_client(lines=lines)
    client._reader = reader
    client._queues['messages'] = asyncio.Queue()
    client._queues['events'] = asyncio.Queue()
    client.running = True

    await client.message_loop()
    event = await client._queues['events'].get()
    assert event == {'Event': 'Newchannel', 'Channel': 'SIP/foo'}


async def test_message_loop_routes_response(make_tcp_client):
    lines = [
        b'Response: Success\r\n',
        b'ActionID: abc\r\n',
        b'\r\n',
    ]
    client, reader, _ = make_tcp_client(lines=lines)
    client._reader = reader
    client._queues['messages'] = asyncio.Queue()
    client._queues['events'] = asyncio.Queue()
    client.running = True
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    client._pending['abc'] = future

    await client.message_loop()
    assert future.done()
    assert future.result() == [{'Response': 'Success', 'ActionID': 'abc'}]


async def test_message_loop_unknown_type_logs(make_tcp_client, caplog):
    lines = [
        b'Unknown: thing\r\n',
        b'\r\n',
    ]
    client, reader, _ = make_tcp_client(lines=lines)
    client._reader = reader
    client._queues['messages'] = asyncio.Queue()
    client._queues['events'] = asyncio.Queue()
    client.running = True

    with caplog.at_level(logging.ERROR):
        await client.message_loop()
    assert 'Проблемы с определением типа сообщения' in caplog.text


# --------------------------------------------------------------------------
# _handle_response
# --------------------------------------------------------------------------

async def test_handle_response_simple():
    client = TCPClient('localhost')
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    client._pending['id1'] = future

    await client._handle_response({'Response': 'Success', 'ActionID': 'id1'})
    assert future.result() == [{'Response': 'Success', 'ActionID': 'id1'}]


async def test_handle_response_no_actionid_falls_back_to_oldest():
    client = TCPClient('localhost')
    loop = asyncio.get_running_loop()
    f1 = loop.create_future()
    f2 = loop.create_future()
    client._pending = {'older': f1, 'newer': f2}

    await client._handle_response({'Response': 'Success'})
    assert f1.done()
    assert not f2.done()


async def test_handle_response_no_pending_warns(caplog):
    client = TCPClient('localhost')
    with caplog.at_level(logging.WARNING):
        await client._handle_response({'Response': 'Success', 'ActionID': 'nope'})
    assert 'without pending request' in caplog.text


async def test_handle_response_eventlist_collects_until_complete(make_tcp_client):
    client, _, _ = make_tcp_client()
    client._queues['messages'] = asyncio.Queue()
    client.running = True
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    client._pending['id1'] = future

    # start-сообщение передаётся аргументом, в очереди — только продолжение списка
    await client._queues['messages'].put(['Event: CoreShowChannel', 'ActionID: id1'])
    await client._queues['messages'].put(['Event: CoreShowChannelsComplete', 'EventList: Complete', 'ActionID: id1'])

    await client._handle_response({'Response': 'Success', 'EventList': 'start', 'ActionID': 'id1'})
    result = future.result()
    assert len(result) == 3
    assert result[0]['EventList'] == 'start'
    assert result[1]['Event'] == 'CoreShowChannel'
    assert result[2]['EventList'] == 'Complete'


async def test_handle_response_eventlist_returns_foreign_message(make_tcp_client):
    client, _, _ = make_tcp_client()
    client._queues['messages'] = asyncio.Queue()
    client.running = True
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    client._pending['id1'] = future

    await client._queues['messages'].put(['Event: Other', 'ActionID: other'])
    await client._queues['messages'].put(['Event: CoreShowChannelsComplete', 'EventList: Complete', 'ActionID: id1'])

    await client._handle_response({'Response': 'Success', 'EventList': 'start', 'ActionID': 'id1'})
    result = future.result()
    assert len(result) == 2
    # чужое сообщение возвращено в очередь
    assert await client._queues['messages'].get() == ['Event: Other', 'ActionID: other']


async def test_handle_response_eventlist_timeout(make_tcp_client):
    client, _, _ = make_tcp_client()
    client._queues['messages'] = asyncio.Queue()
    client._read_timeout = 0.05
    client.running = True
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    client._pending['id1'] = future

    await client._handle_response({'Response': 'Success', 'EventList': 'start', 'ActionID': 'id1'})
    assert future.result() == [{'Response': 'Success', 'EventList': 'start', 'ActionID': 'id1'}]


# --------------------------------------------------------------------------
# ami_request
# --------------------------------------------------------------------------

async def test_ami_request_not_connected_raises():
    client = TCPClient('localhost')
    with pytest.raises(RuntimeError, match='not connected'):
        await client.ami_request({'Action': 'Ping'})


async def test_ami_request_writes_and_resolves(make_tcp_client):
    client, _, writer = make_tcp_client()
    client._queues['messages'] = asyncio.Queue()
    client._writer = writer

    task = asyncio.create_task(client.ami_request({'Action': 'Ping'}))
    await asyncio.sleep(0.01)
    assert b'Action: Ping' in writer.written
    assert b'ActionID:' in writer.written

    action_id = next(iter(client._pending))
    client._pending[action_id].set_result([{'Response': 'Success', 'ActionID': action_id}])
    resp = await asyncio.wait_for(task, timeout=2)
    assert resp == [{'Response': 'Success', 'ActionID': action_id}]
    assert action_id not in client._pending


async def test_ami_request_times_out(make_tcp_client):
    client, _, writer = make_tcp_client()
    client._writer = writer
    client._request_timeout = 0.05

    with pytest.raises(asyncio.TimeoutError):
        await client.ami_request({'Action': 'Ping'})
    assert client._pending == {}


# --------------------------------------------------------------------------
# close / контекстный менеджер
# --------------------------------------------------------------------------

async def test_close_closes_writer(make_tcp_client):
    client, _, writer = make_tcp_client()
    client._writer = writer
    client._reader = object()
    await client.close()
    assert writer.closed is True
    assert client._writer is None
    assert client._reader is None


async def test_close_without_writer():
    client = TCPClient('localhost')
    await client.close()
    assert client._writer is None


async def test_aenter_aexit_not_running_closes(make_tcp_client):
    client, _, writer = make_tcp_client()
    client._writer = writer
    async with client as c:
        assert c is client
    assert client._writer is None
    assert writer.closed is True


async def test_aexit_running_logs_off(make_tcp_client):
    client, _, writer = make_tcp_client()
    client._writer = writer
    client.running = True
    called = {}

    async def fake_logoff():
        called['logoff'] = True
        client.running = False
        return []

    client.logoff = fake_logoff  # type: ignore[method-assign]
    async with client as c:
        assert c is client
    assert called.get('logoff') is True


async def test_close_handles_wait_closed_error(make_tcp_client):
    class BadWriter(FakeWriter):
        async def wait_closed(self):
            raise ConnectionError('boom')

    client, _, _ = make_tcp_client()
    client._writer = BadWriter()
    await client.close()
    assert client._writer is None


# --------------------------------------------------------------------------
# _receiving: сброс незавершённого сообщения при EOF
# --------------------------------------------------------------------------

async def test_receiving_eof_flushes_remaining_lines(make_tcp_client):
    # Сообщение без завершающей пустой строки должно быть сброшено при EOF
    lines = [b'Event: Test\r\n', b'Channel: SIP/foo\r\n']
    client, reader, _ = make_tcp_client(lines=lines)
    client._reader = reader
    client._queues['messages'] = asyncio.Queue()
    client.running = True

    await client._receiving()
    assert await client._queues['messages'].get() == ['Event: Test', 'Channel: SIP/foo']
    assert await client._queues['messages'].get() is None


# --------------------------------------------------------------------------
# message_loop: таймаут ожидания сообщения
# --------------------------------------------------------------------------

async def test_message_loop_timeout_continues(make_tcp_client):
    client, _, _ = make_tcp_client()
    client._queues['messages'] = asyncio.Queue()
    client._queues['events'] = asyncio.Queue()
    client._reader = None  # _receiving сразу вернётся, очередь останется пустой
    client.running = True

    task = asyncio.create_task(client.message_loop())
    await asyncio.sleep(1.1)  # переживём один таймаут get() (timeout=1)
    client.running = False
    await asyncio.wait_for(task, timeout=2)


# --------------------------------------------------------------------------
# _handle_response: sentinel во время сбора EventList
# --------------------------------------------------------------------------

async def test_handle_response_eventlist_sentinel_breaks(make_tcp_client):
    client, _, _ = make_tcp_client()
    client._queues['messages'] = asyncio.Queue()
    client.running = True
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    client._pending['id1'] = future

    await client._queues['messages'].put(None)  # sentinel: соединение закрыто
    await client._handle_response({'Response': 'Success', 'EventList': 'start', 'ActionID': 'id1'})
    assert future.result() == [{'Response': 'Success', 'EventList': 'start', 'ActionID': 'id1'}]


# --------------------------------------------------------------------------
# _load_macos_keychain_certs
# --------------------------------------------------------------------------

def test_load_macos_keychain_certs_non_darwin(monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'linux')
    client = TCPClient('localhost')
    assert client._load_macos_keychain_certs(ssl.create_default_context()) == 0


def test_load_macos_keychain_certs_no_keychains(monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(os.path, 'exists', lambda path: False)
    client = TCPClient('localhost')
    assert client._load_macos_keychain_certs(ssl.create_default_context()) == 0


def test_load_macos_keychain_certs_darwin_loads_certs(monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(os.path, 'exists', lambda path: True)
    pem = '-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n'

    class FakeProc:
        returncode = 0
        stdout = pem

    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: FakeProc())
    loaded = []
    monkeypatch.setattr(ssl.SSLContext, 'load_verify_locations',
                        lambda self, **kw: loaded.append(kw))
    client = TCPClient('localhost')
    assert client._load_macos_keychain_certs(ssl.create_default_context()) == 2
    assert loaded == [{'cadata': pem + '\n' + pem}]


def test_load_macos_keychain_certs_exception_logged(monkeypatch, caplog):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(os.path, 'exists', lambda path: True)

    def boom(*a, **kw):
        raise OSError('nope')

    monkeypatch.setattr(subprocess, 'run', boom)
    client = TCPClient('localhost')
    with caplog.at_level(logging.DEBUG):
        count = client._load_macos_keychain_certs(ssl.create_default_context())
    assert count == 0
    assert 'nope' in caplog.text
