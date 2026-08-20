# -*- coding: utf-8 -*-
"""Начална конфигурация на ПачоЛогистик (bootstrap).

Съдържа само настройки, нужни ПРЕДИ да може да се отвори базата данни —
основно къде физически да стои файлът ѝ (локално или на мрежов диск).
Всички останали настройки (тема, архивиране и т.н.) се пазят в самата база.
"""
import json
import os
import sys

import applog
import secrets_store

if getattr(sys, "frozen", False):
    _BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_PATH = os.path.join(_BASE_DIR, "pacho_config.json")

DEFAULTS = {
    # Ако е зададен, базата данни се отваря от този път (напр. мрежов диск:
    # \\SERVER\share\pacho_logistic.db или Z:\ПачоЛогистик\pacho_logistic.db).
    # Празно = по подразбиране, до .exe/скрипта.
    "db_path": "",
    # Мрежов режим: слуша на 0.0.0.0, за да може да се отваря от други
    # компютри в локалната мрежа през браузър (http://IP-на-сървъра:5000).
    "network_mode": False,
    "network_port": 5000,
    # Автоматична синхронизация на базата данни с GitHub (частно хранилище).
    # Пазят се тук (не в самата база), защото при чисто нова инсталация
    # трябва да знаем откъде да изтеглим базата ПРЕДИ тя изобщо да съществува.
    "gh_owner": "",
    "gh_repo": "",
    "gh_branch": "main",
    "gh_path": "pacho_logistic.db",
    "gh_token": "",  # nosec B105 -- само подразбираща се ПРАЗНА стойност (без синхронизация), не истински secret; реалният токен се пази шифрован (виж secrets_store.py)
    "gh_auto_sync": False,
}


# Одит (19.08.2026, находка №30, средна): ключовете, които ОСТАНАЛИЯТ код
# третира безусловно като текст (`.strip()`, `.startswith()`, конкатенация
# в URL). Списъкът се строи от самите DEFAULTS, за да не може ново текстово
# поле да бъде добавено там и забравено тук.
_TEXT_KEYS = tuple(k for k, v in DEFAULTS.items() if isinstance(v, str))


def _coerce_text_setting(key, value, default):
    """Одит (19.08.2026, находка №30, средна): ръчната редакция на
    pacho_config.json е ДОКУМЕНТИРАНИЯТ bootstrap за мрежови инсталации —
    човек с текстов редактор лесно пише `"db_path": 12345` (без кавички)
    или подава списък. Поправката на находка №45 покри само `network_port`;
    всички ОСТАНАЛИ полета продължаваха да гърмят необработено ПРИ САМИЯ
    ИМПОРТ на db.py (`config.resolve_db_path` → `AttributeError: 'int'
    object has no attribute 'strip'`, а `{"gh_token": 42}` — вътре в
    `load_config` през `secrets_store.decrypt` → `startswith`). В
    компилираната .exe версия (--windowed) това е ТИХА смърт: програмата
    просто не се отваря, без прозорец и без съобщение.

    Толерантно привеждане вместо срив, по същия модел като
    `get_network_port` по-долу: число се ползва като текст (най-вероятно е
    просто забравена кавичка), а стойност без смислен текстов еквивалент
    (списък/речник/булево) пада към подразбиращата се — и в двата случая с
    предупреждение в лога, за да има следа какво е било пренебрегнато."""
    if isinstance(value, str):
        return value
    if value is None:
        return default
    if not isinstance(value, bool) and isinstance(value, (int, float)):
        applog.log_warning(
            "config.load_config",
            "стойността на %s в pacho_config.json е число (%r), а се очаква "
            "текст — използвам я като текст (\"%s\"); ако е пропусната "
            "кавичка, поправете файла." % (key, value, value))
        return str(value)
    applog.log_warning(
        "config.load_config",
        "стойността на %s в pacho_config.json е от неподходящ тип (%r), а се "
        "очаква текст — пренебрегвам я и използвам подразбиращата се (%r)."
        % (key, value, default))
    return default


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            # Одит (16.08.2026, находка №45, дребна): валиден JSON, който
            # НЕ е речник (напр. число, низ, гол списък), водеше до
            # TypeError от cfg.update(loaded) по-долу — извън обхванатите
            # (ValueError, OSError), значи гърмеше при самия импорт на
            # модула (config.load_config() се вика при импорт от db.py).
            if isinstance(loaded, dict):
                cfg.update(loaded)
            else:
                raise ValueError("pacho_config.json не съдържа JSON обект (речник)")
        except (ValueError, OSError) as exc:
            # Одит (находка №24): преди тази поправка развален/отрязан
            # файл (напр. токов удар по средата на save_config по-долу)
            # водеше до ТИХО падане към DEFAULTS — db_path="" означава, че
            # програмата тихо създава НОВА празна локална база данни,
            # докато истинската стои недокосната на мрежовия диск/стария
            # път — за потребителя изглежда като пълна загуба на данните,
            # без никакъв признак какво се е случило. Пазим повредения
            # файл настрани (за диагностика/ръчно възстановяване) и
            # логваме предупреждение, вместо мълчаливо да продължим.
            try:
                corrupt_copy = CONFIG_PATH + ".corrupt"
                with open(CONFIG_PATH, "rb") as src, open(corrupt_copy, "wb") as dst:
                    dst.write(src.read())
            except OSError:
                pass
            applog.log_warning(
                "config.load_config",
                "pacho_config.json е повреден/невалиден (%s) — връщам стойности "
                "по подразбиране (db_path и мрежовите/GitHub настройки НЕ важат "
                "до ръчна поправка); копие на повредения файл е запазено като "
                "pacho_config.json.corrupt за диагностика." % exc)
    # Одит (19.08.2026, находка №30, средна): привеждането става ТУК, преди
    # първата употреба (декриптирането на gh_token точно отдолу вече вика
    # `startswith` върху стойността) — така всички останали модули четат
    # cfg[...] със сигурността, че текстовите полета са текст.
    for key in _TEXT_KEYS:
        cfg[key] = _coerce_text_setting(key, cfg.get(key), DEFAULTS[key])
    # gh_token се пази шифрован на диска (виж secrets_store.py) — тук се
    # декриптира за употреба в паметта, за да не се налага да се пипа кодът
    # навсякъде другаде, където се чете cfg["gh_token"].
    if cfg.get("gh_token"):
        cfg["gh_token"] = secrets_store.decrypt(CONFIG_PATH, cfg["gh_token"])
    return cfg


