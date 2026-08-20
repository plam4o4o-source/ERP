# -*- coding: utf-8 -*-
"""ПачоЛогистик — логистична програма за издаване на ЧМР товарителници,
опаковъчни листи и палетни карти с баркодове.

Стартиране:  python app.py  →  http://127.0.0.1:5000
Първоначален вход: потребител "admin", парола "admin123" (сменете я!).

Този файл е ТЪНКА входна точка (Фаза 3 от плана за разработка — виж
ПЛАН_ЗА_РАЗРАБОТКА.md): цялата логика на приложението (fабрика на Flask
app-а, decorator-и, hook-ове, общи помощни функции) е в appcore.py, а
маршрутите са разпределени по area в routes_*.py модули, всеки с функция
register(app). app.py само ги свързва и пази десктоп стартовия блок
(pywebview/фонов сървър), който трябва да остане тук, защото борави пряко
с процеса (sys.frozen, os._exit и т.н.)."""
import os
import sys
import threading
import time
import webbrowser

# Компилираната .exe версия се билдва без конзолен прозорец (--windowed),
# затова sys.stdout/sys.stderr са None — обикновен print() би гръмнал.
# Пренасочваме извеждането към лог файл до .exe-то в такъв случай. Иначе
# (стартиране от изходния код, или билд с конзола) конзолата на Windows
# често е с кодировка cp1252/cp866 и гърми при кирилица — принуждаваме UTF-8.
if getattr(sys, "frozen", False):
    _base_dir_early = os.path.dirname(os.path.abspath(sys.executable))
else:
    _base_dir_early = os.path.dirname(os.path.abspath(__file__))

# Одит (16.08.2026, находка №27): pacho_startup.log растеше БЕЗКРАЙНО —
# нямаше никаква ротация, а в компилираната .exe версия там отива и
# access-логът на всяка HTTP заявка (werkzeug), плюс всеки applog
# traceback (напр. при недостъпна база — виж находка №9 — логът расте с
# MB/минута точно когато мястото на диска е най-нужно за самите архиви).
# Проста ротация с едно поколение: ако логът е над прага при старт,
# преименувай го на ".1" (презаписвайки предишното ".1"), после отвори
# чист файл — достатъчно за настолна инсталация, без нужда от пълноценна
# библиотека за ротация (logging.handlers), тъй като изходът тук е суров
# print()/stdout, не structured logging.
_LOG_MAX_BYTES = 20 * 1024 * 1024  # 20 MB


def _rotate_startup_log_if_large(path, max_bytes=_LOG_MAX_BYTES):
    try:
        if os.path.exists(path) and os.path.getsize(path) > max_bytes:
            os.replace(path, path + ".1")
    except OSError:
        pass  # best-effort — неуспешна ротация не бива да пречи на самия старт


def _open_startup_log(base_dir):
    """Одит (19.08.2026, информативна находка): отварянето на лог файла
    беше ГОЛО `open(..., "a")` без try/except. При инсталация в папка без
    право на запис (Program Files с обикновен потребител, споделена папка
    само за четене, файл, заключен от антивирус/друго копие на програмата)
    това хвърля PermissionError/OSError на ниво МОДУЛ — тоест в
    компилираната `--windowed` версия програмата умира преди изобщо да е
    създаден прозорец: за потребителя двойното щракване просто „не прави
    нищо“, при това точно логът, който би обяснил защо, е причината.

    Опитваме подред: до .exe-то → %TEMP%/системната временна папка → при
    пълен неуспех None (тогава извикващият пренасочва към os.devnull, за
    да не гърми първият print()). Връща отворения файл или None."""
    candidates = [os.path.join(base_dir, "pacho_startup.log")]
    try:
        import tempfile
        candidates.append(os.path.join(tempfile.gettempdir(), "pacho_startup.log"))
    except Exception:  # nosec B110 -- при липсващ/недостъпен TEMP просто няма резервен път
        pass
    for path in candidates:
        try:
            _rotate_startup_log_if_large(path)
            return open(path, "a", encoding="utf-8", errors="replace", buffering=1)
        except OSError:
            continue
    return None


