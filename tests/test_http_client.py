"""
Юнит-тесты HTTPClient (ami/client/http_client.py).

HTTP-запросы мокаются через httpx.MockTransport (фикстура make_http_client),
а macOS-специфичные ветки — через monkeypatch sys.platform и подменой
вспомогательных методов.

Важно: после любого успешного connect() в фоне работают задачи
event_dispatch/_event_receiving. Чтобы тест не зависал, каждый такой тест
останавливает их через _shutdown() в блоке finally.
"""
import asyncio
import logging
import os
import ssl
import subprocess
import sys
import urllib.parse

import httpx
import pytest

from ami.client.http_client import HTTPClient

from conftest import http_response


LOGIN_OK = 'Response: Success\r\nMessage: Authentication accepted\r\n\r\n'


def _parse_action(request: httpx.Request):
    """Достаёт параметр Action из URL запроса."""
    return request.url.params.get('Action')


async def _shutdown(client):
    """Останавливает фоновые задачи и закрывает клиента (устойчив к ошибкам)."""
    client.running = False
    if 'events' in client._queues:
        try:
            await client._queues['events'].put(None)
        except Exception:
            pass
    await asyncio.gather(*client._loop_tasks, return_exceptions=True)
    client._loop_tasks.clear()
    await client.close()


async def _wait_until(predicate, timeout=2.0):
    async def _loop():
        while not predicate():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(_loop(), timeout=timeout)


# --------------------------------------------------------------------------
# Конструктор
# --------------------------------------------------------------------------

def test_init_default_port_and_verify():
    client = HTTPClient('localhost')
    assert client.port == 8088
    assert client._verify is True
    assert client._encoding == 'windows-1251'
    assert client._read_timeout == 5.0
    assert client._request_timeout == 30.0


def test_init_ssl_switches_port_and_builds_context():
    client = HTTPClient('localhost', ssl_enabled=True)
    assert client.port == 8089
    assert isinstance(client._verify, ssl.SSLContext)


def test_init_ssl_with_cert_ca(monkeypatch):
    monkeypatch.setattr(ssl.SSLContext, 'load_verify_locations', lambda *a, **kw: None)
    client = HTTPClient('localhost', ssl_enabled=True, cert_ca='/tmp/ca.pem')
    assert isinstance(client._verify, ssl.SSLContext)
    assert client._cert_chain == '/tmp/ca.pem'


def test_init_ssl_with_explicit_port_keeps_it():
    client = HTTPClient('localhost', port=9443, ssl_enabled=True)
    assert client.port == 9443


# --------------------------------------------------------------------------
# connect
# --------------------------------------------------------------------------

async def test_connect_already_running_raises(make_http_client):
    client = make_http_client(lambda req: http_response(LOGIN_OK))
    client.running = True
    with pytest.raises(RuntimeError, match='already connected'):
        await client.connect('hello', 'world')
    await client.close()


async def test_connect_success(make_http_client):
    client = make_http_client(lambda req: http_response(LOGIN_OK))
    try:
        resp = await client.connect('hello', 'world')
        assert resp == [{'Response': 'Success', 'Message': 'Authentication accepted'}]
        assert client.running is True
        assert client._client is not None
        assert len(client._loop_tasks) == 1
    finally:
        await _shutdown(client)


async def test_connect_login_error_sets_running_false(make_http_client):
    client = make_http_client(
        lambda req: http_response('Response: Error\r\nMessage: Authentication failed\r\n\r\n'))
    try:
        resp = await client.connect('hello', 'world')
        assert resp[0]['Response'] == 'Error'
        assert client.running is False
    finally:
        await _shutdown(client)


async def test_connect_empty_response_keeps_running(make_http_client):
    # Пустой ответ не содержит 'Response: Error', поэтому running остаётся True
    client = make_http_client(lambda req: http_response(''))
    try:
        resp = await client.connect('hello', 'world')
        assert resp == [{}]
        assert client.running is True
    finally:
        await _shutdown(client)


async def test_connect_http_error_raises_and_resets(make_http_client):
    def handler(request):
        raise httpx.ConnectError('boom', request=request)

    client = make_http_client(handler)
    with pytest.raises(httpx.ConnectError):
        await client.connect('hello', 'world')
    assert client.running is False
    await client.close()


def _ssl_connect_error(request):
    try:
        raise ssl.SSLCertVerificationError('certificate verify failed')
    except ssl.SSLCertVerificationError as exc:
        raise httpx.ConnectError('boom', request=request) from exc


