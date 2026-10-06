# -*- coding: utf-8 -*-
"""Ядро на PH Logistics: фабрика на Flask приложението (create_app),
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
import errno
import gzip
import hmac
import io
import json
import math
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import zipfile
from datetime import date, datetime, timedelta
from functools import wraps
from urllib.parse import urlsplit

from flask import (Flask, abort, flash, g, has_request_context, jsonify, redirect,
                   render_template, request, session, url_for)
from flask_babel import Babel
from flask_babel import gettext as _
from markupsafe import Markup
from werkzeug.exceptions import HTTPException
from werkzeug.routing import IntegerConverter

import applog
import backup
import branding
import db
import jsonutil
import remote_tunnel
import search_index
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


APP_NAME = "PH Logistics"
MIN_PASSWORD_LENGTH = 8  # прилага се еднакво във всички пътища за задаване на парола

#: Одит (04.10.2026, S2): най-често ползваните пароли (вкл. „български“
#: клавиатурни поредици), които минаваха проверката за дължина. Сравнява се
#: без значение от регистъра. Поредици от вида 12345678/abcdefgh и само един
#: повтарящ се знак се хващат отделно (виж password_policy_error).
_COMMON_PASSWORDS = frozenset((
    "password", "password1", "password12", "password123", "passw0rd", "p@ssw0rd",
    "p@ssword", "qwertyui", "qwertyuiop", "qwerty12", "qwerty123", "qwer1234",
    "1234qwer", "asdfghjk", "asdfghjkl", "zxcvbnm1", "1q2w3e4r", "1q2w3e4r5t",
    "1qaz2wsx", "q1w2e3r4", "zaq12wsx", "iloveyou", "letmein1", "welcome1",
    "welcome123", "admin123", "admin1234", "adminadmin", "administrator",
    "abc12345", "abcd1234", "12341234", "11223344", "12121212", "sunshine",
    "princess", "football", "baseball", "trustno1", "superman", "starwars",
    "changeme", "parola123", "parola12", "parolata", "parolaparola",
    "pachologistik", "йцукенгш", "йцукенгшщз", "явертъуи", "явертъуиоп",
    "асдфгхйк", "парола123", "паролата", "пачологистик", "123qweasd",
))


def _is_simple_sequence(text):
    """Цялата парола е една поредица с постоянна стъпка +1 или -1
    (12345678, 87654321, abcdefgh, hgfedcba)."""
    if len(text) < 3:
        return False
    step = ord(text[1]) - ord(text[0])
    if step not in (1, -1):
        return False
    return all(ord(b) - ord(a) == step for a, b in zip(text, text[1:]))


def password_policy_error(password, username=None):
    """Одит (04.10.2026, S2): единната проверка на НОВА парола — дължина,
    често срещани пароли, един повтарящ се знак, проста поредица и парола,
    съдържаща потребителското име. Връща преведен текст на грешката или
    None. Прилага се само при ЗАДАВАНЕ на парола — вече съществуващите пароли
    продължават да влизат (проверката не се вика при вход)."""
    password = password or ""
    if len(password) < MIN_PASSWORD_LENGTH:
        return _("Паролата трябва да е поне %d символа.") % MIN_PASSWORD_LENGTH
    folded = password.strip().casefold()
    if len(set(folded)) == 1:
        return _("Паролата не може да е само един повтарящ се знак.")
    if _is_simple_sequence(folded):
        return _("Паролата не може да е проста поредица като 12345678 или abcdefgh.")
    if folded in _COMMON_PASSWORDS:
        return _("Тази парола е сред най-често използваните и лесно се отгатва — изберете друга.")
    name = (username or "").strip().casefold()
    if len(name) >= 3 and name in folded:
        return _("Паролата не може да съдържа потребителското име.")
    return None

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


def translations_dir():
    """Папката с каталозите за превод (и в .exe, и от изходния код)."""
    if getattr(sys, "frozen", False):
        return os.path.join(sys._MEIPASS, "translations")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "translations")


def init_fallback_babel(app):
    """Одит (04.10.2026, I6/I11): Flask-Babel и за резервното приложение на
    app.py (базата е недостъпна при старт). Там няма база и няма потребител,
    затова езикът идва от бисквитката на сесията (ако ключът за подписване е
    четим), иначе от езика на браузъра, иначе български."""
    def _fallback_locale():
        try:
            lang = session.get("lang")
        except Exception:
            lang = None
        if lang in db.LANGUAGES:
            return lang
        try:
            best = request.accept_languages.best_match(list(db.LANGUAGES))
        except Exception:
            best = None
        return best or db.DEFAULT_LANGUAGE

    Babel(app, default_locale=db.DEFAULT_LANGUAGE,
          default_translation_directories=translations_dir(),
          locale_selector=_fallback_locale)

    @app.context_processor
    def _fallback_globals():
        return {"current_lang": _fallback_locale()}
    return app


def _hide_server_banner():
    """Одит (04.10.2026, S1): вграденият сървър на Werkzeug (локален режим и
    тестовите сървъри) пращаше `Server: Werkzeug/3.1.9 Python/3.11.15` —
    точните версии улесняват търсенето на известни уязвимости. Името на
    сървъра се задава в самия обработчик на заявки (преди отговора на
    приложението), затова не може да се махне от after_request; подменяме
    го веднъж за процеса. Waitress (мрежов режим) получава `ident` в app.py."""
    try:
        from werkzeug.serving import WSGIRequestHandler
        WSGIRequestHandler.version_string = lambda self: SERVER_IDENT
    except Exception:  # nosec B110 -- банерът е козметика; никога не спира старта
        pass


#: Одит (04.10.2026, S1): името на сървъра в заглавието `Server` (без версии).
#: Одит (06.10.2026): новото техническо име.
SERVER_IDENT = "PHLogistics"


class _BoundedIntConverter(IntegerConverter):
    def __init__(self, url_map, *args, **kwargs):
        kwargs.setdefault("max", SQLITE_MAX_INT)
        super().__init__(url_map, *args, **kwargs)


#: Най-голямото число, което SQLite INTEGER побира.
SQLITE_MAX_INT = 2 ** 63 - 1


def create_app(run_boot_tasks=True):
    """Създава и връща напълно конфигуриран Flask app обект.

    `run_boot_tasks` вече не прави нищо (еднократното изтегляне от GitHub
    беше премахнато, виж бележката по-долу) — параметърът остава само за
    съвместимост с извикващите (app.py, тестовете). db.init_db() и
    search_index.ensure_schema() се изпълняват винаги."""
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
    _hide_server_banner()
    # Одит (26.09.2026, находка №26): <int:…> в адресите приемаше произволно
    # голямо число — SQLite гърмеше с OverflowError (/doc/9999…9 → гол 500,
    # другаде 302 от общия обработчик). Над обхвата на INTEGER → 404.
    app.url_map.converters["int"] = _BoundedIntConverter

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
        # Одит (05.09.2026, находка №3, ВИСОКА): Werkzeug 3.1 въведе ОТДЕЛЕН
        # таван за НЕфайловите полета на формата — `MAX_FORM_MEMORY_SIZE`, по
        # подразбиране само 500 000 байта. Програмата вдигаше единствено
        # `MAX_CONTENT_LENGTH` (25 MB), затова документ с много редове тихо
        # опираше в другия, десет пъти по-нисък таван: ВСИЧКИ редове пътуват
        # в `items_json` като поле на формата, не като файл.
        #
        # Проверено с изпълнение: 1000 реда се издават, 1500 реда (255 KB
        # тяло!) вече не — операторът вижда „Файлът е твърде голям (максимум
        # 25 MB). Изберете по-малък файл.“, при положение че НЯМА никакъв
        # файл. Нула записани документи, нула запазени данни. А Excel
        # импортът изрично поддържа 5000 реда (_MAX_IMPORT_DATA_ROWS и в
        # палетите, и във фактурите) — тоест програмата приема файла, казва
        # „Заредени 5000 реда“, операторът попълва брутото на всяка карта и
        # при „Издай“ губи всичко.
        #
        # Изравняваме двата тавана: истинската защита срещу препълване на
        # паметта е `MAX_CONTENT_LENGTH`, който важи за ЦЯЛОТО тяло.
        MAX_FORM_MEMORY_SIZE=25 * 1024 * 1024,
    )

    # Бележка (25.08.2026): тук по-рано стоеше автоматично изтегляне на
    # базата от GitHub при чисто нова инсталация. Синхронизацията с GitHub
    # беше премахната по заявка на потребителя — при липсваща база просто
    # се създава нова, локална (db.init_db() по-долу).
    db.init_db()
    _ensure_search_index()

    _register_globals(app)
    _register_hooks(app)
    app.url_defaults(_static_url_version)

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
    # Одит (04.10.2026, R6): над ~308 цифри float() дава inf — такова „число“
    # не е разчитаемо (в Excel излизаше празна клетка), третира се като текст.
    number = float(text.replace(",", "."))
    return number if math.isfinite(number) else None


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
        # вижте същото решение в _parse_decimal_exact по-долу.
        if n is not None and n >= 0:
            total += n
            has_any = True
    if not has_any:
        return ""
    return _fmt_amount_exact(total, decimals=3)


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


_SQL_COLUMN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")


def json_value_search(column, query):
    """Одит (26.09.2026, находка №11): търсене в СТОЙНОСТИТЕ на JSON колона
    (не в ключовете и не в ескейпнатия суров текст). Връща (SQL израз, параметри).

    Одит (01.10.2026, F2/R1): за `documents.data` търсенето минава през
    страничната таблица search_index (тяло от стойностите, поддържано от
    тригери) — корелиран EXISTS, за да спре LIMIT-ът рано. Повреден JSON
    дава празно тяло, т.е. просто не съвпада, вместо „malformed JSON“ за
    целия списък."""
    if not _SQL_COLUMN_RE.match(column):
        raise ValueError("невалидно име на колона: %r" % (column,))
    alias, _dot, name = column.rpartition(".")
    if name == "data" and search_index.is_ready():
        if search_index.SEP in query:
            return "0", []
        id_col = (alias or "documents") + ".id"
        if search_index.is_foldable(query):
            cond, param = "instr(s.body, ?) > 0", query.lower()
        else:
            cond, param = "ci_contains(s.body, ?)", query
        return ("EXISTS (SELECT 1 FROM document_search s WHERE s.id = %s AND %s)"  # nosec B608 -- id_col е проверено с _SQL_COLUMN_RE; стойността е „?“ параметър
                % (id_col, cond)), [param]
    escaped = json.dumps(query, ensure_ascii=False)[1:-1]
    sql = ("(ci_contains({col}, ?) AND EXISTS (SELECT 1 FROM json_tree("  # nosec B608 -- column е име от кода, проверено с _SQL_COLUMN_RE; стойностите са „?“ параметри
           "CASE WHEN json_valid({col}) THEN {col} ELSE '{{}}' END) AS jt"
           " WHERE jt.type IN ('text', 'integer', 'real')"
           " AND ci_contains(CAST(jt.value AS TEXT), ?)))").format(col=column)
    return sql, [escaped, query]


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


#: Одит (31.08.2026, находка №5, ВИСОКА): числовите полета на РЕД, които
#: двете предупреждения към оператора проверяват.
#:
#: Досега тук стояха само ("qty", "unit_price", "net_weight", "weight") —
#: наборът на фактурите и палетната карта. Но опаковъчният лист сумира
#: qty/volume/net/gross (виж PACKING_TOTAL_FIELDS по-долу) и показва колони
#: qty,length,width,height,volume,net,gross; пресичаше се САМО „qty“.
#:
#: Проверено с изпълнение: редове [{qty:1, net:"10", gross:"11"},
#: {qty:1, net:"-5", gross:"1.234,56"}] → packing_sum(net)="10",
#: packing_sum(gross)="11" (втори ред изпаднал от сборовете),
#: negative_item_rows=[], unparsable_item_rows=[], а POST към /packing/new
#: даваше НУЛА предупреждения — докато fmt_num печата „-5“ и „1.234,56“
#: буквално на бланката. Тоест точно дефектът „ред се вижда на бланката, но
#: липсва от сбора“, за единствения тип документ, чието бруто тегло е
#: товарен показател, придружаващ ЧМР при митническо оформяне.
#:
#: Наборът е ОБЕДИНЕНИЕ за всички типове: поле, което го няма в даден
#: документ, просто липсва в реда и се пропуска — затова един списък е
#: по-безопасен от списък-на-тип (нов тип документ е покрит автоматично,
#: вместо да бъде забравен, което е повтарящият се дефект тук).
_NEGATIVE_CHECK_FIELDS = (
    "qty", "unit_price", "net_weight", "weight",
    "net", "gross", "volume", "length", "width", "height",
)


def negative_item_rows(items):
    """Списък (1-базирани) номера на редове с отрицателна стойност в поне
    едно от полетата qty/unit_price/net_weight/weight.

    Одит (12.08.2026, находка №3): invoice_totals вече
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
#: Одит (05.09.2026, находка №7): „Общо колети“ ОТПАДА от автоматичната
#: сверка.
#:
#: Проверката свързваше `total_packages` с колоната `qty`, но заглавието на
#: тази колона на самата бланка е „Брой / Qty, **pcs**“ — ПАРЧЕТА, докато
#: полето е „Общо колети / Total **packages**“. Различни единици. Проверено
#: с изпълнение: два реда по 24 и 12 броя, опаковани в 2 колета (вярната
#: стойност) → под полето свети червено „Сбор от редовете: 36“ и при
#: издаване излиза предупреждение. Тоест ПРАВИЛНО попълненият документ
#: винаги алармира, а ако операторът „поправи“ на 36, ЧМР-то ще казва 5
#: колета, а опаковъчният лист — 36.
#:
#: В редовете няма колона за брой колети, значи няма и с какво да се сверява.
#: Останалите три обобщения (обем/нето/бруто) си имат точно съответстващи
#: редови колони и остават.
PACKING_TOTAL_FIELDS = (
    # Одит (04.10.2026, I5): N_() — етикетите се превеждат при показване.
    ("total_volume", "volume", N_("Общо обем, м³")),
    ("total_net", "net", N_("Общо нето, кг")),
    ("total_gross", "gross", N_("Общо бруто, кг")),
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
        if not computed_text:
            continue
        # Одит (02.09.2026, десети одит, находка №3): `typed is None` значи
        # „операторът е въвел нещо, което НЕ се чете като число“ — а това се
        # третираше наравно с „не е въвел нищо“ и не даваше предупреждение.
        # Тоест най-очевидно сбърканата форма беше единствената, която тази
        # проверка пропускаше. Проверено с изпълнение: редове със сбор нето
        # 2.875 и въведено „1.234,56“ (двоен разделител) се издаваха без нито
        # едно предупреждение, а `fmt_num` печаташе „1.234,56“ буквално в реда
        # ОБЩО/TOTAL — стойност, която митническият служител може да прочете и
        # като 1.23456, и като 1234.56 кг. `unparsable_item_rows` не покрива
        # този случай: тя обхожда САМО редовете, не четирите обобщаващи полета.
        if typed is None or _fmt_amount_exact(typed, decimals=3) != computed_text:
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


def suspicious_header_numbers(data, numeric_keys, labels=None):
    """Одит (03.09.2026, находка №6): огледално на `negative_item_rows` и
    `unparsable_item_rows`, но за ЗАГЛАВНИТЕ числови полета на документа.

    Двете проверки за числа обхождат САМО редовете. Опаковъчният лист
    получи собствена проверка на обобщаващите си полета
    (`packing_total_mismatches`). Но ЧМР изобщо няма редове
    (`needs_items: False`) — неговите кутии 11 „Бруто тегло, кг“ и 12
    „Обем, м³“ не се проверяваха от нищо; същото за `gross`/`height` на
    палетната карта. `fmt_num` връща неразчетима стойност НЕПРОМЕНЕНА (по
    замисъл — за да не подмени въведеното), значи тя се печата буквално
    върху митническия превозен документ, а Excel я записва като ТЕКСТ,
    тоест не влиза и в `=SUM()` на получателя.

    Проверено с изпълнение: ЧМР с бруто „1.234,56“ и обем „-3“ се
    издаваше без нито едно предупреждение — число, което митническият
    служител може да прочете и като 1,23456, и като 1234,56.

    Връща (отрицателни, неразчетими) — два списъка с ЕТИКЕТИТЕ на
    проблемните полета (или с ключовете, ако липсва етикет).
    """
    negative, unparsable = [], []
    labels = labels or {}
    for key in numeric_keys:
        raw = (data or {}).get(key)
        if raw is None:
            continue
        text = str(raw).strip()
        if not text:
            continue
        parsed = _parse_decimal(text)
        if parsed is None:
            unparsable.append(labels.get(key, key))
        elif parsed < 0:
            negative.append(labels.get(key, key))
    return negative, unparsable


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


#: Одит (26.09.2026, находка №25): стандартният Decimal контекст пази 28
#: цифри — кол. × цена ≈ 1e26 (напр. баркод, поставен в „Количество“)
#: гърмеше в quantize с InvalidOperation, и вече ЗАПИСАНАТА фактура не
#: можеше да се отвори, отпечата или изнесе. Точните сметки минават в
#: контекст с достатъчно цифри, а числа с над _MAX_EXACT_DIGITS цифри се
#: третират като невалиден вход (както всеки друг боклук в полето).
_MAX_EXACT_DIGITS = 50
_EXACT_CONTEXT = decimal.Context(prec=4 * _MAX_EXACT_DIGITS, rounding=decimal.ROUND_HALF_UP)


def _exact_decimal(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        with decimal.localcontext(_EXACT_CONTEXT):
            return func(*args, **kwargs)
    return wrapper


@_exact_decimal
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
#: обикновен float, а живата сума в браузъра (static/app.js,
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
    if sum(ch.isdigit() for ch in text) > _MAX_EXACT_DIGITS:
        return None
    try:
        n = decimal.Decimal(text.replace(",", "."))
    except decimal.InvalidOperation:
        return None
    # Отрицателна цена/количество няма смисъл в тази предметна област (находка С1):
    # почти сигурно е печатна грешка, затова редът се третира като невалиден.
    return None if n < 0 else n


@_exact_decimal
def _fmt_money(value):
    """Парично форматиране: ТОЧНО 2 знака след десетичната запетая, ВИНАГИ
    (за разлика от _fmt_amount, който маха завършващите нули) —
    ROUND_HALF_UP закръгляне на decimal.Decimal стойност. Празно при None."""
    if value is None:
        return ""
    if not isinstance(value, decimal.Decimal):
        value = decimal.Decimal(str(value))
    return str(value.quantize(_CENTS, rounding=decimal.ROUND_HALF_UP))


@_exact_decimal
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


@_exact_decimal
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


@_exact_decimal
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
    # Одит (26.09.2026, находка №14): валута, изписана с думи или в началото
    # („500 лева“, „50 евро“, „€500“, „EUR 500“), също получаваше втори „€“.
    # Всяко споменаване на валута където и да е в текста → оставяме го.
    if any(sym in text for sym in _CURRENCY_SYMBOLS):
        return text
    words = set(re.findall(r"[^\W\d_]+", text.upper()))
    if words & _CURRENCY_WORDS:
        return text
    return "%s €" % text


_CURRENCY_SYMBOLS = ("€", "$", "£")
_CURRENCY_WORDS = {"EUR", "EURO", "EUROS", "ЕВРО", "ЕВР", "BGN", "ЛВ", "ЛЕВ", "ЛЕВА",
                   "USD", "GBP", "CHF", "RON", "TRY", "NOK", "AED"}


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


def _ensure_search_index():
    """Одит (01.10.2026, F1c/F2): индексът за търсене и индексите, които
    db.py не създава (виж search_index.ensure_schema)."""
    con = db.get_db()
    try:
        search_index.ensure_schema(con)
    finally:
        con.close()


def _static_url_version(endpoint, values):
    """Одит (01.10.2026, F6): url_for('static', …) получава ?v=<версия>, за
    да може браузърът да кешира файла дълго (виж _compress_and_cache)."""
    if endpoint == "static":
        values.setdefault("v", __version__)


#: Одит (01.10.2026, F6): компресират се само генерирани текстови отговори
#: (файловете минават през send_file/direct_passthrough и се пропускат).
_GZIP_MIMETYPES = ("text/html", "application/json")
_GZIP_MIN_BYTES = 1024
_STATIC_MAX_AGE = 365 * 24 * 3600


def _compress_and_cache(response):
    """Одит (01.10.2026, F6): gzip за HTML/JSON (списъкът с документи е
    ~200 KB → ~20 KB по мрежата) и дълъг кеш за версионираните статични
    файлове."""
    if request.endpoint == "static" and response.status_code == 200 \
            and request.args.get("v") == __version__:
        response.cache_control.public = True
        response.cache_control.no_cache = None
        response.cache_control.max_age = _STATIC_MAX_AGE
        response.cache_control.immutable = True
        return response
    if (response.status_code != 200 or response.direct_passthrough
            or response.is_streamed or "Content-Encoding" in response.headers
            or response.mimetype not in _GZIP_MIMETYPES
            or request.accept_encodings.quality("gzip") <= 0):
        return response
    data = response.get_data()
    if len(data) < _GZIP_MIN_BYTES:
        return response
    response.set_data(gzip.compress(data, compresslevel=6, mtime=0))
    response.headers["Content-Encoding"] = "gzip"
    response.vary.add("Accept-Encoding")
    return response


#: Одит (04.10.2026): кеш на банера „насрочено възстановяване“ — контекстът
#: се изгражда при ВСЯКА страница, а проверката чете файл до базата (може да
#: е мрежов диск). Няколко секунди закъснение на банера са без значение.
_PENDING_RESTORE_TTL = 5.0
_pending_restore_cache = {"key": None, "at": 0.0, "value": None}
_pending_restore_lock = threading.Lock()


def pending_restore_banner():
    """{"requested_at": "дд.мм.гггг ЧЧ:ММ"}, ако администратор е насрочил
    възстановяване от архив (backup.pending_restore), иначе None. Кешира се
    за _PENDING_RESTORE_TTL секунди по пътя до базата."""
    key = db.DB_PATH
    now = time.monotonic()
    with _pending_restore_lock:
        cached = dict(_pending_restore_cache)
    if cached["key"] == key and now - cached["at"] < _PENDING_RESTORE_TTL:
        return cached["value"]
    try:
        marker = backup.pending_restore()
    except Exception:
        marker = None
    value = None
    if marker:
        raw = str(marker.get("requested_at") or "")
        try:
            shown = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y %H:%M")
        except ValueError:
            shown = raw
        value = {"requested_at": shown}
    with _pending_restore_lock:
        _pending_restore_cache.update(key=key, at=now, value=value)
    return value


def invalidate_pending_restore_banner():
    """Нулира кеша — за маршрутите, които насрочват/отменят възстановяване,
    за да се появи/изчезне банерът веднага след пренасочването."""
    with _pending_restore_lock:
        _pending_restore_cache.update(key=None, at=0.0, value=None)


def _register_globals(app):
    @app.context_processor
    def inject_globals():
        return {
            # Одит (04.10.2026): банер на всяка страница, докато е насрочено
            # възстановяване от архив (рендира се в base.html).
            "pending_restore": pending_restore_banner(),
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
    app.after_request(_compress_and_cache)
    app.before_request(_check_csrf)
    app.before_request(_enforce_password_change)
    app.register_error_handler(413, _request_too_large)
    # Одит (04.10.2026, I2): преведена и оформена страница вместо голата
    # английска страница на Werkzeug (JSON за fetch заявките — виж
    # _handle_http_error).
    for _code in _STYLED_HTTP_ERRORS:
        app.register_error_handler(_code, _handle_http_error)
    app.register_error_handler(Exception, _handle_unexpected_error)
    app.teardown_appcontext(_close_db)


# Одит (31.08.2026, находка №19): брояч на ПОСЛЕДОВАТЕЛНИТЕ аварийни
# пренасочвания. Три са предостатъчни за истински еднократен сблъсък
# (страницата вече е показала грешката си), а спират цикъла A→B→A много
# преди браузърът да покаже ERR_TOO_MANY_REDIRECTS.
ERROR_HOP_KEY = "_err_hops"
ERROR_HOP_FLAG = "_pacho_error_redirect"
MAX_ERROR_HOPS = 3


def _safe_referrer_path(raw):
    """Одит (03.09.2026, находка №9): свежда `Referer` до БЕЗОПАСЕН ВЪТРЕШЕН
    път или до None.

    Браузърът изпраща Referer като ПЪЛЕН адрес („http://192.168.1.5:5000/docs“),
    затова проверката не може да е само „започва ли с /“ — това би убило
    връщането към предишната страница изобщо. Приемаме адрес само ако хостът
    му съвпада с хоста на текущата заявка, и връщаме единствено пътя (плюс
    query), никога пълния адрес: така резултатът е относителен по
    конструкция и „//evil.example.com/x“ или „/\\evil…“ не могат да минат.

    Без това всяка необработена грешка беше отворено пренасочване: линк към
    страница, която гърми (напр. /docs?type=' — вижте и валидирането там),
    изпратен на логнат служител от сайт с `Referrer-Policy: unsafe-url`, го
    изхвърляше на ЧУЖД домейн, където копие на екрана за вход прибира
    паролата му. Възпроизведено: Location: https://evil.example.com/phish.
    """
    if not raw or not isinstance(raw, str):
        return None
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    if parts.netloc:
        # Абсолютен адрес — приемаме го само ако сочи към СЪЩИЯ хост.
        try:
            if parts.netloc != request.host:
                return None
        except Exception:
            return None
    elif not raw.startswith("/") or raw.startswith("//") or raw.startswith("/\\"):
        return None
    path = parts.path or "/"
    if not path.startswith("/") or path.startswith("//"):
        return None
    return path + (("?" + parts.query) if parts.query else "")


_BASE_CSP = "frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'"


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
    # Одит (04.10.2026, S1): CSP само с директиви, които НЕ засягат вградените
    # <script> блокове и style атрибути (script-src/style-src нарочно липсват):
    # без вграждане в чужд iframe, без <base> към чужд адрес, без <object>/
    # <embed> и формите изпращат само към самата програма. setdefault пази
    # по-строгата политика на прикачените файлове (routes_documents).
    response.headers.setdefault("Content-Security-Policy", _BASE_CSP)
    # Одит (31.08.2026, находка №19): всяка УСПЕШНО отдадена страница нулира
    # брояча на аварийни пренасочвания — иначе редки, несвързани грешки в
    # рамките на един работен ден биха се натрупали и по някое време напълно
    # безобидна грешка би показала статичната страница вместо пренасочване.
    # ВАЖНО: самото аварийно пренасочване също е 302 — то НЕ бива да си
    # изтрива брояча (маркерът в `g` го отличава от нормалните отговори).
    if response.status_code < 400 and not g.get(ERROR_HOP_FLAG, False):
        try:
            if session.get(ERROR_HOP_KEY):
                session.pop(ERROR_HOP_KEY, None)
        except Exception:  # nosec B110 -- броячът е удобство, не защита: ако
            # сесията е нечетима (счупена/изтекла бисквитка), правилният
            # отговор е да отдадем страницата, а не да гръмнем в
            # after_request hook и да превърнем успешен отговор в 500.
            pass
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



def _is_disk_full_error(exc):
    """Одит (01.10.2026, O9): пълен диск получава собствен текст вместо
    „проверете мрежовия диск“ — разпознаването е в db.is_disk_full_error."""
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        return True
    if isinstance(exc, _DB_LOGIC_ERRORS):
        return False
    return db.is_disk_full_error(exc)


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


#: Одит (04.10.2026, R4): формите, чийто POST при заета/пълна база се
#: повтаря със СЪЩИТЕ данни от бутона „Опитай пак“ (скрити полета), вместо
#: бутонът да е GET и въведеното да се губи. Документите си имат ?restore=
#: (routes_documents._issue_new_document). Всички изброени записват едно
#: състояние („запази клиента/настройките така“) — повторението е безопасно.
#: Смяната на собствена парола нарочно липсва (не връщаме текущата парола
#: обратно в страницата).
_REPOST_ON_BUSY_ENDPOINTS = frozenset((
    "client_edit", "invoice_client_edit", "settings_page", "my_settings",
    "system_settings", "admin_user_new", "admin_user_password", "materials_save",
))
#: Над този обем не вграждаме формата обратно в страницата за грешка.
_REPOST_MAX_BYTES = 2 * 1024 * 1024


def _repost_context():
    """Скритите полета за повторно изпращане (виж _REPOST_ON_BUSY_ENDPOINTS)
    или празен речник, ако заявката не е такава."""
    try:
        if request.method != "POST" or request.endpoint not in _REPOST_ON_BUSY_ENDPOINTS:
            return {}
        if any(f.filename for f in request.files.values()):
            return {}  # файл не може да се върне в страницата
        fields = [(k, v) for k, v in request.form.items(multi=True) if k != "csrf_token"]
        if sum(len(k) + len(v) for k, v in fields) > _REPOST_MAX_BYTES:
            return {}
        url = request.full_path.rstrip("?") if request.query_string else request.path
        return {"repost_fields": fields, "repost_csrf": _get_csrf_token(),
                "retry_url": url, "back_url": request.path}
    except Exception:
        return {}


def _db_error_page(status, extra_headers=None, **ctx):
    """db_unavailable.html с повторно изпращане на формата, ако е приложимо."""
    repost = _repost_context()
    ctx.setdefault("retry_url", request.path)
    ctx.update(repost)
    headers = dict(extra_headers or {})
    if repost:
        # Страницата съдържа въведените данни — не се пази в кеша на браузъра.
        headers["Cache-Control"] = "no-store"
    return render_template("db_unavailable.html", app_name=APP_NAME, **ctx), status, headers


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
    if _is_disk_full_error(exc):
        return _db_error_page(
            503,
            title=_("Дискът е пълен"),
            message=_("Няма свободно място на диска с базата данни — последната "
                     "промяна НЕ е записана (%s).") % exc,
            hint=_("Освободете място на диска (или в споделената папка с базата) "
                  "и натиснете „Опитай пак“. Вече записаните данни са непокътнати."),
        )
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
        return _db_error_page(503, message=db.error_text(exc))  # I3: преведено
    # Одит (03.09.2026, находка №13): и `RuntimeError`-ът на `db.next_number`
    # („базата данни е заета от друг едновременен запис“) се разпознава като
    # ВРЕМЕННО заета база. Той носи точната диагноза, но не е `sqlite3.*`,
    # затова падаше в общия клон и операторът виждаше „Възникна неочаквана
    # грешка… съобщете на администратор“ вместо ясното „изчакайте няколко
    # секунди и опитайте пак“.
    _busy_text = str(exc).lower()
    if (isinstance(exc, sqlite3.OperationalError)
            or (isinstance(exc, RuntimeError) and "заета" in _busy_text)) and (
            "locked" in _busy_text or "busy" in _busy_text or "заета" in _busy_text):
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
        # Одит (04.10.2026, R4): при POST от форма (клиент, настройки,
        # служители) „Опитай пак“ изпраща СЪЩИТЕ данни отново — виж
        # _REPOST_ON_BUSY_ENDPOINTS; досега беше GET и въведеното се губеше.
        return _db_error_page(
            503, {"Retry-After": "5"},
            title=_("Базата данни е заета в момента"),
            message=_("Друга едновременна операция държи базата данни заета "
                     "(напр. друг служител записва в момента, тече архивиране "
                     "или програмата се обновява на друг компютър). Изчакайте "
                     "няколко секунди и натиснете „Опитай пак“."),
        )
    flash(_("Възникна неочаквана грешка. Опитайте отново — ако продължава, "
           "съобщете на администратор."), "error")
    try:
        # Одит (03.09.2026, находка №9): `request.referrer` е ЧУЖД вход и
        # минава през същата проверка като `?next=` при вход (виж
        # routes_auth._safe_next_target). Иначе всяка необработена грешка
        # ставаше отворено пренасочване: линк към страница, която гърми
        # (напр. /docs?type=' ), изпратен на логнат служител от сайт с
        # `Referrer-Policy: unsafe-url`, го изхвърляше на ЧУЖД домейн —
        # където пиксел-точно копие на екрана за вход прибира паролата му.
        # Възпроизведено: Location: https://evil.example.com/phish.
        target = _safe_referrer_path(request.referrer) or url_for("dashboard")
    except Exception:
        target = url_for("dashboard")
    # Одит (22.08.2026, находка №1): никога не пренасочвай към АДРЕСА, който
    # току-що гръмна — това е самият механизъм на цикъла. При съвпадение
    # падаме към таблото; ако и то е източникът, оставаме на статична
    # страница, вместо да се въртим.
    #
    # Одит (31.08.2026, находка №19): сравнението на пътища хваща САМО
    # цикъла A→A. Браузърът обаче пази Referer през 302, затова две
    # страници, които гърмят една заради друга, се въртят като A→B→A→B…
    # (възпроизведено: 14 скока до ERR_TOO_MANY_REDIRECTS, без нито едно
    # видяно flash съобщение). Затова водим и БРОЯЧ на последователните
    # аварийни пренасочвания в сесията: щом станат твърде много, спираме
    # със статична страница, независимо какви са пътищата. Броячът се
    # нулира при първата успешно отдадена страница (виж _reset_error_hops).
    if target and urlsplit(target).path == request.path:
        target = url_for("dashboard")
    hops = 0
    try:
        g.setdefault(ERROR_HOP_FLAG, True)
        hops = int(session.get(ERROR_HOP_KEY) or 0) + 1
        session[ERROR_HOP_KEY] = hops
    except Exception:
        # Без сесия (напр. счупена бисквитка) — оставаме със старото
        # поведение по пътища, вместо да гърмим вътре в обработчика на грешки.
        hops = 0
    if hops > MAX_ERROR_HOPS or urlsplit(target).path == request.path:
        try:
            session.pop(ERROR_HOP_KEY, None)
        except Exception:  # nosec B110 -- вече сме ВЪТРЕ в обработчика на
            # грешки; изключение оттук би заменило разбираемата страница с
            # гола 500 без никакво обяснение.
            pass
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
    # Одит (05.09.2026, находка №3): съобщението различава двете причини.
    # „Изберете по-малък файл“ при заявка БЕЗ файл активно насочва оператора
    # в грешна посока — той търси несъществуващ файл, вместо да разбере, че
    # документът е с твърде много редове.
    # ВНИМАНИЕ: тук НЕ бива да се пипа `request.files`/`request.form` —
    # достъпът до тях стартира разбора на формата, който хвърля СЪЩОТО
    # изключение отново, само че вече ВЪТРЕ в обработчика на грешката.
    # Затова причината се познава по типа на заявката: multipart означава
    # качване на файл, всичко останало — обикновена форма с много редове.
    if (request.content_type or "").startswith("multipart/"):
        message = _("Файлът е твърде голям (максимум 25 MB). Изберете по-малък файл.")
    else:
        message = _("Заявката е твърде голяма (максимум 25 MB). Документът вероятно "
                    "съдържа твърде много редове — разделете го на два.")
    # Одит (04.10.2026, I2): fetch заявките (импорт на Excel и др.) получават
    # JSON с преведения текст, а не пренасочване към HTML страница.
    if wants_json_response():
        return _json_error(413, message)
    flash(message, "error")
    # Одит (26.09.2026, находка №23): същото отворено пренасочване, поправено
    # на 03.09 в _handle_unexpected_error, беше пропуснато тук.
    return redirect(_safe_referrer_path(request.referrer) or url_for("dashboard"))


# ---------------------------------------------------------------- HTTP грешки (I2)
# Одит (04.10.2026, I2): 400/403/404/405/414 излизаха като голите английски
# страници на Werkzeug на всички езици (вкл. български), без път обратно.
# Сега: преведена страница в общия изглед (със страничната лента при вход,
# минимална иначе), а за fetch/JSON заявки — JSON със същия текст.
# 413 си има собствен обработчик по-горе (пренасочване с flash).
_STYLED_HTTP_ERRORS = (400, 403, 404, 405, 414)


def wants_json_response():
    """Заявката идва от JavaScript (fetch/XHR) и очаква JSON, а не страница.

    Браузърната навигация винаги изпраща `Sec-Fetch-Dest: document` и
    Accept с text/html; fetch() — `Sec-Fetch-Dest: empty`. Поддържат се и
    изричните знаци (JSON тяло, X-Requested-With, Accept само за JSON)."""
    try:
        if request.is_json or request.headers.get("X-Requested-With"):
            return True
        dest = (request.headers.get("Sec-Fetch-Dest") or "").lower()
        if dest == "empty":
            return True
        if dest:
            return False
        accept = request.accept_mimetypes
        return (accept["application/json"] > 0
                and accept["application/json"] > accept["text/html"])
    except Exception:
        return False


def _json_error(code, message, **extra):
    payload = {"ok": False, "error": message, "status": code}
    payload.update(extra)
    return jsonify(payload), code


def _http_error_texts(code):
    """(заглавие, обяснение) за всеки обработен код — преведени."""
    if code == 403:
        return (_("Нямате достъп до тази страница"),
                _("Тази страница или действие е само за администратор. Ако смятате, "
                  "че трябва да имате достъп, обърнете се към администратора."))
    if code == 404:
        return (_("Страницата не е намерена"),
                _("Адресът не съществува или записът вече е изтрит. Проверете "
                  "адреса или се върнете назад."))
    if code == 405:
        return (_("Действието не е позволено"),
                _("Този адрес не приема такъв вид заявка. Върнете се назад и "
                  "използвайте бутоните на страницата."))
    if code == 414:
        return (_("Адресът е твърде дълъг"),
                _("Адресът на страницата е твърде дълъг, за да бъде обработен. "
                  "Съкратете търсенето или филтрите и опитайте пак."))
    return (_("Заявката не може да бъде обработена"),
            _("Заявката съдържа невалидни или непълни данни. Върнете се назад "
              "и опитайте отново."))


def _error_back_url(logged_in):
    """Безопасен адрес „Назад“ — предишната страница от самата програма,
    но никога същият адрес, който току-що е върнал грешката."""
    try:
        back = _safe_referrer_path(request.referrer)
    except Exception:
        back = None
    if back and urlsplit(back).path == request.path and request.method == "GET":
        back = None
    if back:
        return back
    try:
        return url_for("dashboard") if logged_in else url_for("login")
    except Exception:
        return "/"


def render_http_error(code, message=None, title=None, login_url=None, back_url=None):
    """Преведената страница за грешка (templates/http_error.html) или JSON
    за fetch заявките. `login_url` — показва и бутон „Вход“ (изтекла сесия)."""
    default_title, default_message = _http_error_texts(code)
    title = title or default_title
    message = message or default_message
    if wants_json_response():
        return _json_error(code, message, **({"session_expired": True} if login_url else {}))
    try:
        logged_in = bool(session.get("user_id"))
    except Exception:
        logged_in = False
    back_url = back_url or _error_back_url(logged_in)
    try:
        body = render_template("http_error.html", code=code, title=title,
                               message=message, back_url=back_url,
                               login_url=login_url)
    except Exception:
        # Последна мрежа: страницата за грешка никога не бива да стане 500.
        applog.log_exception("appcore.render_http_error: неуспешно рендиране")
        from markupsafe import escape
        body = "<!doctype html><meta charset=utf-8><title>%s</title><h1>%s</h1><p>%s</p>" \
               "<p><a href=\"%s\">%s</a></p>" % (escape(title), escape(title), escape(message),
                                               escape(back_url), escape(_("Назад")))
    return body, code


def _handle_http_error(exc):
    code = getattr(exc, "code", None) or 400
    if code == 405:
        # Allow заглавието е задължително за 405 — пазим го от изключението.
        body, status = render_http_error(code)
        headers = {}
        try:
            for key, value in exc.get_headers():
                if key.lower() == "allow":
                    headers["Allow"] = value
        except Exception:  # nosec B110 -- без Allow отговорът пак е валиден 405
            pass
        return body, status, headers
    return render_http_error(code)


# Бележка (25.08.2026): тук по-рано стоеше `_sync_after_write` — after_request
# кука, която при всяка успешна промяна насрочваше автоматично качване в
# GitHub (backup.mark_dirty). Синхронизацията с GitHub беше премахната по
# заявка на потребителя, затова куката отпадна изцяло. Локалният архив (папка/
# мрежов диск) не зависи от нея — той върви по свой часови таймер
# (backup.start_auto_backup) и през бутона „Архивирай сега“.


# ---------------------------------------------------------------- auth decorators

def _clear_session_keep_lang():
    """Одит (04.10.2026, R1/I2): прекратената сесия губеше и избрания език —
    екранът за вход (с обяснението защо) излизаше на български. Езикът не е
    част от защитата, затова го пазим (както прави и изходът)."""
    lang = session.get("lang")
    session.clear()
    if lang in db.LANGUAGES:
        session["lang"] = lang


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
        _clear_session_keep_lang()
        return True
    # Одит (16.08.2026, находка №5): виж db._m007_session_epoch — смяна на
    # паролата (собствена или от администратор) СЛЕД издаването на тази
    # бисквитка прекратява сесията, дори потребителят да си остане active.
    if row["session_epoch"] != session.get("session_epoch"):
        _clear_session_keep_lang()
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


def _login_redirect(prior_uid=None):
    """Пренасочване към входа от login_required/admin_required.

    Одит (04.10.2026, R1/F4): при POST от форма на документ (сесията е
    прекратена от смяна на парола, деактивиране и т.н.) въведеното се
    запазва и се връща след входа — виж rescue_post."""
    if request.method == "POST":
        rescued = rescue_post(owner=prior_uid, valid_uid=None)
        if rescued is not None:
            return rescued
    return redirect(url_for("login", next=request.path))


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return _login_redirect()
        prior_uid = session.get("user_id")
        if _session_user_deactivated_or_missing():
            return _login_redirect(prior_uid)
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return _login_redirect()
        prior_uid = session.get("user_id")
        if _session_user_deactivated_or_missing():
            return _login_redirect(prior_uid)
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
#
# Одит (04.10.2026, R1/F4): токенът на ВЛЯЗЪЛ потребител носи и номера му
# („<случайно>.u<id>“). Така формата, изпратена след изтекла/прекратена
# сесия, казва ЧИЯ е — запазените данни се връщат само на същия потребител
# след повторния вход (виж rescue_post/claim_rescue). Номерът не е тайна и
# не дава никакви права: проверката на токена си остава сравнение със
# сесията.
_CSRF_UID_SEP = ".u"


def _get_csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_hex(16)
        uid = session.get("user_id")
        if isinstance(uid, int):
            token = "%s%s%d" % (token, _CSRF_UID_SEP, uid)
        session["_csrf_token"] = token
    return token


def _uid_from_csrf_token(token):
    """Номерът на потребителя от токена (виж по-горе) или None."""
    if not token or not isinstance(token, str) or _CSRF_UID_SEP not in token:
        return None
    tail = token.rsplit(_CSRF_UID_SEP, 1)[1]
    return int(tail) if tail.isdecimal() and len(tail) < 19 else None


_CSRF_UNSAFE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


def _check_csrf():
    if request.method not in _CSRF_UNSAFE_METHODS:
        return None
    expected = session.get("_csrf_token")
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRFToken")
    # Одит (01.10.2026, R7): изход от таб с изтекла сесия няма какво да
    # защитава — няма влязъл потребител; вместо 400 → към екрана за вход.
    if request.endpoint == "logout" and "user_id" not in session:
        return redirect(url_for("login"))
    if not expected or not sent or not hmac.compare_digest(str(sent), str(expected)):
        return _csrf_failure(sent)
    return None


def _csrf_failure(sent):
    """Одит (04.10.2026, R1/F4 и I2): при невалиден CSRF токен (изход в друг
    раздел, изтекла 12-часова сесия, смяна на паролата) потребителят виждаше
    голата английска страница „400 Bad Request“ с български текст и без път
    обратно, а цялата попълнена форма (ЧМР, фактура) се губеше.

    Сега: формите на документи се спасяват (rescue_post) — след вход
    въведеното се връща във формата; всички останали POST-ове получават
    преведена страница с връзка назад (и „Вход“, ако сесията я няма); fetch
    заявките — JSON. Самата защита не се отслабва: нищо не се записва."""
    if wants_json_response():
        return _json_error(400, _("Сесията е изтекла — презаредете страницата и влезте отново."),
                           session_expired=True)
    if request.endpoint == "login":
        # Остарял екран за вход (напр. вход/изход в друг раздел подмени
        # сесията) — просто го показваме наново, със свеж токен.
        flash(_("Страницата за вход беше остаряла — въведете данните отново."), "warning")
        return redirect(request.full_path.rstrip("?") if request.query_string
                        else url_for("login"))
    valid_uid = _valid_session_uid()
    rescued = rescue_post(owner=_uid_from_csrf_token(sent), valid_uid=valid_uid)
    if rescued is not None:
        return rescued
    logged_in = valid_uid is not None
    back = _error_back_url(logged_in)
    login_url = None if logged_in else url_for("login", next=back)
    return render_http_error(
        400,
        title=_("Формата е изтекла"),
        message=(_("Формата е заредена в предишна сесия (напр. след изход в друг раздел "
                   "или смяна на паролата) и не беше изпратена. Презаредете страницата "
                   "и попълнете отново.") if logged_in else
                 _("Сесията Ви е изтекла или е прекратена (напр. изход в друг раздел), "
                   "затова формата не беше изпратена. Влезте отново и повторете действието.")),
        login_url=login_url, back_url=back)


def _valid_session_uid():
    """Номерът на влезлия потребител, ако сесията е ВАЛИДНА (вкл. проверката
    на епохата в базата), иначе None. Не гърми — при недостъпна база None."""
    if "user_id" not in session:
        return None
    uid = session.get("user_id")
    try:
        if _session_user_deactivated_or_missing():
            return None
    except Exception:
        return None
    return uid


# ------------------------------------------------- спасяване на форма (R1/F4)
# Одит (04.10.2026, R1/F4): попълнена форма на документ, изпратена след като
# сесията вече я няма, се пази ТУК (на сървъра, не в бисквитката), под
# случаен токен в адреса за вход. Отделно хранилище от _preview_store с
# малки тавани: изпращането е без вход, значи чужд човек не бива да може да
# изтласка чуждите прегледи, нито да напълни паметта.
_rescue_store = collections.OrderedDict()
_rescue_lock = threading.Lock()
_RESCUE_TTL = 20 * 60
_RESCUE_MAX_ENTRIES = 50
_RESCUE_MAX_ENTRY_BYTES = 8 * 1024 * 1024
_RESCUE_MAX_TOTAL_BYTES = 24 * 1024 * 1024


def _rescue_evict(now):
    """Вика се при взет _rescue_lock."""
    for key in [k for k, e in _rescue_store.items() if e["expires"] < now]:
        del _rescue_store[key]
    while len(_rescue_store) > _RESCUE_MAX_ENTRIES:
        _rescue_store.popitem(last=False)
    total = sum(e["size"] for e in _rescue_store.values())
    while total > _RESCUE_MAX_TOTAL_BYTES and _rescue_store:
        total -= _rescue_store.popitem(last=False)[1]["size"]


def _rescue_target():
    """Какво представлява текущият POST: (doc_type или None, doc_id или None,
    preview_token или None) за формите на документи, иначе None."""
    endpoint = request.endpoint or ""
    view_args = request.view_args or {}
    if endpoint == "edit_document":
        return None, view_args.get("doc_id"), None
    if endpoint == "issue_from_preview":
        return None, None, view_args.get("token")
    for doc_type in DOCUMENT_FLOWS:
        if endpoint == doc_type + "_new":
            return doc_type, None, None
        if endpoint == doc_type + "_preview":
            raw = (request.form.get("edit_doc_id") or "").strip()
            return doc_type, (int(raw) if raw.isdecimal() and len(raw) < 19 else None), None
    return None


def _same_origin_post():
    """Заявката е тръгнала от страница на САМАТА програма (Origin/Referer).
    Без това чужда страница би могла да подхвърли „спасени“ данни."""
    origin = request.headers.get("Origin")
    try:
        if origin:
            return origin != "null" and urlsplit(origin).netloc == request.host
        referrer = request.referrer
        return bool(referrer) and urlsplit(referrer).netloc == request.host
    except ValueError:
        return False


def _form_url(doc_type, doc_id):
    if doc_id is not None:
        return url_for("edit_document", doc_id=doc_id)
    return url_for(doc_type + "_new")


def _doc_type_of(doc_id):
    row = get_db().execute("SELECT doc_type FROM documents WHERE id = ?", (doc_id,)).fetchone()
    return row["doc_type"] if row is not None and row["doc_type"] in DOCUMENT_FLOWS else None


def _restore_into_form(doc_type, data, doc_id, version):
    """Пази данните като преглед на ТЕКУЩИЯ (вече влязъл) потребител и връща
    адреса на формата с ?restore=… — същият механизъм като „Назад към
    формата“ от предварителния преглед."""
    if doc_type is None and doc_id is not None:
        doc_type = _doc_type_of(doc_id)
    if doc_type is None:
        return None
    token = _store_preview("doc", (doc_type, data, doc_id, version))
    return "%s?restore=%s" % (_form_url(doc_type, doc_id), token)


def rescue_post(owner, valid_uid):
    """Спасява POST от форма на документ, изпратен с невалидна/изтекла сесия.

    `owner` — чия е формата (от CSRF токена или от прекратената сесия),
    `valid_uid` — кой е влязъл В МОМЕНТА (None, ако никой). Връща отговор
    или None (не е форма на документ / не идва от самата програма / твърде
    голяма — тогава извикващият показва обикновената страница за грешка)."""
    try:
        target = _rescue_target()
    except Exception:
        target = None
    if target is None or not _same_origin_post():
        return None
    doc_type, doc_id, preview_token = target
    if preview_token is not None:
        # „Издай“ от предварителния преглед: данните вече са в прегледа
        # (обвързан с потребителя), връщаме към него след входа.
        try:
            next_url = url_for("preview_document", token=preview_token)
        except Exception:
            return None
        if valid_uid is not None:
            flash(_("Формата беше от предишна сесия — прегледайте документа и натиснете "
                    "„Издай“ отново."), "warning")
            return redirect(next_url)
        flash(_("Сесията Ви е изтекла — влезте отново, за да продължите с прегледания документ."),
              "warning")
        return redirect(url_for("login", next=next_url))
    if owner is None:
        owner = valid_uid
    if valid_uid is not None and owner != valid_uid:
        # Формата е на друг потребител, а в момента е влязъл трети —
        # данните не се показват на чужд човек.
        return None
    data = form_data()
    if "items_json" in request.form:
        data["items"] = parse_items()
    raw_version = (request.form.get("edit_doc_version") or "").strip()
    version = int(raw_version) if raw_version.isdecimal() and len(raw_version) < 19 else None
    if valid_uid is not None:
        url = _restore_into_form(doc_type, data, doc_id, version)
        if url is None:
            return None
        flash(_("Формата беше заредена в предишна сесия и НЕ е изпратена. Въведеното е "
                "възстановено — проверете го и я изпратете отново."), "warning")
        return redirect(url)
    entry = {"doc_type": doc_type, "doc_id": doc_id, "version": version, "data": data,
             "owner": owner, "nonce": None}
    size = _preview_size(entry)
    if size > _RESCUE_MAX_ENTRY_BYTES:
        return None
    if owner is None:
        # Неизвестно чия е формата (стар токен отпреди обновяването) —
        # обвързваме я със СЪЩИЯ браузър чрез случаен знак в бисквитката.
        entry["nonce"] = secrets.token_urlsafe(16)
        session["_rescue_nonce"] = entry["nonce"]
    entry["size"] = size
    token = secrets.token_urlsafe(16)
    now = time.time()
    entry["expires"] = now + _RESCUE_TTL
    with _rescue_lock:
        _rescue_store[token] = entry
        _rescue_evict(now)
    flash(_("Сесията Ви е изтекла или е прекратена (напр. изход в друг раздел). Влезте "
            "отново — попълнената форма е запазена и ще бъде възстановена."), "warning")
    return redirect(url_for("login", next=_form_url(doc_type, doc_id), rescue=token))


def claim_rescue(token, user_id, nonce=None):
    """Вика се от входа СЛЕД успешна автентикация и попълнена сесия. Връща
    адреса на формата с възстановените данни или None (с обяснение)."""
    if not token:
        return None
    now = time.time()
    with _rescue_lock:
        _rescue_evict(now)
        entry = _rescue_store.get(token)
        allowed = entry is not None and (
            entry["owner"] == user_id if entry["owner"] is not None
            else bool(nonce) and bool(entry["nonce"])
            and hmac.compare_digest(str(nonce), str(entry["nonce"])))
        if allowed:
            del _rescue_store[token]
    if not allowed:
        # Чуждият запис остава непокътнат — собственикът му още може да влезе.
        flash(_("Запазената форма не можа да бъде възстановена (изтекло време или "
                "друг потребител) — попълнете я наново."), "warning")
        return None
    try:
        url = _restore_into_form(entry["doc_type"], entry["data"], entry["doc_id"],
                                 entry["version"])
    except Exception:
        applog.log_exception("appcore.claim_rescue: неуспешно възстановяване на форма")
        url = None
    if url is None:
        flash(_("Запазената форма не можа да бъде възстановена (изтекло време или "
                "друг потребител) — попълнете я наново."), "warning")
        return None
    flash(_("Попълнената форма е възстановена — проверете данните и я изпратете отново."),
          "success")
    return url


def _reset_rescue_store():
    """Само за тестове."""
    with _rescue_lock:
        _rescue_store.clear()


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


# ---------------------------------------------------------------- .xlsx защита
# Одит (31.08.2026, находка №7, средна): защита срещу „zip-бомба“ през
# Excel импорта.
#
# .xlsx е ZIP архив. MAX_CONTENT_LENGTH (25 MB) ограничава СВИТИЯ вход, но
# нищо не ограничаваше РАЗАРХИВИРАНИЯ: помощните проверки за слети клетки и
# формули четяха цял член с `zf.read(name)`, а openpyxl чете
# `xl/sharedStrings.xml` изцяло. Обикновен служител можеше да качи валиден
# .xlsx от няколкостотин KB, чийто лист се разархивира до гигабайти.
# Измерено: 204 KB качване → ~713 MB заета памет в ЕДНА заявка (×1028
# коефициент на компресия); при тавана от 25 MB това е десетки GB → сигурен
# OOM. В мрежов режим убива сървъра за целия офис.
#
# Проверката е ПРЕДИ всяко четене (и преди load_workbook), върху
# метаданните на архива — те се четат от директорията на ZIP-а, без да се
# разархивира нищо.

#: Таван на разархивирания размер на ЕДИН член от .xlsx архива.
XLSX_MAX_MEMBER_BYTES = 80 * 1024 * 1024
#: Таван на СБОРА от разархивираните размери на всички членове.
XLSX_MAX_TOTAL_BYTES = 200 * 1024 * 1024


class XlsxTooLargeError(ValueError):
    """Архивът се разархивира до размер, който не приемаме (виж по-горе).

    Одит (04.10.2026, I4): текстът беше твърдо на български и в toast-а, и в
    JSON отговора. Сега изключението носи msgid + параметри и str() го
    превежда на езика на текущата заявка (извън заявка — български, както
    досега). Извикващите (routes_*: flash(str(exc)) / {"error": str(exc)})
    не се променят."""

    def __init__(self, msgid, **params):
        super().__init__(msgid % params)
        self.msgid = msgid
        self.params = params

    def __str__(self):
        try:
            if has_request_context():
                return _(self.msgid, **self.params)
        except Exception:  # nosec B110 -- без превод пада към българския текст
            pass
        return self.msgid % self.params


def ensure_xlsx_within_limits(file_bytes):
    """Проверява метаданните на .xlsx архива и вдига XlsxTooLargeError, ако
    разархивираният размер надхвърля таваните.

    Повреден/невалиден архив НЕ е грешка тук — пропускаме го, за да може
    самият openpyxl да върне собствената си, по-конкретна грешка (същото
    решение като в съществуващите помощни проверки)."""
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
            total = 0
            for info in zf.infolist():
                if info.file_size > XLSX_MAX_MEMBER_BYTES:
                    raise XlsxTooLargeError(
                        N_("Файлът съдържа част (%(part)s), която се разархивира до "
                           "%(size).0f MB — над допустимите %(limit).0f MB. Ако това е "
                           "истинска справка, разделете я на по-малки файлове."),
                        part=info.filename, size=info.file_size / 1e6,
                        limit=XLSX_MAX_MEMBER_BYTES / 1e6)
                total += info.file_size
                if total > XLSX_MAX_TOTAL_BYTES:
                    raise XlsxTooLargeError(
                        N_("Файлът се разархивира до над %(limit).0f MB общо — твърде "
                           "голям за обработка. Ако това е истинска справка, разделете "
                           "я на по-малки файлове."),
                        limit=XLSX_MAX_TOTAL_BYTES / 1e6)
    except zipfile.BadZipFile:
        return


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
    # Одит (26.09.2026, находка №26): и СТОЙНОСТИТЕ в реда трябва да са
    # текст — `{"po_no": 5}` гърмеше с AttributeError (.strip) при запис и
    # формата се губеше, а `{"description": ["a"]}` се записваше, но после
    # Excel износът падаше. Числата стават текст, вложените структури — празно.
    clean = []
    for it in items:
        if not isinstance(it, dict):
            continue
        row = {}
        for key, value in it.items():
            if value is None or isinstance(value, str):
                row[str(key)] = value
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                row[str(key)] = str(value)
            else:
                row[str(key)] = ""
        clean.append(row)
    return clean


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


def paginate_documents(con, where_sql, params, page, page_size=100, order_by="d.id DESC",
                       columns="d.*", count_cap="auto"):
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
    различава от подадения, ако е бил извън диапазона (clamp).

    Одит (05.09.2026, находка №10): при ТЪРСЕНЕ броенето и извличането
    ползваха една и съща WHERE клауза, значи всеки ред се обхождаше ДВА
    ПЪТИ — а при търсене предикатът е Python функция върху цялото JSON тяло
    (виж db._ci_contains), тоест вторият пас е точно толкова скъп, колкото
    първият. Измерено самостоятелно: COUNT-ът сам по себе си отнемаше 1 418
    от 2 807 ms.

    Сега при ПЪРВАТА страница вземаме един ред в повече от нужното: ако се
    върнат по-малко от `page_size + 1` реда, вече ЗНАЕМ точния общ брой без
    второ обхождане. За следващите страници (и когато има какво още да се
    чете) броенето остава — там операторът вече е стеснил търсенето и
    цената е оправдана.

    Одит (01.10.2026, F1b/F2): подредбата и прескачането стават само върху
    id-тата (покриващ индекс, без да се чете ~7 KB JSON на всеки прескочен
    ред), после се зареждат само редовете на страницата (`columns`). При
    търсене броенето спира след `count_cap` съвпадения — броят тогава е
    AtLeastCount („1000+“). "auto" = таван само при търсене.
    """
    # Одит (26.09.2026, находка №26): ?page=-1 показваше „Страница -1 от 2“,
    # а огромно число гърмеше с OverflowError в OFFSET.
    try:
        page = min(max(1, int(page or 1)), 10 ** 9)
    except (TypeError, ValueError):
        page = 1
    if count_cap == "auto":
        count_cap = SEARCH_COUNT_CAP if _is_search_where(where_sql) else None

    def fetch(limit, offset):
        ob = order_by
        if offset >= _DEEP_OFFSET and ob == "d.id DESC":
            # Дълбока страница: сортиране на id-тата от индекс вместо обхождане
            # на листата на таблицата в ред по id.
            ob = "+d.id DESC"
        ids = [r[0] for r in con.execute(
            "SELECT d.id FROM documents d " + where_sql +  # nosec B608 -- where_sql е съставен само от „?“ плейсхолдъри от викащия код
            " ORDER BY " + ob + " LIMIT ? OFFSET ?",  # nosec B608 -- order_by е константа от викащия код, никога request.args
            list(params) + [limit, offset]).fetchall()]
        if not ids:
            return []
        rows = con.execute(
            "SELECT " + columns + ", u.full_name AS author FROM documents d"  # nosec B608 -- columns е константа от викащия код
            " LEFT JOIN users u ON u.id = d.created_by"
            " WHERE d.id IN (%s)" % ",".join("?" * len(ids)), ids).fetchall()
        pos = {doc_id: n for n, doc_id in enumerate(ids)}
        return sorted(rows, key=lambda r: pos[r["id"]])

    docs = fetch(page_size + 1, (page - 1) * page_size)
    if page == 1 and len(docs) <= page_size:
        # Всичко се побра на една страница — броят е известен без COUNT.
        return docs, 1, 1, len(docs)
    docs = docs[:page_size]
    if count_cap:
        limit = max(count_cap, page * page_size)
        total_count = con.execute(
            "SELECT COUNT(*) AS c FROM (SELECT 1 FROM documents d " + where_sql +  # nosec B608 -- виж бележката по-горе
            " LIMIT ?)", list(params) + [limit + 1]).fetchone()["c"]
        if total_count > limit:
            return docs, page, limit // page_size + 1, AtLeastCount(limit)
    else:
        total_count = con.execute(
            "SELECT COUNT(*) AS c FROM documents d " + where_sql, params).fetchone()["c"]  # nosec B608 -- виж бележката по-горе
    total_pages = max(1, (total_count + page_size - 1) // page_size)
    if page > total_pages:
        # Поисканата страница е извън диапазона — извличаме последната.
        page = total_pages
        docs = fetch(page_size, (page - 1) * page_size)
    return docs, page, total_pages, total_count


#: Одит (01.10.2026, F2): след толкова съвпадения при търсене спираме да броим.
SEARCH_COUNT_CAP = 1000
#: От това отместване нататък id-тата се сортират от индекс (виж fetch по-горе).
_DEEP_OFFSET = 1000


def _is_search_where(where_sql):
    return "document_search" in where_sql or "ci_contains(" in where_sql


class AtLeastCount(int):
    """Брой, който е само долна граница — показва се като „1000+“."""
    at_least = True

    def __str__(self):
        return "%d+" % int(self)

    def __html__(self):
        return str(self)


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
#: реално ползва 1–2 наведнъж (текущата форма + връщане назад).
#:
#: Одит (03.09.2026, находка №7): вдигнато от 20 на 200. В мрежов режим
#: ВСИЧКИ оператори споделят ЕДИН процес, тоест и един общ таван — а всяко
#: натискане на „Предварителен преглед“ и всяко запазване при грешка
#: (дублиран номер, конфликт, отменена партида) заема по един запис. При
#: 20 стигаха трима-четирима души за един следобед, за да си изхвърлят
#: взаимно въведеното. Записът е няколко килобайта, значи 200 са
#: пренебрежими за паметта, а TTL-ът от 30 минути така или иначе чисти.
_PREVIEW_MAX_ENTRIES = 200
#: Одит (01.10.2026, F8): и таван по обем (по размера на данните като JSON) —
#: 200 големи групови прегледа държаха ~243 MB памет.
_PREVIEW_MAX_BYTES = 32 * 1024 * 1024
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
    total = sum(entry[4] if len(entry) > 4 else 0 for entry in _preview_store.values())
    while total > _PREVIEW_MAX_BYTES and len(_preview_store) > 1:
        entry = _preview_store.popitem(last=False)[1]
        total -= entry[4] if len(entry) > 4 else 0


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
    size = _preview_size(payload)
    with _preview_lock:
        _preview_store[token] = (time.time() + _PREVIEW_TTL, kind, payload, user_id, size)
        # СЛЕД вписването, не само преди него (_cleanup_previews по-горе):
        # иначе таванът реално щеше да е _PREVIEW_MAX_ENTRIES + 1.
        _evict_previews_over_limit()
    return token


def _preview_size(payload):
    try:
        return len(json.dumps(payload, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return 0


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