if sys.stdout is None or sys.stderr is None:
    _log_file = _open_startup_log(_base_dir_early)
    if _log_file is None:
        # Никъде няма право на запис — стартът НЕ бива да зависи от лога.
        # os.devnull е валиден файлов обект, така че всеки print() по-нататък
        # (включително access-логът на waitress) минава безшумно.
        _log_file = open(os.devnull, "w", encoding="utf-8", errors="replace")
    sys.stdout = _log_file
    sys.stderr = _log_file
else:
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # nosec B110 -- best-effort опит само за конзолния UTF-8 encoding; при неуспех просто продължава със стандартния
                pass

import atexit

import flask

import applog
import appcore
import backup
import config as appconfig
import db
import desktop
import net
import remote_tunnel
import updater
from version import __version__

APP_NAME = appcore.APP_NAME

def _create_app_or_explain():
    """Одит (19.08.2026, находка №24): `app = appcore.create_app()` беше
    гол ред на модулно ниво. `create_app` вика `db.init_db()`, а тя започва
    миграциите с `BEGIN IMMEDIATE` — при база, заета от друг компютър
    по-дълго от busy_timeout, изчакването изтича и излита необработен
    `OperationalError('database is locked')`. Проверено с изпълнение: чужд
    писателски катинар, държан 20 сек, кара init_db да чака 15.2 сек и да
    гръмне. Понеже това се случва при ИМПОРТА на модула, процесът просто
    умира — а в компилирания .exe (--windowed) това е ТИХА смърт: никакъв
    прозорец, никакво съобщение, само traceback в pacho_startup.log, който
    потребителят никога не отваря.

    Сега: няколко опита с нарастващо изчакване (базата обикновено се
    освобождава за секунди), а при трайна невъзможност — минимално
    приложение, което показва СЪЩАТА страница „базата е недостъпна“, която
    вече ползваме при отпаднал мрежов диск (находки №9/№3). Потребителят
    вижда прозорец с ясна причина и бутон „Опитай пак“, вместо нищо."""
    last_exc = None
    for attempt in range(4):
        try:
            return appcore.create_app()
        except Exception as exc:  # включително sqlite3.OperationalError
            last_exc = exc
            applog.log_exception(
                "app: неуспешно стартиране (опит %d от 4)" % (attempt + 1))
            time.sleep(1.5 * (attempt + 1))

    fallback = flask.Flask(__name__)
    # Маркер, по който регистрацията на нормалните маршрути по-долу се
    # пропуска — иначе те биха презаписали catch-all правилото тук и пак
    # биха гърмели на недостъпната база.
    fallback.config["PACHO_DB_UNAVAILABLE"] = True

    @fallback.route("/", defaults={"_path": ""})
    @fallback.route("/<path:_path>")
    def _db_unavailable(_path):
        return flask.render_template(
            "db_unavailable.html",
            app_name=appcore.APP_NAME,
            message=("Базата данни не може да бъде отворена при стартиране "
                     "(%s). Най-честа причина: работи ли програмата в момента "
                     "на друг компютър, който записва в същата база, или "
                     "мрежовата папка е временно недостъпна." % last_exc),
            retry_url="/",
        ), 503

    return fallback


app = _create_app_or_explain()

# Одит (16.08.2026, находка №1): remote_tunnel.stop() при os._exit(0) по-долу
# покриваше само пътя „нативен прозорец затворен“ — конзолният/сървърен
# режим (стартиране от изходния код през start_windows.bat, Ctrl+C,
# затваряне на конзолния прозорец) излизаше без НИКАКВО почистване,
# оставяйки cloudflared „сирак“ по същия начин като поправената критична
# находка №2 от 12.08. atexit покрива нормалния изход И KeyboardInterrupt
# (Ctrl+C); remote_tunnel.stop() вече поглъща собствените си грешки и е
# безопасен за безусловно извикване, дори тунелът никога да не е бил
# стартиран. Не покрива SIGKILL/токов удар — по дефиниция невъзможно да
# се хване от какъвто и да е Python код.
atexit.register(remote_tunnel.stop)

