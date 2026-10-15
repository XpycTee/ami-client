"""
Интеграционный тест исправлений TCPClient с mock-сервером AMI.

Проверяет:
1. connect/login
2. ami_request с сопоставлением по ActionID
3. EventList-ответ (CoreShowChannels)
4. Конкурентные запросы
5. Диспетчеризацию событий в колбэки
6. logoff без зависания
7. Обработку EOF (закрытие соединения сервером)
8. Таймаут ожидания ответа
"""
import asyncio
import logging

from ami.client import TCPClient

logging.basicConfig(level=logging.WARNING)


class MockAMIServer:
    def __init__(self, close_after_login=False, no_response_actions=()):
        self.server = None
        self.port = None
        self.close_after_login = close_after_login
        self.no_response_actions = set(no_response_actions)

    async def start(self):
        self.server = await asyncio.start_server(self.handle, '127.0.0.1', 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def handle(self, reader, writer):
        try:
            while True:
                lines = []
                while True:
                    line = await reader.readline()
                    if not line:
                        return
                    if line in (b'\r\n', b'\n'):
                        break
                    lines.append(line.decode('utf-8', errors='replace').strip())
                request = {}
                for row in lines:
                    if ': ' in row:
                        k, v = row.split(': ', 1)
                        request[k] = v
                action = request.get('Action', '')
                action_id = request.get('ActionID', '')
                if action in self.no_response_actions:
                    continue  # deliberately never answer
                if action == 'Login':
                    await self.send(writer, [
                        'Response: Success',
                        f'ActionID: {action_id}',
                        'Message: Authentication accepted',
                    ])
                    await self.send(writer, [
                        'Event: Newchannel',
                        'Channel: SIP/200',
                    ])
                    if self.close_after_login:
                        writer.close()
                        return
                elif action == 'Ping':
                    await self.send(writer, [
                        'Response: Success',
                        f'ActionID: {action_id}',
                        'Ping: Pong',
                    ])
                elif action == 'CoreShowChannels':
                    await self.send(writer, [
                        'Response: Success',
                        f'ActionID: {action_id}',
                        'EventList: start',
                        'Message: Channels will follow',
                    ])
                    await self.send(writer, [
                        'Event: CoreShowChannel',
                        f'ActionID: {action_id}',
                        'Channel: SIP/100',
                    ])
                    await self.send(writer, [
                        'Event: CoreShowChannelsComplete',
                        f'ActionID: {action_id}',
                        'EventList: Complete',
                        'ListItems: 1',
                    ])
                elif action == 'LogOff':
                    await self.send(writer, [
                        'Response: Goodbye',
                        f'ActionID: {action_id}',
                        'Message: Thanks for all the fish.',
                    ])
                    writer.close()
                    return
                else:
                    await self.send(writer, [
                        'Response: Error',
                        f'ActionID: {action_id}',
                        'Message: Unknown action',
                    ])
        except (ConnectionError, OSError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    @staticmethod
    async def send(writer, lines):
        data = ''.join(f'{line}\r\n' for line in lines) + '\r\n'
        writer.write(data.encode('utf-8'))
        await writer.drain()


async def main():
    server = MockAMIServer()
    await server.start()
    print(f'Mock server on port {server.port}')

    client = TCPClient('127.0.0.1', server.port, request_timeout=5)

    events = []

    async def on_newchannel(event, cli):
        events.append(event)

    await client.register_callback('Newchannel', on_newchannel)

    # 1. connect
    login = await client.connect('user', 'pass')
    assert login[0]['Response'] == 'Success', login
    print('1. connect OK:', login)

    # 2. ping
    ping = await client.ping()
    assert ping[0]['Ping'] == 'Pong', ping
    print('2. ping OK:', ping)

    # 3. EventList (channels)
    channels = await client.channels()
    assert len(channels) == 3, channels
    assert channels[0]['EventList'] == 'start', channels
    assert channels[2]['EventList'] == 'Complete', channels
    print('3. channels OK:', [c.get('Event') or c.get('Response') for c in channels])

    # 4. concurrent requests
    results = await asyncio.gather(client.ping(), client.channels())
    assert results[0][0]['Ping'] == 'Pong', results
    assert len(results[1]) == 3, results
    print('4. concurrent requests OK')

    # 5. event dispatch
    await asyncio.sleep(0.2)
    assert len(events) == 1, events
    print('5. event dispatch OK:', events)

    # 6. logoff (must not hang)
    logoff = await asyncio.wait_for(client.logoff(), timeout=10)
    assert logoff[0]['Response'] == 'Goodbye', logoff
    print('6. logoff OK:', logoff)

    # 7. EOF handling: server closes connection right after login
    server2 = MockAMIServer(close_after_login=True)
    await server2.start()
    client2 = TCPClient('127.0.0.1', server2.port, request_timeout=5)
    await client2.connect('user', 'pass')
    await asyncio.sleep(0.3)
    assert client2.running is False, 'client2 should detect EOF and stop'
    try:
        await client2.ping()
        raise AssertionError('ping after EOF should raise')
    except (ConnectionError, OSError, RuntimeError):
        print('7. EOF handling OK: connection closed detected, request failed')
    await server2.stop()

    # 8. request timeout
    server3 = MockAMIServer(no_response_actions=('Ping',))
    await server3.start()
    client3 = TCPClient('127.0.0.1', server3.port, request_timeout=1)
    await client3.connect('user', 'pass')
    try:
        await client3.ping()
        raise AssertionError('ping should time out')
    except asyncio.TimeoutError:
        print('8. request timeout OK')
    await client3.logoff()
    await server3.stop()

    await server.stop()
    print('\nALL TESTS PASSED')


if __name__ == '__main__':
    asyncio.run(main())