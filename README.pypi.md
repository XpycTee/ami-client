
<h3 align="center">Async AMI Client</h3>

<p align="center">
Asynchronous client for working with Asterisk Manager Interface
</p>

#### PyPI
![PyPI - Version](https://img.shields.io/pypi/v/ami-client) ![PyPI - Downloads](https://img.shields.io/pypi/dm/ami-client) ![PyPI - Python Versions](https://img.shields.io/pypi/pyversions/ami-client) 
#### Github
![Downloads](https://img.shields.io/github/downloads/XpycTee/ami-client/total) ![Contributors](https://img.shields.io/github/contributors/XpycTee/ami-client?color=dark-green) ![Issues](https://img.shields.io/github/issues/XpycTee/ami-client) ![License](https://img.shields.io/github/license/XpycTee/ami-client)


## Table of Contents

  * [Preparation](#preparation)
  * [Examples](#examples)
    * [Connecting](#connecting)
    * [Originate](#originate)
    * [Channels](#channels)
    * [Ping](#ping)
    * [Attended Transfer](#attended-transfer)
    * [Blind Transfer](#blind-transfer)
    * [Redirect](#redirect)
    * [Logoff](#logoff)
    * [Custom requests](#custom-requests)
    * [Callback events](#callback-events)
  * [License](#license)
  * [Authors](#authors)


## Preparation

To get started, you need to enable AMI by configuring the /etc/asterisk/manager.conf file:

```ini
; Enable AMI and have it accept connections only from localhost.
[general]
enabled = yes
; Enable HTTP server, useful for connecting via HTTP
webenabled = yes
bindaddr = 127.0.0.1
; Create an account "hello" with a password "world"
[hello]
secret = world
; Receive all types of events
read = all 
; Allow this user to perform all actions.
write = all 
```

To work with HTTP, enable the built-in HTTP server by configuring the /etc/asterisk/http.conf file:

```ini
; Включить встроенный HTTP-сервер и слушать только соединения на localhost.
[general]
enabled = yes
bindaddr = 127.0.0.1
```

## Examples


### Imports
Import the `HTTPClient` or `TCPClient` class from the `ami.client` module:

```python
from ami.client import HTTPClient

client = HTTPClient('localhost')
```

or

```python
from ami.client import TCPClient

client = TCPClient('localhost')
```


### Connecting

Create a client instance by passing the Asterisk server address as a parameter:

```python
from ami.client import HTTPClient, TCPClient

# Basic connection without encryption
client = HTTPClient('localhost')
client = TCPClient('localhost')

# Connection using SSL/TLS encryption and specifying the CA certificate
client = HTTPClient('localhost', ssl_enabled=True, cert_ca='/path/to/ca.crt')
client = TCPClient('localhost', ssl_enabled=True, cert_ca='/path/to/ca.crt')
```

> Note: If the `cert_ca` and `ssl_enabled` parameters are specified, make sure that SSL/TLS encryption is enabled (`ssl_enabled=True`), otherwise an `AttributeError` exception will be raised.

Establish a connection with the Asterisk server by providing the username and password:

```python
connect_resp = await client.connect(username='hello', password='world')
```

Note that the response of the request will be represented as a list of dictionaries, for example:

```json
[
    {
        "Response": "Success", 
        "Message": "Authentication accepted"
    }
]
```


### Originate

Send a request to the Asterisk server to initiate a call from the `FROM` number to the `DESTINATION` number:

```python
call_resp = await client.originate(originator=FROM, extension=DESTINATION)
```

Note that the result of the request will be represented as a list of dictionaries, for example:

```json
[
    {
        "Response": "Success",
        "Message": "Originate successfully queued"
    }
]
```


### Channels
To get a list of active channels, execute the following request:

```python
channels_resp = await client.channels()
```


The result will be represented as a list of dictionaries, where each dictionary contains information about a channel:
```json
[
    {
        "Response": "Success",
        "EventList": "start",
        "Message": "Channels will follow"
    }, {
        "Event": "CoreShowChannel",
        "Channel": "SIP/trunk",
        "ChannelState": "5",
        "ChannelStateDesc": "Ringing",
        "CallerIDNum": "89999999999",
        "CallerIDName": "CID:999999",
        "ConnectedLineNum": "999999",
        "ConnectedLineName": "<unknown>",
        "Language": "ru",
        "AccountCode": "",
        "Context": "from-trunk",
        "Exten": "89999999999",
        "Priority": "1",
        "Uniqueid": "1694584278.23846",
        "Linkedid": "1694584277.23843",
        "Application": "AppDial",
        "ApplicationData": "(Outgoing Line)",
        "Duration": "00:00:05",
        "BridgeId": ""
    },
  
    ...
  
    {"Event": "CoreShowChannelsComplete", "EventList": "Complete", "ListItems": "N"}
]
```

### Ping

To execute the ping command and check the server availability, use the following code:

```python
ping_resp = await client.ping()
```

The result will be a list of dictionaries indicating the successful execution of the ping command:

```json
[
    {
        "Response": "Success",
        "Ping": "Pong",
        "Timestamp": "1696496997.515802"
    }
]
```

### Attended Transfer

To perform an attended transfer with an operator on the specified `channel` to the `REDIRECT` extension, use the following code:

```python
transfer_resp = await client.attended_transfer(channel=channel, extension=REDIRECT)
```

The result will be a list of dictionaries indicating the successful addition of the transfer request to the queue:

```json
[
    {
        "Response": "Success",
        "Message": "Atxfer successfully queued"
    }
]
```

### Blind Transfer

To perform a blind transfer on the specified `channel` to the `REDIRECT` extension, use the following code:

```python
transfer_resp = await client.blind_transfer(channel=channel, extension=REDIRECT)
```


The result will be a list of dictionaries indicating the successful execution of the transfer:
```json
[
    {
        "Response": "Success",
        "Message": "Transfer succeeded"
    }
]
```


### Redirect
To redirect a call on the specified `channel` to the `REDIRECT` extension, use the following code:

```python
redirect_resp = await client.redirect(extension=REDIRECT, channel=channel)
```

The result will be a list of dictionaries indicating the successful execution of the redirect:

```json
[
    {
        "Response": "Success",
        "Message": "Redirect successful"
    }
]
```


### Logoff
To disconnect from the Asterisk server, use the following code:

```python
logoff_resp = await client.logoff()
```

The result will be a list of dictionaries confirming the disconnection:

```json
[
    {
        "Response": "Goodbye",
        "Message": "Thanks for all the fish."
    }
]
```


### Custom requests
You can also make custom requests manually using the `ami_request` method. Here is an example of how to use this method:

```python
data = {
    "Action": "BlindTransfer",
    "Channel": channel,
    "Exten": extension,
    "Context": context
}

response = await client.ami_request(data)
```

The result of the request will be a list of dictionaries with the response from the Asterisk server:

```json
[
    {
        "Response": "Success",
        "Message": "Transfer succeeded"
    }
]
```
You need to pass a dictionary with the request parameters to the `ami_request` method. In this case, `BlindTransfer` is used in the `Action` field, and specific values are provided for `Channel`, `Exten`, and `Context`.

> For more information about actions, you can refer to the Asterisk documentation: [AMI Actions](https://docs.asterisk.org/Asterisk_16_Documentation/API_Documentation/AMI_Actions)

You can use this method to make requests with different parameters based on your needs.


### Callback events
To register a callback for a specific event, you can use the `register_callback` method. Here is an example of how to use this method:

```python
async def callback(event: dict, client: HTTPClient | TCPClient):
    # Действия, выполняемые при получении события

# Регистрация обратного вызова
await client.register_callback('EventName', callback)
```
In the provided example, we created a `callback` function that takes two parameters: `event` (a dictionary with the event data) and `client` (the client instance).

We then call the `register_callback` method on the client instance, passing it the event name (in this case, `EventName`) and the `callback` function.

Now, when the client receives an event with the name `EventName`, the `callback` callback will be executed, where you can define the necessary actions to be performed upon receiving that event.

> For more information about events, you can refer to the Asterisk documentation: [AMI Events](https://docs.asterisk.org/Asterisk_16_Documentation/API_Documentation/AMI_Events)


## License

Distributed under the Apache License 2.0. See [LICENSE](https://github.com/XpycTee/ami-client/blob/master/LICENSE) for more information.

## Authors

* **XpycTee**