# Регистрация на всички routes_* модули — ВСЕКИ регистрира само своите
# endpoint-и/URL адреси (виж всеки модул за пълния списък), пазейки
# ТОЧНО оригиналните имена/пътища от предишния монолитен app.py, за да не
# се налага промяна в url_for(...) извикванията из шаблоните.
import routes_auth
import routes_dashboard
import routes_documents
import routes_pallet_extra
import routes_invoices
import routes_materials
import routes_clients
import routes_settings
import routes_admin

if not app.config.get("PACHO_DB_UNAVAILABLE"):
    routes_auth.register(app)
    routes_dashboard.register(app)
    routes_documents.register(app)
    routes_pallet_extra.register(app)
    routes_invoices.register(app)
    routes_materials.register(app)
    routes_clients.register(app)
    routes_settings.register(app)
    routes_admin.register(app)

# Комбинира /preview/<token> хендлъра — регистриран директно тук (не в
# отделен routes_ модул), защото е единствен маршрут, споделен между
# всички петте документни потока (виж appcore.render_preview/_get_preview).
from datetime import datetime

from flask import flash, redirect, render_template, session, url_for


@appcore.login_required
def preview_document(token):
    payload = appcore._get_preview(token, "doc")
    if payload is None:
        flash("Прегледът е изтекъл или вече е използван — генерирайте го отново от формата.", "warning")
        return redirect(url_for("dashboard"))
    # Одит (19.08.2026, находка №10): payload-ът вече носи и версията
    # (4-ти елемент). Старите 3-елементни токени, издадени преди
    # обновяването и още живи в паметта (до 30 мин), се четат съвместимо.
    doc_type, data, edit_doc_id = payload[0], payload[1], payload[2]
    draft_doc = appcore.build_draft_doc(
        doc_type, data, session.get("full_name") or session.get("username"))
    return render_template(appcore.PRINT_TEMPLATES[doc_type], doc=draft_doc, d=data,
                           copies=1, preview=True, label_format=False, token=token,
                           edit_doc_id=edit_doc_id)


if not app.config.get("PACHO_DB_UNAVAILABLE"):
    app.add_url_rule("/preview/<token>", "preview_document", preview_document)


def _get_backup_settings():
    con = db.get_db()
    s = db.get_settings(con)
    con.close()
    return s


def _run_server(host, port):
    """Стартира сървъра — vградения Flask dev сървър при обикновен локален
    самостоятелен режим (127.0.0.1, единствен потребител на този компютър),
    или waitress (production-grade WSGI) при мрежов режим (виж находка M6:
    Flask dev сървърът изрично предупреждава да не се ползва в продукция —
    еднонишков по подразбиране, слабо управление на опашка при няколко
    едновременни служители от други компютри в офиса).

    Одит (находка В17): извикващият (__main__ по-долу) вече е потвърдил
    порта СВОБОДЕН чрез _find_available_port ПРЕДИ да стартира тази
    функция във фонова нишка — оставащ риск е чисто състезание (TOCTOU:
    нещо друго заема порта точно между проверката и реалния bind тук),
    затова все пак хващаме и логваме тук като втора линия на защита,
    вместо необработеното изключение тихо да убие daemon нишката."""
    try:
        if host == "0.0.0.0":  # nosec B104 -- изрично, документирано, ИЗКЛЮЧЕНО по подразбиране „Мрежов режим“ от Системни настройки, не хардкоднато поведение
            import waitress
            waitress.serve(app, host=host, port=port, threads=8)
        else:
            app.run(host=host, port=port, debug=False, use_reloader=False)
    except OSError as exc:
        applog.log_exception(
            "app._run_server: сървърът не можа да стартира на %s:%d (%s)" % (host, port, exc))
        print("ГРЕШКА: сървърът не можа да стартира на %s:%d — %s" % (host, port, exc))


