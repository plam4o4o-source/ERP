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

try:
    import certifi
    _FALLBACK_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _FALLBACK_CONTEXT = None


def lan_ip():
    """IP адресът на тази машина в локалната мрежа (напр. 192.168.1.5),
    или None, ако не може да се определи (няма мрежа изобщо).

    Ползва се от QR кода за публичен преглед на документ
    (routes_documents._public_doc_url): адрес „127.0.0.1“, вграден в QR,
    е безполезен на телефон — там 127.0.0.1 сочи самия телефон, не
    компютъра със сървъра — затова се заменя с истинския мрежов адрес.

    Трикът с UDP „connect“ НЕ праща никакъв реален трафик (UDP connect
    само настройва маршрута) — операционната система просто избира кой
    локален интерфейс/адрес би ползвала за външна връзка, което е точно
    търсеният отговор и работи и без реален достъп до интернет (стига да
    има мрежова карта с маршрут по подразбиране). 192.0.2.1 е от
    запазения за документация блок TEST-NET-1 (RFC 5737) — никога не е
    реален адрес в ничия мрежа."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return None
    # 127.x самият той не е полезен резултат — все едно нищо не сме намерили.
    return None if ip.startswith("127.") else ip


def urlopen(request, timeout=8):
    """urlopen с автоматичен резервен опит през certifi при SSL грешка."""
    try:
        # Всички извикващи (backup.py, updater.py, remote_tunnel.py) подават
        # urllib.request.Request с фиксиран, хардкоднат https:// адрес
        # (GitHub API/releases), не адрес, съставен от потребителски вход.
        return urllib.request.urlopen(request, timeout=timeout)  # nosec B310
    except urllib.error.URLError as exc:
        if _FALLBACK_CONTEXT is not None and isinstance(exc.reason, ssl.SSLError):
            return urllib.request.urlopen(request, timeout=timeout, context=_FALLBACK_CONTEXT)  # nosec B310 -- виж бележката по-горе, същият request обект
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
    странични ефекти ПРИ САМИЯ ИМПОРТ (appcore.create_app() с
    run_boot_tasks=True), затова не е безопасен за директен import в
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
