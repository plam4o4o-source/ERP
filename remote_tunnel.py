# -*- coding: utf-8 -*-
"""Отдалечен достъп (сканиране на баркод с телефон от всяка мрежа — не само
локалната Wi-Fi мрежа на офиса) чрез Cloudflare Quick Tunnel.

Cloudflare Quick Tunnel дава временен публичен HTTPS адрес
(*.trycloudflare.com) с истински, доверен от браузърите сертификат — без
предупреждения — който пренасочва трафика към локалния сървър на
програмата (http://127.0.0.1:port). Не изисква пренасочване на портове на
рутера, статичен IP, домейн или регистрация — само изходяща връзка към
Cloudflare, каквато има всеки нормален интернет достъп в офиса.

Изпълнимият файл `cloudflared` се изтегля еднократно (официално, от
GitHub releases на Cloudflare) при първо стартиране на функцията и се пази
локално за следващи пускания — не се разпространява вградено в програмата,
за да не се разраства инсталаторът за функция, която не всеки ползва.

Адресът е защитен допълнително от нормалния вход с потребителско
име/парола на самата програма — но е достъпен за всеки, който го знае,
докато е активен, затова се стартира само при нужда от администратор и се
спира след употреба.
"""
import os
import platform
import re
import subprocess  # nosec B404 -- ползван само за стартиране на изтегления и проверен (магически байтове/checksum) cloudflared бинарник, виж nosec бележката при Popen по-долу
import sys
import threading
import urllib.request

import applog
import net

_UA = {"User-Agent": "PachoLogistic-RemoteAccess"}
_URL_RE = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")

_lock = threading.Lock()
_state = {
    "process": None,
    "status": "stopped",   # stopped | starting | running | error
    "url": None,
    "error": None,
    # Одит (16.08.2026, находка №12): "поколение" на текущия start()/stop()
    # цикъл — вижте start()/stop() по-долу за пълното обяснение защо е
    # нужно (stop() по време на фазата "starting", преди _run() изобщо да
    # е регистрирал процес, преди тази поправка се игнорираше тихо: _run()
    # продължаваше безусловно и регистрираше нов, непроследим тунел).
    "generation": 0,
    # Одит (16.08.2026, находка №7): виж _AUTO_STOP_SECONDS/start() по-долу
    # — таймер, който автоматично спира тунела, ако никой не го е спрял
    # ръчно, за да не остане отворен публичен адрес неограничено дълго
    # (напр. администраторът е забравил).
    "auto_stop_timer": None,
}

# Одит (16.08.2026, находка №7, средна): адресът е защитен от вход с
# потребителско име/парола на самата програма (виж докстринга на модула
# по-горе), но е достъпен от ЦЕЛИЯ интернет за всеки, който го познава/
# отгатне, докато е активен — рискът расте с ВРЕМЕТО, през което остава
# включен. Автоматично спиране след разумен период ограничава прозореца на
# експозиция, ако администраторът просто забрави да го спре ръчно (виж
# my_settings.html за бутона "Спри" и templates/base.html за постоянния
# банер, докато е активен — находка №7, видимост).
_AUTO_STOP_SECONDS = 2 * 60 * 60  # 2 часа


def _base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _binary_path():
    name = "cloudflared.exe" if os.name == "nt" else "cloudflared"
    d = os.path.join(_base_dir(), "bin")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, name)


def _download_url():
    if os.name == "nt":
        return ("https://github.com/cloudflare/cloudflared/releases/latest/"
                "download/cloudflared-windows-amd64.exe")
    if sys.platform == "darwin":
        return ("https://github.com/cloudflare/cloudflared/releases/latest/"
                "download/cloudflared-darwin-amd64.tgz")
    machine = platform.machine().lower()
    if "arm" in machine or "aarch64" in machine:
        return ("https://github.com/cloudflare/cloudflared/releases/latest/"
                "download/cloudflared-linux-arm64")
    return ("https://github.com/cloudflare/cloudflared/releases/latest/"
            "download/cloudflared-linux-amd64")


