# -*- coding: utf-8 -*-
"""Ядро на ПачоЛогистик: фабрика на Flask приложението (create_app),
общи decorator-и/помощни функции и hook-ове, споделени от всички routes_*
модули. Извлечено от предишния монолитен app.py (Фаза 3 от плана за
разработка — виж ПЛАН_ЗА_РАЗРАБОТКА.md) с ЦЕЛ да НЕ променя поведение,
само да раздели файла по отговорност.

Защо фабрика (create_app), а не готов `app` обект на ниво модул: преди
това app.py създаваше Flask приложението И извикваше db.init_db() при
самия ИМПОРТ на модула — това правеше app.py практически невъзможен за
тестване с Flask test client (всеки импорт пипаше реалната база данни).
create_app(run_boot_tasks=True) отлага всичко това до ИЗРИЧНО извикване,
след като тестът вече е пренасочил db.DB_PATH към временен файл (виж
tests/conftest.py: fixture-ът `flask_app`)."""
import hmac
import json
import os
import secrets
import sys
import threading
import time
from datetime import date, timedelta
from functools import wraps

from flask import Flask, abort, flash, redirect, request, session, url_for
from markupsafe import Markup

import backup
import branding
import config as appconfig
import db
import jsonutil
from barcode128 import code128_svg
from icons import render_icon
from version import __version__

APP_NAME = "ПачоЛогистик"
MIN_PASSWORD_LENGTH = 8  # прилага се еднакво във всички пътища за задаване на парола

# Шаблони за печат/форма по тип документ — споделени от routes_documents.py
# и routes_pallet_extra.py (bulk преглед/резултат ползват PRINT_TEMPLATES).
PRINT_TEMPLATES = {
    "cmr": "cmr_print.html",
    "packing": "packing_print.html",
    "pallet": "pallet_print.html",
    "waybill": "waybill_print.html",
    "dualuse": "dualuse_print.html",
    "export_it": "export_it_print.html",
}

FORM_TEMPLATES = {
    "cmr": "cmr_form.html",
    "packing": "packing_form.html",
    "pallet": "pallet_form.html",
    "waybill": "waybill_form.html",
    "dualuse": "dualuse_form.html",
    "export_it": "export_it_form.html",
}

# Регистър на петте документни потока (издаване/преглед) — заменя петте
# почти еднакви двойки *_new/*_preview хендлъра от стария app.py с ДАННИ,
# консумирани от единствения generic _document_new/_document_preview в
# routes_documents.py. Полетата тук пазят ТОЧНО предишните различия между
# типовете (виж git история на app.py преди Фаза 3):
#   - needs_items: дали формата има таблица с артикули (items_json)
#   - embed_unload_points: само ЧМР вгражда client.unload_points в
#     clients_json (за избор на пункт за товарене/разтоварване)
#   - success_message: ТОЧНИЯТ текст на flash съобщението (пазен дословно,
#     не генериран от db.DOC_TYPES[...]['title'], защото текстовете не
#     съвпадат буквално с заглавията там — напр. "ЧМР" вместо
#     "ЧМР товарителница")
DOCUMENT_FLOWS = {
    "cmr": {
        "form_template": FORM_TEMPLATES["cmr"],
        "needs_items": False,
        "embed_unload_points": True,
        "success_message": "ЧМР № %s е издадено и запазено в базата данни.",
    },
    "packing": {
        "form_template": FORM_TEMPLATES["packing"],
        "needs_items": True,
        "embed_unload_points": False,
        "success_message": "Опаковъчен лист № %s е издаден и запазен.",
    },
    "pallet": {
        "form_template": FORM_TEMPLATES["pallet"],
        "needs_items": True,
        "embed_unload_points": False,
        "success_message": "Палетна карта № %s е издадена и запазена.",
    },
    "waybill": {
        "form_template": FORM_TEMPLATES["waybill"],
        "needs_items": True,
        "embed_unload_points": False,
        "success_message": "Товарителница № %s е издадена и запазена.",
    },
    "dualuse": {
        "form_template": FORM_TEMPLATES["dualuse"],
        "needs_items": True,
        "embed_unload_points": False,
        "success_message": "Декларация за двойна употреба № %s е издадена и запазена.",
    },
    "export_it": {
        "form_template": FORM_TEMPLATES["export_it"],
        "needs_items": True,
        "embed_unload_points": False,
        "success_message": "Декларация за износ № %s е издадена и запазена.",
    },
}


