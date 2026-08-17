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

# Одит (12.08.2026, находка №28, дребна): заключването по-горе е по
# потребителско име — записите се трият само ЛЕНИВО (при следваща проверка
# на СЪЩИЯ ключ, извън WINDOW_SECONDS), никога проактивно. Нападател, който
# опитва хиляди РАЗЛИЧНИ несъществуващи потребителски имена към /login (за
# да провокира скъпото check_password_hash при съществуващи, или просто за
# да пълни паметта), кара речника да расте неограничено за целия uptime на
# процеса. `register_failure`/`is_locked_out` вече обхождат речника само по
# конкретния ключ (O(1)), затова пълно обхождане при всеки Nти опит е
# евтино в сравнение с неограничения растеж, който предотвратява.
_CLEANUP_EVERY_N_CALLS = 500
_calls_since_cleanup = [0]


def _cleanup_stale_locked(now):
    """Обхожда ЦЕЛИЯ речник и маха записи, чийто прозорец/заключване вече е
    изтекъл — вика се само на всеки _CLEANUP_EVERY_N_CALLS извиквания (виж
    _maybe_cleanup), не при всяко, за да остане евтино. Извиква се ВЕЧЕ
    заключен _lock (виж _maybe_cleanup) — не взима заключването сам."""
    stale = []
    for key, (count, first_ts, locked_until) in _attempts.items():
        if locked_until is not None:
            if now >= locked_until:
                stale.append(key)
        elif now - first_ts > WINDOW_SECONDS:
            stale.append(key)
    for key in stale:
        del _attempts[key]


def _maybe_cleanup(now):
    """Вика се ВЕЧЕ под _lock (viz register_failure/is_locked_out) —
    периодична проактивна чистка вместо ленива по ключ."""
    _calls_since_cleanup[0] += 1
    if _calls_since_cleanup[0] >= _CLEANUP_EVERY_N_CALLS:
        _calls_since_cleanup[0] = 0
        _cleanup_stale_locked(now)


# Одит (12.08.2026, находка №14, средна): заключването по-горе е САМО по
# потребителско име — не пречи на самата СКЪПА стъпка (check_password_hash,
# scrypt, ~120ms CPU/памет) да се изпълни при ВСЯКА заявка към /login с
# ВАЛИДНО съществуващо потребителско име (напр. „admin“) и произволна
# грешна парола, дори акаунтът вече да е заключен. При мрежов режим
# (waitress, няколко нишки) само няколко паралелни заявки в секунда могат
# да запълнят всички нишки на сървъра — DoS от нападател БЕЗ никакъв
# акаунт. Този брояч е ГЛОБАЛЕН (не по потребителско име) и се проверява
# от routes_auth.login() ПРЕДИ check_password_hash изобщо да се извика.
_GLOBAL_MAX_ATTEMPTS = 30
_GLOBAL_WINDOW_SECONDS = 10
_global_lock = threading.Lock()
_global_attempts = []  # списък с timestamps на скорошни POST /login опити


def register_global_attempt(now=None):
    """Отбелязва нов опит за вход (независимо от резултата, ПРЕДИ
    check_password_hash) — вика се безусловно на всяка POST /login."""
    now = time.time() if now is None else now
    with _global_lock:
        cutoff = now - _GLOBAL_WINDOW_SECONDS
        while _global_attempts and _global_attempts[0] < cutoff:
            _global_attempts.pop(0)
        _global_attempts.append(now)


def is_globally_throttled(now=None):
    """Дали общият брой опити (всички потребителски имена заедно) в
    последните _GLOBAL_WINDOW_SECONDS секунди надвишава прага — при
    надвишаване routes_auth.login() отказва заявката с ясно съобщение
    ПРЕДИ да похарчи каквото и да е CPU време за хеширане."""
    now = time.time() if now is None else now
    with _global_lock:
        cutoff = now - _GLOBAL_WINDOW_SECONDS
        recent = [t for t in _global_attempts if t >= cutoff]
        return len(recent) > _GLOBAL_MAX_ATTEMPTS