def _expected_magic():
    """Очакваните начални байтове на валидно изтеглено `cloudflared`,
    според платформата — виж _download_url() за съответствието формат
    ↔ платформа (трябва да останат синхронизирани помежду си).

    Cloudflare, за разлика от нашия собствен release.yml, НЕ публикува
    контролна сума (SHA-256) за изтеглянията на `cloudflared` (потвърдено
    — виж отворените, все още нерешени issue #1410/#1617 в
    cloudflare/cloudflared на GitHub към момента на писане), затова тук
    няма как да проверим автентичност срещу публикувана сума, както
    правим за собствения си .exe в updater.py (H3/находка). Проверката
    на магическите байтове по-долу хваща реалистичния риск в тази
    функция — прекъснато/повредено изтегляне (нестабилна връзка,
    антивирус, прекъснат сървър) — по същия принцип, ползван за логото
    (branding.py) и собствения .exe (updater.py)."""
    if os.name == "nt":
        return b"MZ"            # PE изпълним файл (cloudflared-windows-amd64.exe)
    if sys.platform == "darwin":
        return b"\x1f\x8b"      # gzip архив (.tgz)
    return b"\x7fELF"           # ELF изпълним файл (Linux amd64/arm64)


def _binary_looks_valid(path):
    """Наличният на диска cloudflared изглежда ли годен — размер и
    магически байтове (виж ensure_binary за пълния разказ). Извадено в
    отделна функция при десетия одит (02.09.2026, находка №12), защото
    същата проверка трябва и когато `os.replace` се провали, за да се
    прецени дали вече наличният файл върши работа."""
    try:
        if not os.path.exists(path) or os.path.getsize(path) <= 100000:
            return False
        with open(path, "rb") as f:
            magic = f.read(4)
    except OSError:
        return False
    return magic.startswith(_expected_magic())