if __name__ == "__main__":
    _cfg = appconfig.load_config()
    _host = "0.0.0.0" if _cfg.get("network_mode") else "127.0.0.1"  # nosec B104 -- виж коментара в _run_server по-горе: изрично opt-in, не по подразбиране
    _configured_port = appconfig.get_network_port(_cfg)
    # В17: потвърждаваме порта СВОБОДЕН, преди изобщо да пускаме сървъра
    # или прозореца — вижте пълното обяснение при net.find_available_port.
    try:
        _port = net.find_available_port(_host, _configured_port)
    except RuntimeError as _port_exc:
        print("ГРЕШКА: %s" % _port_exc)
        applog.log_exception("app.__main__: не бе намерен свободен порт около %d" % _configured_port)
        sys.exit(1)
    if _port != _configured_port:
        print("Забележка: порт %d е зает — програмата ще ползва свободния порт %d вместо това."
             % (_configured_port, _port))
    # Одит (12.08.2026, находка №10): запазва РЕАЛНО използвания порт (може
    # да се различава от _configured_port точно в случая по-горе), за да
    # могат system_remote_start()/updating.html (routes_admin.py) да сочат
    # към правилния адрес вместо сляпо да четат конфигурацията.
    appcore.set_runtime_port(_port)
    _local_url = "http://127.0.0.1:%d" % _port

    # Фоновият архивиращ таймер винаги стартира; сам проверява дали е
    # зададена папка за архив в „Системни настройки“ и иначе не прави нищо.
    backup.start_auto_backup(_get_backup_settings)

    # Истински автоматично обновяване: проверява и инсталира новата версия
    # във фонов режим, без потребителят да трябва да натиска бутон.
    # Пропуска се, ако тази инсталация в момента е централен сървър за
    # други компютри в офиса (мрежов режим) — там рестарт би прекъснал
    # работата на всички останали неочаквано, затова остава ръчно.
    updater.start_auto_update_loop(lambda: _host == "0.0.0.0")  # nosec B104 -- само СРАВНЕНИЕ с константата, не bind; виж коментара в _run_server

    if getattr(sys, "frozen", False):
        # Истинско настолно приложение: Flask сървърът работи във фонова
        # нишка в СЪЩИЯ процес, а прозорецът е вграден (pywebview/WebView2)
        # — без изобщо да се стартира отделен браузър процес. При неуспех
        # (напр. WebView2 липсва) пада към Chrome/Edge в режим „приложение“,
        # а като последна мярка — обикновен браузър.
        server_thread = threading.Thread(
            target=lambda: _run_server(_host, _port),
            daemon=True,
        )
        server_thread.start()
        print("%s v%s — %s (настолен режим)" % (APP_NAME, __version__, _local_url))
        opened_native = desktop.run_native_window(
            _local_url, title="%s v%s" % (APP_NAME, __version__))
        if not opened_native:
            if not desktop.open_app_window(_local_url):
                webbrowser.open(_local_url)
            server_thread.join()
        else:
            # Одит (12.08.2026, находка №2, критична): os._exit(0) спира
            # процеса рязко — Windows НЕ убива автоматично дъщерни процеси
            # при рязко спиране на родителя, затова стартиран тунел за
            # отдалечен достъп (remote_tunnel.start, cloudflared) оставаше
            # „сирак“ и продължаваше да работи в мрежата неограничено
            # дълго, без администраторът да го вижда/спре от интерфейса.
            # Изричното спиране тук е best-effort (терминира процеса, ако
            # има такъв) — remote_tunnel.stop() вече поглъща собствените
            # си грешки (виж модула), затова е безопасно да се вика
            # безусловно, дори тунелът никога да не е бил стартиран.
            remote_tunnel.stop()
            os._exit(0)
    else:
        print("%s v%s — %s%s" % (
            APP_NAME, __version__, _local_url,
            " (мрежов режим — достъпно и от други компютри в мрежата)" if _host == "0.0.0.0" else ""))  # nosec B104 -- само СРАВНЕНИЕ, не bind; виж _run_server
        _run_server(_host, _port)