def create_app(run_boot_tasks=True):
    """Създава и връща напълно конфигуриран Flask app обект.

    run_boot_tasks=False пропуска еднократните действия при СТУДЕН старт
    на съвсем нова инсталация (GitHub bootstrap-изтегляне на базата, ако е
    настроена синхронизация) — ползва се от тестовете, за да не правят
    мрежови заявки. db.init_db() ВИНАГИ се изпълнява (нужна е схемата)."""
    if getattr(sys, "frozen", False):
        # Компилираната .exe версия: шаблоните/статичните файлове са
        # разопаковани във временната папка на PyInstaller (sys._MEIPASS).
        _bundle = sys._MEIPASS
        app = Flask(__name__,
                    template_folder=os.path.join(_bundle, "templates"),
                    static_folder=os.path.join(_bundle, "static"))
    else:
        app = Flask(__name__)
    app.secret_key = db.get_secret_key()
    app.json.ensure_ascii = False

    # Сесийни бисквитки: HttpOnly пречи на JS да ги прочете (значимо при
    # XSS), SameSite=Lax пречи на браузъра да я изпрати при заявка,
    # започната от чужд сайт (базова CSRF защита в допълнение към явния
    # токен по-долу). SESSION_COOKIE_SECURE НЕ се задава: по подразбиране
    # програмата се ползва по обикновено HTTP (127.0.0.1 или LAN);
    # Secure=True би направило бисквитката невидима за самия локален достъп.
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    )

    if run_boot_tasks and not os.path.exists(db.DB_PATH):
        # Чисто нова инсталация с настроена GitHub синхронизация: изтегляме
        # автоматично последната запазена база, за да могат нови служители
        # да заредят вече съществуващите данни веднага, без ръчна стъпка.
        _boot_cfg = appconfig.load_config()
        if _boot_cfg.get("gh_owner") and _boot_cfg.get("gh_repo") and _boot_cfg.get("gh_token"):
            try:
                backup.pull_db(
                    _boot_cfg["gh_owner"], _boot_cfg["gh_repo"], _boot_cfg["gh_token"],
                    _boot_cfg.get("gh_branch", "main") or "main",
                    _boot_cfg.get("gh_path", "pacho_logistic.db") or "pacho_logistic.db",
                    db.DB_PATH,
                )
            except Exception:
                pass  # без интернет или друга грешка — просто тръгваме с нова база

    db.init_db()

    _register_globals(app)
    _register_hooks(app)

    # Регистрацията на routes_* модулите (внасяне на всеки модул тук, не на
    # ниво файл, за да останат db.init_db()/config зависимостите им заредени
    # едва СЛЕД горните стъпки) става от app.py (входната точка), за да
    # избегнем кръгов внос (routes_*.py внасят appcore; appcore не бива да
    # внася routes_*.py обратно).
    return app


def _register_globals(app):
    @app.context_processor
    def inject_globals():
        return {
            "APP_NAME": APP_NAME,
            "APP_VERSION": __version__,
            "current_year": date.today().year,
            "today": date.today().isoformat(),
            "has_logo": branding.logo_path() is not None,
        }

    @app.template_filter("barcode")
    def barcode_filter(code, height=55, responsive=False):
        return Markup(code128_svg(code, height=height, responsive=responsive))

    app.add_template_global(render_icon, name="icon")
    app.add_template_global(_get_csrf_token, name="csrf_token")


