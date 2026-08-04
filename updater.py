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
import subprocess
import sys
import threading
import time
import urllib.request

from version import __version__, GITHUB_REPO, EXE_NAME

API_URL = "https://api.github.com/repos/%s/releases/latest" % GITHUB_REPO
LATEST_EXE_URL = "https://github.com/%s/releases/latest/download/%s" % (GITHUB_REPO, EXE_NAME)
_UA = {"User-Agent": "PachoLogistic-Updater"}

_cache = {"time": 0.0, "info": None}


def parse_version(v):
    try:
        return tuple(int(x) for x in str(v).strip().lstrip("vV").split("."))
    except (ValueError, AttributeError):
        return (0,)


def check_for_update(timeout=5):
    """Връща информация за последния релийз в GitHub."""
    req = urllib.request.Request(API_URL, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
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
    """Кеширана проверка (веднъж на час), не блокира при липса на интернет."""
    now = time.time()
    if _cache["info"] is None or now - _cache["time"] > max_age:
        try:
            _cache["info"] = check_for_update()
        except Exception:
            _cache["info"] = None
        _cache["time"] = now
    return _cache["info"]


def is_frozen_windows():
    return bool(getattr(sys, "frozen", False)) and os.name == "nt"


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
    with urllib.request.urlopen(req, timeout=120) as resp, open(new_exe, "wb") as f:
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
