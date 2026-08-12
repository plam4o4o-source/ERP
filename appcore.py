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
import decimal
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from datetime import date, datetime, timedelta
from functools import wraps

from flask import Flask, abort, flash, g, redirect, request, session, url_for
from flask_babel import Babel
from flask_babel import gettext as _
from markupsafe import Markup
from werkzeug.exceptions import HTTPException

import applog
import backup
import branding
import config as appconfig
import db
import jsonutil
import updater
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
    "invoice_br": "invoice_br_print.html",
    "invoice_no": "invoice_no_print.html",
    "invoice_dubai": "invoice_dubai_print.html",
}

FORM_TEMPLATES = {
    "cmr": "cmr_form.html",
    "packing": "packing_form.html",
    "pallet": "pallet_form.html",
    "waybill": "waybill_form.html",
    "dualuse": "dualuse_form.html",
    "export_it": "export_it_form.html",
    "invoice_br": "invoice_br_form.html",
    "invoice_no": "invoice_no_form.html",
    "invoice_dubai": "invoice_dubai_form.html",
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
        # Заявка: „подразбиране да е включен английски в опаковъчен лист,
        # ЧМР, палетна карта“ — за разлика от waybill/dualuse/export_it
        # (подразбиране "bg" по-долу), тези три стартират на "en", както
        # трите фактури. sender_lang_toggle си остава наличен — операторът
        # пак може да превключи обратно към БГ с един клик.
        "default_sender_lang": "en",
    },
    "packing": {
        "form_template": FORM_TEMPLATES["packing"],
        "needs_items": True,
        "embed_unload_points": False,
        "success_message": "Опаковъчен лист № %s е издаден и запазен.",
        "default_sender_lang": "en",
    },
    "pallet": {
        "form_template": FORM_TEMPLATES["pallet"],
        "needs_items": True,
        "embed_unload_points": False,
        "success_message": "Палетна карта № %s е издадена и запазена.",
        "default_sender_lang": "en",
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
    # Фактурите се различават от останалите типове по две неща (виж
    # заявката): номерът се въвежда РЪЧНО (manual_number_field), а формата
    # им ползва отделната адресна книга за фактури, не общата с клиентите
    # (invoice_clients). И двете са данни тук, а не разклонения в
    # _document_new, по същата причина, поради която целият регистър
    # съществува.
    "invoice_br": {
        "form_template": FORM_TEMPLATES["invoice_br"],
        "needs_items": True,
        "embed_unload_points": False,
        "manual_number_field": "invoice_number",
        "invoice_clients": True,
        "success_message": "Фактура за Бразилия № %s е издадена и запазена.",
        # Заявка: „във фактурите за Бразилия, Норвегия, Дубай да се добави
        # опция за изпращач Bg/EN, подразбиране да е английски“ — за
        # разлика от другите 6 документа (подразбиране "bg"), тук
        # sender_lang_toggle стартира на "en" (виж _document_new).
        "default_sender_lang": "en",
    },
    "invoice_no": {
        "form_template": FORM_TEMPLATES["invoice_no"],
        "needs_items": True,
        "embed_unload_points": False,
        "manual_number_field": "invoice_number",
        "invoice_clients": True,
        "success_message": "Фактура за Норвегия № %s е издадена и запазена.",
        "default_sender_lang": "en",
    },
    "invoice_dubai": {
        "form_template": FORM_TEMPLATES["invoice_dubai"],
        "needs_items": True,
        "embed_unload_points": False,
        "manual_number_field": "invoice_number",
        "invoice_clients": True,
        "success_message": "Фактура за Дубай № %s е издадена и запазена.",
        "default_sender_lang": "en",
    },
}

# Типовете отпреди фактурите нямат тези ключове — попълваме ги веднъж тук,
# за да може _document_new да ги чете безусловно, вместо всяко извикване
# да ползва .get(...) с подразбиране.
for _flow in DOCUMENT_FLOWS.values():
    _flow.setdefault("manual_number_field", None)
    _flow.setdefault("invoice_clients", False)
    _flow.setdefault("default_sender_lang", "bg")


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


