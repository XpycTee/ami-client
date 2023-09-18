
<h3 align="center">Async AMI Client</h3>

<p align="center">
Асинхронный клиент для работы с Asterisk Manager Interface
</p>

![PyPI - Downloads](https://img.shields.io/pypi/dm/ami-client) ![PyPI - Version](https://img.shields.io/pypi/v/ami-client) ![pyversions](https://img.shields.io/pypi/pyversions/ami-client) ![Downloads](https://img.shields.io/github/downloads/XpycTee/ami_client/total) ![Contributors](https://img.shields.io/github/contributors/XpycTee/ami_client?color=dark-green) ![Issues](https://img.shields.io/github/issues/XpycTee/ami_client) ![License](https://img.shields.io/github/license/XpycTee/ami_client) 

[EN Readme](./EN-README.md)

## Содержание

  * [Подготовка](#подготовка)
  * [Примеры](#Примеры)
    * [Подключение](#Подключение)
    * [Originate](#originate)
    * [Channels](#channels)
    * [Blind Transfer](#blind-transfer)
    * [Redirect](#redirect)
    * [Logoff](#logoff)
    * [Custom requests](#custom-requests)
    * [Callback events](#callback-events)
  * [Лицензия](#Лицензия)
  * [Авторы](#Авторы)


## Подготовка

Для начала работы нужно включить AMI прописав конфигурацию в файл /etc/asterisk/manager.conf:

```ini
; Включите AMI и попросите его принимать соединения только с localhost.
[general]
enabled = yes
; Включить HTTP-сервер, пригодиться для подключения по HTTP
webenabled = yes 
bindaddr = 127.0.0.1
; Создайте учетную запись «hello» с паролем «world»
[hello]
secret = world
; Получать все типы событий
read = all 
; Разрешить этому пользователю выполнять все действия.
write = all 
```

Для работы с HTTP включите встроенный сервер HTTP, прописав конфигурацию в файл /etc/asterisk/http.conf:
```ini
; Включить встроенный HTTP-сервер и слушать только соединения на localhost.
[general]
enabled = yes
bindaddr = 127.0.0.1
```

## Примеры


### Импорты
Импортируйте класс `HTTPClient` или `TCPClient` из модуля `ami.client`:

```python
from ami.client import HTTPClient
```

или

```python
from ami.client import TCPClient
```


### Подключение

Создайте экземпляр клиента, передавая адрес сервера Asterisk в качестве параметра:

```python
from ami.client import HTTPClient

client = HTTPClient('localhost')
```

или

```python
from ami.client import TCPClient

client = TCPClient('localhost')
```

Установите соединение с сервером Asterisk, указав имя пользователя и пароль:

```python
await client.connect(username='hello', password='world')
```


### Originate
Отправьте запрос на сервер Asterisk для инициирования звонка с номера `FROM` на номер `DESTINATION`:

```python
call_resp = await client.originate(originator=FROM, extension=DESTINATION)
```

Обратите внимание, что результат запроса будет представлен в виде словаря, например:
```json
[
    {
        "Response": "Success",
        "Message": "Originate successfully queued"
    }
]
```


### Channels
Чтобы получить список активных каналов, выполните запрос:

```python
channels_resp = await client.channels()
```


Результат будет представлен в виде списка словарей, где каждый словарь содержит информацию о канале:
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


### Blind Transfer

Для выполнения слепого перевода в указанном канале `channel` на номер `REDIRECT`, используйте следующий код:

```python
transfer_resp = await client.blind_transfer(channel=channel, extension=REDIRECT)
```


Результатом будет словарь, указывающий на успешное выполнение перевода:

```json
[
    {
        "Response": "Success",
        "Message": "Transfer succeeded"
    }
]
```


### Redirect
Для перенаправления вызова в указанном канале `channel` на номер `REDIRECT`, используйте следующий код:

```python
redirect_resp = await client.redirect(extension=REDIRECT, channel=channel)
```

Результатом будет словарь, указывающий на успешное выполнение перенаправления:

```json
[
    {
        "Response": "Success",
        "Message": "Redirect successful"
    }
]
```


### Logoff
Чтобы отключиться от сервера Asterisk, используйте следующий код:

```python
logoff_resp = await client.logoff()
```

Результатом будет словарь с подтверждением разрыва соединения:

```json
[
    {
        "Response": "Goodbye",
        "Message": "Thanks for all the fish."
    }
]
```


### Custom requests

Вы также можете делать запросы вручную, используя метод `ami_request`. Ниже представлен пример использования этого метода:
```python
data = {
    "Action": "BlindTransfer",
    "Channel": channel,
    "Exten": extension,
    "Context": context
}

response = await client.ami_request(data)
```

Результатом выполнения запроса будет словарь с ответом от сервера Asterisk

```json
[
    {
        "Response": "Success",
        "Message": "Transfer succeeded"
    }
]
```
Вы должны передать словарь с параметрами запроса в метод `ami_request`. В данном случае, используется `BlindTransfer` в поле `Action`, а также указываются значения для `Channel`, `Exten` и `Context`.

Подробнее об действиях (Actions), вы можете узнать в документации Asterisk: [AMI Actions](https://docs.asterisk.org/Asterisk_16_Documentation/API_Documentation/AMI_Actions)

Вы можете использовать этот метод для выполнения запросов с различными параметрами в зависимости от ваших потребностей.


### Callback events

Чтобы зарегистрировать обратный вызов (callback) для определенного события, вы можете использовать метод `register_callback`. Ниже приведен пример использования этого метода:

```python
async def callback(event: dict, client: HTTPClient | TCPClient):
    # Действия, выполняемые при получении события

# Регистрация обратного вызова
await client.register_callback('EventName', callback)
```

В приведенном примере, мы создали функцию `callback`, которая принимает два параметра: `event` (словарь с данными о событии) и `client` (экземпляр клиента).

Мы затем вызываем метод `register_callback` у экземпляра клиента, передавая ему имя события (в данном случае `EventName`) и функцию `callback`.

Теперь, когда клиент получит событие с именем `EventName`, будет выполнен обратный вызов `callback`, где вы можете определить необходимые действия, которые должны выполняться при получении данного события.

Подробнее о событиях, вы можете узнать в документации Asterisk: [AMI Events](https://docs.asterisk.org/Asterisk_16_Documentation/API_Documentation/AMI_Events)



## Лицензия

Распространяется в рамках Apache License 2.0. Смотрите [ЛИЦЕНЗИЯ](./LICENSE) для получения дополнительной информации.

## Авторы

* **XpycTee**

