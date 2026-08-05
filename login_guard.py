# -*- coding: utf-8 -*-
"""Ограничаване на опитите за вход (защита от груб/автоматизиран brute-force).

Изнесено в отделен, чист модул (без зависимост от Flask `request`/`session`),
за да е лесно тестваем и преизползваем — app.py само подава потребителското
име, извлечено от заявката, и извиква тези функции.

Заключването е ПО ПОТРЕБИТЕЛСКО ИМЕ, не по IP адрес: в мрежов режим всички
служители в офиса споделят една и съща публична/локална мрежа/IP зад NAT,
затова заключване по IP би блокирало всички наведнъж от един лош опит.
Заключване по потребителско име спира именно целенасочен brute-force към
конкретен акаунт (напр. „admin“), без да засяга останалите потребители.
"""
import threading
import time

MAX_ATTEMPTS = 5
WINDOW_SECONDS = 15 * 60     # прозорец, в който броим неуспешните опити
LOCKOUT_SECONDS = 5 * 60     # колко стои заключен акаунт след превишен лимит

_lock = threading.Lock()
_attempts = {}  # key -> [count, first_attempt_ts, locked_until_ts_or_None]


def _normalize(key):
    return (key or "").strip().lower()


def is_locked_out(key, now=None):
    """Връща (locked: bool, seconds_remaining: int)."""
    now = time.time() if now is None else now
    key = _normalize(key)
    with _lock:
        entry = _attempts.get(key)
        if not entry:
            return False, 0
        count, first_ts, locked_until = entry
        if locked_until is not None:
            if now < locked_until:
                return True, int(locked_until - now) + 1
            del _attempts[key]
            return False, 0
        if now - first_ts > WINDOW_SECONDS:
            del _attempts[key]
            return False, 0
        return False, 0


def register_failure(key, now=None):
    """Отбелязва неуспешен опит; заключва акаунта при достигане на лимита."""
    now = time.time() if now is None else now
    key = _normalize(key)
    with _lock:
        entry = _attempts.get(key)
        if not entry or now - entry[1] > WINDOW_SECONDS:
            _attempts[key] = [1, now, None]
            return
        count = entry[0] + 1
        locked_until = now + LOCKOUT_SECONDS if count >= MAX_ATTEMPTS else None
        _attempts[key] = [count, entry[1], locked_until]


def clear(key):
    """Изчиства историята след успешен вход."""
    key = _normalize(key)
    with _lock:
        _attempts.pop(key, None)


def reset_all():
    """Само за тестове — изчиства цялото състояние между тестовете."""
    with _lock:
        _attempts.clear()
