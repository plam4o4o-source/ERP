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
import subprocess  # nosec B404 -- ползван само за стартиране на генериран локално .bat файл (виж nosec бележката при Popen по-долу), без shell=True
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


# Одит (находка В6, висок риск): преди тази поправка start_auto_update_loop
# извикваше install_update() веднага щом откриеше нова версия — а тя
# приключва с `os._exit(0)` (виж по-долу) БЕЗ никакво предупреждение към
# работещ в момента потребител. Ако някой в момента попълва дълга форма
# (напр. масово издаване на палетни карти), програмата просто изчезва
# изпод него — незапазеното въведено пада безвъзвратно.
#
# _pending_restart пази „предстои рестарт след…“ състояние, което таблото
# (виж routes_dashboard.dashboard/JS полинг по-долу) показва като видимо,
# но НЕ блокиращо предупреждение — потребителят вижда точно колко има
# до рестарта и може да довърши/запази текущата си работа междувременно.
_pending_restart_lock = threading.Lock()
_pending_restart = {"scheduled_at": None, "version": None}

#: Колко секунди предупреждението стои видимо, преди install_update() да
#: изпълни реалната подмяна+рестарт. Достатъчно за кратка форма, не
#: толкова дълго, че обновяването на практика никога да не се случи в
#: офис с почти непрекъснато поне един отворен таб.
AUTO_RESTART_WARNING_SECONDS = 90


def get_pending_restart():
    """Текущото състояние на предстоящ автоматичен рестарт (или None), за
    показване на предупреждение в интерфейса — вижте routes_dashboard.py."""
    with _pending_restart_lock:
        info = dict(_pending_restart)
    if info["scheduled_at"] is None:
        return None
    seconds_left = max(0, int(info["scheduled_at"] - time.time()))
    return {"version": info["version"], "seconds_left": seconds_left}


def _clear_pending_restart():
    """Изчиства „предстои рестарт“ състоянието — вижте _schedule_auto_install
    по-долу.

    Заявка: „автоматично обновяване след старт на програмата неработи“.
    Причина: ако install_update() се провали СЛЕД като вече е показал на
    всички отворени в момента таблото потребители банера „Ще се
    рестартира след X сек“ (напр. повреден/непълен файл при изтеглянето,
    несъвпадаща SHA-256 контролна сума, антивирус карантинира новото
    .exe, недостатъчно място на диска — виж всички проверки в
    install_update по-горе), самото рестартиране НИКОГА не се случва, но
    `_pending_restart["scheduled_at"]` оставаше завинаги закован в
    МИНАЛОТО — get_pending_restart() clamp-ва seconds_left до 0 (max(0, …)),
    така банерът в интерфейса оставаше показан с „…след 0 сек“ до края на
    сесията (следващата проверка е чак след `interval`, виж
    start_auto_update_loop), а самото обновяване никога реално не
    завършва. За потребителя изглежда сякаш „автоматичното обновяване не
    работи“ — точно обратното на предупреждение: лъжлив банер за
    предстоящ рестарт, който никога не идва."""
    with _pending_restart_lock:
        _pending_restart["scheduled_at"] = None
        _pending_restart["version"] = None


def _schedule_auto_install(download_url, expected_sha256, version, warning_seconds=None):
    """Обвивка около install_update() за автоматичния (не ръчния през
    бутона) път — вижте находка В6 по-горе. Отбелязва „предстои рестарт“
    веднага, изчаква `warning_seconds`, за да имат отворените в момента
    потребители видимо предупреждение и шанс да запазят работата си, чак
    тогава извиква истинското install_update() (което рестартира
    процеса).

    `warning_seconds=None` (подразбиране) чете AUTO_RESTART_WARNING_SECONDS
    ДИНАМИЧНО от модула при ИЗВИКВАНЕ, не като Python default-параметър
    (който би се обвързал ЕДНОКРАТНО при дефиниране на функцията — тестове,
    monkeypatch-ващи updater.AUTO_RESTART_WARNING_SECONDS, иначе тихо не
    биха имали ефект).

    При успех install_update() никога не се връща тук — приключва с
    os._exit(0). При НЕуспех (виж _clear_pending_restart за пълното
    обяснение защо) изчистваме показания вече банер и подаваме
    изключението нагоре, за да го хване и логне start_auto_update_loop
    (и да пробва отново по-скоро — виж _FAIL_RETRY_SECONDS там)."""
    if warning_seconds is None:
        warning_seconds = AUTO_RESTART_WARNING_SECONDS
    with _pending_restart_lock:
        _pending_restart["scheduled_at"] = time.time() + warning_seconds
        _pending_restart["version"] = version
    time.sleep(warning_seconds)
    try:
        install_update(download_url, expected_sha256)
    except Exception:
        _clear_pending_restart()
        raise


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


