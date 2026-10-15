"""
Общие фикстуры для тестов ami-client.

Содержит:
- make_http_client: фабрика HTTPClient с подменённым httpx.AsyncClient на MockTransport
- FakeReader / FakeWriter: замена asyncio.StreamReader/StreamWriter для юнит-тестов TCP
- make_tcp_client: фабрика TCPClient с инъекцией фейковых reader/writer
- AMIServer: локальный asyncio-сервер, эмулирующий базовый AMI-протокол
  для интеграционных тестов TCP
"""
import asyncio
import inspect
import httpx
import pytest

from ami.client import HTTPClient, TCPClient


# --------------------------------------------------------------------------
# HTTP: подмена httpx.AsyncClient на MockTransport
# --------------------------------------------------------------------------

@pytest.fixture
def make_http_client(monkeypatch):
    """Возвращает фабрику, создающую HTTPClient, у которого httpx.AsyncClient
    заменён на MockTransport с переданным обработчиком.

    handler(request: httpx.Request) -> httpx.Response | Exception
    """
    def factory(handler, host='localhost', **kwargs):
        async def async_handler(request):
            # Уступаем управление event loop. Без этого фоновый цикл
            # _event_receiving при мгновенных ответах MockTransport крутится
            # без единого await и навсегда блокирует loop (тест зависает).
            await asyncio.sleep(0)
            result = handler(request)
            if inspect.isawaitable(result):
                result = await result
            return result

        transport = httpx.MockTransport(async_handler)
        original = httpx.AsyncClient

        def fake_async_client(*args, **kw):
            kw['transport'] = transport
            return original(*args, **kw)

        monkeypatch.setattr('ami.client.http_client.httpx.AsyncClient', fake_async_client)
        return HTTPClient(host, **kwargs)

    return factory


def http_response(body: str = 'Response: Success\r\nMessage: OK\r\n\r\n',
                  encoding: str = 'windows-1251',
                  status: int = 200) -> httpx.Response:
    """Собирает httpx.Response с телом в нужной кодировке."""
    return httpx.Response(status, content=body.encode(encoding))


# --------------------------------------------------------------------------
# TCP: фейковые reader/writer для юнит-тестов
# --------------------------------------------------------------------------

class FakeReader:
    """Замена asyncio.StreamReader, возвращающая предзаданные строки.

    Когда строки закончились — возвращает b'' (EOF), как реальный StreamReader.
    """

    def __init__(self, lines=None):
        self._lines = list(lines or [])
        self.eof = False

    async def readline(self):
        if not self._lines:
            self.eof = True
            return b''
        return self._lines.pop(0)


class FakeWriter:
    """Замена asyncio.StreamWriter, фиксирующая записанные данные."""

    def __init__(self):
        self.written = b''
        self.closed = False
        self.close_count = 0

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True
        self.close_count += 1

    async def wait_closed(self) -> None:
        return None


def ami_headers(data: dict) -> bytes:
    """Кодирует dict в AMI-заголовки (как TCPClient._dict_to_headers)."""
    result = ''
    for key, value in data.items():
        if value is None:
            continue
        result += f"{key}: {value}\r\n"
    result += '\r\n'
    return result.encode('windows-1251')


@pytest.fixture
def make_tcp_client():
    """Возвращает фабрику, создающую TCPClient с фейковыми reader/writer.

    Строки reader задаются списком байтовых строк (каждая — одна линия,
    включая пустые строки-разделители).
    """
    def factory(lines=None, host='localhost', **kwargs):
        client = TCPClient(host, **kwargs)
        reader = FakeReader(lines or [])
        writer = FakeWriter()

        async def fake_open_connection(ssl_context=None):
            # Повторяем поведение реального _open_connection: устанавливаем
            # reader/writer на клиенте (connect() не использует возврат).
            client._reader = reader
            client._writer = writer
            return reader, writer

        # Точечно заменяем метод экземпляра: connect() использует именно его
        client._open_connection = fake_open_connection  # type: ignore[assignment]
        return client, reader, writer

    return factory


# --------------------------------------------------------------------------
# TCP: локальный AMI-сервер для интеграционных тестов
# --------------------------------------------------------------------------