def reset_global():
    """Само за тестове."""
    with _global_lock:
        _global_attempts.clear()


# Одит (16.08.2026, находка №6, средна): _GLOBAL_MAX_ATTEMPTS/
# _GLOBAL_WINDOW_SECONDS по-горе е ЕДИН ОБЩ праг за ВСИЧКИ клиенти заедно
# (защита от изчерпване на нишките на сървъра — виж коментара там) — но
# точно затова ЕДИН нападателски IP адрес, заливащ /login с достатъчно
# опити, надвишава глобалния праг и заключва ВСИЧКИ легитимни потребители
# (дори от съвсем различни IP-та/офиси) извън системата за следващите
# _GLOBAL_WINDOW_SECONDS секунди — тривиален DoS от един-единствен източник.
#
# Добавя допълнителен, ПО-СТРОГ праг ПО IP АДРЕС (request.remote_addr —
# директната TCP връзка към Flask/waitress; НЕ се чете X-Forwarded-For или
# подобно заглавие, защото е подправимо от самия клиент — в тази
# инсталация няма доверен обратен прокси пред waitress, който да го
# гарантира), който спира ЕДИН нападателски адрес много преди да стигне до
# общия праг. Глобалният праг остава като последна защита срещу
# изчерпване на нишките; новият per-IP праг е ПЪРВАТА линия — засяга само
# реалния източник, не всички останали потребители.
_IP_MAX_ATTEMPTS = 15
_IP_WINDOW_SECONDS = 10
_ip_lock = threading.Lock()
_ip_attempts = {}  # ip -> [timestamps на скорошни опити]
_ip_calls_since_cleanup = [0]
_IP_CLEANUP_EVERY_N_CALLS = 500


def _cleanup_stale_ip_attempts(now):
    """Обхожда всички следени IP адреси и маха тези, чийто прозорец вече е
    напълно изтекъл — вика се само периодично (виж register_ip_attempt),
    за да не расте речникът неограничено при масирана атака от много
    различни IP адреси (същият модел като _cleanup_stale_locked по-горе,
    находка №28)."""
    cutoff = now - _IP_WINDOW_SECONDS
    stale = [ip for ip, times in _ip_attempts.items() if not times or times[-1] < cutoff]
    for ip in stale:
        del _ip_attempts[ip]


def register_ip_attempt(ip, now=None):
    """Отбелязва нов опит за вход от конкретен IP адрес (независимо от
    резултата) — вика се безусловно на всяка POST /login, редом с
    register_global_attempt."""
    now = time.time() if now is None else now
    ip = ip or "unknown"
    with _ip_lock:
        cutoff = now - _IP_WINDOW_SECONDS
        times = _ip_attempts.setdefault(ip, [])
        while times and times[0] < cutoff:
            times.pop(0)
        times.append(now)
        _ip_calls_since_cleanup[0] += 1
        if _ip_calls_since_cleanup[0] >= _IP_CLEANUP_EVERY_N_CALLS:
            _ip_calls_since_cleanup[0] = 0
            _cleanup_stale_ip_attempts(now)


def is_ip_throttled(ip, now=None):
    """Дали броят опити от КОНКРЕТНИЯ IP адрес в последните
    _IP_WINDOW_SECONDS секунди надвишава прага."""
    now = time.time() if now is None else now
    ip = ip or "unknown"
    with _ip_lock:
        cutoff = now - _IP_WINDOW_SECONDS
        recent = [t for t in _ip_attempts.get(ip, []) if t >= cutoff]
        return len(recent) > _IP_MAX_ATTEMPTS


def reset_ip():
    """Само за тестове."""
    with _ip_lock:
        _ip_attempts.clear()
        _ip_calls_since_cleanup[0] = 0


def _normalize(key):
    return (key or "").strip().lower()


def is_locked_out(key, now=None):
    """Връща (locked: bool, seconds_remaining: int)."""
    now = time.time() if now is None else now
    key = _normalize(key)
    with _lock:
        _maybe_cleanup(now)
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
        _maybe_cleanup(now)
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
        _calls_since_cleanup[0] = 0
    reset_global()
    reset_ip()
