"""Общий сетевой слой: повтор запроса, сорванного сетью, а не сервисом.

Приложение опрашивает десятки внешних сервисов одновременно, и на домашней
сети часть запросов срывается до ответа: DNS не успел ответить
(``getaddrinfo failed``), TLS-соединение оборвал провайдер или антивирус
(``SSLEOFError``), случился таймаут чтения. Сервис при этом рабочий —
следующий запрос проходит, — но канал уже показывал «ошибка».

`install()` один раз подменяет `requests.Session.request` для GET и HEAD:
такие запросы ничего не меняют на сервере, поэтому их безопасно повторить.
POST не повторяем: вторая отправка сообщения в Telegram дала бы дубль.
"""

import random
import socket
import ssl
import time

import requests

ATTEMPTS = 3
BACKOFF_SECONDS = 0.7
MAX_PAUSE_SECONDS = 6.0
RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

_installed = False
_original_request = None


def is_transient(error) -> bool:
    """Сбой, который проходит сам: DNS, TLS, обрыв, таймаут, 5xx."""
    if isinstance(error, requests.HTTPError):
        status = int(getattr(getattr(error, "response", None),
                             "status_code", 0) or 0)
        return status in RETRY_STATUSES or status >= 500
    if isinstance(error, requests.RequestException):
        if isinstance(error, (requests.ConnectionError, requests.Timeout)):
            return True
        text = str(error).lower()
        return any(word in text for word in
                   ("ssl", "eof", "getaddrinfo", "name resolution",
                    "connection aborted", "connection reset",
                    "remote end closed", "timed out"))
    return isinstance(error, (ssl.SSLError, socket.gaierror, ConnectionError))


def _pause(attempt, retry_after=None) -> None:
    delay = BACKOFF_SECONDS * (2 ** attempt) + random.uniform(0.0, 0.3)
    if retry_after:
        try:
            delay = max(delay, min(float(retry_after), 15.0))
        except (TypeError, ValueError):
            pass
    time.sleep(min(delay, MAX_PAUSE_SECONDS))


def _request(self, method, url, **kwargs):
    """GET/HEAD с повторами; остальные методы идут как обычно."""
    if str(method or "").upper() not in ("GET", "HEAD"):
        return _original_request(self, method, url, **kwargs)
    last_error = None
    for attempt in range(ATTEMPTS):
        try:
            response = _original_request(self, method, url, **kwargs)
        except requests.RequestException as error:
            if attempt == ATTEMPTS - 1 or not is_transient(error):
                raise
            last_error = error
            _pause(attempt)
            continue
        if response.status_code in RETRY_STATUSES and attempt < ATTEMPTS - 1:
            retry_after = response.headers.get("Retry-After")
            last_error = requests.HTTPError(
                f"HTTP {response.status_code}", response=response)
            response.close()
            _pause(attempt, retry_after)
            continue
        return response
    raise last_error if last_error else requests.ConnectionError(url)


def install() -> bool:
    """Включает повторы один раз за процесс. True — если включил сейчас."""
    global _installed, _original_request
    if _installed:
        return False
    _original_request = requests.sessions.Session.request
    requests.sessions.Session.request = _request
    _installed = True
    return True


def post_connect_retry(url, attempts=2, **kwargs):
    """POST с одной повторной попыткой только при сбое соединения.

    Повторяем лишь когда запрос не дошёл до сервера (DNS, TLS до установки
    соединения): таймаут чтения и 5xx не трогаем — сообщение могло уйти.
    """
    count = max(1, int(attempts))
    last_error = None
    for attempt in range(count):
        try:
            return requests.post(url, **kwargs)
        except requests.RequestException as error:
            last_error = error
            connection_only = (isinstance(error, requests.ConnectionError)
                               and not isinstance(error, requests.Timeout))
            if attempt >= count - 1 or not connection_only:
                raise
            _pause(attempt)
    raise last_error if last_error else requests.ConnectionError(url)
