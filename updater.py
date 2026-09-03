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
import remote_tunnel
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

# Одит (16.08.2026, находка №10): install_update() нямаше никакво взаимно
# изключване — ръчният бутон „Обнови сега“ (routes_admin.update_install)
# и автоматичният фонов цикъл (_schedule_auto_install) можеха да се
# застъпят (напр. админ натиска бутона точно по време на 90-секундния
# банер за автоматично обновяване), и двата пишеха/четяха ЕДИН И СЪЩ
# `<exe>.new` файл конкурентно — възможни резултати, потвърдени по кода:
# единият процес изтрива вече проверения файл на другия точно преди
# move-а (bat-ът тихо стартира СТАРАТА версия), или os._exit() убива
# процеса по средата на чуждото изтегляне (bat премества частичен файл,
# чиято SHA-256 проверка никога не се е изпълнила — повреден .exe).
_install_lock = threading.Lock()

#: Колко секунди предупреждението стои видимо, преди install_update() да
#: изпълни реалната подмяна+рестарт.
#:
#: Заявка (26.08.2026): „направи при всяко стартиране на програмата да
#: проверява за нова версия и ВЕДНАГА да се инсталира“ — след изричен избор
#: между запазване на предупреждението (по-безопасно за споделена/офис
#: инсталация) и премахването му изцяло (по-бързо за самостоятелна
#: инсталация), потребителят предпочете второто. Стойността е 0 (не
#: премахнат механизъм) — банерът/`_pending_restart` продължават да
#: съществуват (виж находка В6 по-горе за защо изобщо са въведени) и биха
#: се показали, ако някой отново вдигне тази константа, но по подразбиране
#: install_update() тръгва веднага след успешното изтегляне+проверка,
#: без пауза. Рискът, който находка В6 описва (изчезнала незапазена работа
#: в отворена форма), остава — приет съзнателно от потребителя за тази
#: инсталация.
AUTO_RESTART_WARNING_SECONDS = 0


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
    веднага, изчаква `warning_seconds`, чак тогава извиква истинското
    install_update() (което рестартира процеса).

    Заявка (26.08.2026): по подразбиране `warning_seconds` вече е 0
    (`AUTO_RESTART_WARNING_SECONDS` — виж обяснението там) — потребителят
    избра „веднага да се инсталира“ без изчакване пред останалите отворени
    прозорци. `_pending_restart` продължава да се отбелязва (нулево
    изчакване не е специален случай в кода) — ако някой вдигне константата
    обратно, банерът веднага проработва отново без допълнителна промяна тук.

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
        # Одит (31.08.2026, находка №6): версията пътува надолу, за да може
        # install_update да откаже повторен опит за версия, чиято подмяна
        # вече се е провалила (маркерът преживява рестарта).
        install_update(download_url, expected_sha256, version=version)
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
    очакваната контролна сума за EXE_NAME, или None ако релийзът НЯМА
    такъв файл.

    Одит (02.09.2026, десети одит, находка №8): всяка грешка при
    изтеглянето се поглъщаше и връщаше СЪЩОТО None като „този релийз няма
    манифест“. А install_update проверява `elif expected_sha256:` — тоест
    None значи „пропусни проверката“. Резултат: трепкаща връзка, 403 при
    rate limit (самият модул описва това при describe_error) или прокси
    засечка и ~20 MB .exe се инсталира само срещу размер + „MZ“ — по
    собственото признание на коментара при CHECKSUMS_ASSET_NAME това е
    „недостатъчно“. Потребителят не научаваше нищо. Отгоре на всичко тази
    заявка е с най-краткия таймаут (8 сек., подаден от check_for_update),
    тоест е и най-вероятната да се провали.

    Затова провалът вече ИЗБИВА нагоре: check_for_update се проваля,
    кешът отбелязва грешката и след `_FAIL_RETRY_SECONDS` се пробва пак.
    Обновяването се ОТЛАГА, вместо да се инсталира непроверено — а стар
    релийз без манифест минава както преди (там изобщо не се стига до
    мрежова заявка)."""
    for asset in assets:
        if asset.get("name") == CHECKSUMS_ASSET_NAME:
            url = asset.get("browser_download_url")
            if not url:
                return None
            try:
                req = urllib.request.Request(url, headers=_UA)
                with net.urlopen(req, timeout=timeout) as resp:
                    text = resp.read().decode("utf-8", errors="replace")
            except Exception as exc:
                applog.log_exception("updater._fetch_expected_checksum: неуспешно изтегляне на SHA256SUMS.txt")
                raise RuntimeError(
                    "Контролната сума на релийза (%s) не можа да бъде "
                    "изтеглена: %s. Обновяването се отлага — новият файл не "
                    "се инсталира непроверен." % (CHECKSUMS_ASSET_NAME, exc))
            return parse_sha256sums(text, EXE_NAME)
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


def start_auto_update_loop(is_server_func, first_delay=2, interval=7200):
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
    секунди след старта на програмата — ако тя се провали заради нещо
    ВРЕМЕННО (Wi-Fi още се свързва, DNS все още не отговаря, антивирус
    сканира прясно разопакованите файлове), програмата практически никога
    не проверява пак до края на тази сесия (типична работна сесия е по-
    кратка от 2 часа) — точно поведението, докладвано като „не работи“.

    Заявка (22.08.2026): „направи веднага като се стартира да проверява за
    нова версия автоматично, и да се инсталира“ — `first_delay` намален от
    20 на 2 сек. (не 0 — кратка пауза, за да се разминат мрежовите/
    файловите операции от самия старт на процеса с първата HTTPS заявка
    към GitHub, вместо да се състезават за същите ресурси в първите
    милисекунди). Проверката и инсталирането вече бяха напълно автоматични
    (виж находка В6/2-ри кръг по-горе) — единствената промяна тук е КОЛКО
    БЪРЗО се случва първата проверка след старта."""
    if not is_frozen_windows():
        return

    # Одит (02.09.2026, десети одит, находка №10): smoke тестът в release.yml
    # пуска ПРЯСНО компилираното .exe на windows-latest — тоест
    # is_frozen_windows() е True и цикълът тръгва НАИСТИНА: две секунди
    # по-късно пита api.github.com и при `available` сваля и подменя
    # `dist\\PachoLogistic.exe`, точно файла, който следващите стъпки
    # („Generate SHA256SUMS.txt“ и „Publish release“) хешират и публикуват.
    # `kill $APP_PID` не помага — подмяната я прави ОТДЕЛЕН, откачен cmd.exe
    # (DETACHED_PROCESS), който преживява убиването на процеса. Условието е
    # `parse_version(latest) > parse_version(__version__)`, тоест сработва при
    # всяка версия, която сортира ПОД последния релийз (напр. „3.7.0“ след
    # „3.69.2“ — (3,7,0) < (3,69,2)) — и тогава под новия етикет се публикува
    # бинарният файл на ПРЕДИШНИЯ релийз. Отделно всеки билд харчи излишна
    # заявка към GitHub и зависи от rate limit-а му.
    if os.environ.get("PACHO_DISABLE_AUTO_UPDATE"):
        applog.log_warning("updater.start_auto_update_loop",
                           "автоматичното обновяване е изключено през "
                           "PACHO_DISABLE_AUTO_UPDATE")
        return

    # Одит (16.08.2026, находка №13): версии, чиято ИНСТАЛАЦИЯ (не просто
    # проверка) вече се е провалила в тази сесия на програмата — виж по-
    # долу защо не бива да се пробват отново на всеки _FAIL_RETRY_SECONDS.
    _failed_install_versions = set()

    def _loop():
        time.sleep(first_delay)
        while True:
            wait = interval
            try:
                if not is_server_func():
                    info = check_for_update()
                    latest = info.get("latest", "?")
                    if info["available"] and latest in _failed_install_versions:
                        # Одит (находка №13): тази версия вече се е
                        # провалила при ИНСТАЛАЦИЯ в тази сесия (не при
                        # проверка) — пълен диск/антивирус карантина/
                        # повреден release asset обикновено са ТРАЙНИ
                        # условия, не временни. Преди тази поправка
                        # цикълът показваше банера и теглеше отново ~20MB
                        # на всеки _FAIL_RETRY_SECONDS (120 сек) БЕЗКРАЙНО
                        # — стотици MB трафик/ден + мигащ лъжлив банер за
                        # рестарт. Изчакваме пълния `interval`, преди да
                        # пробваме тази версия пак.
                        applog.log_warning(
                            "updater.start_auto_update_loop",
                            "версия %s вече се провали при инсталация в тази сесия — "
                            "пропускам повторен опит до следващата пълна проверка" % latest)
                    elif info["available"]:
                        # В6: _schedule_auto_install показва предупреждение
                        # AUTO_RESTART_WARNING_SECONDS преди истинския
                        # рестарт, вместо да гърми веднага.
                        try:
                            _schedule_auto_install(info["download"], info.get("expected_sha256"), latest)
                        except Exception:
                            # Одит (находка №13): разграничение от ГРЕШКА
                            # ПРИ ПРОВЕРКА (except по-долу) — тук е провалена
                            # ИНСТАЛАЦИЯ (изтеглен файл/checksum/диск), не
                            # временна мрежова липса. Пълният `interval` за
                            # тази версия (не бързият _FAIL_RETRY_SECONDS
                            # retry), плюс запомняне, за да не се повтори
                            # изобщо до следващия път.
                            applog.log_exception(
                                "updater.start_auto_update_loop: неуспешна инсталация на версия %s" % latest)
                            _failed_install_versions.add(latest)
                            time.sleep(wait)
                            continue
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


