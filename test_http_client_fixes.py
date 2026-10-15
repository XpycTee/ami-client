"""
Интеграционный тест исправлений HTTPClient с mock-сервером AMI.

Проверяет:
1. connect/login
2. Повторный connect -> RuntimeError
3. ping
4. EventList-ответ (CoreShowChannels)
5. Конкурентные запросы
6. Диспетчеризацию событий в колбэки
7. logoff без зависания
8. Таймаут ожидания ответа
9. Контекстный менеджер
10. Форматирование запроса (регистр ключей, bool -> true/false, пропуск None)
"""
import asyncio
import logging
import urllib.parse

from ami.client import HTTPClient

logging.basicConfig(level=logging.WARNING)


class MockAMIServer:
    def __init__(self, no_response_actions=()):
        self.server = None
        self.port = None
        self.no_response_actions = set(no_response_actions)
        self.event_queue = asyncio.Queue()

    async def start(self):
        self.server = await asyncio.start_server(self.handle, '127.0.0.1', 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def handle(self, reader, writer):
        try:
            while True:
                request_line = await reader.readline()
                if not request_line:
                    return
                # skip remaining request headers until the blank line
                while True:
                    line = await reader.readline()
                    if line in (b'\r\n', b'\n', b''):
                        break
                parts = request_line.decode('latin-1', errors='replace').strip().split(' ')
                if len(parts) < 2:
                    continue
                path = parts[1]
                query_string = path.split('?', 1)[1] if '?' in path else ''
                request = {k: v[0] for k, v in urllib.parse.parse_qs(query_string).items()}

                action = request.get('Action', '')
                if action in self.no_response_actions:
                    continue  # deliberately never answer

                body = self.build_response(request)
                close_conn = action == 'LogOff'
                body_bytes = body.encode('utf-8')
                writer.write(
                    b'HTTP/1.1 200 OK\r\n'
                    b'Content-Type: text/plain\r\n'
                    b'Content-Length: ' + str(len(body_bytes)).encode() + b'\r\n'
                    b'Connection: close\r\n'
                    b'\r\n'
                    + body_bytes
                )
                await writer.drain()
                if close_conn:
                    writer.close()
                    return
        except (ConnectionError, OSError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    def build_response(self, request):
        action = request.get('Action', '')
        if action == 'Login':
            self.event_queue.put_nowait('Event: Newchannel\r\nChannel: SIP/200\r\n\r\n')
            return 'Response: Success\r\nMessage: Authentication accepted\r\n\r\n'
        elif action == 'Ping':
            return 'Response: Success\r\nPing: Pong\r\n\r\n'
        elif action == 'CoreShowChannels':
            return (
                'Response: Success\r\nEventList: start\r\nMessage: Channels will follow\r\n\r\n'
                'Event: CoreShowChannel\r\nChannel: SIP/100\r\n\r\n'
                'Event: CoreShowChannelsComplete\r\nEventList: Complete\r\nListItems: 1\r\n\r\n'
            )
        elif action == 'WaitEvent':
            try:
                return self.event_queue.get_nowait()
            except asyncio.QueueEmpty:
                return 'Event: WaitEventComplete\r\n\r\n'
        elif action == 'LogOff':
            return 'Response: Goodbye\r\nMessage: Thanks for all the fish.\r\n\r\n'
        else:
            return 'Response: Error\r\nMessage: Unknown action\r\n\r\n'


async def main():
    server = MockAMIServer()
    await server.start()
    print(f'Mock server on port {server.port}')

    client = HTTPClient('127.0.0.1', server.port, read_timeout=1, request_timeout=5)

    events = []

    async def on_newchannel(event, cli):
        events.append(event)

    await client.register_callback('Newchannel', on_newchannel)

    # 1. connect
    login = await client.connect('user', 'pass')
    assert login[0]['Response'] == 'Success', login
    print('1. connect OK:', login)

    # 2. duplicate connect
    try:
        await client.connect('user', 'pass')
        raise AssertionError('second connect should raise RuntimeError')
    except RuntimeError:
        print('2. duplicate connect OK')

    # 3. ping
    ping = await client.ping()
    assert ping[0]['Ping'] == 'Pong', ping
    print('3. ping OK:', ping)

    # 4. EventList (channels)
    channels = await client.channels()
    assert len(channels) == 3, channels
    assert channels[0]['EventList'] == 'start', channels
    assert channels[2]['EventList'] == 'Complete', channels
    print('4. channels OK:', [c.get('Event') or c.get('Response') for c in channels])

    # 5. concurrent requests
    results = await asyncio.gather(client.ping(), client.channels())
    assert results[0][0]['Ping'] == 'Pong', results
    assert len(results[1]) == 3, results
    print('5. concurrent requests OK')

    # 6. event dispatch
    await asyncio.sleep(0.3)
    assert len(events) == 1, events
    print('6. event dispatch OK:', events)

    # 7. logoff (must not hang)
    logoff = await asyncio.wait_for(client.logoff(), timeout=10)
    assert logoff[0]['Response'] == 'Goodbye', logoff
    print('7. logoff OK:', logoff)

    await server.stop()

    # 8. request timeout
    server2 = MockAMIServer(no_response_actions=('Ping',))
    await server2.start()
    client2 = HTTPClient('127.0.0.1', server2.port, read_timeout=1, request_timeout=1)
    await client2.connect('user', 'pass')
    try:
        await client2.ping()
        raise AssertionError('ping should time out')
    except asyncio.TimeoutError:
        print('8. request timeout OK')
    await client2.logoff()
    await server2.stop()

    # 9. context manager
    server3 = MockAMIServer()
    await server3.start()
    async with HTTPClient('127.0.0.1', server3.port, read_timeout=1, request_timeout=5) as cm_client:
        login3 = await cm_client.connect('user', 'pass')
        assert login3[0]['Response'] == 'Success', login3
        ping3 = await cm_client.ping()
        assert ping3[0]['Ping'] == 'Pong', ping3
    print('9. context manager OK')
    await server3.stop()

    # 10. request formatting preserves case, converts bools and skips None
    q = HTTPClient._dict_to_query({
        'Action': 'Ping',
        'Async': True,
        'Enabled': False,
        'Skip': None,
    })
    assert 'Action=Ping' in q, q
    assert 'Async=true' in q, q
    assert 'Enabled=false' in q, q
    assert 'Skip' not in q, q
    print('10. query formatting OK:', q)

    print('\nALL TESTS PASSED')


if __name__ == '__main__':
    asyncio.run(main())
