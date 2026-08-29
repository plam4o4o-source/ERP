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
import collections
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
from urllib.parse import urlsplit

from flask import (Flask, abort, flash, g, has_request_context, redirect,
                   render_template, request, session, url_for)
from flask_babel import Babel
from flask_babel import gettext as _
from markupsafe import Markup
from werkzeug.exceptions import HTTPException

import applog
import branding
import db
import jsonutil
import remote_tunnel
import updater
from barcode128 import code128_svg
from icons import render_icon
from version import __version__


def N_(text):
    """gettext „noop“ маркер — връща низа НЕПРОМЕНЕН, само го прави видим
    за `pybabel extract` (N_ е сред подразбиращите се ключови думи на
    Babel). Ползва се за низове, дефинирани на ниво модул, ПРЕДИ да има
    заявка и текущ locale — реалният превод става на мястото на употреба
    (напр. flash(_(flow["success_message"]) % …) в routes_documents.py).
    Одит (19.08.2026, находка №13)."""
    return text


APP_NAME = "ПачоЛогистик"
MIN_PASSWORD_LENGTH = 8  # прилага се еднакво във всички пътища за задаване на парола

# Одит (12.08.2026, находка №10): реално използваният мрежов порт при
# СТАРТИРАНЕ (app.py __main__) — може да се различава от конфигурирания
# network_port, ако той е бил зает и net.find_available_port е избрал
# резервен (виж app.py). Преди тази поправка system_remote_start() и
# updating.html (routes_admin.py) четяха порта директно от конфигурацията
# — сочеха към грешен/мъртъв порт точно в случая на fallback. app.py
# попълва тази стойност веднъж при стартиране чрез set_runtime_port();
# при тестове (Flask app, създаден директно без да минава през app.py
# __main__) остава None — извикващият пада обратно към конфигурирания
# порт (виж get_runtime_port).
_RUNTIME_STATE = {"port": None}


def set_runtime_port(port):
    """Извиква се от app.py веднага след като реалният порт е определен."""
    _RUNTIME_STATE["port"] = port