#: Одит (находки К6/С1): единен, стриктен разбор на число от свободно
#: въведен текст (количество/тегло/цена). Позволени са само цифри и
#: НАЙ-МНОГО ЕДИН десетичен разделител (запетая ИЛИ точка) — структурно
#: изключва „nan“/„inf“/„Infinity“ (приемани преди от голия float(), вижте
#: по-долу), както и текст с разделител на хилядите (напр. „1,234.56“ или
#: „1.234,56“) — те са двусмислени без да се познае locale-ът на подадения
#: текст, затова се ОТХВЪРЛЯТ, вместо тихо да се разчетат грешно (напр. със
#: сгрешен фактор 1000). Точно същата логика (буква по буква) е приложена и
#: в браузъра — виж parseDecimal() в static/app.js — за да не могат живата
#: сума на екрана и записаният в документа резултат да излязат РАЗЛИЧНИ
#: числа за един и същ въведен текст: преди тази поправка JS-ката
#: `parseFloat` четеше само водещите цифри и мълчаливо пренебрегваше
#: остатъка (напр. „12 кг“ → 12), докато Python изискваше ЦЯЛОТО поле да е
#: валиден `float()` литерал — резултат: екранът показваше сума, а готовата
#: фактура излизаше с ПРАЗНА клетка за същия ред, без никакво предупреждение.
_DECIMAL_RE = re.compile(r"^-?\d+([.,]\d+)?$")


def _parse_decimal(value):
    """Връща float или None — вижте обяснението на _DECIMAL_RE по-горе."""
    if value is None:
        return None
    text = re.sub(r"\s+", "", str(value).strip())
    if not text or not _DECIMAL_RE.match(text):
        return None
    return float(text.replace(",", "."))


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
        n = _parse_decimal(it.get("qty"))
        # Одит (находка С1): отрицателно количество се третира като
        # невалиден ред (пропуска се), не се изважда мълчаливо от сумата —
        # вижте същото решение в _to_number по-горе за пълното обяснение.
        if n is not None and n >= 0:
            total += n
            has_any = True
    if not has_any:
        return ""
    return str(int(total)) if total == int(total) else str(total)


def _to_number(value):
    """Толерантно (но СТРИКТНО откъм формат — виж _parse_decimal по-горе)
    четене на число от свободно текстово поле (количество, тегло, цена) —
    приема и десетична запетая, и точка, но не и „боклук“ след числото или
    nan/inf. Връща None при празна/невалидна/ОТРИЦАТЕЛНА стойност, за да
    може извикващият да реши какво да покаже. Обща основа на изчисленията
    по фактурите по-долу — единствената употреба в проекта е точно за
    количество/тегло/единична цена, затова отрицателните числа тук СЕ
    ТРЕТИРАТ КАТО НЕВАЛИДНИ (одит, находка С1: преди тази поправка
    `qty=-5` минаваше напълно мълчаливо и даваше отрицателна „Обща цена“ на
    ред от търговска фактура — количество/цена под нула няма смисъл в тази
    предметна област и почти сигурно е печатна грешка, не съзнателно
    въведена стойност)."""
    n = _parse_decimal(value)
    return None if (n is not None and n < 0) else n


def _fmt_amount(value, decimals=2):
    """Число към текст за бланка: закръглено, без излишни нули накрая, но
    без да губи стойността (напр. 45.3 → „45.3“, 0.10346 → „0.10346“ при
    decimals=5). Празно при None.

    ЗАБЕЛЕЖКА: ползвана е за количество/тегло (където „2“ вместо „2.000“ е
    по-четимо и желано) — НЕ за пари (виж _fmt_money по-долу за фактурните
    суми, находка С1: маха-нето на завършващите нули там би дало
    счетоводно нестандартно „1234.5 €“ вместо „1234.50 €“)."""
    if value is None:
        return ""
    text = ("%." + str(decimals) + "f") % value
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


