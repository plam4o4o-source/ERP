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

if sys.stdout is None or sys.stderr is None:
    _log_file = open(os.path.join(_base_dir_early, "pacho_startup.log"),
                     "a", encoding="utf-8", errors="replace", buffering=1)
    sys.stdout = _log_file
    sys.stderr = _log_file
else:
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

import appcore
import backup
import config as appconfig
import db
import desktop
import updater
from version import __version__

APP_NAME = appcore.APP_NAME

app = appcore.create_app()

# Регистрация на всички routes_* модули — ВСЕКИ регистрира само своите
# endpoint-и/URL адреси (виж всеки модул за пълния списък), пазейки
# ТОЧНО оригиналните имена/пътища от предишния монолитен app.py, за да не
# се налага промяна в url_for(...) извикванията из шаблоните.
import routes_auth
import routes_dashboard
import routes_documents
import routes_pallet_extra
import routes_clients
import routes_settings
import routes_admin

routes_auth.register(app)
routes_dashboard.register(app)
routes_documents.register(app)
routes_pallet_extra.register(app)
routes_clients.register(app)
routes_settings.register(app)
routes_admin.register(app)

# Комбинира /preview/<token> хендлъра — регистриран директно тук (не в
# отделен routes_ модул), защото е единствен маршрут, споделен между
# всички петте документни потока (виж appcore.render_preview/_get_preview).
from datetime import datetime

from flask import flash, redirect, render_template, session, url_for


@app.route("/preview/<token>")
@appcore.login_required
def preview_document(token):
    payload = appcore._get_preview(token, "doc")
    if payload is None:
        flash("Прегледът е изтекъл или вече е използван — генерирайте го отново от формата.")
        return redirect(url_for("dashboard"))
    doc_type, data = payload
    draft_doc = {
        "id": 0,
        "doc_type": doc_type,
        "number": "ПРЕДВАРИТЕЛЕН ПРЕГЛЕД / DRAFT",
        "barcode": "DRAFT-PREVIEW",
        "author": session.get("full_name") or session.get("username"),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    return render_template(appcore.PRINT_TEMPLATES[doc_type], doc=draft_doc, d=data,
                           copies=1, preview=True, label_format=False)


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
    едновременни служители от други компютри в офиса)."""
    if host == "0.0.0.0":
        import waitress
        waitress.serve(app, host=host, port=port, threads=8)
    else:
        app.run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    _cfg = appconfig.load_config()
    _host = "0.0.0.0" if _cfg.get("network_mode") else "127.0.0.1"
    _port = int(_cfg.get("network_port") or 5000)
    _local_url = "http://127.0.0.1:%d" % _port

    # Фоновият архивиращ таймер винаги стартира; сам проверява дали е
    # зададена папка за архив в „Системни настройки“ и иначе не прави нищо.
    backup.start_auto_backup(_get_backup_settings)

    # Истински автоматично обновяване: проверява и инсталира новата версия
    # във фонов режим, без потребителят да трябва да натиска бутон.
    # Пропуска се, ако тази инсталация в момента е централен сървър за
    # други компютри в офиса (мрежов режим) — там рестарт би прекъснал
    # работата на всички останали неочаквано, затова остава ръчно.
    updater.start_auto_update_loop(lambda: _host == "0.0.0.0")

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
            os._exit(0)
    else:
        print("%s v%s — %s%s" % (
            APP_NAME, __version__, _local_url,
            " (мрежов режим — достъпно и от други компютри в мрежата)" if _host == "0.0.0.0" else ""))
        _run_server(_host, _port)