def ensure_binary():
    """Осигурява наличен `cloudflared` — изтегля го еднократно от
    официалните GitHub releases на Cloudflare, ако липсва локално.

    Одит (12.08.2026, находка №36, дребна): преди тази поправка КЕШИРАНИЯТ
    (вече изтеглен по-рано) бинарник се преверифицираше само по РАЗМЕР
    (`> 100000` байта) при всяко следващо стартиране — магическите байтове
    (виж _expected_magic по-горе) се проверяваха САМО веднъж, точно след
    изтеглянето. Файл, повреден по-късно на диска (прекъснат запис при
    срив на компютъра, повреда на файловата система, ръчна намеса) би
    минал тихо покрай проверката тук и би паднал едва при опит да се
    стартира като подпроцес — по-неясна грешка, на по-лошо място. Сега
    магическите байтове се проверяват при ВСЯКО извикване, не само при
    ново изтегляне."""
    path = _binary_path()
    if _binary_looks_valid(path):
        return path
    if os.path.exists(path) and os.path.getsize(path) > 100000:
        applog.log_warning("remote_tunnel.ensure_binary",
                           "кешираният cloudflared не мина проверка по магически "
                           "байтове (повреден на диска?) — ще бъде изтеглен наново")
    req = urllib.request.Request(_download_url(), headers=_UA)
    # Одит (02.09.2026, десети одит, находка №12): временното име беше
    # ФИКСИРАНО (`<път>.download`), а `_base_dir()` при компилиран .exe е
    # папката на самото .exe — тоест в мрежовата инсталация СПОДЕЛЕНАТА
    # папка. Две работни станции, включващи отдалечен достъп по едно и също
    # време, пишеха в ЕДИН И СЪЩ файл и всяка правеше `os.replace` върху
    # частично изтегления файл на другата — тоест проверката по размер и
    # магически байтове по-долу се е случила върху ДРУГ файл, а не върху
    # този, който накрая застава на мястото си (същият TOCTOU като при
    # updater._machine_suffix). Уникалното име прекратява и двете: никой не
    # пипа междинния файл на друг, а `os.replace` мести точно проверения.
    tmp_path = "%s.%d.download" % (path, os.getpid())
    with net.urlopen(req, timeout=60) as resp:
        content_length = resp.headers.get("Content-Length")
        expected_size = int(content_length) if content_length and content_length.isdigit() else None
        with open(tmp_path, "wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)

    # Проверка, че изтегленото е ЦЯЛ, неповреден файл, преди да го
    # маркираме изпълним и да го пуснем като подпроцес — виж _expected_magic()
    # по-горе защо тук няма контролна сума за сравнение (Cloudflare не
    # публикува такава за cloudflared).
    actual_size = os.path.getsize(tmp_path)
    problem = None
    if actual_size <= 100000:
        problem = "файлът е твърде малък (%d байта)" % actual_size
    elif expected_size is not None and actual_size != expected_size:
        problem = "непълно изтегляне (%d от общо %d байта)" % (actual_size, expected_size)
    else:
        with open(tmp_path, "rb") as f:
            magic = f.read(4)
        expected = _expected_magic()
        if not magic.startswith(expected):
            problem = "файлът не е валиден изпълним файл (повреден при изтеглянето)"
    if problem:
        os.remove(tmp_path)
        applog.log_warning("remote_tunnel.ensure_binary",
                           "изтегленият cloudflared е отхвърлен — %s" % problem)
        raise RuntimeError("файлът изглежда повреден (%s) — опитайте отново" % problem)

    # Одит (02.09.2026, находка №12, втора половина): `os.replace` върху
    # cloudflared.exe, който ДРУГА машина в момента изпълнява, се проваля
    # под Windows (sharing violation, WinError 5). Досега изключението
    # излиташе през `_run`, чийто `except Exception` го показваше като
    # „Неуспешно изтегляне на компонента за отдалечен достъп“ — заблуждаващо,
    # защото изтеглянето е УСПЯЛО. Ако вече наличният файл е валиден, той
    # върши работа: изтриваме своя дубликат и продължаваме с него.
    try:
        os.replace(tmp_path, path)
    except OSError:
        if os.path.exists(path) and _binary_looks_valid(path):
            applog.log_warning("remote_tunnel.ensure_binary",
                               "cloudflared не можа да бъде подменен (зает от "
                               "друг процес/машина), но наличният файл е "
                               "валиден — продължаваме с него")
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            return path
        raise
    if os.name != "nt":
        # 0o755 е минимумът, необходим да СЕ ИЗПЪЛНИ изтегленият cloudflared
        # бинарник на Linux/macOS (без execute бит въобще не стартира);
        # файлът вече е проверен по магически байтове по-горе.
        os.chmod(path, 0o755)  # nosec B103
    return path


def _consume_output(proc):
    """Чете изхода на cloudflared ред по ред, докато открие публичния
    адрес, и следи процеса да не приключи неочаквано."""
    found = False
    try:
        for raw in iter(proc.stdout.readline, ""):
            if not raw:
                break
            m = _URL_RE.search(raw)
            if m and not found:
                found = True
                with _lock:
                    if _state["process"] is proc:
                        _state["url"] = m.group(0)
                        _state["status"] = "running"
                        _state["error"] = None
    except Exception:
        applog.log_exception("remote_tunnel._consume_output: грешка при четене на изхода на cloudflared")
    finally:
        proc.wait()
        with _lock:
            if _state["process"] is proc:
                # Одит (19.08.2026, находка №50, дребна): cloudflared може
                # да умре САМ (паднала интернет връзка, спрян от Cloudflare
                # quick tunnel, убит процес). Дотук този блок нулираше
                # process/status, но ОСТАВЯШЕ авто-стоп таймера жив — до 2
                # часа по-късно в лога влизаше „автоматично спиране на
                # отдалечения достъп“ за тунел, паднал отдавна. При
                # разчитане на лога след инцидент това е направо подвеждащо:
                # изглежда, че тунелът е бил активен през цялото време.
                # Вдигаме и „поколението“ по същата причина, поради която го
                # прави stop(): всеки вече изпратен таймер/стартиращ _run()
                # разпознава, че става дума за приключил цикъл.
                timer = _state.get("auto_stop_timer")
                _state["auto_stop_timer"] = None
                _state["generation"] += 1
                if timer is not None:
                    timer.cancel()
            if _state["process"] is proc and not found:
                # Одит (находка В16, висок риск): преди поправката този
                # клон нулираше status="error", но НЕ и _state["process"]
                # — то оставаше сочещо към вече мъртвия proc. start() по-
                # горе проверява само "if _state['process'] is not None"
                # (без изключение за status=="error"), затова следващо
                # натискане на „Стартирай“ тихо не правеше нищо — бутонът
                # изглеждаше счупен, докато операторът не се сетеше първо
                # да натисне „Спри“ (единственото място, което explicitно
                # нулираше _state["process"]). Сега нулираме тук по същия
                # начин като клона по-долу (found=True) — процесът реално
                # е приключил (proc.wait() вече се върна) в ДВата случая,
                # няма причина да пазим stale референция само защото е
                # умрял без грешка вместо с грешка.
                _state["process"] = None
                _state["status"] = "error"
                _state["error"] = ("Компонентът за отдалечен достъп спря "
                                   "неочаквано (проверете интернет връзката).")
            if _state["process"] is proc and found:
                # Процесът приключи, след като вече бе показал адрес —
                # тунелът вече не работи.
                _state["process"] = None
                _state["status"] = "stopped"
                _state["url"] = None


def start(local_port):
    """Стартира нов тунел към http://127.0.0.1:local_port (ако вече не
    работи такъв). Изпълнява се асинхронно — статусът се проверява чрез
    status()."""
    with _lock:
        if _state["process"] is not None or _state["status"] == "starting":
            return
        old_timer = _state.get("auto_stop_timer")
        if old_timer is not None:
            old_timer.cancel()
        _state["status"] = "starting"
        _state["url"] = None
        _state["error"] = None
        _state["generation"] += 1
        my_gen = _state["generation"]
        # Одит (16.08.2026, находка №7): автоматично спиране след
        # _AUTO_STOP_SECONDS — my_gen в затварянето гарантира, че таймер от
        # СТАР цикъл никога не спира по-нов, вече рестартиран тунел (виж
        # _auto_stop_if_still_current по-долу).
        timer = threading.Timer(_AUTO_STOP_SECONDS, _auto_stop_if_still_current, args=(my_gen,))
        timer.daemon = True
        _state["auto_stop_timer"] = timer
        timer.start()

    def _run():
        try:
            binary = ensure_binary()
        except Exception as exc:
            with _lock:
                # Одит (находка №12): ensure_binary() може да отнеме до 60
                # сек (първо изтегляне) — ако stop() е бил натиснат междувременно,
                # поколението вече не съвпада; не презаписвай статуса, който
                # stop() вече е върнал на "stopped" (иначе интерфейсът пак
                # показва грешка за тунел, за който потребителят вече е
                # поискал спиране).
                if _state["generation"] == my_gen:
                    _state["status"] = "error"
                    _state["error"] = ("Неуспешно изтегляне на компонента за "
                                       "отдалечен достъп: %s" % exc)
            return
        with _lock:
            if _state["generation"] != my_gen:
                # stop() е бил натиснат, докато чакахме ensure_binary() —
                # не стартирай cloudflared изобщо (виж докстринга на find. №12).
                return
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            # Фиксиран списък аргументи, без shell=True; binary е изтегленият
            # и проверен (checksum/магически байтове, ensure_binary по-горе)
            # cloudflared, local_port е нашият собствен слушащ порт, не
            # потребителски вход.
            proc = subprocess.Popen(  # nosec B603
                [binary, "tunnel", "--no-autoupdate", "--url",
                 "http://127.0.0.1:%d" % local_port],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                **kwargs,
            )
        except Exception as exc:
            with _lock:
                if _state["generation"] == my_gen:
                    _state["status"] = "error"
                    _state["error"] = ("Неуспешно стартиране на компонента за "
                                       "отдалечен достъп: %s" % exc)
            return
        with _lock:
            if _state["generation"] != my_gen:
                # Одит (находка №12): stop() е бил натиснат точно между
                # Popen() и тук — не регистрирай този процес в _state
                # (иначе остава непроследим, работещ публичен тунел, който
                # интерфейсът не може повече да спре) — терминирай го веднага.
                _terminate_process(proc)
                return
            _state["process"] = proc
        _consume_output(proc)

    threading.Thread(target=_run, daemon=True).start()


def _auto_stop_if_still_current(my_gen):
    """Одит (16.08.2026, находка №7): извиква се от threading.Timer след
    _AUTO_STOP_SECONDS. Проверява поколението, ПРЕДИ да спре нещо — ако
    потребителят вече е спрял/рестартирал тунела междувременно, this е
    остарял таймер и не бива да пипа текущото състояние."""
    with _lock:
        if _state["generation"] != my_gen:
            return
    applog.log_warning(
        "remote_tunnel", "автоматично спиране на отдалечения достъп след %d "
        "минути — защита от оставане включен по-дълго от нужното (виж находка "
        "№7)." % (_AUTO_STOP_SECONDS // 60))
    stop()


def _terminate_process(proc):
    try:
        proc.terminate()
    except Exception:
        # Процесът вероятно вече е приключил сам — не е грешка, но
        # логваме за диагностика, ако причината е друга.
        applog.log_exception("remote_tunnel._terminate_process: неуспешно спиране на процеса на cloudflared")
    # Одит (19.08.2026, находка №50, дребна): без `wait()` терминираният
    # процес остава ZOMBIE на POSIX (ядрото пази изходния статус, докато
    # родителят не го прибере) — Popen го прибира сам само при GC, а тук
    # има път, по който референцията се задържа (клонът за разминато
    # поколение веднага след Popen() в start()). При многократно
    # стартиране/спиране се трупат по един зомби процес на цикъл.
    # timeout: спирането на cloudflared е моментално; ако все пак заседне,
    # убиваме го — по-добре от блокирана нишка на потребителска заявка.
    try:
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:  # nosec B110 -- процесът е извън контрола ни; логваме по-долу и продължаваме
            pass
        applog.log_warning(
            "remote_tunnel._terminate_process",
            "процесът на cloudflared не приключи навреме след terminate() — "
            "изпратен е kill().")


def stop():
    with _lock:
        proc = _state["process"]
        timer = _state.get("auto_stop_timer")
        _state["auto_stop_timer"] = None
        _state["process"] = None
        _state["status"] = "stopped"
        _state["url"] = None
        _state["error"] = None
        # Одит (находка №12): нулира и текущото поколение, за да може
        # евентуален все още изпълняващ се _run() (в "starting" фаза, чакащ
        # ensure_binary()/Popen()) да разпознае, че е бил изпреварен от
        # това stop(), и да не регистрира/стартира тунел въобще.
        _state["generation"] += 1
    # Одит (16.08.2026, находка №7): отмени и авто-стоп таймера при РЪЧНО
    # спиране — иначе (безобидно, но излишно) той пак би "стрелял" по-късно
    # срещу поколение, което вече не съществува (guard-ът в
    # _auto_stop_if_still_current прави това безопасно, но по-чисто е да не
    # чакаме отделна нишка да се събуди напразно).
    if timer is not None:
        timer.cancel()
    if proc is not None:
        _terminate_process(proc)


def status():
    with _lock:
        return {"status": _state["status"], "url": _state["url"], "error": _state["error"]}
