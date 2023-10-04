import asyncio
import logging
import ssl
import urllib.parse
from typing import List, Coroutine, Callable, Union

import aiohttp

from ami.base import AMIClientBase


class HTTPClient(AMIClientBase):
    def __init__(self, host: str, port: int = 8088, ssl_enabled: bool = False,
                 cert_ca: Union[str, bytes] = None):
        if ssl_enabled and port == 8088:
            port = 8089
        super().__init__(host, port, ssl_enabled, cert_ca)
        self.logger = logging.getLogger('HTTP Client')
        self._queues = {}
        self._cookies = aiohttp.CookieJar()

    async def connect(self, username, password) -> List[dict]:
        self.running = True
        self._queues['events'] = asyncio.Queue()

        login_resp = await self._login(username, password)

        loop = asyncio.get_event_loop()
        self._loop_tasks.append(loop.create_task(self.event_dispatch()))

        return login_resp

    async def register_callback(self, event_name: str, callback: Callable[[dict, 'HTTPClient'], Coroutine]) -> None:
        await super().register_callback(event_name, callback)

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
                    await self._queues['events'].put(event)

    def _get_functions(self, event_name):
        return self._event_callbacks.get(event_name, []) + self._event_callbacks.get('*', [])

    async def event_dispatch(self):
        loop = asyncio.get_event_loop()
        self._loop_tasks.append(loop.create_task(self._event_receiving()))

        while self.running:
            try:
                # retrieve the get() awaitable
                get_await = self._queues['events'].get()
                # await the awaitable with a timeout
                event = await asyncio.wait_for(get_await, 0.5)
            except asyncio.TimeoutError:
                self.logger.debug('Consumer: gave up waiting...')
                continue
            # check for stop
            if event is None:
                break
            functions = self._get_functions(event['Event'])
            if len(functions) != 0:
                loop = asyncio.get_event_loop()
                [loop.create_task(fn(event, self)) for fn in functions]
                self.logger.info(f"Execute callbacks for event '{event['Event']}'")

    @staticmethod
    def _headers_to_dict(headers: str) -> List[dict]:
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

    async def ami_request(self, query: dict) -> List[dict]:
        headers = {"Content-Type": "text/plain"}
        kwargs = {}
        if self._ssl_enabled:
            scheme = 'https'
            if self._cert_chain is not None:
                context = ssl.SSLContext()
                context.verify_mode = ssl.VerifyMode.CERT_REQUIRED
                context.load_verify_locations(self._cert_chain)
                kwargs['ssl'] = context
        else:
            scheme = 'http'
        url = f"{scheme}://{self.host}:{self.port}/rawman?{urllib.parse.urlencode(query).lower()}"
        async with aiohttp.ClientSession(cookie_jar=self._cookies) as session:
            async with session.get(url=url, headers=headers, **kwargs) as resp:
                response_text = await resp.text(encoding='windows-1251', errors='replace')
                self.logger.debug(resp.status, resp.reason, response_text)
                response = self._headers_to_dict(response_text)
                if query['Action'] != 'WaitEvent':
                    self.logger.info(f"Response {response} for {query}")
                return response
