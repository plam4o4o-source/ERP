# -*- coding: utf-8 -*-
"""Автоматично обновяване на ПачоЛогистик от GitHub Releases.

Проверява последния релийз в хранилището; ако версията му е по-нова от
текущата, изтегля новия PachoLogistic.exe и се рестартира с него.
Обновяването работи само в компилираната .exe версия за Windows —
при стартиране от изходния код се показва само известие.
"""
import json
import os
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import net
from version import __version__, GITHUB_REPO, EXE_NAME

API_URL = "https://api.github.com/repos/%s/releases/latest" % GITHUB_REPO
LATEST_EXE_URL = "https://github.com/%s/releases/latest/download/%s" % (GITHUB_REPO, EXE_NAME)
_UA = {"User-Agent": "PachoLogistic-Updater", "Accept": "application/vnd.github+json"}

_cache = {"time": 0.0, "info": None, "last_error": None}
_FAIL_RETRY_SECONDS = 120  # при неуспех пробваме пак скоро, не чак след час


def parse_version(v):
    try:
        return tuple(int(x) for x in str(v).strip().lstrip("vV").split("."))
    except (ValueError, AttributeError):
        return (0,)


def describe_error(exc):
    """Ясно, конкретно описание на грешката вместо общо 'няма връзка' —
    за да може реалната причина (таймаут, SSL сертификат, ограничение на
    GitHub API, DNS) да се вижда, а не да се крие зад една обща фраза."""
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 403:
            return ("GitHub отказа заявката (403) — вероятно временно ограничение "
                    "на брой заявки (rate limit). Опитайте отново след няколко минути.")
        if exc.code == 404:
            return "Хранилището или релийзът не са намерени в GitHub (404)."
        return "GitHub отговори с грешка %s." % exc.code
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, ssl.SSLError):
            return ("Проблем със сигурната връзка (SSL сертификат): %s. Възможна причина: "
                    "антивирусна програма с TLS/SSL инспекция, корпоративен прокси, или "
                    "остарели системни сертификати на Windows." % reason)
        return "Няма връзка с GitHub (%s). Проверете интернет връзката, защитната стена или антивирусната програма." % reason
    if isinstance(exc, TimeoutError):
        return "Заявката към GitHub изтече (timeout) — бавна или нестабилна връзка."
    return "Неочаквана грешка: %s: %s" % (type(exc).__name__, exc)


def check_for_update(timeout=8):
    """Връща информация за последния релийз в GitHub."""
    req = urllib.request.Request(API_URL, headers=_UA)
    with net.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    latest = str(data.get("tag_name", "")).lstrip("vV")
    download = LATEST_EXE_URL
    for asset in data.get("assets", []):
        if asset.get("name") == EXE_NAME:
            download = asset.get("browser_download_url") or download
    return {
        "current": __version__,
        "latest": latest,
        "available": parse_version(latest) > parse_version(__version__),
        "url": data.get("html_url", "https://github.com/%s/releases" % GITHUB_REPO),
        "download": download,
        "can_install": is_frozen_windows(),
    }


def check_cached(max_age=3600):
    """Кеширана проверка (веднъж на час при успех; при неуспех пробва пак
    много по-скоро, вместо да „заключи“ грешка за цял час)."""
    now = time.time()
    age_limit = max_age if _cache["info"] is not None else _FAIL_RETRY_SECONDS
    if now - _cache["time"] > age_limit:
        try:
            _cache["info"] = check_for_update()
            _cache["last_error"] = None
        except Exception as exc:
            _cache["info"] = None
            _cache["last_error"] = describe_error(exc)
        _cache["time"] = now
    return _cache["info"]


def is_frozen_windows():
    return bool(getattr(sys, "frozen", False)) and os.name == "nt"


def start_auto_update_loop(is_server_func, first_delay=20, interval=7200):
    """Фонов цикъл, който проверява за нова версия и я инсталира НАИСТИНА
    автоматично — без потребителят да трябва да отваря таблото и да
    натиска „Обнови сега“. Стартира се веднъж при пускане на програмата.

    Пропуска се изцяло, ако тази инсталация в момента служи като
    централен сървър за други компютри в офиса (мрежов режим,
    is_server_func() == True) — там автоматичен рестарт би прекъснал
    работата на всички останали служители неочаквано; обновяването остава
    ръчно през бутона на таблото за тези инсталации.

    При грешка (няма връзка, GitHub недостъпен и т.н.) просто изчаква и
    пробва отново на следващата итерация — никога не гърми програмата."""
    if not is_frozen_windows():
        return

    def _loop():
        time.sleep(first_delay)
        while True:
            try:
                if not is_server_func():
                    info = check_for_update()
                    if info["available"]:
                        install_update(info["download"])
                        return  # install_update рестартира процеса (os._exit) при успех
            except Exception:
                pass
            time.sleep(interval)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()


def install_update(download_url):
    """Изтегля новата версия и рестартира програмата с нея (само .exe/Windows)."""
    if not is_frozen_windows():
        raise RuntimeError(
            "Автоматичното обновяване работи само в PachoLogistic.exe за Windows. "
            "Изтеглете новата версия ръчно от GitHub."
        )
    exe = sys.executable
    new_exe = exe + ".new"
    req = urllib.request.Request(download_url, headers=_UA)
    with net.urlopen(req, timeout=120) as resp, open(new_exe, "wb") as f:
        shutil.copyfileobj(resp, f)
    if os.path.getsize(new_exe) < 1_000_000:
        os.remove(new_exe)
        raise RuntimeError("Изтегленият файл изглежда повреден — обновяването е прекратено.")

    # Скрипт, който изчаква затварянето, подменя .exe и стартира новата версия
    bat_path = os.path.join(os.path.dirname(exe), "pacho_update.bat")
    with open(bat_path, "w", encoding="ascii") as f:
        f.write(
            "@echo off\r\n"
            "timeout /t 3 /nobreak >nul\r\n"
            'move /y "%s" "%s" >nul\r\n'
            'start "" "%s"\r\n'
            'del "%%~f0"\r\n' % (new_exe, exe, exe)
        )
    DETACHED_PROCESS = 0x00000008
    subprocess.Popen(["cmd.exe", "/c", bat_path],
                     creationflags=DETACHED_PROCESS, close_fds=True)
    # кратко изчакване, за да стигне отговорът до браузъра, после изход
    threading.Timer(1.5, lambda: os._exit(0)).start()
