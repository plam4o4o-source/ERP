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