def save_config(values):
    cfg = load_config()
    cfg.update(values)
    to_write = dict(cfg)
    if to_write.get("gh_token"):
        to_write["gh_token"] = secrets_store.encrypt(CONFIG_PATH, to_write["gh_token"])
    # Одит (16.08.2026, находка №24): преди тази поправка се записваше
    # ДИРЕКТНО върху CONFIG_PATH ("w" отрязва файла ВЕДНАГА при отваряне)
    # — токов удар/паднал мрежов диск точно по средата на json.dump()
    # оставя отрязан/невалиден JSON, който load_config() по-горе преди
    # затваряше тихо (виж поправката там). Запис през временен файл +
    # os.replace() е атомарен — или старият пълен файл остава непокътнат,
    # или новият, също пълен, го замества; никога отрязано междинно
    # състояние, каквото и да прекъсне записа.
    tmp_path = CONFIG_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(to_write, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, CONFIG_PATH)
    return cfg  # декриптирана версия — за директна употреба от извикващия код


def get_network_port(cfg, default=5000):
    """Одит (16.08.2026, находка №45, дребна): нечислов `network_port` в
    ръчно редактиран pacho_config.json (традиционният bootstrap за
    мрежови инсталации е точно ръчна редакция на този файл) водеше до
    необработен ValueError от голото `int(...)` на трите места, които го
    четяха (app.py, routes_admin.py) — тиха смърт при старт БЕЗ никакъв
    прозорец/съобщение (в frozen режим само traceback в лога). Толерантен
    parse с ясен fallback вместо срив."""
    raw = cfg.get("network_port")
    try:
        return int(raw) if raw else default
    except (TypeError, ValueError):
        applog.log_warning(
            "config.get_network_port",
            "невалидна стойност network_port=%r в pacho_config.json — "
            "използвам подразбиращия се порт %d" % (raw, default))
        return default


def resolve_db_path(base_dir, default_filename="pacho_logistic.db"):
    cfg = load_config()
    # str(...) е втора защитна мрежа към привеждането в load_config (одит
    # 19.08.2026, находка №30): тази функция се вика при самия ИМПОРТ на
    # db.py — единственото място, където необработено изключение означава
    # програмата изобщо да не се стартира, при това без прозорец.
    custom = str(cfg.get("db_path") or "").strip()
    if custom:
        return custom
    return os.path.join(base_dir, default_filename)