#: Одит (31.08.2026, находка №6, ВИСОКА): файл-маркер до .exe-то, в който
#: САМИЯТ скрипт за рестарт записва версията, чиято подмяна се е провалила.
#:
#: Защо трябва да е файл, а не променлива в паметта: провалът настъпва СЛЕД
#: като процесът вече е излязъл (`os._exit`) — скриптът опитва `move` 20
#: пъти, не успява и стартира СТАРОТО .exe. Тоест `_failed_install_versions`
#: (локално множество вътре в start_auto_update_loop) не се попълва изобщо и
#: така или иначе умира с процеса. Новият процес пуска проверката 2 секунди
#: след старта, вижда същия по-нов релийз, сваля пак ~20 MB, пише скрипта,
#: излиза — и така в кръг: програмата се затваря и отваря на всеки ~40
#: секунди безкрайно, със стотици MB трафик на ден. Свалянето УСПЯВА, значи
#: нито една от съществуващите защити не се задейства.
#:
#: Трайни причини `move` да се проваля: .exe-то стои в споделена папка и се
#: пуска оттам от няколко компютъра (обичайната мрежова инсталация — виж
#: db.py) → образът е заключен от другите машини; файлът е само за четене;
#: антивирус/Controlled Folder Access го държи.
FAILED_INSTALL_MARKER = "pacho_update_failed.txt"