#: Одит (находка С1): паричните суми на фактурите вече минават през
#: decimal.Decimal, ПОСТРОЕН ДИРЕКТНО ОТ ОРИГИНАЛНИЯ ТЕКСТОВ ВХОД (не през
#: float като преди) — float() съхранява десетични стойности като двоично
#: приближение (напр. 0.145 реално се пази като 0.1449999999999999...),
#: затова стандартното `"%.2f" % value` закръгляше НАДОЛУ стойности,
#: очаквано (от човек, смятащ на ръка) закръгляеми НАГОРЕ — напр. 10 реда
#: по 7×0.145 даваха ред „1.01“/сбор „10.1“ вместо коректните 1.02/10.15.
#: Decimal("0.145") пази стойността ТОЧНО, а ROUND_HALF_UP възпроизвежда
#: обичайното "училищно"/търговско закръгляне.
_CENTS = decimal.Decimal("0.01")


def _parse_decimal_exact(value):
    """Като _parse_decimal (същата стриктна валидация — вижте _DECIMAL_RE),
    но връща decimal.Decimal вместо float, за точни парични изчисления."""
    if value is None:
        return None
    text = re.sub(r"\s+", "", str(value).strip())
    if not text or not _DECIMAL_RE.match(text):
        return None
    try:
        n = decimal.Decimal(text.replace(",", "."))
    except decimal.InvalidOperation:
        return None
    # Отрицателна цена/количество няма смисъл в тази предметна област — вижте
    # същото решение в _to_number по-горе за пълното обяснение (находка С1).
    return None if n < 0 else n


def _fmt_money(value):
    """Парично форматиране: ТОЧНО 2 знака след десетичната запетая, ВИНАГИ
    (за разлика от _fmt_amount, който маха завършващите нули) —
    ROUND_HALF_UP закръгляне на decimal.Decimal стойност. Празно при None."""
    if value is None:
        return ""
    if not isinstance(value, decimal.Decimal):
        value = decimal.Decimal(str(value))
    return str(value.quantize(_CENTS, rounding=decimal.ROUND_HALF_UP))


def invoice_row_total(item):
    """Обща цена на ред от фактура = количество × единична цена. В
    приложените Excel образци това е формула в колоната „Total Price“;
    тук се смята на момента, за да не може да се разминe с реда, ако
    количеството или цената се редактират по-късно. Празна при липсващо
    количество или цена."""
    if not isinstance(item, dict):
        return ""
    qty = _parse_decimal_exact(item.get("qty"))
    price = _parse_decimal_exact(item.get("unit_price"))
    if qty is None or price is None:
        return ""
    return _fmt_money(qty * price)


def invoice_row_weight(item):
    """Общо нето тегло на ред = нето тегло за брой × количество (колоната,
    която в образеца за Бразилия стои най-вдясно). Празна при липсващо
    тегло или количество."""
    if not isinstance(item, dict):
        return ""
    qty, weight = _to_number(item.get("qty")), _to_number(item.get("net_weight"))
    if qty is None or weight is None:
        return ""
    return _fmt_amount(weight * qty, decimals=3)


def invoice_totals(items):
    """Обобщените суми под таблицата на фактурата: общо количество, обща
    стойност и общо нето тегло. Връща речник с вече форматирани текстове
    (празен низ, ако няма нито един годен ред за съответната сума), за да
    може шаблонът просто да ги изпише. Пропуска развалени/празни редове,
    вместо да гърми — същата толерантност като pallet_total_qty."""
    total_qty = total_weight = 0.0
    total_price = decimal.Decimal("0")
    has_qty = has_price = has_weight = False
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        qty = _to_number(it.get("qty"))
        if qty is not None:
            total_qty += qty
            has_qty = True
        # ВАЖНО: сумата се трупа от ТОЧНО ТЕЗИ стойности, които се
        # отпечатват на самите редове (invoice_row_total), не от суровите
        # произведения. Иначе фактурата си противоречи: при 10 реда по
        # 0.005 всеки ред се изписва „0.01“ (сбор 0.10), а необработената
        # сума дава „0.05“ — тоест сборът долу не отговаря на видимите
        # редове. За търговска фактура, която върви към клиент и митница,
        # това е недопустимо, затова сумираме закръглените редове (вече
        # Decimal, точно — виж invoice_row_total/_fmt_money по-горе).
        row_price_text = invoice_row_total(it)
        if row_price_text:
            total_price += decimal.Decimal(row_price_text)
            has_price = True
        row_weight = _to_number(invoice_row_weight(it))
        if row_weight is not None:
            total_weight += row_weight
            has_weight = True
    return {
        "qty": _fmt_amount(total_qty, decimals=3) if has_qty else "",
        "price": _fmt_money(total_price) if has_price else "",
        "weight": _fmt_amount(total_weight, decimals=3) if has_weight else "",
    }