#: Одит (находка С7, среден риск): true, докато вече тече фонова
#: опресняваща заявка (виж _refresh_cache_in_background по-долу) —
#: пази да не се пуснат няколко успоредни GitHub заявки, ако много
#: заявки на таблото видят остарял кеш почти едновременно.
_refresh_in_progress = False


def _refresh_cache_in_background():
    """Извиква се САМО в отделна фонова нишка (виж check_cached) — прави
    РЕАЛНАТА мрежова заявка (check_for_update, до 8 сек. по подразбиране)
    ИЗВЪН пътя на който и да е HTTP request, затова забавяне/недостъпен
    GitHub тук никога не бави зареждането на таблото за потребителя."""
    global _refresh_in_progress
    try:
        info = check_for_update()
        with _cache_lock:
            _cache["info"] = info
            _cache["last_error"] = None
            _cache["time"] = time.time()
    except Exception as exc:
        applog.log_exception("updater._refresh_cache_in_background: неуспешна проверка за обновление")
        with _cache_lock:
            _cache["info"] = None
            _cache["last_error"] = describe_error(exc)
            _cache["time"] = time.time()
    finally:
        with _cache_lock:
            _refresh_in_progress = False


def check_cached(max_age=3600):
    """Кеширана проверка (веднъж на час при успех; при неуспех пробва пак
    много по-скоро, вместо да „заключи“ грешка за цял час).

    Одит (находка С7, среден риск): преди поправката САМАТА мрежова
    заявка (check_for_update, до 8 сек. по подразбиране) течеше ТУК,
    СИНХРОННО в заявката за /  (routes_dashboard.dashboard) — недостъпен
    GitHub блокираше зареждането на таблото за ЦЕЛИТЕ 8 секунди, при
    ВСЯКО зареждане, докато неуспехът не се кешира (само 120 сек. — виж
    _FAIL_RETRY_SECONDS, значи проблемът се повтаря на всеки ~2 минути).
    Проверката за нова версия няма никакво място в синхронния път на
    заявката, обслужваща реалната работа на служителите.

    Сега функцията ВИНАГИ връща веднага текущото съдържание на кеша
    (дори остаряло или None, ако още няма нито една успешна проверка) —
    ако е остаряло, само СТАРТИРА фонова нишка да го опресни (вижте
    _refresh_cache_in_background), без да чака резултата ѝ. Следващото
    зареждане на таблото ще види вече опреснения кеш. _refresh_in_progress
    пази да не тръгнат няколко успоредни фонови опреснявания, ако много
    заявки видят остарял кеш почти едновременно (огледално на старата
    защита с _cache_lock, само че вече не блокира заявката, само
    броячите/флага)."""
    global _refresh_in_progress
    with _cache_lock:
        now = time.time()
        age_limit = max_age if _cache["info"] is not None else _FAIL_RETRY_SECONDS
        stale = now - _cache["time"] > age_limit
        result = _cache["info"]
        should_refresh = stale and not _refresh_in_progress
        if should_refresh:
            _refresh_in_progress = True
    if should_refresh:
        threading.Thread(target=_refresh_cache_in_background, daemon=True).start()
    return result


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
    пробва отново на следващата итерация — никога не гърми програмата.

    Заявка: „автоматично обновяване след старт на програмата неработи“.
    Причина (втора половина на поправката — виж и _clear_pending_restart):
    при ГРЕШКА (изключение — временно няма връзка, GitHub недостъпен,
    повреден/непълен изтеглен файл, несъвпадаща контролна сума и т.н.)
    цикълът заспиваше за СЪЩОТО `interval` (2 часа по подразбиране), както
    и при нормален успешен резултат „няма нова версия“ — вместо да пробва
    пак скоро, точно както вече прави check_cached()/_FAIL_RETRY_SECONDS
    за таблото. Първата (и често единствена) проверка е само `first_delay`
    (20 сек.) след старта на програмата — ако тя се провали заради нещо
    ВРЕМЕННО (Wi-Fi още се свързва, DNS все още не отговаря, антивирус
    сканира прясно разопакованите файлове), програмата практически никога
    не проверява пак до края на тази сесия (типична работна сесия е по-
    кратка от 2 часа) — точно поведението, докладвано като „не работи“."""
    if not is_frozen_windows():
        return

    def _loop():
        time.sleep(first_delay)
        while True:
            wait = interval
            try:
                if not is_server_func():
                    info = check_for_update()
                    if info["available"]:
                        # В6: _schedule_auto_install показва предупреждение
                        # AUTO_RESTART_WARNING_SECONDS преди истинския
                        # рестарт, вместо да гърми веднага.
                        _schedule_auto_install(info["download"], info.get("expected_sha256"),
                                               info.get("latest", "?"))
                        return  # install_update рестартира процеса (os._exit) при успех
            except Exception:
                applog.log_exception("updater.start_auto_update_loop: грешка при проверка/инсталация на обновяване")
                # По-скоро повторен опит при грешка, не пълния `interval`
                # (min(), за да не УДЪЛЖИМ изчакването, ако някой тест/
                # конфигурация подаде interval < _FAIL_RETRY_SECONDS).
                wait = min(interval, _FAIL_RETRY_SECONDS)
            time.sleep(wait)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()


def _env_without_pyinstaller_vars(environ=None):
    """Среда за стартиране на НОВО копие на програмата — без служебните
    променливи на PyInstaller onefile bootloader-а.

    PyInstaller onefile работи с ДВА процеса: родител (bootloader), който
    разопакова всичко във временна папка Temp\\_MEIxxxxxx, и дете (нашият
    Python код). Родителят казва на детето къде е разопакован чрез
    променливи на средата: _MEIPASS2 при старите версии, а при PyInstaller
    6.x (billd-ваме с 6.21.0, виж release.yml) — _PYI_APPLICATION_HOME_DIR,
    _PYI_ARCHIVE_FILE и _PYI_PARENT_PROCESS_LEVEL.

    Ако новото .exe наследи тези променливи (Popen → cmd.exe → start →
    новото .exe — всяко звено наследява средата на предишното), неговият
    bootloader решава, че САМИЯТ ТОЙ е вече разопакованото дете, пропуска
    собственото си разопаковане и зарежда python312.dll директно от
    _MEIxxxxxx папката на СТАРИЯ процес — а тя вече е изтрита при неговия
    изход. Резултатът е диалогът "Failed to load Python DLL
    ...\\_MEIxxxxxx\\python312.dll — The specified module could not be
    found" при ВСЯКО автоматично обновяване, макар самата подмяна на .exe
    файла да е минала успешно (затова ръчното стартиране след това работи
    нормално — Explorer дава чиста среда).

    Премахваме _MEIPASS2 и всичко, започващо с _PYI_ (покрива и бъдещи
    служебни променливи на bootloader-а), и оставяме всичко останало
    (SYSTEMROOT, PATH и т.н. са нужни на cmd.exe/новия процес). Това е и
    официално документираната препоръка на PyInstaller при стартиране на
    друго PyInstaller приложение от PyInstaller приложение."""
    if environ is None:
        environ = os.environ
    return {k: v for k, v in environ.items()
            if k != "_MEIPASS2" and not k.startswith("_PYI_")}


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
    #
    # ВНИМАНИЕ: СЪЩОТО съобщение за грешка има и ВТОРА, независима причина
    # — новото .exe, стартирано от bat-а, наследява служебните променливи
    # на PyInstaller bootloader-а от стария процес и се опитва да зареди
    # python312.dll от вече изтритата _MEIxxxxxx папка на СТАРИЯ процес.
    # При нея .exe файлът е напълно здрав (ръчното стартиране работи),
    # диалогът излиза само при автоматичния рестарт. Лекува се при
    # стартирането на bat-а (env=_env_without_pyinstaller_vars() при
    # Popen по-долу), НЕ с проверките тук — двете защити пазят срещу
    # различни неща и трябват и двете.
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
    # "cmd.exe" е с фиксиран, известен системен път (Windows винаги го
    # намира през PATH), bat_path е файл, генериран малко по-горе от самата
    # тази функция (не потребителски вход).
    #
    # env=: ЗАДЪЛЖИТЕЛНО почистена от служебните променливи на PyInstaller
    # bootloader-а — иначе новото .exe, стартирано от bat-а, ги наследява и
    # гърми с "Failed to load Python DLL ...\_MEIxxxxxx\python312.dll" при
    # всяко автоматично обновяване (виж _env_without_pyinstaller_vars за
    # пълното обяснение на веригата).
    subprocess.Popen(["cmd.exe", "/c", bat_path],  # nosec
                     creationflags=DETACHED_PROCESS, close_fds=True,
                     env=_env_without_pyinstaller_vars())
    # кратко изчакване, за да стигне отговорът до браузъра, после изход
    threading.Timer(1.5, lambda: os._exit(0)).start()
