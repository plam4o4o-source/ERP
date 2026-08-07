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
import ssl
import urllib.error
import urllib.request

try:
    import certifi
    _FALLBACK_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _FALLBACK_CONTEXT = None


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
