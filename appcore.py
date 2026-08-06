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

from flask import Flask, abort, flash, g, redirect, request, session, url_for
from flask_babel import Babel
from flask_babel import gettext as _
from markupsafe import Markup

import applog
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
#     "ЧМР товарителница"). ВНИМАНИЕ (i18n): низовете тук НЕ се обвиват с
#     _() на това ниво — речникът е ниво модул и се зарежда еднократно при
#     импорт, преди Flask-Babel да знае текущия locale. Затова превода се
#     прави в routes_documents.py на мястото на flash()-а:
#     flash(_(flow["success_message"]) % ...). pybabel extract НЕ ги
#     засича автоматично (динамично търсене по ключ, не литерал в _()),
#     затова тези 5 низа се добавят РЪЧНО в .po каталозите.
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


def _select_locale():
    """Кой език на интерфейса да се ползва за текущата заявка — вика се
    от Flask-Babel за всяка заявка (locale_selector). Ред на избор:

    1. session["lang"] — задава се или от личния избор на потребителя в
       Настройки (routes_settings.my_settings, пази се трайно в
       user_settings в БД, важи на всяко устройство при следващ вход —
       виж routes_auth.login), или временно от превключвателя в логин
       панела ПРЕДИ вход (важи само за текущата сесия/браузър, докато
       потребителят не влезе с профил с личен избор).
    2. db.DEFAULT_LANGUAGE ("bg") — ако сесията изобщо няма зададен език
       (съвсем нова сесия, никой превключвател не е ползван)."""
    lang = session.get("lang")
    return lang if lang in db.LANGUAGES else db.DEFAULT_LANGUAGE


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
        _translations_dir = os.path.join(_bundle, "translations")
    else:
        app = Flask(__name__)
        _translations_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "translations")
    app.secret_key = db.get_secret_key()
    app.json.ensure_ascii = False

    # Многоезичен интерфейс (БГ/EN/TR) — Flask-Babel добавя автоматично
    # {{ _('...') }} и {% trans %} във всички шаблони (configure_jinja=True
    # по подразбиране). Печатните документи НЕ минават през това — техните
    # BG/EN надписи са твърдо вградени в самите print шаблони (законово
    # изискване), напълно отделно от избрания език на интерфейса. Виж
    # _select_locale() по-долу за реда на избор на език.
    Babel(app, default_locale=db.DEFAULT_LANGUAGE,
          default_translation_directories=_translations_dir,
          locale_selector=_select_locale)

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
        # Находка M9: без горна граница, качен от служител (умишлено или по
        # грешка) огромен файл (лого, Excel импорт на палети) би стигнал
        # изцяло в паметта на процеса преди Flask изобщо да го подаде на
        # хендлъра — риск за наличността (out-of-memory), не само за диска.
        # 25 MB е достатъчно щедро за лого изображение или Excel файл с
        # хиляди редове, но спира явно погрешен/злонамерен ъплоуд рано.
        MAX_CONTENT_LENGTH=25 * 1024 * 1024,
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
                # без интернет или друга грешка — просто тръгваме с нова база
                applog.log_exception("appcore.create_app: неуспешно първоначално изтегляне на базата от GitHub")

    db.init_db()

    _register_globals(app)
    _register_hooks(app)

    # Регистрацията на routes_* модулите (внасяне на всеки модул тук, не на
    # ниво файл, за да останат db.init_db()/config зависимостите им заредени
    # едва СЛЕД горните стъпки) става от app.py (входната точка), за да
    # избегнем кръгов внос (routes_*.py внасят appcore; appcore не бива да
    # внася routes_*.py обратно).
    return app


def pallet_total_qty(items):
    """„Общ брой“ на палетна карта — сума на количествата (полето 'qty') от
    редовете ѝ. Заменя старото ръчно въвеждано „Нето, кг“ (виж заявката:
    „нетно тегло замени със общ брой - сумата количество от палетната
    карта“) — изчислява се ВИНАГИ наново от текущите редове, вместо да се
    пази като отделен, лесно остаряващ ръчен запис. Изложена и като Jinja
    global (pallet_total_qty), за да я ползват печатните шаблони и
    формата по абсолютно същия начин, както Excel износа/routes_pallet_extra.
    Толерантна към нечислови/празни стойности — просто ги пропуска, не
    гърми при развален ред."""
    total = 0.0
    has_any = False
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        raw = it.get("qty")
        if raw is None or raw == "":
            continue
        try:
            total += float(str(raw).strip().replace(",", "."))
            has_any = True
        except ValueError:
            continue
    if not has_any:
        return ""
    return str(int(total)) if total == int(total) else str(total)