def _register_hooks(app):
    app.after_request(_sync_after_write)
    app.before_request(_check_csrf)
    app.before_request(_enforce_password_change)


def _sync_after_write(response):
    """След всяка успешна POST/PUT/DELETE заявка (нов документ, клиент,
    служител, настройка) насрочваме автоматична синхронизация с GitHub
    (ако е включена) — обединена с кратко забавяне, за да не се качва база
    данни при всяко единично поле, а веднъж след кратка пауза в работата."""
    if request.method in ("POST", "PUT", "DELETE") and response.status_code < 400:
        try:
            backup.mark_dirty(appconfig.load_config)
        except Exception:
            pass
    return response


# ---------------------------------------------------------------- auth decorators

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.path))
        if session.get("role") != "admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapped


# ---------------------------------------------------------------- CSRF защита
# Всяка POST/PUT/PATCH/DELETE заявка трябва да носи токен, съвпадащ с този в
# сесията на потребителя — иначе заявка, стартирана от чужда страница (напр.
# скрита форма/картинка на злонамерен сайт, докато служителят е логнат тук),
# не може да предизвика реално действие (създаване на admin, изтриване на
# документ/клиент и т.н.). Токенът се генерира лениво (при първото четене)
# и се пази в сесията; шаблоните го вграждат чрез {{ csrf_token() }}.
def _get_csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_hex(16)
        session["_csrf_token"] = token
    return token


_CSRF_UNSAFE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


def _check_csrf():
    if request.method not in _CSRF_UNSAFE_METHODS:
        return None
    expected = session.get("_csrf_token")
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRFToken")
    if not expected or not sent or not hmac.compare_digest(str(sent), str(expected)):
        abort(400, description=(
            "Невалидна или изтекла сесия на формата (CSRF защита). "
            "Презаредете страницата и опитайте отново."
        ))
    return None


# ---------------------------------------------------------- задължителна смяна на парола
# Прилага се към акаунти с users.must_change_password = 1 (първоначалният
# 'admin' със засятата парола 'admin123', и всеки служител, на когото друг
# администратор е задал/нулирал паролата). Пренасочва навсякъде другаде към
# „Смяна на парола“, докато служителят не си зададе собствена.
_PASSWORD_CHANGE_EXEMPT_ENDPOINTS = {"change_password", "logout", "static", "barcode_svg"}


def _enforce_password_change():
    if "user_id" not in session or not session.get("must_change_password"):
        return None
    if request.endpoint and request.endpoint not in _PASSWORD_CHANGE_EXEMPT_ENDPOINTS:
        flash("Първо задайте нова парола, преди да продължите.")
        return redirect(url_for("change_password"))
    return None


# ---------------------------------------------------------------- общи помощни функции

def form_data(exclude=("csrf_token", "items_json")):
    """Всички полета от формата като речник (за съхранение в JSON)."""
    return {k: v.strip() for k, v in request.form.items() if k not in exclude}


def load_clients(con):
    return con.execute("SELECT * FROM clients ORDER BY name COLLATE NOCASE").fetchall()


def clients_json(clients, con=None):
    """JSON списък с клиентите за автопопълване във формите. Ако е подаден
    отворен con (ПРЕДИ да се затвори — виж cmr_new), вгражда за всеки
    клиент и списъка му с пунктове за разтоварване (unload_points), за да
    може ЧМР формата да ги предложи за избор без допълнителна заявка.

    Резултатът се вгражда directно в <script> блок в шаблоните (с |safe —
    виж cmr_form.html и др.), затова минава през
    jsonutil.dumps_for_inline_script вместо обикновен json.dumps: иначе
    име/адрес на клиент, съдържащ "</script><script>...", би прекъснало
    блока и изпълнило произволен JS за всеки, отворил формата (stored XSS)."""
    data = [dict(c) for c in clients]
    points_map = (db.get_unload_points_map(con, [c["id"] for c in data])
                  if con is not None and data else {})
    for c in data:
        c["unload_points"] = [
            {k: p.get(k, "") for k in ("label", "address", "city", "postcode", "country")}
            for p in points_map.get(c["id"], [])
        ]
    return jsonutil.dumps_for_inline_script(data)


