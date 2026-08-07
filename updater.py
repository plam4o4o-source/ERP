# -*- coding: utf-8 -*-
"""Автоматично обновяване на ПачоЛогистик от GitHub Releases.

Проверява последния релийз в хранилището; ако версията му е по-нова от
текущата, изтегля новия PachoLogistic.exe и се рестартира с него.
Обновяването работи само в компилираната .exe версия за Windows —
при стартиране от изходния код се показва само известие.
"""
import hashlib
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

import applog
import net
from version import __version__, GITHUB_REPO, EXE_NAME

API_URL = "https://api.github.com/repos/%s/releases/latest" % GITHUB_REPO
LATEST_EXE_URL = "https://github.com/%s/releases/latest/download/%s" % (GITHUB_REPO, EXE_NAME)
# Публикуван от release.yml до всеки релийз — списък с SHA-256 контролни
# суми на изтегляемите файлове, в стандартния формат на `sha256sum`
# (виж parse_sha256sums по-долу). Позволява да проверим, че свалената .exe
# е БИТ ЗА БИТ същата като компилираната в CI, преди да я пуснем да замести
# работещата версия — вместо само размер + магически байтове (недостатъчно
# срещу компрометирано хранилище/токен, публикуващ подменен .exe).
CHECKSUMS_ASSET_NAME = "SHA256SUMS.txt"
_UA = {"User-Agent": "PachoLogistic-Updater", "Accept": "application/vnd.github+json"}

_cache = {"time": 0.0, "info": None, "last_error": None}
_FAIL_RETRY_SECONDS = 120  # при неуспех пробваме пак скоро, не чак след час

# Пази _cache от надпревара между заявки (виж находка M5:
# ПЛАН_ЗА_РАЗРАБОТКА.md — _cache е споделен обект, четен/писан от
# нишките на заявки БЕЗ заключване). check_cached() се вика на ВСЯКО
# зареждане на таблото (routes_dashboard.py) — в мрежов режим с няколко
# едновременни служители две заявки могат едновременно да видят
# остарял кеш, да пуснат по 2 излишни GitHub заявки, и да презапишат
# резултата на другата в грешен ред (последната записва "печели", без
# значение коя всъщност е по-новата проверка). Огледален модел на
# appcore._preview_lock — заключването около самата мрежова заявка
# also служи като полезен bonus: конкурентни заявки просто изчакват
# резултата от вече текущата проверка, вместо да дублират GitHub
# заявката (по-малък риск от rate limit, виж describe_error по-долу).
_cache_lock = threading.Lock()


def set_cache(info, last_error=None):
    """Записва резултат от РЪЧНА проверка (routes_admin.update_check) в
    споделения кеш, под заключване — за да го вижда и таблото веднага
    след това, без несъответствие с check_cached()."""
    with _cache_lock:
        _cache["info"] = info
        _cache["last_error"] = last_error
        _cache["time"] = time.time()


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


def parse_sha256sums(text, filename):
    """Извлича hex SHA-256 за `filename` от съдържание във формат на
    стандартния `sha256sum` инструмент: "<64 hex символа>  <име_на_файл>"
    на всеки ред (два интервала или единичен интервал, `sha256sum` вариант
    без значение). Връща None, ако файлът не е упоменат в списъка."""
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == filename:
            digest = parts[0].strip().lower()
            if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest):
                return digest
    return None