def get_runtime_port(default):
    """Реално използваният порт, ако е известен (виж set_runtime_port),
    иначе подаденото подразбиране (обичайно конфигурираният network_port)."""
    return _RUNTIME_STATE["port"] if _RUNTIME_STATE["port"] is not None else default

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
#     flash(_(flow["success_message"]) % ...).
#     Одит (19.08.2026, находка №13): преди това тези низове се добавяха
#     РЪЧНО в .po каталозите — при следващия `pybabel update` те изпаднаха
#     като „остарели“ (#~), защото ги няма в .pot файла, а четирите нови
#     (товарителница + трите фактури) изобщо никога не бяха добавяни.
#     Затова сега са маркирани с N_() по-долу — стандартният gettext
#     „noop“ маркер, който Е сред подразбиращите се ключови думи на Babel
#     и връща низа непроменен, значи pybabel extract вече ги вижда, без
#     нищо в поведението да се променя.
DOCUMENT_FLOWS = {
    "cmr": {
        "form_template": FORM_TEMPLATES["cmr"],
        "needs_items": False,
        "embed_unload_points": True,
        "success_message": N_("ЧМР № %s е издадено и запазено в базата данни."),
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
        "success_message": N_("Опаковъчен лист № %s е издаден и запазен."),
        "default_sender_lang": "en",
    },
    "pallet": {
        "form_template": FORM_TEMPLATES["pallet"],
        "needs_items": True,
        "embed_unload_points": False,
        "success_message": N_("Палетна карта № %s е издадена и запазена."),
        "default_sender_lang": "en",
    },
    "waybill": {
        "form_template": FORM_TEMPLATES["waybill"],
        "needs_items": True,
        "embed_unload_points": False,
        "success_message": N_("Товарителница № %s е издадена и запазена."),
    },
    "dualuse": {
        "form_template": FORM_TEMPLATES["dualuse"],
        "needs_items": True,
        "embed_unload_points": False,
        "success_message": N_("Декларация за двойна употреба № %s е издадена и запазена."),
    },
    "export_it": {
        "form_template": FORM_TEMPLATES["export_it"],
        "needs_items": True,
        "embed_unload_points": False,
        "success_message": N_("Декларация за износ № %s е издадена и запазена."),
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
        "success_message": N_("Фактура за Бразилия № %s е издадена и запазена."),
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
        "success_message": N_("Фактура за Норвегия № %s е издадена и запазена."),
        "default_sender_lang": "en",
    },
    "invoice_dubai": {
        "form_template": FORM_TEMPLATES["invoice_dubai"],
        "needs_items": True,
        "embed_unload_points": False,
        "manual_number_field": "invoice_number",
        "invoice_clients": True,
        "success_message": N_("Фактура за Дубай № %s е издадена и запазена."),
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

    # Бележка (25.08.2026): тук по-рано стоеше автоматично изтегляне на
    # базата от GitHub при чисто нова инсталация. Синхронизацията с GitHub
    # беше премахната по заявка на потребителя — при липсваща база просто
    # се създава нова, локална (db.init_db() по-долу).
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
    гърми при развален ред.

    Одит (12.08.2026, находка №4): преди тази поправка сумата се връщаше
    като суров `str(float)`, без закръгляне — за разлика от ВСИЧКИ други
    суми в модула (invoice_totals/invoice_row_total минават през
    _fmt_amount/_fmt_money). Потвърдено: 0.1 + 0.2 връщаше буквално
    „0.30000000000000004“ (класически float артефакт) в списъка на
    издадени карти (pallet_bulk_result.html) и в Excel/PDF износа на
    самата карта — разминаване с живото (правилно закръглено) JS
    изчисление на екрана, докато потребителят попълва картата
    (static/app.js, sumQtyForDisplay). Сега минава през _fmt_amount със
    същите 3 знака след десетичната запетая, каквито ползва JS еквивалента
    и останалите тегловни суми (invoice_row_weight/invoice_totals).

    Одит (19.08.2026, находка №9): сумирането вече е с decimal.Decimal,
    ПОСТРОЕН ДИРЕКТНО ОТ ТЕКСТОВИЯ ВХОД, и със закръгляне ROUND_HALF_UP —
    точно както JS еквивалентът на екрана. Поправката на №17 (16.08)
    премина към Decimal/BigInt само за РЕДОВИТЕ произведения (цена и
    тегло), а сумарните количества останаха на float: `"%.3f" % 0.0625`
    дава „0.062“ (float форматирането в Python закръгля към ЧЕТНО), докато
    JS показваше „0.063“ — операторът виждаше едно число на екрана, а
    издаденият документ и Excel износът твърдяха друго."""
    total = decimal.Decimal("0")
    has_any = False
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        n = _parse_decimal_exact(it.get("qty"))
        # Одит (находка С1): отрицателно количество се третира като
        # невалиден ред (пропуска се), не се изважда мълчаливо от сумата —
        # вижте същото решение в _to_number по-горе за пълното обяснение.
        if n is not None and n >= 0:
            total += n
            has_any = True
    if not has_any:
        return ""
    return _fmt_amount_exact(total, decimals=3)


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


# Одит (12.08.2026, находка №3): полетата, в които отрицателна стойност
# няма смисъл в тази предметна област (количество/цена/тегло на ред от
# документ) — вижте negative_item_rows по-долу.
#: Одит (19.08.2026, находка №20): колко дълго важи публичният QR адрес на
#: НОВО издаден документ. 180 дни покрива реалния живот на транспортен
#: документ (доставка, рекламация, митническа проверка), без да е вечен.
#: Вече издадените документи имат NULL в public_token_expires_at и остават
#: безсрочни — за да не спрат изведнъж QR кодове върху бланки, които са в
#: движение при клиенти (виж миграция db._m009_public_token_expiry).
PUBLIC_TOKEN_TTL_DAYS = 180


def public_token_expiry(days=None):
    """Момент, до който важи публичният QR адрес, ако се издаде/поднови
    СЕГА — форматиран точно като останалите времена в схемата.

    Одит (22.08.2026, находка №8, средна): находка №20 (19.08) добави
    колоната `public_token_expires_at` и 180-дневния срок, но НИЩО в кода
    не пишеше в тази колона след първоначалното създаване на документа
    (`grep` потвърждава: единственото място беше save_document по-долу).
    Тоест заявената функционалност — операторът да може да ОТНЕМЕ или да
    ПОДНОВИ публичния достъп — изобщо не беше доставена; доставен беше
    само страничният ѝ ефект (бланка, сканирана след 6 месеца, дава гол
    404, а единственият изход беше преиздаване на документа).

    Изнесено като отделна функция, за да е ЕДНА пресметната стойност и за
    издаването, и за подновяването (routes_documents.public_link_renew)."""
    return (datetime.now() + timedelta(days=days or PUBLIC_TOKEN_TTL_DAYS)
            ).strftime("%Y-%m-%d %H:%M:%S")


_NEGATIVE_CHECK_FIELDS = ("qty", "unit_price", "net_weight", "weight")


def negative_item_rows(items):
    """Списък (1-базирани) номера на редове с отрицателна стойност в поне
    едно от полетата qty/unit_price/net_weight/weight.

    Одит (12.08.2026, находка №3): _to_number/invoice_totals вече
    ИЗКЛЮЧВАТ отрицателни редове от изчислените суми долу под таблицата
    (находка С1), но самата сурова стойност продължаваше да се вижда
    НЕФИЛТРИРАНА на самата печатна бланка (`{{ it.qty }}` директно в
    invoice_*_print.html, без филтър) — клиентска/митническа фактура може
    да излезе с видим ред „-5“ количество и празна цена, без той изобщо
    да участва в сбора долу — объркваща, потенциално некоректна бланка.
    Ползва се при ЗАПИС на документ (routes_documents._document_new), за
    да предупреди оператора преди издаване, вместо той да разбере чак от
    готовата бланка."""
    rows = []
    for idx, it in enumerate(items or [], start=1):
        if not isinstance(it, dict):
            continue
        for field in _NEGATIVE_CHECK_FIELDS:
            n = _parse_decimal(it.get(field))
            if n is not None and n < 0:
                rows.append(idx)
                break
    return rows


#: Одит (19.08.2026, находка №8): кое обобщаващо поле на опаковъчния лист
#: от кое поле на редовете се сумира. Ползва се от packing_total_mismatches
#: по-долу и от живата сума в интерфейса (static/app.js, bindPackingTotals).
PACKING_TOTAL_FIELDS = (
    ("total_packages", "qty", "Общо колети"),
    ("total_volume", "volume", "Общо обем, м³"),
    ("total_net", "net", "Общо нето, кг"),
    ("total_gross", "gross", "Общо бруто, кг"),
)


def packing_sum(items, field):
    """Сумата на едно поле от редовете на опаковъчен лист, форматирана като
    останалите количества (Decimal, ROUND_HALF_UP, без завършващи нули).
    Празен низ, ако нито един ред няма валидна стойност."""
    total = decimal.Decimal("0")
    has_any = False
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        n = _parse_decimal_exact(it.get(field))
        if n is not None and n >= 0:
            total += n
            has_any = True
    return _fmt_amount_exact(total, decimals=3) if has_any else ""


def packing_total_mismatches(data):
    """Одит (19.08.2026, находка №8, висока): списък (етикет, въведено,
    изчислено) за обобщаващите полета на опаковъчния лист, които се
    РАЗМИНАВАТ със сбора на редовете.

    За разлика от палетната карта („Общ брой“ се преизчислява ВИНАГИ) и от
    фактурите (живи суми + сървърно изчисление), четирите полета
    „Общо колети/обем/нето/бруто“ се преписваха НА РЪКА и се печатаха
    буквално в реда ОБЩО/TOTAL — без никаква проверка. Проверено с
    изпълнение: документ, чиито редове дават нето 2.875, се издаде,
    отпечата и изнесе с въведено „1.11“, без нито едно предупреждение.

    Опаковъчният лист придружава ЧМР при митническо оформяне — бруто
    теглото там е товарен документ, не козметика.

    НЕ блокира: има легитимни случаи, в които общото включва тара на
    палета, опаковка и т.н. Затова връща само разминаванията, а
    извикващият ги показва като предупреждение."""
    items = data.get("items") or []
    if not items:
        return []
    out = []
    for total_key, row_field, label in PACKING_TOTAL_FIELDS:
        typed_raw = (data.get(total_key) or "").strip()
        if not typed_raw:
            continue  # непопълнено обобщение не е грешка
        typed = _parse_decimal_exact(typed_raw)
        computed_text = packing_sum(items, row_field)
        if typed is None or not computed_text:
            continue
        if _fmt_amount_exact(typed, decimals=3) != computed_text:
            out.append((label, typed_raw, computed_text))
    return out


def fmt_num(value, decimals=None):
    """Одит (19.08.2026, находка №44): числова стойност за ПОКАЗВАНЕ на
    печатна бланка — с точка за десетичен знак, независимо какво е въвел
    операторът.

    Преди това шаблоните печатаха суровия текст (`{{ it.qty }}`): ред с
    въведени „2,5“ и „1,20“ излизаше на официалната бланка като
    „|2,5| |1,20| |3.00|“ — запетая и точка едновременно, на един и същ
    документ, при това с изчислената колона (винаги с точка) до тях.
    Excel износът на СЪЩИЯ ред дава „2.5“, тоест бланката и износът си
    противоречаха.

    Неразчитаема стойност се връща НЕПРОМЕНЕНА — операторът трябва да я
    види точно както я е въвел (плюс предупреждението от находка №7), а не
    да изчезне от бланката.

    `decimals=None` (по подразбиране) ПАЗИ въведената точност и само сменя
    разделителя: „1,20“ → „1.20“, не „1.2“. Това е важно за цените —
    счетоводно „1.20“ е правилният вид, а закръгляне/рязане на нулите тук
    би било самоволна промяна на въведеното от оператора. Изрично зададен
    `decimals` квантува (ползва се там, където сборът трябва да съвпадне с
    изчисленото)."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    parsed = _parse_decimal_exact(text)
    if parsed is None:
        return text
    if decimals is None:
        return text.replace(",", ".")
    return _fmt_amount_exact(parsed, decimals=decimals)


def unparsable_item_rows(items):
    """Одит (19.08.2026, находка №7, висока): списък (1-базирани) номера на
    редове, в които числово поле е ПОПЪЛНЕНО, но не може да бъде разчетено.

    Огледално на `negative_item_rows` по-горе, за втория (и по-коварен)
    начин, по който ред изчезва от сборовете. `_parse_decimal` приема само
    строгия формат `^-?\\d+([.,]\\d+)?$` — всичко друго („1.234,56“ с
    разделител за хиляди, „12 бр“, „1 000.5“, „~5“) връща None. Тогава
    `invoice_row_total` дава празен низ, `invoice_totals` изключва реда
    ИЗЦЯЛО от общата сума, но самата сурова стойност се печата на бланката.

    Проверено с изпълнение: фактура с ред „1.234,56 × 10.00“ излиза с видим
    ред на стойност 12 345.60 EUR, който липсва от TOTAL — а операторът не
    получава НИТО ЕДНО предупреждение. За търговска фактура към клиент и
    митница това е недопустимо мълчание.

    Празно поле НЕ е грешка (много редове легитимно нямат тегло/цена) —
    сигнализира се само непразна стойност, която не е число."""
    rows = []
    for idx, it in enumerate(items or [], start=1):
        if not isinstance(it, dict):
            continue
        for field in _NEGATIVE_CHECK_FIELDS:
            raw = it.get(field)
            if raw is None:
                continue
            text = str(raw).strip()
            if text and _parse_decimal(text) is None:
                rows.append(idx)
                break
    return rows


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


def _fmt_amount_exact(value, decimals=3):
    """Одит (19.08.2026, находка №9): точният аналог на `_fmt_amount`, но за
    `decimal.Decimal` вход и с ИЗРИЧНО ROUND_HALF_UP.

    Разликата не е козметична. `_fmt_amount` минава през форматирането на
    float (`"%.3f" %`), което закръгля половинката към ЧЕТНО: 0.0625 →
    „0.062“. JS-ът на екрана (и BigInt конвейерът, който вече ползваме за
    парите и редовите тегла) закръгля половинката НАГОРЕ: 0.0625 → „0.063“.
    Резултатът беше документ, който противоречи на екрана, от който е
    издаден. Тук фиксираме половинката нагоре за ВСИЧКИ количества/тегла.

    Завършващите нули се махат както при `_fmt_amount` (2 вместо „2.000“) —
    важи и за живите суми в интерфейса, виж находка №45."""
    if value is None:
        return ""
    quant = decimal.Decimal(1).scaleb(-decimals)
    text = str(value.quantize(quant, rounding=decimal.ROUND_HALF_UP))
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

#: Одит (16.08.2026, находка №17, средна): СЪЩАТА точна decimal.Decimal
#: аритметика като _CENTS по-горе, но за реда „Общо тегло“ (invoice_row_
#: weight по-долу) — преди тази поправка тегло×количество минаваше през
#: обикновен float (_to_number), а живата сума в браузъра (static/app.js,
#: bindInvoiceTotals) — през JS `(qty*weight).toFixed(3)`. И двете страни
#: закръгляха „правилно“ поотделно, но при стойност точно на границата
#: (напр. x.xxx5) Python-овото форматиране на float (закръгля до четна
#: цифра при равенство — round-half-even) и JS-кото `toFixed` (закръгля
#: half-away-from-zero в повечето реализации) МОГАТ да дадат различен
#: резултат за ЕДНА и СЪЩА въведена двойка тегло/количество — живата сума
#: на екрана показва различно число от готовата бланка. Сега и двете
#: страни минават през ТОЧНО СЪЩАТА логика като парите: decimal.Decimal,
#: построен директно от суровия текст (JS: multiplyDecimalScaled/BigInt),
#: ROUND_HALF_UP при точно 3 знака.
_GRAMS = decimal.Decimal("0.001")


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
    тегло или количество.

    Одит (16.08.2026, находка №17): decimal.Decimal (виж _GRAMS по-горе),
    не float — огледално на invoice_row_total/_fmt_money, за да не се
    разминава живата сума в браузъра (static/app.js bindInvoiceTotals,
    вече пренасочена към СЪЩАТА BigInt-точна аритметика) с готовата
    бланка при гранични .5 стойности."""
    if not isinstance(item, dict):
        return ""
    qty = _parse_decimal_exact(item.get("qty"))
    weight = _parse_decimal_exact(item.get("net_weight"))
    if qty is None or weight is None:
        return ""
    product = (qty * weight).quantize(_GRAMS, rounding=decimal.ROUND_HALF_UP)
    text = str(product)
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def invoice_totals(items):
    """Обобщените суми под таблицата на фактурата: общо количество, обща
    стойност и общо нето тегло. Връща речник с вече форматирани текстове
    (празен низ, ако няма нито един годен ред за съответната сума), за да
    може шаблонът просто да ги изпише. Пропуска развалени/празни редове,
    вместо да гърми — същата толерантност като pallet_total_qty."""
    # Одит (19.08.2026, находка №9): количеството и теглото вече се трупат
    # в decimal.Decimal (както парите), не във float — виж _fmt_amount_exact.
    total_qty = total_weight = decimal.Decimal("0")
    total_price = decimal.Decimal("0")
    has_qty = has_price = has_weight = False
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        qty = _parse_decimal_exact(it.get("qty"))
        if qty is not None and qty >= 0:
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
        row_weight = _parse_decimal_exact(invoice_row_weight(it))
        if row_weight is not None:
            total_weight += row_weight
            has_weight = True
    return {
        "qty": _fmt_amount_exact(total_qty, decimals=3) if has_qty else "",
        "price": _fmt_money(total_price) if has_price else "",
        "weight": _fmt_amount_exact(total_weight, decimals=3) if has_weight else "",
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
            # Одит (16.08.2026, находка №7): постоянен банер на ВСЯКА
            # страница (не само в Настройки), докато отдалеченият достъп е
            # активен — status() е евтино четене на паметта (виж
            # remote_tunnel.status), безопасно на всяка заявка. Целта е
            # никой служител/администратор да не забрави, че адресът в
            # момента е публично достъпен.
            "remote_tunnel_active": remote_tunnel.status()["status"] == "running",
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
    # Одит (19.08.2026, находка №44): нормализира десетичния знак на
    # ПОКАЗВАНИТЕ числови клетки на бланката — виж fmt_num по-долу.
    app.add_template_global(fmt_num, name="fmt_num")


def _register_hooks(app):
    app.after_request(_add_security_headers)
    app.before_request(_check_csrf)
    app.before_request(_enforce_password_change)
    app.register_error_handler(413, _request_too_large)
    app.register_error_handler(Exception, _handle_unexpected_error)
    app.teardown_appcontext(_close_db)


def _add_security_headers(response):
    """Одит (12.08.2026, находка №18, средна): нямаше НИТО ЕДИН
    `after_request` hook, който да задава защитни HTTP хедъри — CSRF
    (_check_csrf) и сесийните бисквитки (HTTPONLY/SAMESITE=Lax) вече бяха
    покрити, но не и UI-redressing (clickjacking) чрез вграждане на
    формата за вход/смяна на парола/административните форми в чужд
    `<iframe>`. Евтина поправка — приложението не се нуждае легитимно от
    вграждане в чужд iframe (desktop/LAN контекст), затова X-Frame-Options
    DENY е безопасно по подразбиране. X-Content-Type-Options спира
    браузъра да „познава“ MIME типа на отговор въпреки обявения
    Content-Type (напр. качен файл, обслужен като text/plain, но
    интерпретиран като HTML/JS от стар браузър).

    Одит (16.08.2026, находка №44, дребна): Referrer-Policy липсваше — по
    подразбиране браузърът изпраща ПЪЛНИЯ адрес (вкл. query string) като
    Referer при клик върху ВЪНШЕН линк от която и да е страница тук.
    Адреси в тази програма понякога носят чувствителни низове в пътя/
    заявката (напр. ?public_token=…/?restore=<token> — виж appcore.
    _get_preview/_store_preview, или самите номера на документи) — при
    клик върху линк към трета страна (напр. carrier tracking, ако някога
    се добави) тези низове биха изтекли в логовете на чуждия сайт.
    "same-origin" изпраща пълния Referer само между страници В РАМКИТЕ на
    самото приложение, а нищо при преход към друг домейн."""
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


# Одит (19.08.2026, находка №3): подкласовете на sqlite3.DatabaseError,
# които означават ЛОГИЧЕСКА грешка (нарушено ограничение, грешен SQL,
# невалидни данни), а НЕ недостъпна/повредена база. Изключват се изрично
# в _is_db_unavailable_error по-долу.
_DB_LOGIC_ERRORS = (sqlite3.IntegrityError, sqlite3.ProgrammingError,
                    sqlite3.DataError, sqlite3.InterfaceError)

# Одит (19.08.2026, находка №3): съобщения на sqlite3.OperationalError,
# които означават ТРАЙНА недостъпност/повреда, а не временна заетост.
# "no such table"/"no such column" е разминаване на схемата — случва се
# реално след възстановяване на бекъп от по-стара версия (виж находка №6).
#: Одит (22.08.2026, находка №9): съобщения, при които базата наистина е
#: ПОВРЕДЕНА — само те оправдават страницата, подтикваща към възстановяване.
_DB_CORRUPT_MARKERS = (
    "malformed",
    "file is not a database",
    "encrypted",
    "not a database",
    "corrupt",
)

# Одит (25.08.2026, находка №13): „no such table“/„no such column“ БЯХА и тук.
# Дублираха се със `is_schema_mismatch_error` — ЕДИНСТВЕНИЯТ правилен собственик
# на разминаването на схемата, който `_handle_unexpected_error` проверява ПРЕДИ
# този класификатор и показва различна страница („рестартирайте — НЕ
# възстановявайте архив“, находка №9). Докато редът в _handle_unexpected_error
# се пази, дублирането беше само маскирано, но семантично невярно:
# `_is_db_unavailable_error` връщаше True за база, която всъщност Е достъпна
# (просто чака миграция) — латентен капан за всяко бъдещо ново извикване или
# разместване. Схема-разминаването вече се класифицира САМО от
# is_schema_mismatch_error; тук останаха истинските недостъпности/повреди.
_DB_UNAVAILABLE_MARKERS = (
    "unable to open database file",
    "disk i/o error",
    "file is not a database",
    "malformed",
    "database or disk is full",
    "attempt to write a readonly database",
)


def _is_db_unavailable_error(exc):
    """Одит (16.08.2026, находка №9): различава ТРАЙНА недостъпност на
    самата база (папката/мрежовият диск липсва в момента — db.get_db()
    хвърля RuntimeError; или файлът не може да се отвори изобщо) от
    ВРЕМЕННО заетата база (sqlite3 "database is locked"/"busy" — вижте
    клона малко по-долу, който вече показва отделно, по-леко съобщение и
    ПРАВИ redirect, защото следващият опит съвсем скоро вероятно ще
    успее). Тази разлика е важна, защото redirect само за втория клас е
    безопасен — за първия води до безкраен цикъл (виж по-долу).

    Одит (19.08.2026, находка №3, КРИТИЧНА — разширяване): първата версия
    на този класификатор изброяваше само три конкретни съобщения и
    ПРОПУСКАШЕ два цели класа трайни грешки, при които се получаваше точно
    безкрайният redirect цикъл, който находка №9 твърдеше, че затваря:

    * `sqlite3.DatabaseError: database disk image is malformed` — ПОВРЕДЕНА
      база. Това не е екзотика: критична находка К1 от първия одит описва
      точно как се стига дотам (прекъснат запис върху мрежов диск).
      `DatabaseError` е БАЗОВИЯТ клас на `OperationalError`, но самата
      повреда се вдига като него, не като подкласа — затова проверката по
      `OperationalError` не я хващаше.
    * `no such column` / `no such table` — РАЗМИНАВАНЕ НА СХЕМАТА. Случва
      се реално след възстановяване на локален архив, направен от по-стара
      версия на програмата: файлът е подменен, но миграциите се изпълняват
      само при старт, така че до рестарт всяка заявка гърми на липсваща
      колона.

    ВНИМАНИЕ при бъдещи промени: `sqlite3.IntegrityError` (нарушен UNIQUE —
    напр. дублиран ръчен номер на фактура), `ProgrammingError` и `DataError`
    СЪЩО наследяват `DatabaseError`, но са нормални логически/данни грешки,
    не недостъпност. Те се изключват ИЗРИЧНО — иначе едно дублирано число
    би показало страницата „базата е недостъпна“ вместо смисленото
    съобщение за заетия номер."""
    if isinstance(exc, RuntimeError) and "мрежовият диск" in str(exc):
        return True
    if isinstance(exc, _DB_LOGIC_ERRORS):
        return False
    if isinstance(exc, sqlite3.OperationalError):
        msg = str(exc).lower()
        # Временно заета база — НЕ е трайна недостъпност: следващият опит
        # съвсем скоро вероятно ще успее, затова там redirect-ът е уместен
        # (виж клона в _handle_unexpected_error).
        if "locked" in msg or "busy" in msg:
            return False
        return any(marker in msg for marker in _DB_UNAVAILABLE_MARKERS)
    if isinstance(exc, sqlite3.DatabaseError):
        # Одит (22.08.2026, находка №9): базовият клас вече НЕ е „всичко
        # трайно по подразбиране“. sqlite3 вдига `DatabaseError` директно за
        # повредена база („database disk image is malformed“ — последствието
        # от критична находка К1), но и за други, съвсем не толкова тежки
        # състояния. „Каквото не разпознавам = повредена база“ е обратното на
        # консервативното: страницата, която показваме, подтиква оператора да
        # възстанови бекъп — разрушително действие срещу проблем, който може
        # да е чисто софтуерен.
        msg = str(exc).lower()
        return any(marker in msg for marker in _DB_CORRUPT_MARKERS)
    return False


def is_schema_mismatch_error(exc):
    """Одит (22.08.2026, находка №9): разминаване на СХЕМАТА (липсваща
    колона/таблица) — различен проблем от недостъпна или повредена база.

    Случва се при провалена/пропусната миграция или при база, подменена на
    живо от по-стара версия. Лекарството е РЕСТАРТ (миграциите се прилагат
    при старт), не възстановяване на бекъп — затова заслужава собствен текст,
    вместо да се смесва с „проверете мрежовия диск“."""
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    msg = str(exc).lower()
    return "no such table" in msg or "no such column" in msg


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
    # Одит (22.08.2026, находка №9): схема-разминаването получава СВОЙ текст.
    # Лекарството е рестарт (миграциите се прилагат при старт), не
    # възстановяване на бекъп — а точно към него подтикваше общата страница.
    if is_schema_mismatch_error(exc):
        return render_template(
            "db_unavailable.html",
            app_name=APP_NAME,
            title=_("Базата данни изисква обновяване"),
            message=_("Структурата на базата данни не съвпада с тази версия на "
                     "програмата (%s). Обикновено се случва след възстановяване "
                     "на архив, записан от друга версия.") % exc,
            hint=_("Затворете и стартирайте програмата отново — обновяването на "
                  "структурата се извършва автоматично при стартиране. НЕ "
                  "възстановявайте архив: данните Ви са непокътнати."),
            retry_url=request.path,
        ), 503
    if _is_db_unavailable_error(exc):
        # Одит (16.08.2026, находка №9, висока): при ТРАЙНО недостъпна база
        # (напр. паднал мрежов диск) redirect(target) по-долу водеше до
        # БЕЗКРАЕН цикъл — целта на пренасочването (referrer/dashboard, а
        # дори /login САМАТА тя чете от базата за login_scene) гърми пак
        # със СЪЩОТО изключение, което пак води до нов redirect. Браузърът
        # показва „ERR_TOO_MANY_REDIRECTS“/бял екран; flash съобщението
        # никога не се рендерира, защото никоя страница не оцелява. Тук
        # рендираме самостоятелна статична страница (БЕЗ никаква DB
        # заявка — вижте templates/db_unavailable.html), директно, без
        # redirect — потребителят вижда ясна причина и бутон „Опитай пак“
        # към СЪЩИЯ адрес, вместо безкраен цикъл.
        return render_template(
            "db_unavailable.html",
            app_name=APP_NAME,
            message=str(exc),
            retry_url=request.path,
        ), 503
    if isinstance(exc, sqlite3.OperationalError) and (
            "locked" in str(exc).lower() or "busy" in str(exc).lower()):
        # Одит (22.08.2026, находка №1, КРИТИЧНА): и този клон вече рендира
        # самостоятелна страница, вместо да прави redirect.
        #
        # Поправката на №9/№3 разграничи „трайно недостъпна“ от „временно
        # заета“ и остави redirect САМО за втората — с разсъждението, че
        # следващият опит съвсем скоро ще успее. Това важи за ЕДИНИЧЕН
        # сблъсък, но не и за ТРАЙНО заета база: втора машина в мрежов режим,
        # държаща писателски катинар по-дълго от busy_timeout (миграции при
        # старт на друг компютър, локален бекъп, антивирус/индексатор върху
        # мрежовия дял, увиснал клиент в средата на транзакция). Тогава
        # целта на пренасочването гърми със СЪЩОТО изключение и се получава
        # точно безкрайният цикъл, който №3 твърди, че затваря — само през
        # другия клон. Възпроизведено: `/docs → / → / → /` …, 12 хопа без
        # спиране, а flash съобщението не се вижда НИКОГА, защото никоя
        # страница не оцелява.
        #
        # Статус 503 + Retry-After: коректно за „опитайте пак след малко“ и
        # разбираемо за прокси/монитори, за разлика от 200 с пренасочване.
        response = render_template(
            "db_unavailable.html",
            app_name=APP_NAME,
            title=_("Базата данни е заета в момента"),
            message=_("Друга едновременна операция държи базата данни заета "
                     "(напр. друг служител записва в момента, тече архивиране "
                     "или програмата се обновява на друг компютър). Изчакайте "
                     "няколко секунди и натиснете „Опитай пак“."),
            retry_url=request.path,
        )
        return response, 503, {"Retry-After": "5"}
    flash(_("Възникна неочаквана грешка. Опитайте отново — ако продължава, "
           "съобщете на администратор."), "error")
    try:
        target = request.referrer or url_for("dashboard")
    except Exception:
        target = url_for("dashboard")
    # Одит (22.08.2026, находка №1): никога не пренасочвай към АДРЕСА, който
    # току-що гръмна — това е самият механизъм на цикъла. При съвпадение
    # падаме към таблото; ако и то е източникът, оставаме на статична
    # страница, вместо да се въртим.
    if target and urlsplit(target).path == request.path:
        target = url_for("dashboard")
        if urlsplit(target).path == request.path:
            return render_template(
                "db_unavailable.html", app_name=APP_NAME,
                title=_("Възникна грешка"),
                message=_("Страницата не можа да бъде заредена заради "
                         "неочаквана грешка. Опитайте пак след малко."),
                retry_url=request.path), 500
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


# Бележка (25.08.2026): тук по-рано стоеше `_sync_after_write` — after_request
# кука, която при всяка успешна промяна насрочваше автоматично качване в
# GitHub (backup.mark_dirty). Синхронизацията с GitHub беше премахната по
# заявка на потребителя, затова куката отпадна изцяло. Локалният архив (папка/
# мрежов диск) не зависи от нея — той върви по свой часови таймер
# (backup.start_auto_backup) и през бутона „Архивирай сега“.


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
    row = con.execute(
        "SELECT role, active, session_epoch, must_change_password FROM users WHERE id = ?",
        (session.get("user_id"),)).fetchone()
    if row is None or not row["active"]:
        session.clear()
        return True
    # Одит (16.08.2026, находка №5): виж db._m007_session_epoch — смяна на
    # паролата (собствена или от администратор) СЛЕД издаването на тази
    # бисквитка прекратява сесията, дори потребителят да си остане active.
    if row["session_epoch"] != session.get("session_epoch"):
        session.clear()
        return True
    if row["role"] != session.get("role"):
        session["role"] = row["role"]
    # Одит (19.08.2026, находка №35): и `must_change_password` се сверява с
    # БАЗАТА, не се чете само от бисквитката. Поправката на находка №5
    # (16.08) покриваше администраторското нулиране на парола само защото
    # то СЪЩО вдига `session_epoch` и убива сесията. Самият флаг обаче
    # оставаше без проверка: всеки друг път, който го вдига без да пипне
    # епохата (поддържащ скрипт, миграция, бъдещ бутон „принуди смяна“), не
    # принуждаваше нищо на вече отворена сесия. Проверено с изпълнение:
    # UPDATE на флага при отворена сесия не пренасочваше към /password.
    # Цената е нулева — колоната идва от СЪЩАТА заявка, която вече правим.
    if bool(row["must_change_password"]) != bool(session.get("must_change_password")):
        session["must_change_password"] = bool(row["must_change_password"])
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

def form_data(exclude=("csrf_token", "items_json", "edit_doc_id", "edit_doc_version")):
    """Всички полета от формата като речник (за съхранение в JSON).

    „edit_doc_id“ (виж render_preview по-горе) е служебно поле — носи ID-то
    на редактирания документ ЕДИНСТВЕНО за да знае _document_preview накъде
    да върне „Назад към формата“; никога не бива да свърши в самите данни
    на документа (нито при ново издаване, нито при запис на редакция).

    „edit_doc_version“ (одит 16.08.2026, находка №39) — носи версията на
    документа, каквато е била при ЗАРЕЖДАНЕ на формата за редакция, за
    оптимистично заключване (виж routes_documents.edit_document); също
    служебно поле, никога не свършва в самите данни."""
    return {k: v.strip() for k, v in request.form.items() if k not in exclude}


#: Одит (19.08.2026, находка №25, средна): колко клиента се ВГРАЖДАТ в
#: самата форма (като <option> и като JSON за автодовършването). Преди тази
#: поправка се вграждаха ВСИЧКИ — измерено при 5 000 клиента: /cmr/new →
#: 2 695 KB, /invoice-br/new → 2 123 KB HTML на всяко отваряне на форма, и
#: то през тунел/по-бавна LAN. Расте линейно и с нищо не се ограничава.
#:
#: 300 е нарочно ЩЕДРО: реалната адресна книга на офиса е десетки записи,
#: тоест при типична инсталация НИЩО не се променя — целият списък си
#: остава вграден, автодовършването работи мигновено и БЕЗ мрежа. Над този
#: праг падащото меню показва първите 300 (по азбучен ред), а останалите се
#: намират през сървърното търсене (routes_clients.clients_lookup), което
#: се задейства при писане в полето за търсене над менюто.
CLIENT_EMBED_LIMIT = 300


def load_clients(con, limit=None):
    """Клиентите за форма/списък, подредени по име.

    `limit` (одит 19.08.2026, находка №25) ограничава броя ВГРАДЕНИ във
    формата записи — виж CLIENT_EMBED_LIMIT по-горе. Без него поведението
    е точно както преди (всички записи)."""
    if limit is None:
        return con.execute("SELECT * FROM clients ORDER BY name COLLATE NOCASE").fetchall()
    return con.execute(
        "SELECT * FROM clients ORDER BY name COLLATE NOCASE LIMIT ?", (limit,)).fetchall()


def count_clients(con):
    """Общият брой клиенти в адресната книга — формите го подават на
    JavaScript-а, за да знае дали вграденият списък е пълен, или трябва да
    предложи сървърно търсене (одит 19.08.2026, находка №25)."""
    return con.execute("SELECT COUNT(*) AS c FROM clients").fetchone()["c"]


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
    """Редовете от таблицата с артикули, подадени като JSON от формата.

    Одит (29.08.2026, находка №3): филтрираме и НЕ-РЕЧНИКОВИТЕ елементи, не
    само не-списък на горното ниво. Дотук се проверяваше единствено, че
    външното JSON е списък, затова `items_json='["развален"]'` се записваше
    буквално. Всички сумиращи функции пазят `isinstance(it, dict)` и просто
    пропускат такъв ред, но ИЗНОСЪТ го подаваше на `it.get(...)`: за
    опаковъчен лист / палетна карта / товарителница (типовете БЕЗ фактурно
    обогатяване) Excel износът гърмеше с `AttributeError: 'str' object has no
    attribute 'get'`, а PDF-ът — със същото през шаблона. Документът се
    записваше и се показваше нормално, но износът му оставаше НЕВЪЗМОЖЕН.
    Проверено с изпълнение преди поправката: `/packing/new` с такъв ред →
    `/doc/<id>/export.xlsx` дава AttributeError.

    Тук е единствената точка, през която редовете влизат от формите, затова
    филтърът пази ВСИЧКИ типове документи наведнъж. (За вече записани
    развалени данни има втора защита в самия износ — виж
    routes_documents._export_fields_and_items.)"""
    raw = request.form.get("items_json", "[]")
    try:
        items = json.loads(raw)
    except ValueError:
        items = []
    if not isinstance(items, list):
        return []
    return [it for it in items if isinstance(it, dict)]


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
    # Одит (19.08.2026, находка №20): срок на публичния QR адрес. Досега
    # той беше ВЕЧЕН и неотменяем — сканирал веднъж (шофьор, спедитор)
    # виждаше документа ЖИВО, включително всички по-късни редакции,
    # завинаги. TTL-ът е дълъг (виж PUBLIC_TOKEN_TTL_DAYS), за да покрие
    # реалния живот на един транспортен документ, но не безкраен.
    public_expires = public_token_expiry()
    cur = con.execute(
        "INSERT INTO documents (doc_type, number, year, seq, barcode, public_token,"
        " public_token_expires_at, data, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (doc_type, number, year, seq, barcode, public_token, public_expires,
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


def paginate_documents(con, where_sql, params, page, page_size=100, order_by="d.id DESC"):
    """Обща пагинация за списъка с документи/фактури.

    Одит (12.08.2026, находка №20): преди тази поправка почти идентичен
    блок (броене, изчисление на total_pages, clamp на page, LIMIT/OFFSET
    заявка) беше копи-пейстнат отделно в routes_documents.documents() И
    routes_invoices.invoices_list() — DRY нарушение, разминало се вече
    веднъж (invoices_list нямаше филтър по дата, макар интерфейсите да
    изглеждат еднакви).

    `where_sql`/`params`: WHERE клауза (само с „?“ плейсхолдъри от
    викащия код) и стойностите ѝ. `order_by`: подава се БЕЗ потребителски
    вход (само константи от повикващия код, никога от request.args) —
    позволява групирането по клиент (находка №5) да стане част от самата
    SQL заявка, ПРЕДИ LIMIT/OFFSET, вместо Python-сортиране СЛЕД
    пагинацията (виж documents() за пълното обяснение защо предишният ред
    беше грешен).

    Връща (docs, page, total_pages, total_count) — `page` може да се
    различава от подадения, ако е бил извън диапазона (clamp)."""
    total_count = con.execute(
        "SELECT COUNT(*) AS c FROM documents d " + where_sql, params).fetchone()["c"]  # nosec B608 -- where_sql е съставен само от „?“ плейсхолдъри от викащия код
    total_pages = max(1, (total_count + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    docs = con.execute(
        "SELECT d.*, u.full_name AS author FROM documents d"
        " LEFT JOIN users u ON u.id = d.created_by " + where_sql +  # nosec B608 -- виж бележката по-горе
        " ORDER BY " + order_by + " LIMIT ? OFFSET ?",  # nosec B608 -- order_by е константа от викащия код, никога request.args
        list(params) + [page_size, (page - 1) * page_size],
    ).fetchall()
    return docs, page, total_pages, total_count


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
#: Одит (19.08.2026, информативна находка): OrderedDict, а не обикновен
#: речник — прегледите се изхвърлят и по БРОЙ (най-отдавна ползваният
#: първи), не само по време. Дотук единственият таван беше TTL: групов
#: преглед на 5 000 реда се пази 30 минути НА ТОКЕН, а нов токен се издава
#: при всяко натискане на „Предварителен преглед“. Няколко оператора,
#: работещи с големи импорти, лесно държат десетки такива копия
#: едновременно в паметта на един офисен компютър — без никаква горна
#: граница.
_preview_store = collections.OrderedDict()
_PREVIEW_TTL = 1800  # 30 минути — достатъчно за преглед, без да трупа памет за постоянно
#: Толкова наскоро ползвани прегледа се пазят най-много. Един оператор
#: реално ползва 1–2 наведнъж (текущата форма + връщане назад); 20 е
#: щедро дори за няколко едновременни потребителя, но е ТАВАН.
_PREVIEW_MAX_ENTRIES = 20
# Пази _preview_store от надпревара между заявки, обслужвани от различни
# нишки на Flask dev/production сървъра (виж M5 — несинхронизирани
# споделени глобални променливи в оригиналния app.py).
_preview_lock = threading.Lock()


def _cleanup_previews():
    now = time.time()
    with _preview_lock:
        for token in [t for t, entry in _preview_store.items() if entry[0] < now]:
            del _preview_store[token]
        _evict_previews_over_limit()


def _evict_previews_over_limit():
    """Одит (19.08.2026, информативна находка): таван по БРОЙ освен по
    време — изхвърля се най-отдавна ПОЛЗВАНИЯТ преглед (виж move_to_end в
    _get_preview по-долу), не просто най-старият по издаване, за да не се
    обезсили точно прегледът, който операторът в момента презарежда.
    ВИКА СЕ ПРИ ВЗЕТ `_preview_lock`."""
    while len(_preview_store) > _PREVIEW_MAX_ENTRIES:
        _preview_store.popitem(last=False)


def _store_preview(kind, payload):
    _cleanup_previews()
    token = secrets.token_urlsafe(16)
    # Одит (19.08.2026, информативна находка): токенът се ОБВЪРЗВА с
    # потребителя, който го е създал. Дотук всеки логнат служител, узнал
    # чужд токен (адресът от историята на браузъра на общия компютър, от
    # изпратена връзка, от лог на прокси), можеше да отвори чуждия
    # предварителен преглед — а прегледът съдържа пълните данни на още
    # неиздаден документ (получател, цени, бележки). Пази се в самия ЗАПИС
    # на хранилището, не в payload-а: така не се пипа структурата, която
    # четат извикващите (виж render_preview — payload-ът е 4-елементен
    # заради находка №10, а при груповите палети е списък с чернови).
    user_id = None
    try:
        if has_request_context():
            user_id = session.get("user_id")
    except Exception:  # nosec B110 -- извън заявка (напр. тест/фонов код): токенът остава необвързан
        user_id = None
    with _preview_lock:
        _preview_store[token] = (time.time() + _PREVIEW_TTL, kind, payload, user_id)
        # СЛЕД вписването, не само преди него (_cleanup_previews по-горе):
        # иначе таванът реално щеше да е _PREVIEW_MAX_ENTRIES + 1.
        _evict_previews_over_limit()
    return token


def _get_preview(token, kind):
    """Чете преглед по токен, БЕЗ да го трие — трябва да остане валиден за
    многократно презареждане/връщане назад, докато не изтече (_PREVIEW_TTL),
    иначе първото презареждане би счупило точно проблема, който поправяме."""
    _cleanup_previews()
    with _preview_lock:
        entry = _preview_store.get(token)
        if entry is not None:
            _preview_store.move_to_end(token)  # LRU — виж _cleanup_previews
    if entry is None or entry[1] != kind:
        return None
    # Одит (19.08.2026, информативна находка): токен, издаден на ДРУГ
    # потребител, не се чете. Записи БЕЗ обвързване (user_id is None —
    # издадени извън заявка, напр. от тест или фонов код) остават
    # съвместими, за да не се променя поведението там.
    owner = entry[3] if len(entry) > 3 else None
    if owner is not None:
        try:
            if not has_request_context() or session.get("user_id") != owner:
                return None
        except Exception:  # nosec B110 -- без Flask контекст проверката не е приложима
            return None
    return entry[2]


def render_preview(doc_type, data, edit_doc_id=None, edit_doc_version=None):
    """Приема POST-а с still-незаписаните данни на формата, пази ги временно
    на сървъра и пренасочва към GET адрес, който показва документа както ще
    изглежда при печат — БЕЗ да го запазва в базата и БЕЗ да изразходва
    пореден номер. GET адресът е безопасен за презареждане/връщане назад.

    `edit_doc_id` (заявка на потребителя: „при връщане назад от преглед за
    печат въведената информация се губи“): преди тази поправка бутонът
    „Предварителен преглед“ винаги сочеше към ОБЩ endpoint за издаване на
    НОВ документ (напр. cmr_preview), независимо дали формата в момента
    редактира вече ИЗДАДЕН документ (/doc/<id>/edit) — „Назад към формата“
    от прегледа тогава връщаше към ПРАЗНАТА форма за издаване на нов
    документ (само с възстановени полета чрез ?restore=), а не към
    /doc/<id>/edit — потребителят губеше самата връзка коя редакция
    продължава, не самите въведени стойности (те се възстановяваха), но на
    практика резултатът изглежда точно като загубена информация: „Запази
    промените“ вече не съществуваше на новата страница (само „Издай...“),
    а истинският редактиран документ оставаше непроменен. Пазим id-то на
    редактирания документ в самия preview payload, за да можем по-долу
    (preview_document) да пресметнем правилния адрес за връщане.

    `edit_doc_version` (одит 19.08.2026, находка №10): версията, с която
    формата е била ЗАРЕДЕНА, пътува заедно с данните през прегледа. Преди
    това „Назад към формата“ рендираше скритото поле от ПРЕСНО прочетения
    ред в базата — тоест ако друг служител е записал междувременно,
    оптимистичното заключване се „презареждаше“ с новата версия и
    конфликтът никога не се засичаше. Проверено с изпълнение: промяната на
    втория служител изчезваше безшумно, при това през препоръчания работен
    поток (преглед преди печат).

    Payload-ът е 4-елементен; старите 3-елементни токени (издадени преди
    обновяването, още живи в паметта до 30 мин) се четат съвместимо —
    вижте разопаковането в routes_documents/app.py."""
    token = _store_preview("doc", (doc_type, data, edit_doc_id, edit_doc_version))
    return redirect(url_for("preview_document", token=token))
