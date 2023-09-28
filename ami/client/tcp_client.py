import asyncio
import dataclasses
import logging
import re
from typing import List, Union

import jmespath

from ami import AMIClientBase


@dataclasses.dataclass
class TCPQueues:
    messages: asyncio.Queue = asyncio.Queue()
    events: asyncio.Queue = asyncio.Queue()
    responses: asyncio.Queue = asyncio.Queue()


class TCPClient(AMIClientBase):
    def __init__(self, host: str, port: int = 5038):
        super().__init__(host, port)
        self.logger = logging.getLogger('TCP Client')
        self._reader: Union[asyncio.StreamReader, None] = None
        self._writer: Union[asyncio.StreamWriter, None] = None
        self._queues = TCPQueues()
        self._resp_waiting = []

    async def connect(self, username, password):
        self.running = True
        self._reader, self._writer = await asyncio.open_connection(host=self.host, port=self.port)

        loop = asyncio.get_event_loop()
        loop.create_task(self.message_loop())
        loop.create_task(self.event_dispatch())

        await self.login(username, password)

    @staticmethod
    def _dict_to_headers(data: dict) -> str:
        """
        This static method converts a dictionary into a formatted string with key-value pairs suitable for headers.

        :param data: The dictionary containing the data.
        :return: The formatted string with key-value pairs.
        """
        result = ''
        for key, value in data.items():
            key_name = re.sub(r"\[\d+]", "", key)
            result += f"{key_name}: {value}\r\n"
        result += '\r\n'
        return result

    @staticmethod
    def _message_to_dict(data: list) -> dict:
        """
        This static method converts a list of strings formatted as key-value pairs into a dictionary.

        :param data: The list containing the key-value pairs.
        :return: The dictionary with the converted key-value pairs.
        """
        result = {}
        for row in data:
            line = row.split(': ', 1)
            if len(line) == 2:
                key, value = line
                result[key] = value
        return result

    async def _receiving(self):
        """
        This method reads lines from the TCP stream and puts them in a queue until an empty line is encountered.
        It decodes each line and strips leading and trailing whitespace before adding it to the list.

        :return: None
        """
        lines = []
        while self.running:
            line = await asyncio.wait_for(self._reader.readline(), timeout=1)
            if not line.strip():
                await self._queues.messages.put(lines)
                lines = []
                continue
            logging.debug(f"Line: {line}")
            decoded_line = line.decode('windows-1251').strip()
            lines.append(decoded_line)

    def _get_functions(self, event_name):
        return self._event_callbacks.get(event_name, []) + self._event_callbacks.get('*', [])

    async def event_dispatch(self):
        while self.running:
            event = await self._queues.events.get()
            if not event:
                break
            functions = self._get_functions(event['Event'])

            loop = asyncio.get_event_loop()
            [loop.create_task(fn(event, self)) for fn in functions]

    async def message_loop(self):
        """
        This method is responsible for processing messages.

        :return: None
        """
        loop = asyncio.get_event_loop()
        loop.create_task(self._receiving())

        event_list = []

        while self.running:
            data = await asyncio.wait_for(self._queues.messages.get(), timeout=1)
            self.logger.debug(f"New message: {data}")
            if not data:
                await self._queues.events.put(None)
                for _ in self._resp_waiting:
                    await self._queues.responses.put(None)
                break

            message = self._message_to_dict(data)

            if jmespath.search("Response && (EventList == 'start')", message):
                while True:
                    list_data = await self._queues.messages.get()
                    list_message = self._message_to_dict(list_data)
                    if jmespath.search("Event && (EventList == 'Complete')", list_message):
                        event_list.append(list_message)
                        break
                    event_list.append(list_message)

            if jmespath.search('Event', message):
                await self._queues.events.put(message)
            elif jmespath.search('Response', message):
                response_list = [message]
                if event_list:
                    response_list.extend(event_list)
                    event_list = []
                await self._queues.responses.put(response_list)
            else:
                logging.error(f'Проблемы с определением типа сообщения "{message}"')

    async def ami_request(self, query: dict) -> List[dict]:
        request = self._dict_to_headers(query)
        self._writer.write(request.encode('utf8'))
        await self._writer.drain()

        self.logger.debug(f"Start wait for {query}")
        self._resp_waiting.insert(0, 1)
        response = await self._queues.responses.get()
        self._resp_waiting.pop(0)
        self.logger.debug(f"Stop wait for {query}")

        self.logger.debug(response)
        return response
