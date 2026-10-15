import asyncio
import logging
import os
import re
import ssl
import subprocess
import sys
import urllib.parse
from typing import List, Coroutine, Callable, Union, Optional, Any

import httpx

from ami.base import AMIClientBase


class HTTPClient(AMIClientBase):
    def __init__(self, host: str, port: int = 8088, ssl_enabled: bool = False,
                 cert_ca: Optional[Union[str, bytes]] = None, encoding: str = 'windows-1251',
                 read_timeout: float = 5.0, request_timeout: float = 30.0):
        """
        Initializes the HTTP AMI client.

        :param host: The server hostname or ip
        :param port: The server port (default 8088, 8089 when ssl_enabled)
        :param ssl_enabled: Indicates whether SSL/TLS encryption is enabled (default is False)
        :param cert_ca: The CA certificate chain file or bytes (optional)
        :param encoding: Encoding used for decoding AMI responses
        :param read_timeout: Timeout for WaitEvent polling in seconds
        :param request_timeout: Maximum time to wait for an AMI response in seconds
        """
        if ssl_enabled and port == 8088:
            port = 8089
        super().__init__(host, port, ssl_enabled, cert_ca)
        self.logger = logging.getLogger('HTTP Client')
        self._verify = True
        if ssl_enabled:
            self._verify = self._build_ssl_context()
        self._client: Optional[httpx.AsyncClient] = None
        self._queues = {}
        self._cookies = httpx.Cookies()
        self._callback_tasks = []
        self._encoding = encoding
        self._read_timeout = read_timeout
        self._request_timeout = request_timeout

    async def connect(self, username, password) -> List[dict]:
        """
        Establishes an HTTP session with the AMI server and logs in.

        :param username: The username for authentication
        :param password: The password for authentication
        :return: The login response from the server
        """
        if self.running:
            raise RuntimeError('Client is already connected')

        self.running = True
        self._queues['events'] = asyncio.Queue()
        self._client = httpx.AsyncClient(
            cookies=self._cookies,
            verify=self._verify,
            timeout=None,
        )

        try:
            login_resp = await self._login(username, password)
        except (httpx.HTTPError, OSError) as exc:
            # httpx wraps ssl.SSLCertVerificationError into httpx.ConnectError
            # (ConnectError -> httpcore.ConnectError -> SSLCertVerificationError),
            # so the exception chain must be inspected to detect certificate
            # verification failures behind those wrappers.
            if not self._is_ssl_verify_error(exc):
                self.running = False
                self.logger.error("Failed to connect to %s:%s: %s", self.host, self.port, exc)
                raise
            # Python 3.13+ no longer reads the macOS keychain
            # (ssl.enum_certificates was removed, the bundled cert.pem may be
            # missing), so trusted roots installed there are invisible.
            # Retry with CA certificates loaded from the System/login keychains.
            if self._cert_chain is not None or sys.platform != 'darwin':
                self.running = False
                self.logger.error("Failed to connect to %s:%s: %s", self.host, self.port, exc)
                raise
            self.logger.info(
                "SSL verify failed (%s). Expanding trust with CA from macOS keychain...", exc)
            ssl_context = self._verify if isinstance(self._verify, ssl.SSLContext) else self._build_ssl_context()
            added = self._load_macos_keychain_certs(ssl_context)
            if added > 0:
                self._verify = ssl_context
                await self._client.aclose()
                self._client = httpx.AsyncClient(
                    cookies=self._cookies,
                    verify=self._verify,
                    timeout=None,
                )
                try:
                    login_resp = await self._login(username, password)
                except (httpx.HTTPError, OSError) as retry_exc:
                    self.running = False
                    self.logger.error("Failed to connect to %s:%s: %s", self.host, self.port, retry_exc)
                    raise retry_exc
            else:
                self.running = False
                self.logger.error("Failed to connect to %s:%s: %s", self.host, self.port, exc)
                raise

        if not login_resp or login_resp[0].get('Response') == 'Error':
            self.running = False
            self.logger.error("Login failed: %s", login_resp)

        loop = asyncio.get_running_loop()
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
            try:
                events = await asyncio.wait_for(
                    self.events(timeout=self._read_timeout),
                    timeout=self._read_timeout + 5,
                )
            except asyncio.TimeoutError:
                self.logger.debug('Events timeout, retry')
                continue
            except (httpx.HTTPError, OSError) as exc:
                self.logger.error("Connection error while receiving events: %s", exc)
                self.running = False
                break

            for event in events:
                if 'Event' in event and event['Event'] != 'WaitEventComplete':
                    await self._queues['events'].put(event)

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
        loop = asyncio.get_running_loop()
        self._loop_tasks.append(loop.create_task(self._event_receiving()))

        while self.running:
            try:
                # retrieve the get() awaitable
                get_await = self._queues['events'].get()
                # await the awaitable with a timeout
                event = await asyncio.wait_for(get_await, 1)
            except asyncio.TimeoutError:
                self.logger.debug('Events timeout, retry')
                continue
            # check for stop
            if event is None:
                break
            functions = self._get_functions(event.get('Event'))
            if len(functions) != 0:
                for fn in functions:
                    task = loop.create_task(self._run_callback(fn, event))
                    self._callback_tasks.append(task)
                    task.add_done_callback(self._callback_tasks.remove)
                self.logger.info("Execute callbacks for event '%s'", event.get('Event'))

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
    def _dict_to_query(data: dict) -> str:
        """
        Converts a dictionary into URL-encoded query parameters suitable for AMI rawman.

        None values are skipped and booleans are converted to 'true'/'false'
        (mirroring the TCP client behaviour). Key names keep their original case.

        :param data: The dictionary containing the data.
        :return: The URL-encoded query string.
        """
        filtered = {}
        for key, value in data.items():
            if value is None:
                continue
            key_name = re.sub(r"\[\d+\]", "", key)
            filtered[key_name] = HTTPClient._format_value(value)
        return urllib.parse.urlencode(filtered)

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
                if ': ' in line:
                    key, value = line.split(': ', 1)
                    header_dict[key] = value
                elif line.endswith(':'):
                    header_dict[line[:-1]] = ''
            header_list.append(header_dict)
        return header_list

    async def ami_request(self, query: dict) -> List[dict]:
        """
        Sends an AMI request via HTTP and waits for the response.

        :param query: The data to be sent
        :return: The response from the server
        :raises RuntimeError: If the client is not connected
        :raises asyncio.TimeoutError: If no response arrives within request_timeout
        """
        client = self._client
        if client is None:
            raise RuntimeError('Client is not connected')

        scheme = 'https' if self._ssl_enabled else 'http'
        headers = {"Content-Type": "text/plain"}
        get_query = self._dict_to_query(query)
        url = f"{scheme}://{self.host}:{self.port}/rawman?{get_query}"

        # WaitEvent blocks until an event arrives (or its own Timeout expires),
        # so it is not limited by request_timeout.
        timeout = None if query.get('Action') == 'WaitEvent' else self._request_timeout

        try:
            self.logger.debug("Start request %s", query)
            resp = await asyncio.wait_for(client.get(url, headers=headers), timeout=timeout)
            resp.raise_for_status()
            response_text = resp.content.decode(self._encoding, errors='replace')
            self.logger.debug("%s %s %s", resp.status_code, getattr(resp, 'reason_phrase', ''), response_text)
            response = self._headers_to_dict(response_text)
            if query.get('Action') != 'WaitEvent':
                self.logger.info("Response %s for %s", response, query)
            return response
        except asyncio.TimeoutError:
            self.logger.error("Timeout waiting for response to %s", query)
            raise

    async def close(self) -> None:
        """
        Closes the underlying HTTP client.

        :return: None
        """
        if self._client is not None:
            try:
                await asyncio.wait_for(self._client.aclose(), timeout=1)
            except (httpx.HTTPError, OSError, asyncio.TimeoutError):
                pass
            self._client = None

    async def __aenter__(self) -> 'HTTPClient':
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self.running:
            await self.logoff()
        else:
            await self.close()

    @staticmethod
    def _is_ssl_verify_error(exc: BaseException) -> bool:
        """
        Walks the exception chain (__cause__/__context__) looking for an
        ssl.SSLCertVerificationError.

        httpx/httpcore wrap SSL errors into httpx.ConnectError /
        httpcore.ConnectError, so this is needed to detect certificate
        verification failures behind those wrappers.

        :param exc: The caught exception
        :return: True if the chain contains ssl.SSLCertVerificationError
        """
        current = exc
        seen = set()
        while current is not None and id(current) not in seen:
            if isinstance(current, ssl.SSLCertVerificationError):
                return True
            seen.add(id(current))
            current = current.__cause__ or current.__context__
        return False

    def _build_ssl_context(self) -> ssl.SSLContext:
        """
        Creates the SSL context used for a secure connection.

        - If cert_ca is provided, it is loaded as the trusted CA.
        - Otherwise the system trust store is used. On macOS, Python 3.13+
          no longer sees the keychain (ssl.enum_certificates was removed,
          the bundled cert.pem may be missing), so on a verification failure
          the client expands trust with System/login keychain certificates
          (see _load_macos_keychain_certs).

        :return: the configured SSL context
        """
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = True
        ssl_context.verify_mode = ssl.VerifyMode.CERT_REQUIRED
        if self._cert_chain is not None:
            ssl_context.load_verify_locations(self._cert_chain)
        return ssl_context

    def _load_macos_keychain_certs(self, ssl_context: ssl.SSLContext) -> int:
        """
        Adds trusted CA certificates from the macOS keychains to the context.

        Since Python 3.13, ssl.enum_certificates was removed, so on macOS
        ssl.create_default_context() may not see root certificates installed
        in the keychain (neither System nor login). This method loads them
        through the `security` command line tool so that internally issued
        certificates (e.g. corporate CAs) are trusted again.

        On non-macOS platforms this method does nothing: on Linux the system
        CA bundle (/etc/ssl/certs/...) is used by default, and on Windows the
        trust store is handled by the standard library.

        :param ssl_context: SSL context to extend with keychain certificates
        :return: the number of certificates added (0 if none or on failure)
        """
        if sys.platform != 'darwin':
            return 0
        try:
            keychains = [
                '/Library/Keychains/System.keychain',
                os.path.expanduser('~/Library/Keychains/login.keychain-db'),
            ]
            pem_parts = []
            for keychain in keychains:
                if not os.path.exists(keychain):
                    continue
                proc = subprocess.run(
                    ['security', 'find-certificate', '-a', '-p', keychain],
                    capture_output=True, text=True, timeout=15,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    pem_parts.append(proc.stdout)
            if not pem_parts:
                return 0
            pem_data = '\n'.join(pem_parts)
            ssl_context.load_verify_locations(cadata=pem_data)
            return pem_data.count('-----BEGIN CERTIFICATE-----')
        except Exception as exc:
            self.logger.debug("Unable to load CA certificates from macOS keychain: %s", exc)
            return 0