def _machine_suffix():
    """Кратък ASCII отпечатък на ТАЗИ машина — за имената на временните
    файлове при обновяване.

    Одит (02.09.2026, десети одит, находка №6, ВИСОКА): цялото временно
    състояние на обновяването (`<exe>.new`, `pacho_update.bat`, маркерът за
    провал) се пишеше до самото .exe — тоест в СПОДЕЛЕНАТА мрежова папка,
    която е документираният начин на работа (виж db.py). Единственото
    взаимно изключване беше `_install_lock`, а той е нишков, В РАМКИТЕ НА
    ЕДИН ПРОЦЕС. Реалната последица при две работни станции, пуснати сутрин
    в рамките на минута:
      1. Машина A сваля `\\\\сървър\\споделено\\PachoLogistic.exe.new`,
         затваря файла, проверява размер + MZ + SHA-256, записва bat-а и
         насрочва изход след 1.5 сек.
      2. Машина B стига до `os.remove(new_exe)` и ТРИЕ проверения файл на A,
         после започва СВОЕТО сваляне на ~20 MB.
      3. cmd.exe на A се събужда след 1-2 сек. и прави
         `move` върху ПОЛОВИН свалeния файл на B — който никой не е проверил.
      4. Всяка станция в офиса стартира отрязано .exe → „Failed to load
         Python DLL … python312.dll“, без път за възстановяване отвътре.
    Същият прозорец обезсмисляше и проверката на контролната сума: между
    `sha256_of_file` и `move` файлът е обикновен, незаключен файл в папка,
    в която пишат и други машини (находка №7).

    Уникалното име по машина затваря и двете: никоя машина вече не пипа
    междинния файл на друга, а `move` мести точно това, което сме проверили.
    Хешът пази името чисто ASCII (името на компютъра под Windows може да е
    на кирилица) и къс (пътищата тук са и без това дълги)."""
    name = ""
    try:
        import platform
        name = platform.node() or ""
    except Exception:  # nosec B110 -- при липсващо име на машината се пада към променливите на средата
        pass
    if not name:
        name = os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "local"
    return hashlib.sha256(name.encode("utf-8", "replace")).hexdigest()[:8]