class AMIServer:
    """Локальный asyncio-сервер, эмулирующий базовый AMI-протокол.

    Поддерживает действия Login, Ping, CoreShowChannels, LogOff, а также
    произвольные события, отправляемые после логина.
    """

    def __init__(self, host='127.0.0.1'):
        self.host = host
        self.port = None
        self._server = None
        self.reject_login = False
        self.echo_actionid_in_events = True
        self.close_after_login = False
        self.outgoing_events = []
        self.login_username = None
        self.login_password = None
        self.received_actions = []

    # --- жизненный цикл сервера ---

    async def start(self):
        self._server = await asyncio.start_server(self._on_client, self.host, 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    # --- обслуживание клиентов ---

    async def _on_client(self, reader, writer):
        try:
            await self._serve(reader, writer)
        except (ConnectionError, OSError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _serve(self, reader, writer):
        while True:
            message = await self._read_message(reader)
            if message is None:
                break
            action = message.get('Action')
            self.received_actions.append(action)
            action_id = message.get('ActionID')

            if action == 'Login':
                self.login_username = message.get('UserName')
                self.login_password = message.get('Secret')
                if self.reject_login:
                    payload = {'Response': 'Error', 'Message': 'Authentication failed'}
                else:
                    payload = {'Response': 'Success', 'Message': 'Authentication accepted'}
                await self._write(writer, self._resp(payload, action_id))
                if self.reject_login:
                    continue
                for event in self.outgoing_events:
                    await self._write(writer, self._resp(event, None))
                if self.close_after_login:
                    writer.close()
                    break
            elif action == 'Ping':
                await self._write(writer, self._resp(
                    {'Response': 'Success', 'Ping': 'Pong', 'Timestamp': '1234567890.123'}, action_id))
            elif action == 'CoreShowChannels':
                await self._write(writer, self._channels_response(action_id))
            elif action == 'LogOff':
                await self._write(writer, self._resp(
                    {'Response': 'Goodbye', 'Message': 'Thanks for all the fish.'}, action_id))
                writer.close()
                break
            else:
                await self._write(writer, self._resp(
                    {'Response': 'Error', 'Message': f'Unknown action {action}'}, action_id))

    # --- хелперы протокола ---

    @staticmethod
    async def _read_message(reader):
        lines = []
        while True:
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=10)
            except asyncio.TimeoutError:
                return None
            if not line:
                return None
            if not line.strip():
                break
            lines.append(line.decode('windows-1251', errors='replace').strip())
        return AMIServer._lines_to_dict(lines)

    @staticmethod
    def _lines_to_dict(lines):
        result = {}
        for row in lines:
            if ': ' in row:
                key, value = row.split(': ', 1)
                result[key] = value
            elif row.endswith(':'):
                result[row[:-1]] = ''
        return result

    @staticmethod
    async def _write(writer, data: bytes):
        writer.write(data)
        await writer.drain()

    @classmethod
    def _msg(cls, data) -> bytes:
        out = []
        for key, value in data.items():
            if value is None:
                continue
            out.append(f'{key}: {value}')
        return ('\r\n'.join(out) + '\r\n\r\n').encode('windows-1251')

    def _resp(self, data: dict, action_id=None) -> bytes:
        data = dict(data)
        if action_id:
            data['ActionID'] = action_id
        return self._msg(data)

    def _channels_response(self, action_id) -> bytes:
        head = {'Response': 'Success', 'EventList': 'start', 'Message': 'Channels will follow'}
        ch = {
            'Event': 'CoreShowChannel',
            'Channel': 'SIP/trunk',
            'ChannelState': '5',
            'ChannelStateDesc': 'Ringing',
            'CallerIDNum': '89999999999',
            'CallerIDName': 'CID:999999',
            'ConnectedLineNum': '999999',
            'ConnectedLineName': '<unknown>',
            'Language': 'ru',
            'AccountCode': '',
            'Context': 'from-trunk',
            'Exten': '89999999999',
            'Priority': '1',
            'Uniqueid': '1694584278.23846',
            'Linkedid': '1694584277.23843',
            'Application': 'AppDial',
            'ApplicationData': '(Outgoing Line)',
            'Duration': '00:00:05',
            'BridgeId': '',
        }
        complete = {'Event': 'CoreShowChannelsComplete', 'EventList': 'Complete', 'ListItems': '1'}
        parts = [self._resp(head, action_id)]
        parts.append(self._resp(ch, action_id) if self.echo_actionid_in_events else self._msg(ch))
        parts.append(self._resp(complete, action_id) if self.echo_actionid_in_events else self._msg(complete))
        return b''.join(parts)


@pytest.fixture
async def ami_server():
    """Локальный AMI-сервер, автоматически останавливаемый после теста."""
    server = AMIServer()
    await server.start()
    yield server
    await server.stop()
