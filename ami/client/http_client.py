import asyncio
import dataclasses
import logging
import urllib.parse

import aiohttp

from ami import AMIClientBase


@dataclasses.dataclass
class HTTPQueues:
    events: asyncio.Queue = asyncio.Queue()


class HTTPClient(AMIClientBase):
    def __init__(self, host: str, port: int = 8088):
        super().__init__(host, port)
        self.logger = logging.getLogger('HTTP Client')
        self._queues = HTTPQueues()
        self._cookies = aiohttp.CookieJar()

    async def connect(self, username, password):
        self.running = True

        await self.login(username, password)

        loop = asyncio.get_event_loop()
        loop.create_task(self.event_dispatch())

    async def events(self, timeout=-1):
        """
        Waits for an event from the AMI server

        :return: The response from the server
        """
        return await self.ami_request({"Action": "WaitEvent", "Timeout": timeout})

    async def _event_receiving(self):
        """
        Receives events from the server in a continuous loop, and puts them into a queue for processing.

        :return: None
        """
        while self.running:
            events = await self.events()

            for event in events:
                if 'Event' in event and event['Event'] != "WaitEventComplete":
                    await self._queues.events.put(event)

    async def event_dispatch(self):
        loop = asyncio.get_event_loop()
        loop.create_task(self._event_receiving())

        # цикл отправки событий
        while self.running:
            # получаем/ожидаем событие
            event = await self._queues.events.get()

            # если получили None в качестве события, то мы завершаем работу
            if not event:
                break
            # обрабатываем наши события
            # сначала создаем список функций для выполнения
            callbacks = (self._event_callbacks.get(event["Event"], []) + self._event_callbacks.get('*', []))

            # теперь выполняем функции
            loop = asyncio.get_event_loop()
            for callback in callbacks:
                loop.create_task(callback(event, self))


    @staticmethod
    def _headers_to_dict(headers: str) -> list[dict]:
        """
        This static method converts a strings to formatted as key-value pairs into a dictionary.

        :param headers: The string containing the key-value pairs.
        :return: The list of dictionaries with the converted key-value pairs.
        """
        header_list = []
        blocks = headers.strip().split('\r\n\r\n')
        for block in blocks:
            header_dict = {}
            lines = block.split('\r\n')
            for line in lines:
                key, value = line.split(': ', 1)
                header_dict[key] = value
            header_list.append(header_dict)
        return header_list

    async def ami_request(self, query: dict) -> list[dict]:
        headers = {"Content-Type": "text/plain"}
        url = f"http://{self.host}:{self.port}/rawman?{urllib.parse.urlencode(query).lower()}"
        async with aiohttp.ClientSession(cookie_jar=self._cookies) as session:
            async with session.get(url=url, headers=headers) as resp:
                response_text = await resp.text()
                self.logger.debug(resp.status, resp.reason, response_text)
                response = self._headers_to_dict(response_text)
                self.logger.debug(response)
                return response
