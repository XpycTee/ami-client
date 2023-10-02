"""
Пакет ami-client помогает работать с Asterisk Manager Interface используя asyncio и aiohttp

Для начала работы нужно включить AMI прописав конфигурацию в файл /etc/asterisk/manager.conf:

```
; Включите AMI и попросите его принимать соединения только с localhost.
[general]
enabled = yes
webenabled = yes ; Включить HTTP-сервер, пригодиться для подключения по HTTP
bindaddr = 127.0.0.1
; Создайте учетную запись «hello» с паролем «world»
[hello]
secret = world
read = all ; Получать все типы событий
write = all ; Разрешить этому пользователю выполнять все действия.
```


У данного пакета есть 2 способа подключения:
    - TCP:
        Класс TCPClient: Позволяет работать асинхронно через tcp
    - HTTP:
        Класс HTTPClient: Позволяет работать асинхронно используя HTTP запросы к встроенному HTTP серверу Asterisk.

Для работы с HTTP включите встроенный сервер HTTP, прописав конфигурацию в файл /etc/asterisk/http.conf:
```
; Включить встроенный HTTP-сервер и слушать только соединения на localhost.
[general]
enabled = yes
bindaddr = 127.0.0.1
```


Ilya Pavlushin
ilyoni@ya.ru
# License: Apache License 2.0
"""
__author__ = 'XpycTee'