async def test_connect_ssl_verify_retries_with_keychain(make_http_client, monkeypatch):
    class RetryHandler:
        def __init__(self):
            self.calls = 0

        def __call__(self, request):
            self.calls += 1
            if self.calls == 1:
                _ssl_connect_error(request)
            return http_response(LOGIN_OK)

    monkeypatch.setattr(sys, 'platform', 'darwin')
    client = make_http_client(RetryHandler())
    monkeypatch.setattr(client, '_load_macos_keychain_certs', lambda ctx: 1)

    try:
        resp = await client.connect('hello', 'world')
        assert resp[0]['Response'] == 'Success'
        assert client.running is True
    finally:
        await _shutdown(client)


async def test_connect_ssl_verify_no_keychain_certs_raises(make_http_client, monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    client = make_http_client(_ssl_connect_error)
    monkeypatch.setattr(client, '_load_macos_keychain_certs', lambda ctx: 0)

    with pytest.raises(httpx.ConnectError):
        await client.connect('hello', 'world')
    assert client.running is False
    await client.close()


async def test_connect_ssl_verify_with_cert_chain_does_not_retry(make_http_client, monkeypatch):
    monkeypatch.setattr(ssl.SSLContext, 'load_verify_locations', lambda *a, **kw: None)
    monkeypatch.setattr(sys, 'platform', 'darwin')

    def handler(request):
        _ssl_connect_error(request)

    client = make_http_client(handler, ssl_enabled=True, cert_ca='/tmp/ca.pem')
    monkeypatch.setattr(client, '_load_macos_keychain_certs', lambda ctx: 1)

    with pytest.raises(httpx.ConnectError):
        await client.connect('hello', 'world')
    assert client.running is False
    await client.close()


async def test_connect_ssl_verify_retry_fails_second_time(make_http_client, monkeypatch):
    class AlwaysFail:
        def __call__(self, request):
            _ssl_connect_error(request)

    monkeypatch.setattr(sys, 'platform', 'darwin')
    client = make_http_client(AlwaysFail())
    monkeypatch.setattr(client, '_load_macos_keychain_certs', lambda ctx: 1)

    with pytest.raises(httpx.ConnectError):
        await client.connect('hello', 'world')
    assert client.running is False
    await client.close()


# --------------------------------------------------------------------------
# events / ami_request
# --------------------------------------------------------------------------

async def test_events_sends_wait_event(make_http_client):
    # Не проверяем handler напрямую: параллельно с events() фоновый
    # _event_receiving тоже шлёт WaitEvent, поэтому порядок вызовов handler
    # недетерминирован. Проверяем только результат events().
    client = make_http_client(lambda req: http_response('Event: Test\r\n\r\n'))
    try:
        await client.connect('hello', 'world')
        resp = await client.events(timeout=5)
        assert resp == [{'Event': 'Test'}]
    finally:
        await _shutdown(client)


async def test_ami_request_not_connected_raises():
    client = HTTPClient('localhost')
    with pytest.raises(RuntimeError, match='not connected'):
        await client.ami_request({'Action': 'Ping'})


async def test_ami_request_success(make_http_client):
    captured = {}

    def handler(request):
        captured['url'] = str(request.url)
        captured['method'] = request.method
        return http_response('Response: Success\r\nMessage: OK\r\n\r\n')

    client = make_http_client(handler)
    try:
        await client.connect('hello', 'world')
        resp = await client.ami_request({'Action': 'Ping'})
        assert resp == [{'Response': 'Success', 'Message': 'OK'}]
        assert 'rawman' in captured['url']
        assert 'Action=Ping' in captured['url']
        assert captured['method'] == 'GET'
    finally:
        await _shutdown(client)


async def test_ami_request_wait_event_not_limited_by_request_timeout(make_http_client):
    client = make_http_client(lambda req: http_response(''), request_timeout=0.001)
    try:
        await client.connect('hello', 'world')
        # WaitEvent не ограничивается request_timeout; с быстрым моком не должно быть TimeoutError
        resp = await client.ami_request({'Action': 'WaitEvent', 'Timeout': -1})
        assert resp == [{}]
    finally:
        await _shutdown(client)


async def test_ami_request_times_out(make_http_client):
    client = make_http_client(lambda req: http_response(LOGIN_OK), request_timeout=0.05)
    try:
        await client.connect('hello', 'world')

        blocker = asyncio.Event()

        async def slow_get(*args, **kwargs):
            await blocker.wait()  # никогда не завершается
            raise AssertionError('unreachable')

        client._client.get = slow_get  # type: ignore[method-assign]
        with pytest.raises(asyncio.TimeoutError):
            await client.ami_request({'Action': 'Ping'})
    finally:
        await _shutdown(client)


# --------------------------------------------------------------------------
# Статические хелперы
# --------------------------------------------------------------------------

def test_format_value():
    assert HTTPClient._format_value(True) == 'true'
    assert HTTPClient._format_value(False) == 'false'
    assert HTTPClient._format_value(42) == '42'
    assert HTTPClient._format_value('text') == 'text'


def test_dict_to_query_skips_none_and_formats():
    query = HTTPClient._dict_to_query({
        'Action': 'Originate',
        'Codecs[0]': 'alaw',
        'Codecs[1]': 'ulaw',
        'Async': True,
        'Empty': None,
        'Number': 5,
    })
    assert 'Action=Originate' in query
    # индексы удаляются; дублирующиеся имена схлопываются в последнее значение
    assert 'Codecs=ulaw' in query
    assert query.count('Codecs=') == 1
    assert 'Async=true' in query
    assert 'Empty' not in query
    assert 'Number=5' in query


def test_headers_to_dict_parses_blocks():
    text = ('Response: Success\r\nMessage: OK\r\n\r\n'
            'Event: Test\r\nChannel: SIP/foo\r\n\r\n')
    result = HTTPClient._headers_to_dict(text)
    assert result == [
        {'Response': 'Success', 'Message': 'OK'},
        {'Event': 'Test', 'Channel': 'SIP/foo'},
    ]


def test_headers_to_dict_empty_value():
    result = HTTPClient._headers_to_dict('Response: Success\r\nVariable:\r\n\r\n')
    assert result == [{'Response': 'Success', 'Variable': ''}]


def test_headers_to_dict_empty_body():
    assert HTTPClient._headers_to_dict('') == [{}]


# --------------------------------------------------------------------------
# Callbacks и диспетчеризация событий
# --------------------------------------------------------------------------

async def test_get_functions_combines_event_and_wildcard():
    client = HTTPClient('localhost')

    async def cb1(event, client):
        pass

    async def cb2(event, client):
        pass

    await client.register_callback('Newchannel', cb1)
    await client.register_callback('*', cb2)
    assert client._get_functions('Newchannel') == [cb1, cb2]
    assert client._get_functions('Unknown') == [cb2]


async def test_run_callback_success():
    client = HTTPClient('localhost')
    calls = []

    async def cb(event, client):
        calls.append((event, client))

    await client._run_callback(cb, {'Event': 'Test'})
    assert len(calls) == 1


async def test_run_callback_exception_logged(caplog):
    client = HTTPClient('localhost')

    async def cb(event, client):
        raise ValueError('boom')

    with caplog.at_level(logging.ERROR):
        await client._run_callback(cb, {'Event': 'Test'})
    assert 'boom' in caplog.text


async def test_event_dispatch_runs_callback(make_http_client):
    client = make_http_client(lambda req: http_response(''))
    try:
        await client.connect('hello', 'world')
        received = asyncio.Event()

        async def cb(event, client):
            received.set()

        await client.register_callback('Newchannel', cb)
        await client._queues['events'].put({'Event': 'Newchannel'})
        await asyncio.wait_for(received.wait(), timeout=2)
    finally:
        await _shutdown(client)


async def test_event_receiving_puts_events_and_skips_complete(make_http_client):
    events_body = ('Event: Newchannel\r\nChannel: SIP/foo\r\n\r\n'
                   'Event: WaitEventComplete\r\n\r\n'
                   'Event: VarSet\r\n\r\n')
    client = make_http_client(lambda req: http_response(events_body))
    try:
        await client.connect('hello', 'world')

        task = asyncio.create_task(client._event_receiving())
        await asyncio.sleep(0.1)
        client.running = False
        await task

        queue = client._queues['events']
        names = []
        while not queue.empty():
            names.append((await queue.get())['Event'])
        assert 'Newchannel' in names
        assert 'VarSet' in names
        assert 'WaitEventComplete' not in names
    finally:
        await _shutdown(client)


async def test_event_receiving_timeout_retries(make_http_client):
    async def handler(request):
        await asyncio.sleep(0.2)
        return http_response('')

    client = make_http_client(handler, read_timeout=0.05)
    try:
        await client.connect('hello', 'world')

        task = asyncio.create_task(client._event_receiving())
        await asyncio.sleep(0.1)  # дольше read_timeout → TimeoutError → continue
        client.running = False
        await task
    finally:
        await _shutdown(client)


async def test_event_receiving_connection_error_stops(make_http_client):
    def handler(request):
        if _parse_action(request) == 'Login':
            return http_response(LOGIN_OK)
        raise httpx.ConnectError('boom', request=request)

    client = make_http_client(handler)
    try:
        await client.connect('hello', 'world')
        # event_dispatch из connect уже запустил _event_receiving; он должен остановиться
        await _wait_until(lambda: client.running is False)
        assert client.running is False
    finally:
        await _shutdown(client)


# --------------------------------------------------------------------------
# close / контекстный менеджер
# --------------------------------------------------------------------------

async def test_close_idempotent(make_http_client):
    client = make_http_client(lambda req: http_response(''))
    await client.close()
    try:
        await client.connect('hello', 'world')
        assert client._client is not None
    finally:
        await _shutdown(client)
    await client.close()
    assert client._client is None


async def test_close_handles_aclose_error(make_http_client):
    client = make_http_client(lambda req: http_response(''))
    try:
        await client.connect('hello', 'world')

        async def bad_aclose():
            raise httpx.HTTPError('boom')

        client._client.aclose = bad_aclose  # type: ignore[method-assign]
        await client.close()
        assert client._client is None
    finally:
        await _shutdown(client)


async def test_aenter_aexit_not_running_closes(make_http_client):
    client = make_http_client(lambda req: http_response(''))
    async with client as c:
        assert c is client
    assert client._client is None


async def test_aenter_aexit_running_logs_off(make_http_client):
    client = make_http_client(lambda req: http_response(LOGIN_OK))
    async with client as c:
        assert c is client
        await client.connect('hello', 'world')
        assert client.running is True
    assert client.running is False
    assert client._client is None


# --------------------------------------------------------------------------
# SSL-хелперы
# --------------------------------------------------------------------------

def test_is_ssl_verify_error_true():
    request = httpx.Request('GET', 'http://localhost')
    try:
        _ssl_connect_error(request)
    except httpx.ConnectError as exc:
        assert HTTPClient._is_ssl_verify_error(exc) is True


def test_is_ssl_verify_error_false():
    request = httpx.Request('GET', 'http://localhost')
    err = httpx.ConnectError('boom', request=request)
    assert HTTPClient._is_ssl_verify_error(err) is False


def test_is_ssl_verify_error_plain_oserror_false():
    assert HTTPClient._is_ssl_verify_error(OSError('nope')) is False


def test_build_ssl_context_without_cert_ca():
    client = HTTPClient('localhost', ssl_enabled=True)
    ctx = client._build_ssl_context()
    assert ctx.check_hostname is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_build_ssl_context_with_cert_ca(monkeypatch):
    calls = []
    monkeypatch.setattr(ssl.SSLContext, 'load_verify_locations',
                        lambda self, *a, **kw: calls.append((a, kw)))
    client = HTTPClient('localhost', ssl_enabled=True)
    client._cert_chain = '/tmp/ca.pem'
    ctx = client._build_ssl_context()
    assert ctx.check_hostname is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert calls == [(('/tmp/ca.pem',), {})]


def test_load_macos_keychain_certs_non_darwin(monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'linux')
    client = HTTPClient('localhost')
    assert client._load_macos_keychain_certs(ssl.create_default_context()) == 0


def test_load_macos_keychain_certs_no_keychains(monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(os.path, 'exists', lambda path: False)
    client = HTTPClient('localhost')
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
    client = HTTPClient('localhost')
    count = client._load_macos_keychain_certs(ssl.create_default_context())
    assert count == 2  # оба keychain существуют → два сертификата
    assert loaded == [{'cadata': pem + '\n' + pem}]


def test_load_macos_keychain_certs_skips_failed_command(monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(os.path, 'exists', lambda path: True)

    class FakeProc:
        returncode = 1
        stdout = ''

    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: FakeProc())
    client = HTTPClient('localhost')
    assert client._load_macos_keychain_certs(ssl.create_default_context()) == 0


def test_load_macos_keychain_certs_exception_logged(monkeypatch, caplog):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(os.path, 'exists', lambda path: True)

    def boom(*a, **kw):
        raise OSError('nope')

    monkeypatch.setattr(subprocess, 'run', boom)
    client = HTTPClient('localhost')
    with caplog.at_level(logging.DEBUG):
        count = client._load_macos_keychain_certs(ssl.create_default_context())
    assert count == 0
    assert 'nope' in caplog.text
