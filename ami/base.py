import logging
from abc import abstractmethod
from typing import Dict, List, Callable, Any, Coroutine, Optional, Union


class AMIClientBase:
    """
    Класс AMIClientBase является родительским для классов HTTPClient и TCPClient.
    """

    def __init__(self, host: str, port: int):
        """
        Initializes the AMI Client

        :param host: The server host
        :param port: The server port
        """
        self.logger = logging.getLogger('AMI Client')
        self._event_callbacks: Dict[str, List[Callable[[dict, Any], Coroutine]]] = {}
        self.running = False
        self.host = host
        self.port = port

    @abstractmethod
    async def connect(self, username: str, password: str) -> List[dict]:
        """
        Establishes a connection to a server, using the provided username and password for authentication.

        :param username: The username to use for authentication.
        :param password: The password to use for authentication.
        :return: The response from the server
        """
        pass

    @abstractmethod
    async def ami_request(self, query: dict) -> List[dict]:
        """
        Sends an AMI request to the server

        :param query: The data to be sent
        :return: The response from the server
        """
        pass

    @abstractmethod
    async def event_dispatch(self) -> None:
        """
        Dispatches events for processing.

        :return: None
        """
        pass

    async def register_callback(self, event_name: str, callback: Callable[[dict, Any], Coroutine]) -> None:
        """
        Registers a callback function to be called when a specific event occurs.

        :param event_name: The name of the event to register the callback for.
        :param callback: The callback function to be called when the event occurs.
        :return: None
        """
        if event_name in self._event_callbacks:
            self._event_callbacks[event_name].append(callback)
        else:
            self._event_callbacks[event_name] = [callback]

    async def _login(self, username: str, password: str) -> List[dict]:
        """
        Login to the AMI server using the specified username and password

        :param username: The username for authentication
        :param password: The password for authentication
        :return: The response from the server
        """
        data = {
            "Action": "Login",
            "UserName": username,
            "Secret": password
        }
        return await self.ami_request(data)

    async def logoff(self) -> List[dict]:
        """
        Logoff from the AMI server

        :return: The response from the server
        """
        response = await self.ami_request({"Action": "LogOff"})
        self.running = False
        return response

    async def channels(self) -> List[dict]:
        """
        Shows the channels on the AMI server

        :return: The response from the server
        """
        return await self.ami_request({"Action": "CoreShowChannels"})

    async def originate(
            self,
            originator: int,
            extension: int,
            priority: int = 1,
            run_async: bool = True,
            timeout: int = 15,
            context: str = "from-internal",
            caller_id: Optional[str] = None,
            application: Optional[str] = None,
            app_data: Optional[str] = None,
            account: Optional[str] = None,
            early_media: Optional[bool] = None,
            codecs: Optional[List[str]] = None,
            other_channel_id: Optional[str] = None,
            variables: Optional[List[str]] = None
    ) -> List[dict]:
        """
        Sends an originate AMI request

        :param originator: The originator channel
        :param extension: The extension to call
        :param priority: The priority of the call
        :param run_async: Whether to run the call asynchronously
        :param timeout: The timeout for the call
        :param context: The context to use for the call
        :param caller_id: The caller ID to use for the call
        :param application: The application to use for the call
        :param app_data: The data to send to the application
        :param account: The account to use for the call
        :param early_media: Whether to enable early media for the call
        :param codecs: The codecs to use for the call
        :param other_channel_id: The ID of another channel involved in the call
        :param variables: The variables to set for the call
        :return: The response from the server
        """
        data = {
            "Action": "Originate",
            "Channel": f"Local/{originator}@{context}",
            "Exten": extension,
            "Context": context,
            "Priority": priority,
            "Async": run_async,
            "Timeout": timeout * 1000,
            "Callerid": extension if caller_id is None else caller_id
        }

        if application is not None and app_data is not None:
            data["Application"] = application
            data["Data"] = app_data
        elif (application is not None and app_data is None) or (application is None and app_data is not None):
            raise Exception(f'For "{"Application" if application is not None else "Data"}" '
                            f'required "{"Application" if application is None else "Data"}"')

        if account is not None:
            data["Account"] = account
        if early_media is not None:
            data["EarlyMedia"] = early_media
        if codecs is not None:
            for n, codec in enumerate(codecs):
                data[f"Codecs[{n}]"] = codec
        if other_channel_id is not None:
            data["OtherChannelId"] = other_channel_id
        if variables is not None:
            for n, var in enumerate(variables):
                data[f"Variable[{n}]"] = variables

        return await self.ami_request(data)

    async def redirect(
            self,
            channel: str,
            extension: int,
            context: str = "from-internal",
            priority: int = 1,
            extra_channel: str = None,
            extra_extension: int = None,
            extra_context: str = None,
            extra_priority: int = None,
    ) -> List[dict]:
        """
        Sends a redirect AMI request

        :param channel: The channel to redirect
        :param extension: The extension to redirect to
        :param context: The context to redirect to
        :param priority: The priority of the redirect
        :param extra_channel: The extra channel to redirect
        :param extra_extension: The extra extension to redirect to
        :param extra_context: The extra context to redirect to
        :param extra_priority: The extra priority of the redirect
        :return: The response from the server
        """
        data = {
            "Action": "Redirect",
            "Channel": channel,
            "Exten": extension,
            "Context": context,
            "Priority": priority
        }

        if extra_channel is not None:
            data["ExtraChannel"] = extra_channel
        if extra_extension is not None:
            data["ExtraExten"] = extra_extension
        if extra_context is not None:
            data["ExtraContext"] = extra_context
        if extra_priority is not None:
            data["ExtraPriority"] = extra_priority

        return await self.ami_request(data)

    async def blind_transfer(self, channel: str, extension: Union[str, int], context: str = "from-internal") -> List[dict]:
        """
        Sends a blind transfer AMI request

        :param channel: The channel to transfer
        :param extension: The number or extension to transfer to
        :param context: The context to transfer to
        :return: The response from the server
        """
        data = {
            "Action": "BlindTransfer",
            "Channel": channel,
            "Exten": extension,
            "Context": context
        }
        return await self.ami_request(data)
