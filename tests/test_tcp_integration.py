"""
Интеграционные тесты TCPClient против локального asyncio-сервера,
эмулирующего AMI-протокол (фикстура ami_server из conftest).
"""
import asyncio

import pytest

from ami.client import TCPClient


async def _wait_until(predicate, timeout=5.0):
    async def _loop():
        while not predicate():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(_loop(), timeout=timeout)


async def test_integration_connect_ping_logoff(ami_server):
    client = TCPClient(ami_server.host, ami_server.port)
    try:
        login = await client.connect('hello', 'world')
        assert login[0]['Response'] == 'Success'
        assert login[0]['Message'] == 'Authentication accepted'
        assert client.running is True

        ping = await client.ping()
        assert ping[0]['Response'] == 'Success'
        assert ping[0]['Ping'] == 'Pong'

        logoff = await client.logoff()
        assert logoff[0]['Response'] == 'Goodbye'
        assert client.running is False
    finally:
        await client.close()


async def test_integration_connect_sends_credentials(ami_server):
    client = TCPClient(ami_server.host, ami_server.port)
    try:
        await client.connect('hello', 'world')
        assert ami_server.login_username == 'hello'
        assert ami_server.login_password == 'world'
        assert 'Login' in ami_server.received_actions
    finally:
        await client.logoff()


async def test_integration_events_delivered_to_callback(ami_server):
    ami_server.outgoing_events = [{'Event': 'Newchannel', 'Channel': 'SIP/foo'}]
    client = TCPClient(ami_server.host, ami_server.port)
    received = asyncio.Event()
    received_event = {}

    async def on_newchannel(event, client):
        received_event.update(event)
        received.set()

    await client.register_callback('Newchannel', on_newchannel)
    try:
        await client.connect('hello', 'world')
        await asyncio.wait_for(received.wait(), timeout=5)
        assert received_event['Event'] == 'Newchannel'
        assert received_event['Channel'] == 'SIP/foo'
    finally:
        await client.logoff()


async def test_integration_events_wildcard_callback(ami_server):
    ami_server.outgoing_events = [{'Event': 'VarSet', 'Variable': 'x'}]
    client = TCPClient(ami_server.host, ami_server.port)
    received = asyncio.Event()

    async def on_any(event, client):
        received.set()

    await client.register_callback('*', on_any)
    try:
        await client.connect('hello', 'world')
        await asyncio.wait_for(received.wait(), timeout=5)
    finally:
        await client.logoff()


async def test_integration_channels_eventlist(ami_server):
    client = TCPClient(ami_server.host, ami_server.port)
    try:
        await client.connect('hello', 'world')
        resp = await client.channels()
        assert resp[0]['Response'] == 'Success'
        assert resp[0]['EventList'] == 'start'
        assert any(r.get('Event') == 'CoreShowChannel' for r in resp)
        assert resp[-1]['EventList'] == 'Complete'
    finally:
        await client.logoff()


async def test_integration_connection_closed_by_server(ami_server):
    ami_server.close_after_login = True
    client = TCPClient(ami_server.host, ami_server.port)
    try:
        await client.connect('hello', 'world')
        # Сервер закрыл соединение после логина → клиент должен обнаружить EOF
        await _wait_until(lambda: client.running is False)
        assert client.running is False
    finally:
        await client.close()


async def test_integration_login_rejected(ami_server):
    ami_server.reject_login = True
    client = TCPClient(ami_server.host, ami_server.port)
    try:
        resp = await client.connect('hello', 'wrong')
        assert resp[0]['Response'] == 'Error'
        assert client.running is False
    finally:
        await client.close()


async def test_integration_originate_request(ami_server):
    client = TCPClient(ami_server.host, ami_server.port)
    try:
        await client.connect('hello', 'world')
        resp = await client.originate(originator='SIP/100', extension='200')
        # сервер не знает действие Originate → вернёт Error, но без падения
        assert resp[0]['Response'] == 'Error'
    finally:
        await client.logoff()
