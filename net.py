# -*- coding: utf-8 -*-
"""Общ HTTPS помощник с резервен сертификатен пакет (certifi).

На някои Windows машини системното хранилище на сертификати е повредено,
непълно или „прихванато“ от антивирусна програма с TLS/SSL инспекция —
Python не може да построи верига до доверен корен и хвърля
CERTIFICATE_VERIFY_FAILED, въпреки че връзката иначе е наред. Първо
пробваме нормално (системното хранилище — зачита легитимни корпоративни
CA сертификати), а само при SSL грешка правим втори опит с вградения в
пакета certifi актуален пакет доверени коренови сертификати (същият,
който ползват requests/pip и повечето Python HTTPS клиенти).
"""
import os
import socket
import ssl
import urllib.error
import urllib.request

#: Перф (13.10.2026): резервният контекст (certifi, ~45 ms) се създава при
#: първата SSL грешка, не при всяко стартиране. _UNSET = още не е пробван.
_UNSET = object()
_FALLBACK_CONTEXT = _UNSET


def _fallback_context():
    global _FALLBACK_CONTEXT
    if _FALLBACK_CONTEXT is _UNSET:
        try:
            import certifi
            _FALLBACK_CONTEXT = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            _FALLBACK_CONTEXT = None
    return _FALLBACK_CONTEXT


# Одит (15.10.2026, QR): lan_ip() е махната — ползваше я само адресът за
# QR кода на бланките (routes_documents), а той е спрян окончателно.


def urlopen(request, timeout=8):
    """urlopen с автоматичен резервен опит през certifi при SSL грешка."""
    try:
        # Всички извикващи (backup.py, updater.py, remote_tunnel.py) подават
        # urllib.request.Request с фиксиран, хардкоднат https:// адрес
        # (GitHub API/releases), не адрес, съставен от потребителски вход.
        return urllib.request.urlopen(request, timeout=timeout)  # nosec B310
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ssl.SSLError) and _fallback_context() is not None:
            return urllib.request.urlopen(request, timeout=timeout, context=_fallback_context())  # nosec B310 -- виж бележката по-горе, същият request обект
        raise


def find_available_port(host, preferred_port, max_tries=10):
    """Одит (находка В17, висок риск): app.py стартираше сървъра (Flask
    dev/waitress) във фонова daemon нишка БЕЗ никаква проверка дали
    портът изобщо е свободен — ако беше зает (друго стартирало копие на
    програмата, или напълно чуждо приложение), OSError изтичаше тихо
    ВЪТРЕ в daemon нишката (изключение там не спира процеса и не се вижда
    никъде видимо), а desktop прозорецът/браузърът СЕ ОТВАРЯХА все пак —
    показвайки каквото и да е ДРУГО приложение, случайно слушащо на
    същия порт, без нито едно съобщение, че истинският сървър изобщо не
    е тръгнал.

    Прави кратка, синхронна проверка (истински bind+close на тестов
    сокет) ПРЕДИ изобщо да се стартира фоновата нишка/прозореца — ако
    предпочитаният порт е зает, пробва следващите `max_tries` поредни
    номера (5000, 5001, 5002, ...) и връща първия свободен. Хвърля
    RuntimeError с ясно, разбираемо съобщение само ако НИТО ЕДИН от
    пробваните портове не е свободен (изключително рядко за диапазон от
    10 поредни номера).

    Живее в net.py (не в app.py) НАРОЧНО — app.py прави тежки
    странични ефекти ПРИ САМИЯ ИМПОРТ (appcore.create_app()), затова не е безопасен за директен import в
    тестовете; net.py е чист помощен модул без такива ефекти."""
    for candidate in range(preferred_port, preferred_port + max_tries):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Одит (31.08.2026, находка №12): `SO_REUSEADDR` тук беше СЛЯП на
        # Windows. На POSIX опцията засяга само сокети в TIME_WAIT, затова
        # пробата коректно вижда зает порт. На Windows същата опция позволява
        # bind ВЪРХУ активно слушащ сокет (освен ако той не е сложил
        # SO_EXCLUSIVEADDRUSE, което нито Python, нито waitress правят) —
        # значи при вече работещо копие на програмата пробата връщаше 5000
        # като „свободен“ и второто копие тръгваше на същия порт.
        if os.name == "nt":
            exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if exclusive is not None:
                try:
                    sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
                except OSError:
                    pass
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host if host != "0.0.0.0" else "", candidate))  # nosec B104 -- host идва от config (мрежов режим opt-in), виж app._run_server
        except OSError:
            continue
        finally:
            sock.close()
        return candidate
    raise RuntimeError(
        "Всички портове от %d до %d са заети — освободете поне един "
        "(затворете друго копие на програмата или приложението, което го "
        "ползва) и пробвайте отново." % (preferred_port, preferred_port + max_tries - 1)
    )