def build_draft_doc(doc_type, data, author):
    """Псевдо-документът за „Предварителен преглед“ (още не е записан в
    базата, но печатните шаблони очакват обект с number/barcode/author).

    Живее ТУК, а не в app.py, защото същата конструкция трябва на две
    места (app.preview_document и тестовата fixture в conftest) и лесно се
    разминаваха.

    При типовете с ръчен номер (фактурите) прегледът показва РЕАЛНО
    въведения номер, а не надписа „ПРЕДВАРИТЕЛЕН ПРЕГЛЕД / DRAFT“ — иначе
    операторът не може да провери на прегледа точно това, което сам е
    написал. Че документът още не е издаден, си личи от воден знак „DRAFT“
    върху бланката (виж _macros.draft_watermark)."""
    manual_field = DOCUMENT_FLOWS.get(doc_type, {}).get("manual_number_field")
    number = "ПРЕДВАРИТЕЛЕН ПРЕГЛЕД / DRAFT"
    if manual_field:
        typed = (data.get(manual_field) or "").strip()
        if typed:
            number = typed
    return {
        "id": 0,
        "doc_type": doc_type,
        "number": number,
        "barcode": "DRAFT-PREVIEW",
        "author": author,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def invoice_bank_line(settings):
    """Банковият ред на фактурата, сглобен от данните на фирмата в
    „Фирма изпращач“ — заявка: „във фирма изпращач добави IBAN-а на
    фирмата; да се зарежда във фактурите“.

    Форматът следва приложените образци:
        „IBAN : BG26… SWIFT : BPBIBGSF / Postbank Gabrovo-Bulgaria /“
    Пропуска липсващите части (само IBAN, без SWIFT/банка, си е напълно
    редовен ред) и връща празен низ, ако нищо не е попълнено — тогава
    полето на фактурата просто остава за ръчно въвеждане, вместо да излезе
    „IBAN :  SWIFT :“ с празни стойности."""
    settings = settings or {}
    iban = (settings.get("sender_iban") or "").strip()
    swift = (settings.get("sender_swift") or "").strip()
    bank = (settings.get("sender_bank") or "").strip()
    parts = []
    if iban:
        parts.append("IBAN : %s" % iban)
    if swift:
        parts.append("SWIFT : %s" % swift)
    line = "    ".join(parts)
    if bank:
        line = ("%s   / %s /" % (line, bank)) if line else "/ %s /" % bank
    return line


def format_eur_amount(value):
    """Форматира парична стойност с „€“ в края — заявка: „да остане валута
    само евро“ (без избор на валута/поле за валута). Изложена и като Jinja
    global (format_eur), ползвана от печатния/PDF/Excel износ на
    товарителницата (transport_price/extra_costs) за еднакво показване
    навсякъде, без да принуждава конкретен числов формат при въвеждане.

    Толерантна към вече въведени по-стари данни (свободен текст, някои може
    да съдържат "лв." или вече изрично "€"/EUR) — само добавя „€“, ако
    стойността вече не завършва на такъв знак/съкращение; не пренаписва
    съществуващи стойности насила.

    ВАЖНО за старите данни в лева: полето беше свободен текст преди тази
    промяна, затова напълно реално е в стари товарителници да пише „500 лв.“
    Такава стойност НЕ получава „€“ — иначе на бланката щеше да излезе
    „500 лв. €“, тоест две валути наведнъж, което е по-подвеждащо от това
    просто да остане както е било въведено. Не я преобразуваме и по курс:
    програмата не знае към коя дата се отнася сумата, а мълчаливо
    преизчислена цена в счетоводен документ е недопустима."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    upper = text.upper()
    if text[-1] == "€" or upper.endswith("EUR"):
        return text
    # Стари стойности, вече означени в друга валута — оставяме ги както са.
    if upper.endswith("BGN") or upper.rstrip(".").endswith("ЛВ"):
        return text
    return "%s €" % text


_ISO_DATE_RE = re.compile(
    r"^(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})"
    r"(?:[ T](?P<h>\d{2}):(?P<mi>\d{2})(?::\d{2})?)?$"
)


def format_bg_date(value):
    """Преобразува ISO дата/дата-час ("ГГГГ-ММ-ДД" или "ГГГГ-ММ-ДД ЧЧ:ММ[:СС]"
    — форматът, връщан от SQLite `datetime('now','localtime')`/`date()` и
    подаван от `<input type="date">`) в изгледа „ДД.ММ.ГГГГ“ (или
    „ДД.ММ.ГГГГ ЧЧ:ММ“, ако имаше час) — заявка: „в цялата програма
    промени изгледа на дата да е ден.месец.година“. Изложена и като Jinja
    global (format_date).

    НЕ пипа самите `<input type="date">` елементи (браузърът винаги ги
    показва по своя локал/формат, независимо от сървъра — техническо
    ограничение на HTML5, не пропуск тук) и НЕ пипа стойността, записана в
    базата/подавана към сървъра (винаги остава ISO — само visual слой).

    Толерантна към вече нестандартен/свободен текст — връща стойността
    непроменена, ако не разпознае ISO формат (напр. вече ръчно въведена
    друга дата в по-стари документи, или изобщо не е дата)."""
    if not value:
        return value
    text = str(value).strip()
    m = _ISO_DATE_RE.match(text)
    if not m:
        return value
    result = "%s.%s.%s" % (m.group("d"), m.group("mo"), m.group("y"))
    if m.group("h") is not None:
        result += " %s:%s" % (m.group("h"), m.group("mi"))
    return result


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
            # В6: наличен на ВСЯКА страница (не само таблото), защото
            # автоматичният рестарт може да настъпи, докато потребителят е
            # на съвсем друг екран (напр. попълва форма) — вижте
            # updater.get_pending_restart и base.html за банера.
            "pending_restart": updater.get_pending_restart(),
        }

    @app.template_filter("barcode")
    def barcode_filter(code, height=55, responsive=False):
        # code128_svg() вече XML-екранира текста преди да го вгради в SVG
        # (виж barcode128._xml_escape) — безопасно за Markup.
        return Markup(code128_svg(code, height=height, responsive=responsive))  # nosec B704

    app.add_template_global(render_icon, name="icon")
    app.add_template_global(_get_csrf_token, name="csrf_token")
    app.add_template_global(pallet_total_qty, name="pallet_total_qty")
    app.add_template_global(format_eur_amount, name="format_eur")
    app.add_template_global(format_bg_date, name="format_date")
    app.add_template_global(invoice_bank_line, name="invoice_bank_line")
    app.add_template_global(invoice_row_total, name="invoice_row_total")
    app.add_template_global(invoice_row_weight, name="invoice_row_weight")
    app.add_template_global(invoice_totals, name="invoice_totals")


def _register_hooks(app):
    app.after_request(_sync_after_write)
    app.before_request(_check_csrf)
    app.before_request(_enforce_password_change)
    app.register_error_handler(413, _request_too_large)
    app.register_error_handler(Exception, _handle_unexpected_error)
    app.teardown_appcontext(_close_db)


def _handle_unexpected_error(exc):
    """Одит (находка В1 — корен на голяма част от доклада, плюс В10
    „database is locked“): ПРЕДИ тази поправка приложението нямаше НИТО
    ЕДИН регистриран обработчик за грешки освен 413 (_request_too_large
    по-горе) — всяко друго необработено изключение (повреден JSON,
    „database is locked“, TypeError при липсващо поле, каквото и да е)
    стигаше директно до потребителя като гол Werkzeug „Internal Server
    Error“, БЕЗ съобщение, БЕЗ следа в лог. Именно затова при поправката на
    PDF срива в v3.57.0 потребителят можеше да докладва само скрийншот на
    бял екран — нямаше НИКАКВА диагностика никъде.

    Съзнателно НЕ пипаме HTTPException (abort(404)/abort(403)/413/...) —
    Werkzeug вече ги показва коректно сами със собствени, смислени
    страници; тук ги връщаме непроменени, вместо да ги подменим с общото
    съобщение „нещо се обърка“, което би било подвеждащо за напр. 404.

    За sqlite3.OperationalError с „database is locked“/„busy“ показваме
    по-конкретно, разбираемо съобщение (обяснява ПРИЧИНАТА — друг
    едновременен запис, а не просто "грешка"), защото това е честа и
    очаквана ситуация в мрежов режим с няколко служителя, не изключение."""
    if isinstance(exc, HTTPException):
        return exc
    applog.log_exception(
        "appcore._handle_unexpected_error: необработено изключение в %s %s"
        % (request.method, request.path))
    if isinstance(exc, sqlite3.OperationalError) and (
            "locked" in str(exc).lower() or "busy" in str(exc).lower()):
        flash(_("Базата данни е временно заета от друга едновременна операция "
               "(напр. друг служител записва в момента). Изчакайте няколко "
               "секунди и опитайте отново."), "error")
    else:
        flash(_("Възникна неочаквана грешка. Опитайте отново — ако продължава, "
               "съобщете на администратор."), "error")
    try:
        target = request.referrer or url_for("dashboard")
    except Exception:
        target = url_for("dashboard")
    return redirect(target)


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
    flash(_("Файлът е твърде голям (максимум 25 MB). Изберете по-малък файл."), "error")
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

def _session_user_deactivated_or_missing():
    """Одит (находка В3, висок риск): при деактивиране/изтриване на
    служител (или смяна на ролята му admin<->employee) от администратор,
    ВЕЧЕ ОТВОРЕНАТА сесия на засегнатия потребител преди тази поправка
    оставаше напълно валидна до края на бисквитката — login_required/
    admin_required проверяваха САМО session["user_id"]/session["role"],
    записани еднократно при вход, без никаква повторна справка към
    базата. Личeн пример от одита: деактивиран служител продължаваше да
    издава документи с вече неактивния си акаунт; admin, свален до
    "employee" от друг администратор, пазеше пълни администраторски права
    до край на сесията си.

    Тук на ВСЯКА заявка презареждаме актуалния ред от users и: (а)
    връщаме True (сесията се прекратява), ако потребителят вече не
    съществува или active=0; (б) синхронизираме session["role"] с
    текущата стойност в базата, за да важи веднага промяна на ролята,
    направена междувременно от друг администратор — без това admin_
    required по-долу би продължил да сравнява спрямо остарялата стойност
    в бисквитката."""
    con = get_db()
    row = con.execute("SELECT role, active FROM users WHERE id = ?",
                       (session.get("user_id"),)).fetchone()
    if row is None or not row["active"]:
        session.clear()
        return True
    if row["role"] != session.get("role"):
        session["role"] = row["role"]
    return False


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.path))
        if _session_user_deactivated_or_missing():
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.path))
        if _session_user_deactivated_or_missing():
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
        flash(_("Първо задайте нова парола, преди да продължите."), "warning")
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


def save_document(con, doc_type, data, manual_number=None, commit=True):
    """Записва нов документ и му дава номер.

    `manual_number` (само за фактурите — заявка: „номера на фактурата да се
    вписват ръчно“) замества автоматично генерирания номер с въведения от
    оператора. Вътрешният брояч и баркодът ВСЕ ПАК се генерират: колоната
    `barcode` е UNIQUE NOT NULL в схемата и служи за вътрешна
    идентификация, а `seq`/`year` пазят реда на издаване. При фактурите
    баркодът просто не се показва никъде (нито на бланката, нито във
    формата) — заявка: „без баркод на фактурите“.

    Празен/само интервали `manual_number` пада обратно към автоматичния
    номер, вместо документът да остане без номер изобщо.

    `commit=False` (одит, находка В14): масовото издаване на палетни карти
    (routes_pallet_extra.pallet_bulk_issue) записва по няколко документа в
    ЕДИН цикъл — с подразбиращия се commit=True всеки документ се
    фиксираше ОТДЕЛНО, т.е. грешка по средата на партида от 10 карти
    оставяше първите 5 трайно записани, а последните 5 — изгубени, без
    ясен начин операторът да разбере кои точно номера реално са издадени.
    С commit=False извикващият (bulk_issue) поема отговорността да commit-
    не/rollback-не ЦЯЛАТА партида наведнъж — вижте коментара там."""
    number, year, seq, barcode = db.next_number(con, doc_type)
    if manual_number is not None and str(manual_number).strip():
        number = str(manual_number).strip()
    data["number"] = number
    data["barcode"] = barcode
    # Случаен (128-битов), непредвидим токен за публичен преглед БЕЗ вход
    # през QR код на бланката (виж db.SCHEMA/миграция _m002_public_token и
    # routes_documents.public_document_view за пълното обяснение) —
    # генериран за ВСЕКИ документ (включително фактури), макар печатните
    # шаблони на фактурите да не показват QR за него (заявка: само вече
    # баркодираните видове документи) — по-просто и еднообразно, отколкото
    # да разклоняваме самия INSERT по тип документ.
    public_token = secrets.token_hex(16)
    cur = con.execute(
        "INSERT INTO documents (doc_type, number, year, seq, barcode, public_token,"
        " data, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (doc_type, number, year, seq, barcode, public_token,
         json.dumps(data, ensure_ascii=False), session["user_id"]),
    )
    if commit:
        con.commit()
    return cur.lastrowid


def safe_json_data(raw):
    """Безопасен разбор на съдържанието на документна `data` колона.

    Одит (находка К2, критична): преди тази поправка над 9 места из целия
    проект (таблото, списъкът с документи, историята на клиента, износът,
    прегледът на самия документ...) четяха тази колона с директен, незащитен
    `json.loads(row["data"])`. Един-единствен ред с повреден/отрязан JSON
    (напр. заради прекъснат мрежов диск по средата на запис, спиране на
    тока, или находка К1 по-горе) събаряше не само прегледа на ТОЗИ
    документ, а и таблото, и целия списък с документи — потребителят
    оставаше БЕЗ начин дори да изтрие счупения запис от интерфейса (самата
    страница, на която е бутонът „Изтрий“, също гърмеше).

    Тук вместо да оставим изключението да пропътува чак до Flask, връщаме
    празен речник и логваме проблема — извикващият код тогава вижда просто
    документ с непопълнени полета (форматиран нормално, полетата излизат
    като „—“), вместо блокираща грешка. Невалиден, но синтактично коректен
    JSON, който не е речник (напр. `null`, `[]`, число) също се третира
    като „няма данни“, вместо да продължи да гърми по-надолу (AttributeError
    при .get(...))."""
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError) as exc:
        applog.log_exception("appcore.safe_json_data: повреден JSON в данните на документ (%s)" % exc)
        return {}
    return parsed if isinstance(parsed, dict) else {}


def fetch_document(con, doc_id):
    row = con.execute(
        "SELECT d.*, u.full_name AS author FROM documents d"
        " LEFT JOIN users u ON u.id = d.created_by WHERE d.id = ?",
        (doc_id,),
    ).fetchone()
    if row is None:
        abort(404)
    return row, safe_json_data(row["data"])


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