def sha256_of_file(path, chunk_size=1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch_expected_checksum(assets, timeout):
    """Изтегля SHA256SUMS.txt (ако release-ът го публикува) и връща
    очакваната контролна сума за EXE_NAME, или None ако липсва/недостъпен."""
    for asset in assets:
        if asset.get("name") == CHECKSUMS_ASSET_NAME:
            url = asset.get("browser_download_url")
            if not url:
                return None
            try:
                req = urllib.request.Request(url, headers=_UA)
                with net.urlopen(req, timeout=timeout) as resp:
                    text = resp.read().decode("utf-8", errors="replace")
                return parse_sha256sums(text, EXE_NAME)
            except Exception:
                applog.log_exception("updater._fetch_expected_checksum: неуспешно изтегляне на SHA256SUMS.txt")
                return None
    return None


def check_for_update(timeout=8):
    """Връща информация за последния релийз в GitHub."""
    req = urllib.request.Request(API_URL, headers=_UA)
    with net.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    latest = str(data.get("tag_name", "")).lstrip("vV")
    assets = data.get("assets", [])
    download = LATEST_EXE_URL
    for asset in assets:
        if asset.get("name") == EXE_NAME:
            download = asset.get("browser_download_url") or download
    return {
        "current": __version__,
        "latest": latest,
        "available": parse_version(latest) > parse_version(__version__),
        "url": data.get("html_url", "https://github.com/%s/releases" % GITHUB_REPO),
        "download": download,
        "can_install": is_frozen_windows(),
        "expected_sha256": _fetch_expected_checksum(assets, timeout),
    }


def check_cached(max_age=3600):
    """Кеширана проверка (веднъж на час при успех; при неуспех пробва пак
    много по-скоро, вместо да „заключи“ грешка за цял час).

    Цялата проверка-и-обновяване (вкл. самата мрежова заявка) е под
    _cache_lock (М5) — конкурентни заявки на таблото просто изчакват
    резултата от вече текущата проверка, вместо да пускат дублирани
    GitHub заявки и да си презаписват резултатите в произволен ред."""
    with _cache_lock:
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
                        install_update(info["download"], info.get("expected_sha256"))
                        return  # install_update рестартира процеса (os._exit) при успех
            except Exception:
                applog.log_exception("updater.start_auto_update_loop: грешка при проверка/инсталация на обновяване")
            time.sleep(interval)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()


def install_update(download_url, expected_sha256=None):
    """Изтегля новата версия и рестартира програмата с нея (само .exe/Windows).

    `expected_sha256`, ако е подаден (от check_for_update()["expected_sha256"],
    четено от SHA256SUMS.txt в самия GitHub релийз), се проверява СЛЕД
    размер+магически байтове, преди .exe-то да замести работещата версия.
    Ако липсва (по-стар релийз отпреди тази проверка да съществува, или
    временна мрежова грешка при изтеглянето на манифеста), проверката се
    пропуска и се разчита само на съществуващите size/MZ проверки — преходно
    поведение, докато всички клиенти минат отвъд тази версия."""
    if not is_frozen_windows():
        raise RuntimeError(
            "Автоматичното обновяване работи само в PachoLogistic.exe за Windows. "
            "Изтеглете новата версия ръчно от GitHub."
        )
    exe = sys.executable
    new_exe = exe + ".new"
    req = urllib.request.Request(download_url, headers=_UA)
    with net.urlopen(req, timeout=120) as resp:
        content_length = resp.headers.get("Content-Length")
        expected_size = int(content_length) if content_length and content_length.isdigit() else None
        with open(new_exe, "wb") as f:
            shutil.copyfileobj(resp, f)

    # Проверка, че изтегленото е ЦЯЛ, неповреден .exe файл — иначе
    # прекъснат/непълен интернет пренос (нестабилна връзка, антивирус,
    # прекъснат сървър) може да остави частично изтеглен файл, който все
    # пак е над 1MB (реалният .exe е ~20MB) и преминава стар, твърде слаб
    # проверка. Такъв повреден .exe при стартиране гърми с "Failed to load
    # Python DLL ... LoadLibrary: The specified module could not be found"
    # — вградените в PyInstaller onefile ресурси просто липсват в опашката
    # на файла. Затова проверяваме и точния размер (Content-Length от
    # сървъра), и че файлът реално започва като валидна Windows програма
    # (магическите байтове "MZ"), преди изобщо да го пуснем да замести
    # работещата стара версия.
    actual_size = os.path.getsize(new_exe)
    problem = None
    if actual_size < 1_000_000:
        problem = "файлът е твърде малък (%d байта)" % actual_size
    elif expected_size is not None and actual_size != expected_size:
        problem = "непълно изтегляне (%d от общо %d байта)" % (actual_size, expected_size)
    else:
        with open(new_exe, "rb") as f:
            magic = f.read(2)
        if magic != b"MZ":
            problem = "файлът не е валидна Windows програма (повреден при изтеглянето)"
        elif expected_sha256:
            actual_hash = sha256_of_file(new_exe)
            if actual_hash.lower() != expected_sha256.lower():
                problem = ("контролната сума не съвпада с публикуваната от build "
                           "конвейера (SHA-256 %s ≠ очаквано %s) — файлът може да е "
                           "подменен или повреден при пренос" % (actual_hash, expected_sha256))
    if problem:
        os.remove(new_exe)
        raise RuntimeError(
            "Изтегленият файл изглежда повреден — %s. Обновяването е прекратено — "
            "старата версия остава активна и работеща; ще се пробва пак автоматично "
            "по-късно." % problem
        )

    # Скрипт, който изчаква затварянето, подменя .exe и стартира новата
    # версия. ВАЖНО: НЕ ползваме "timeout" — стартиран е под
    # DETACHED_PROCESS (без никаква конзола), а "timeout" изисква конзола
    # за вход и там гърми веднага с "Input redirection is not supported",
    # без изобщо да изчака. Тогава "move" се опитва да презапише .exe-то,
    # докато то все още е заключено от затварящия се стар процес — move-ът
    # тихо се проваля, и се стартира СТАРАТА, незаменена версия (точно
    # симптомът, докладван от потребител: рестарт, но старата версия).
    # "ping" не изисква конзола и работи навсякъде; цикълът пробва
    # многократно, докато файлът реално се освободи, вместо да разчита на
    # фиксирано (и евентуално недостатъчно) закъснение.
    log_path = os.path.join(os.path.dirname(exe), "pacho_update.log")
    bat_path = os.path.join(os.path.dirname(exe), "pacho_update.bat")
    bat_content = (
        "@echo off\r\n"
        "set TRIES=0\r\n"
        ":retry\r\n"
        'if not exist "%s" goto done\r\n' % new_exe +
        "ping -n 2 127.0.0.1 >nul\r\n"
        'move /y "%s" "%s" >nul 2>&1\r\n' % (new_exe, exe) +
        "set /a TRIES+=1\r\n"
        'if exist "%s" if %%TRIES%% LSS 20 goto retry\r\n' % new_exe +
        ":done\r\n"
        'if exist "%s" (echo FAILED: could not replace exe after 20 tries> "%s"'
        ') else (echo OK: updated successfully> "%s")\r\n' % (new_exe, log_path, log_path) +
        'start "" "%s"\r\n' % exe +
        'del "%~f0"\r\n'
    )
    with open(bat_path, "w", encoding="ascii") as f:
        f.write(bat_content)
    DETACHED_PROCESS = 0x00000008
    subprocess.Popen(["cmd.exe", "/c", bat_path],
                     creationflags=DETACHED_PROCESS, close_fds=True)
    # кратко изчакване, за да стигне отговорът до браузъра, после изход
    threading.Timer(1.5, lambda: os._exit(0)).start()