def _register_globals(app):
    @app.context_processor
    def inject_globals():
        return {
            "APP_NAME": APP_NAME,
            "APP_VERSION": __version__,
            "current_year": date.today().year,
            "today": date.today().isoformat(),
            "has_logo": branding.logo_path() is not None,
            "current_lang": _select_locale(),
            "languages": db.LANGUAGES,
        }

    @app.template_filter("barcode")
    def barcode_filter(code, height=55, responsive=False):
        return Markup(code128_svg(code, height=height, responsive=responsive))

    app.add_template_global(render_icon, name="icon")
    app.add_template_global(_get_csrf_token, name="csrf_token")
    app.add_template_global(pallet_total_qty, name="pallet_total_qty")


def _register_hooks(app):
    app.after_request(_sync_after_write)
    app.before_request(_check_csrf)
    app.before_request(_enforce_password_change)
    app.register_error_handler(413, _request_too_large)
    app.teardown_appcontext(_close_db)


# ---------------------------------------------------------------- връзка с базата (per-request)
# Централизиран жизнен цикъл на връзката (отложено от Фаза 2 към Фаза 3 в
# оригиналния план — виж ПЛАН_ЗА_РАЗРАБОТКА.md — приложено тук). Преди
# всеки routes_*.py хендлър отваряше собствена db.get_db() и я затваряше
# ръчно (`con.close()`) точно преди всеки `return` — лесно за пропускане
# при нов маршрут/нов ранен `return`, и означаваше отделна SQLite връзка
# при всяко повторно извикване в РАМКИТЕ на една и съща заявка (напр.
# routes_auth.login() отваряше con, затваряше я, после отваряше con2 за
# следваща справка). get_db() тук кешира ЕДНА връзка в `flask.g` за
# целия живот на заявката — повторни извиквания връщат СЪЩИЯ обект — и
# _close_db (app.teardown_appcontext) я затваря автоматично след
# отговора, ДОРИ при необработено изключение по средата на хендлъра
# (Flask винаги вика teardown функциите, за разлика от ръчен `con.close()`
# в тялото на функцията, което не се достига при exception). Явните
# `con.commit()` преди запис ОСТАВАТ непроменени навсякъде — teardown САМО
# затваря връзката, никога не commit-ва вместо кода (некомитнати промени
# биха се загубили/rollback-нали при close(), точно както преди).
#
# ВАЖНО: валидно е само вътре в Flask application/request context. Кодът
# извън заявка (фоновата нишка за автоматичен архив — app.py:
# _get_backup_settings, извиквана от backup.start_auto_backup) НЕ минава
# през тук — продължава да ползва db.get_db()/con.close() директно,
# защото `flask.g` не съществува извън контекста на заявка.
def get_db():
    if "db" not in g:
        g.db = db.get_db()
    return g.db


def _close_db(exception=None):
    con = g.pop("db", None)
    if con is not None:
        con.close()


def _request_too_large(exc):
    """Приятелско съобщение при файл над MAX_CONTENT_LENGTH (M9), вместо
    суровата Werkzeug грешка. Връщаме ОБИКНОВЕНО пренасочване (302), не
    413 — браузърът следва Location само при 3xx; 413 тук би показал
    само суровия статус без реално връщане към формата.
    request.referrer пази откъде е дошла заявката (напр. формата за
    лого/Excel импорт), за да пренасочим точно там; ако липсва (директна
    заявка без referrer), падаме към таблото."""
    flash(_("Файлът е твърде голям (максимум 25 MB). Изберете по-малък файл."))
    return redirect(request.referrer or url_for("dashboard"))


def _sync_after_write(response):
    """След всяка успешна POST/PUT/DELETE заявка (нов документ, клиент,
    служител, настройка) насрочваме автоматична синхронизация с GitHub
    (ако е включена) — обединена с кратко забавяне, за да не се качва база
    данни при всяко единично поле, а веднъж след кратка пауза в работата."""
    if request.method in ("POST", "PUT", "DELETE") and response.status_code < 400:
        try:
            backup.mark_dirty(appconfig.load_config)
        except Exception:
            applog.log_exception("appcore._sync_after_write: неуспешно насрочване на синхронизация")
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
        flash(_("Първо задайте нова парола, преди да продължите."))
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