def parse_items():
    """Редовете от таблицата с артикули, подадени като JSON от формата."""
    raw = request.form.get("items_json", "[]")
    try:
        items = json.loads(raw)
    except ValueError:
        items = []
    return items if isinstance(items, list) else []


def save_document(con, doc_type, data):
    number, year, seq, barcode = db.next_number(con, doc_type)
    data["number"] = number
    data["barcode"] = barcode
    cur = con.execute(
        "INSERT INTO documents (doc_type, number, year, seq, barcode, data, created_by)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (doc_type, number, year, seq, barcode,
         json.dumps(data, ensure_ascii=False), session["user_id"]),
    )
    con.commit()
    return cur.lastrowid


def fetch_document(con, doc_id):
    row = con.execute(
        "SELECT d.*, u.full_name AS author FROM documents d"
        " LEFT JOIN users u ON u.id = d.created_by WHERE d.id = ?",
        (doc_id,),
    ).fetchone()
    if row is None:
        abort(404)
    return row, json.loads(row["data"])


# ---------------------------------------------------------------- предварителен преглед
# Прегледите се показват през POST (формата подава още незаписаните данни).
# Ако страницата се рендира директно като отговор на този POST, презареждане
# ѝ (F5), връщане/възстановяване на раздел от браузъра, или Windows
# автоматично възстановяване на затворен прозорец, кара браузъра да се
# опита да ПОВТОРИ същата POST заявка — което дава „Повторно изпращане на
# формуляра?“ или направо ERR_CACHE_MISS. Затова тук ползваме POST →
# съхрани → пренасочи → GET: POST-ът пази данните временно на сървъра под
# случаен токен и пренасочва към обикновен GET адрес, който само ги чете —
# презареждане/връщане назад там е напълно безопасно.
_preview_store = {}
_PREVIEW_TTL = 1800  # 30 минути — достатъчно за преглед, без да трупа памет за постоянно
# Пази _preview_store от надпревара между заявки, обслужвани от различни
# нишки на Flask dev/production сървъра (виж M5 — несинхронизирани
# споделени глобални променливи в оригиналния app.py).
_preview_lock = threading.Lock()


def _cleanup_previews():
    now = time.time()
    with _preview_lock:
        for token in [t for t, (exp, _k, _p) in _preview_store.items() if exp < now]:
            del _preview_store[token]


def _store_preview(kind, payload):
    _cleanup_previews()
    token = secrets.token_urlsafe(16)
    with _preview_lock:
        _preview_store[token] = (time.time() + _PREVIEW_TTL, kind, payload)
    return token


def _get_preview(token, kind):
    """Чете преглед по токен, БЕЗ да го трие — трябва да остане валиден за
    многократно презареждане/връщане назад, докато не изтече (_PREVIEW_TTL),
    иначе първото презареждане би счупило точно проблема, който поправяме."""
    _cleanup_previews()
    with _preview_lock:
        entry = _preview_store.get(token)
    if entry is None or entry[1] != kind:
        return None
    return entry[2]


def render_preview(doc_type, data):
    """Приема POST-а с still-незаписаните данни на формата, пази ги временно
    на сървъра и пренасочва към GET адрес, който показва документа както ще
    изглежда при печат — БЕЗ да го запазва в базата и БЕЗ да изразходва
    пореден номер. GET адресът е безопасен за презареждане/връщане назад."""
    token = _store_preview("doc", (doc_type, data))
    return redirect(url_for("preview_document", token=token))
