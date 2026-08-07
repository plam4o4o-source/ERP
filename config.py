# -*- coding: utf-8 -*-
"""Начална конфигурация на ПачоЛогистик (bootstrap).

Съдържа само настройки, нужни ПРЕДИ да може да се отвори базата данни —
основно къде физически да стои файлът ѝ (локално или на мрежов диск).
Всички останали настройки (тема, архивиране и т.н.) се пазят в самата база.
"""
import json
import os
import sys

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
                cfg.update(json.load(f))
        except (ValueError, OSError):
            pass
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
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(to_write, f, ensure_ascii=False, indent=2)
    return cfg  # декриптирана версия — за директна употреба от извикващия код


def resolve_db_path(base_dir, default_filename="pacho_logistic.db"):
    cfg = load_config()
    custom = (cfg.get("db_path") or "").strip()
    if custom:
        return custom
    return os.path.join(base_dir, default_filename)
