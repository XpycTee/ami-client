import asyncio
import logging
import re
import ssl
import uuid
from typing import List, Union, Coroutine, Callable, Optional, Dict, Any

from ami.base import AMIClientBase


class TCPClient(AMIClientBase):
    def __init__(self, host: str, port: int = 5038, ssl_enabled: bool = False,
                 cert_ca: Optional[Union[str, bytes]] = None, encoding: str = 'windows-1251',
                 read_timeout: float = 5.0, request_timeout: float = 30.0):
        """
        Initializes the TCP AMI client.

        :param host: The server hostname or ip
        :param port: The server port (default 5038, 5039 when ssl_enabled)
        :param ssl_enabled: Indicates whether SSL/TLS encryption is enabled (default is False)
        :param cert_ca: The CA certificate chain file or bytes (optional)
        :param encoding: Encoding used for both sending and receiving AMI messages
        :param read_timeout: Socket read timeout in seconds
        :param request_timeout: Maximum time to wait for an AMI response in seconds
        """
        if ssl_enabled and port == 5038:
            port = 5039
        super().__init__(host, port, ssl_enabled, cert_ca)
        self.logger = logging.getLogger('TCP Client')
        self._reader: Union[asyncio.StreamReader, None] = None
        self._writer: Union[asyncio.StreamWriter, None] = None
        self._queues = {}
        self._pending: Dict[str, asyncio.Future] = {}
        self._callback_tasks = []
        self._encoding = encoding
        self._read_timeout = read_timeout
        self._request_timeout = request_timeout

    async def connect(self, username, password) -> List[dict]:
        """
        Establishes a connection to the AMI server and logs in.

        :param username: The username for authentication
        :param password: The password for authentication
        :return: The login response from the server
        """
        if self.running:
            raise RuntimeError('Client is already connected')

        self.running = True
        self._queues['messages'] = asyncio.Queue()
        self._queues['events'] = asyncio.Queue()

        ssl_context = None
        if self._ssl_enabled:
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = True
            ssl_context.verify_mode = ssl.VerifyMode.CERT_REQUIRED
            if self._cert_chain is not None:
                ssl_context.load_verify_locations(self._cert_chain)

        try:
            self._reader, self._writer = await asyncio.open_connection(
                host=self.host,
                port=self.port,
                ssl=ssl_context,
                server_hostname=self.host if ssl_context is not None else None,
            )
        except (OSError, ssl.SSLError) as exc:
            self.running = False
            self.logger.error("Failed to connect to %s:%s: %s", self.host, self.port, exc)
            raise

        loop = asyncio.get_running_loop()
        self._loop_tasks.append(loop.create_task(self.message_loop()))
        self._loop_tasks.append(loop.create_task(self.event_dispatch()))

        login_resp = await self._login(username, password)
        if not login_resp or login_resp[0].get('Response') == 'Error':
            self.running = False
            self.logger.error("Login failed: %s", login_resp)

        return login_resp

    async def register_callback(self, event_name: str, callback: Callable[[dict, 'TCPClient'], Coroutine]) -> None:
        await super().register_callback(event_name, callback)

    @staticmethod
    def _format_value(value: Any) -> str:
        """
        Converts a value to its AMI string representation.

        :param value: The value to convert
        :return: The string representation
        """
        if isinstance(value, bool):
            return 'true' if value else 'false'
        return str(value)

    @staticmethod
    def _dict_to_headers(data: dict) -> str:
        """
        Converts a dictionary into a formatted string with key-value pairs suitable for AMI headers.

        :param data: The dictionary containing the data.
        :return: The formatted string with key-value pairs.
        """
        result = ''
        for key, value in data.items():
            if value is None:
                continue
            key_name = re.sub(r"\[\d+\]", "", key)
            result += f"{key_name}: {TCPClient._format_value(value)}\r\n"
        result += '\r\n'
        return result

    @staticmethod
    def _message_to_dict(data: list) -> dict:
        """
        Converts a list of strings formatted as key-value pairs into a dictionary.

        :param data: The list containing the key-value pairs.
        :return: The dictionary with the converted key-value pairs.
        """
        result = {}
        for row in data:
            if ': ' in row:
                key, value = row.split(': ', 1)
                result[key] = value
            elif row.endswith(':'):
                result[row[:-1]] = ''
        return result

    async def _receiving(self):
        """
        Reads lines from the TCP stream and puts complete AMI messages into the 'messages' queue.

        :return: None
        """
        reader = self._reader
        if reader is None:
            return
        lines = []
        while self.running:
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=self._read_timeout)
            except asyncio.TimeoutError:
                self.logger.debug("Socket timeout, retry")
                continue
            except (ConnectionError, OSError) as exc:
                self.logger.error("Connection error while reading: %s", exc)
                break

            if not line:
                # EOF: the server closed the connection
                self.logger.warning("Connection closed by server (EOF)")
                break

            if not line.strip():
                if lines:
                    await self._queues['messages'].put(lines)
                    lines = []
                continue

            self.logger.debug("Line: %s", line)
            decoded_line = line.decode(self._encoding, errors='replace').strip()
            lines.append(decoded_line)

        self.running = False
        self._fail_pending(ConnectionError('Connection closed'))
        if lines:
            await self._queues['messages'].put(lines)

    def _fail_pending(self, exc: Exception) -> None:
        """
        Fails all pending AMI requests with the given exception.

        :param exc: The exception to set on pending futures
        :return: None
        """
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    def _get_functions(self, event_name):
        return self._event_callbacks.get(event_name, []) + self._event_callbacks.get('*', [])

    async def _run_callback(self, fn, event):
        """
        Runs a single event callback, logging any exception instead of losing it.

        :param fn: The callback coroutine function
        :param event: The event dict
        :return: None
        """
        try:
            await fn(event, self)
        except Exception as exc:
            self.logger.exception("Callback %s failed for event '%s': %s", fn, event.get('Event'), exc)

    async def event_dispatch(self):
        """
        Dispatches events from the 'events' queue to registered callbacks.

        :return: None
        """
        while self.running:
            try:
                get_await = self._queues['events'].get()
                event = await asyncio.wait_for(get_await, 1)
            except asyncio.TimeoutError:
                self.logger.debug('Events timeout, retry')
                continue
            if event is None:
                break
            functions = self._get_functions(event.get('Event'))
            if len(functions) != 0:
                loop = asyncio.get_running_loop()
                for fn in functions:
                    task = loop.create_task(self._run_callback(fn, event))
                    self._callback_tasks.append(task)
                    task.add_done_callback(self._callback_tasks.remove)
                self.logger.info("Execute callbacks for event '%s'", event.get('Event'))

    async def message_loop(self):
        """
        Processes messages from the 'messages' queue: routes events to the 'events' queue
        and resolves pending AMI requests by ActionID.

        :return: None
        """
        loop = asyncio.get_running_loop()
        self._loop_tasks.append(loop.create_task(self._receiving()))

        while self.running:
            try:
                data = await asyncio.wait_for(self._queues['messages'].get(), timeout=1)
            except asyncio.TimeoutError:
                continue
            if data is None:
                break

            self.logger.debug("New message: %s", data)
            message = self._message_to_dict(data)

            if 'Event' in message:
                await self._queues['events'].put(message)
            elif 'Response' in message:
                await self._handle_response(message)
            else:
                self.logger.error('Проблемы с определением типа сообщения "%s"', message)

    async def _handle_response(self, message: dict) -> None:
        """
        Routes an AMI response to the pending request that matches its ActionID.

        :param message: The response message dict
        :return: None
        """
        action_id = message.get('ActionID')
        future = self._pending.get(action_id) if action_id else None
        if future is None and self._pending:
            # Fallback for servers that do not echo ActionID: resolve the oldest pending request
            future = next(iter(self._pending.values()))
            self.logger.debug("Response without ActionID, matched oldest pending request")

        if future is None:
            self.logger.warning("Response without pending request: %s", message)
            return

        if message.get('EventList') == 'start':
            response_list = [message]
            while self.running:
                try:
                    list_data = await asyncio.wait_for(
                        self._queues['messages'].get(), timeout=self._read_timeout)
                except asyncio.TimeoutError:
                    self.logger.error("Timeout waiting for EventList completion (ActionID=%s)", action_id)
                    break
                if list_data is None:
                    break
                list_message = self._message_to_dict(list_data)
                if 'Event' in list_message and list_message.get('EventList') == 'Complete':
                    response_list.append(list_message)
                    break
                if list_message.get('ActionID') != action_id:
                    # Not part of this response — put it back for normal processing
                    await self._queues['messages'].put(list_data)
                    continue
                response_list.append(list_message)
            if not future.done():
                future.set_result(response_list)
        else:
            if not future.done():
                future.set_result([message])

    async def ami_request(self, query: dict) -> List[dict]:
        """
        Sends an AMI request and waits for the matching response.

        :param query: The data to be sent
        :return: The response from the server
        :raises RuntimeError: If the client is not connected
        :raises asyncio.TimeoutError: If no response arrives within request_timeout
        """
        writer = self._writer
        if writer is None:
            raise RuntimeError('Client is not connected')

        action_id = str(uuid.uuid4())
        request = dict(query)
        request['ActionID'] = action_id
        headers = self._dict_to_headers(request)
        writer.write(headers.encode(self._encoding))
        await writer.drain()

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[action_id] = future
        try:
            self.logger.debug("Start wait for %s", query)
            response = await asyncio.wait_for(future, timeout=self._request_timeout)
            self.logger.debug("Stop wait for %s", query)
            self.logger.info("Response %s for %s", response, query)
            return response
        except asyncio.TimeoutError:
            self.logger.error("Timeout waiting for response to %s", query)
            raise
        finally:
            self._pending.pop(action_id, None)

    async def close(self) -> None:
        """
        Closes the underlying TCP connection.

        :return: None
        """
        if self._writer is not None:
            try:
                self._writer.close()
                await asyncio.wait_for(self._writer.wait_closed(), timeout=1)
            except (ConnectionError, OSError, asyncio.TimeoutError):
                pass
            self._writer = None
            self._reader = None

    async def __aenter__(self) -> 'TCPClient':
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self.running:
            await self.logoff()
        else:
            await self.close()
