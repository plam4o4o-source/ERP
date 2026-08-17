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
    custom = (cfg.get("db_path") or "").strip()
    if custom:
        return custom
    return os.path.join(base_dir, default_filename)