def _failed_marker_name():
    return "pacho_update_failed_%s.txt" % _machine_suffix()


def _failed_marker_path():
    return os.path.join(os.path.dirname(sys.executable), _failed_marker_name())


def read_failed_install_version():
    """Версията, чиято подмяна се е провалила при предишен опит (или None).

    Чете се при ВСЕКИ опит за инсталация — включително след рестарт, което е
    целият смисъл: провалът се случва в скрипта, след изхода на процеса."""
    try:
        with open(_failed_marker_path(), "r", encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def clear_failed_install_marker():
    """Маркерът се маха при успешна инсталация (скриптът го трие сам) и при
    ръчен опит от бутона — админът съзнателно казва „пробвай пак“, напр.
    след като е затворил програмата на другите компютри."""
    try:
        os.remove(_failed_marker_path())
    except OSError:
        pass


def install_update(download_url, expected_sha256=None, version=None,
                   ignore_failed_marker=False):
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
    # Одит (31.08.2026, находка №6): ако предишният опит за ТОЧНО ТАЗИ
    # версия се е провалил при подмяната, не сваляме отново — иначе се
    # получава безкраен цикъл рестарт↔сваляне (виж FAILED_INSTALL_MARKER).
    # Ръчният бутон подава ignore_failed_marker=True: там админът съзнателно
    # казва „пробвай пак“, обикновено след като е отстранил причината.
    if version and not ignore_failed_marker and read_failed_install_version() == version:
        raise RuntimeError(
            "Обновяването до версия %s вече беше опитано и подмяната на "
            "програмния файл не успя (файлът е зает или защитен от запис). "
            "Затворете програмата на другите компютри, които я стартират от "
            "същата папка, проверете антивирусната програма и опитайте пак "
            "от бутона „Обнови сега“." % version
        )
    # Одит (16.08.2026, находка №10): неблокиращо заключване — вместо да
    # изчака (и потенциално да се преплете с вече текущата инсталация),
    # веднага отказва с ясна грешка. Освобождава се автоматично при изход
    # от функцията по всякакъв път (return/raise/os._exit не се стига,
    # докато не сме вече отвъд with блока — виж по-долу).
    if not _install_lock.acquire(blocking=False):
        raise RuntimeError(
            "Обновяване вече тече в момента (стартирано от друг опит — "
            "ръчен или автоматичен). Изчакайте да приключи, преди да пробвате пак."
        )
    try:
        _install_update_locked(download_url, expected_sha256, version)
    finally:
        _install_lock.release()


def _install_update_locked(download_url, expected_sha256=None, version=None):
    """Реалното тяло на install_update() — изпълнява се само докато
    _install_lock е държан от install_update() по-горе (виж находка №10)."""
    exe = sys.executable
    # Одит (02.09.2026, находка №6): името е УНИКАЛНО ЗА МАШИНАТА —
    # вижте _machine_suffix() за пълния разказ защо споделеното
    # „<exe>.new“ е опасно в мрежовата инсталация.
    new_exe = exe + "." + _machine_suffix() + ".new"
    # Одит (12.08.2026, находка №37, дребна): ако предишен опит за
    # обновяване е стигнал до bat-а, но самата замяна (`move`) се е
    # провалила след 20 опита (виж bat_content по-долу — старият процес е
    # държал файла заключен по-дълго от очакваното), pacho_update.bat
    # логва "FAILED", но НЕ трие new_exe — той оставаше на диска до
    # следващия РЪЧНО стартиран цикъл на обновяване, който просто щеше да
    # го презапише при следващото изтегляне (не истинско изтичане при
    # УСПЕШЕН следващ опит), но между двата остава излишен файл, а ако
    # следваща проверка реши „вече е най-новата версия“ (няма нова версия
    # за изтегляне), той увисва завинаги без изобщо да се стигне до нов
    # опит за запис тук. Изчистваме евентуален остатък ПРЕДИ да започнем
    # ново изтегляне, за да не се трупат стари ".new" файлове.
    if os.path.exists(new_exe):
        try:
            os.remove(new_exe)
        except OSError:
            applog.log_exception("updater.install_update: неуспешно изчистване на "
                                 "стар остатъчен %s" % new_exe)
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
    # Одит (02.09.2026, находка №6): и скриптът е уникален за машината —
    # cmd.exe го държи отворен, докато го изпълнява, и накрая го трие сам
    # (`del "%~f0"`), тоест споделеното име позволяваше една машина да
    # изтрие скрипта, който cmd.exe на друга машина още изпълнява.
    bat_path = os.path.join(os.path.dirname(exe),
                            "pacho_update_%s.bat" % _machine_suffix())
    # Одит (29.08.2026, находка №1, ВИСОКА): пътищата вече НЕ се вграждат в
    # текста на .bat файла — подават се като АРГУМЕНТИ (%1 = новото .exe,
    # %2 = текущото), а логът се извежда от собствената папка на скрипта
    # (%~dp0). Така съдържанието на файла е чисто ASCII, независимо къде е
    # инсталирана програмата.
    #
    # Защо: досега файлът се записваше с `encoding="ascii"`, докато вътре в
    # него стоеше `sys.executable`. Инсталаторът слага програмата по
    # подразбиране в %localappdata%\Programs\PachoLogistic, тоест реалният
    # път е C:\Users\<потребител>\... — а за българските потребители
    # потребителското име в Windows е КИРИЛСКО. Резултат: `f.write(...)`
    # гърмеше с `UnicodeEncodeError` СЛЕД успешно изтегляне и проверка на
    # ~20 MB, точно преди рестарта; изключението се логваше, версията
    # влизаше в `_failed_install_versions` и обновяването не се пробваше
    # повече — тихо, без нито едно съобщение към потребителя. Тоест
    # автоматичното обновяване беше МЪРТВО за мнозинството реални
    # инсталации. Възпроизведено с изпълнение:
    #     open(..., "w", encoding="ascii").write(bat с "C:\\Users\\Пламен\\...")
    #     → UnicodeEncodeError: 'ascii' codec can't encode characters …
    # Съществуващият тест не го хващаше, защото ползва ASCII `tmp_path`.
    #
    # Защо АРГУМЕНТИ, а не просто друго кодиране: cmd.exe чете .bat файла в
    # кодовата страница на конзолата (OEM, cp866 на българска система), а не
    # в кодировката, с която Python го е записал — затова само смяната на
    # `encoding` (напр. на "mbcs"/"utf-8") спира срива, но оставя реален риск
    # `move`/`start` да разчетат кирилския път погрешно. Аргументите на
    # процеса пътуват като Unicode (CreateProcessW), значи проблемът с
    # кодировките отпада ИЗЦЯЛО, вместо да се премести един слой по-нататък.
    # Записът все пак е с "utf-8" (не "ascii") като втора защита, ако някой
    # ден в скрипта попадне не-ASCII литерал.
    #
    # `%~1`/`%~2` махат кавичките, които subprocess слага около път с
    # интервали (напр. "Program Files"), и ги връщаме сами — стандартният
    # идиом; логиката на самия цикъл за подмяна остава непроменена.
    # Одит (31.08.2026, находка №6): скриптът вече оставя МАШИННОЧЕТИМ
    # маркер при провал (%~3 = версията) и го ТРИЕ при успех. Само така
    # следващият процес научава, че подмяната не е минала — иначе се въртеше
    # безкрайно: сваляне → рестарт → същият релийз → сваляне…
    # (`pacho_update.log` остава за човешка диагностика, но не се чете от
    # кода.) Версията се подава като аргумент, за да остане .bat-ът чисто
    # ASCII — виж находка №1 от седмия одит.
    # Одит (02.09.2026, десети одит, находка №5, ВИСОКА): маркерът се
    # записваше с `echo %~3> "…"`. cmd.exe разширява %~3 в първата фаза и
    # ЧАК ПОСЛЕ разбира пренасочването — а една цифра, залепена вляво до
    # „>“, се тълкува като НОМЕР НА ДЕСКРИПТОР. Версията винаги завършва на
    # цифра („3.69.2“), затова редът се изпълняваше като `echo 3.69.` с
    # `2> "…"` (пренасочване на stderr): маркерът се СЪЗДАВАШЕ, но оставаше
    # ПРАЗЕН. `read_failed_install_version` прави `.read().strip() or None`
    # → None → пазачът срещу безкрайното обновяване (находка №6 от 31.08)
    # НИКОГА не се задействаше. Реалната последица в споделената папка:
    # `move` не успява (други машини държат .exe-то отворено), bat-ът
    # въпреки това стартира СТАРОТО .exe, то вижда същия релийз, сваля пак
    # ~20 MB, рестартира се — цикъл на всеки 30-40 секунди, безкрайно.
    # Поправката е интервалът пред „>“: тогава няма цифра, залепена до
    # знака, и няма как да бъде прочетена като дескриптор (`strip()` в
    # четеца и без това маха и интервала, и новия ред).
    #
    # Одит (02.09.2026, находка №9): 20 опита × `ping -n 2` ≈ 20 секунди, а
    # изходът на стария процес може да отнеме до ~16.5 сек. (`Timer(1.5)` +
    # `remote_tunnel.stop()` чака 10 сек. за `wait` и още 5 след `kill`) —
    # запас под 4 секунди, който `move` през SMB плюс антивирусна проверка
    # на прясно 20-мегабайтово .exe изяжда. Бюджетът става 60 опита ≈ 60
    # секунди: цената при истински провал е една минута чакане веднъж,
    # печалбата е че не влизаме в цикъла по-горе заради секунда закъснение.
    # Одит (03.09.2026, находка №21): успехът се решава от РЕЗУЛТАТА на
    # `move`, не от това дали новото .exe още стои на диска.
    #
    # Дотук скриптът питаше само `if exist "%~1"`. „Файлът го няма“ обаче
    # има ДВЕ причини, а тестът различаваше само едната: (а) `move` е
    # успял — истински успех; (б) файлът е изчезнал, БЕЗ `move` изобщо да е
    # минал (антивирус/Controlled Folder Access карантинира прясно свалено
    # неподписано .exe — самият код изброява това като очаквана причина; или
    # втори опит за инсталация го е изтрил в прозореца преди рестарта).
    # В случай (б) се изпълняваше else-клонът: записваше се „OK: updated
    # successfully“, ТРИЕШЕ СЕ маркерът за провалена инсталация и се
    # стартираше СТАРОТО .exe. Оттам старият процес пита GitHub 2 секунди
    # по-късно, маркерът вече го няма, сваля пак ~20 MB и излиза — точно
    # безкрайният цикъл „рестарт↔сваляне“, срещу който са писани находка №6
    # от 31.08 (файловият маркер) и находка №5 от 02.09. И двете поправки
    # стоят и са коректни; този else-клон ги обезсилваше.
    #
    # `MOVED` се вдига САМО когато `move` върне успех (`&&`), значи и двете
    # причини за липсващ файл вече се различават. Липсващ файл ПРЕДИ първия
    # опит води до собствен клон (`:missing`), който също пише маркера.
    marker = _failed_marker_name()
    bat_content = (
        "@echo off\r\n"
        "set TRIES=0\r\n"
        "set MOVED=\r\n"
        ":retry\r\n"
        'if not exist "%~1" goto missing\r\n'
        "ping -n 2 127.0.0.1 >nul\r\n"
        'move /y "%~1" "%~2" >nul 2>&1 && set MOVED=1\r\n'
        "set /a TRIES+=1\r\n"
        "if defined MOVED goto done\r\n"
        'if exist "%~1" if %TRIES% LSS 60 goto retry\r\n'
        "goto done\r\n"
        ":missing\r\n"
        'echo FAILED: new exe disappeared before it could be moved'
        '> "%~dp0pacho_update.log"\r\n'
        'echo %~3 > "%~dp0' + marker + '"\r\n'
        'goto launch\r\n'
        ":done\r\n"
        'if defined MOVED (echo OK: updated successfully'
        '> "%~dp0pacho_update.log" & del "%~dp0' + marker + '" 2>nul'
        ') else (echo FAILED: could not replace exe after 60 tries'
        '> "%~dp0pacho_update.log" & echo %~3 > "%~dp0' + marker + '"'
        ")\r\n"
        ":launch\r\n"
        'start "" "%~2"\r\n'
        'del "%~f0"\r\n'
    )
    with open(bat_path, "w", encoding="utf-8") as f:
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
    # Одит (29.08.2026, находка №1): новото и текущото .exe пътуват като
    # АРГУМЕНТИ (%1/%2 в скрипта по-горе) — Windows ги подава като Unicode
    # (CreateProcessW), затова кирилски път минава непокътнат.
    # %~3 = версията, която се инсталира (находка №6) — скриптът я записва в
    # маркера при провал, за да не се пробва пак безкрайно след рестарта.
    subprocess.Popen(["cmd.exe", "/c", bat_path, new_exe, exe, version or "?"],  # nosec
                     creationflags=DETACHED_PROCESS, close_fds=True,
                     env=_env_without_pyinstaller_vars())

    def _exit_and_stop_tunnel():
        # Одит (16.08.2026, находка №1): os._exit(0) НЕ изпълнява atexit
        # хендлъри (app.py регистрира remote_tunnel.stop() там, но само за
        # НОРМАЛЕН изход) — рестартът при обновяване (и ръчен, и
        # автоматичен) е отделен изходен път, който досега оставяше
        # отдалечения тунел (cloudflared) „сирак“, точно както
        # поправената критична находка №2 от 12.08 за затварянето на
        # прозореца. Best-effort, безусловно — remote_tunnel.stop()
        # поглъща собствените си грешки и е безопасно да се вика, дори
        # тунелът никога да не е бил стартиран.
        remote_tunnel.stop()
        os._exit(0)

    # кратко изчакване, за да стигне отговорът до браузъра, после изход
    threading.Timer(1.5, _exit_and_stop_tunnel).start()
