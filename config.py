# -*- coding: utf-8 -*-
"""Начална конфигурация на ПачоЛогистик (bootstrap).

Съдържа само настройки, нужни ПРЕДИ да може да се отвори базата данни —
основно къде физически да стои файлът ѝ (локално или на мрежов диск).
Всички останали настройки (тема, архивиране и т.н.) се пазят в самата база.
"""
import json
import os
import sys

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
}


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except (ValueError, OSError):
            pass
    return cfg


def save_config(values):
    cfg = load_config()
    cfg.update(values)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg


def resolve_db_path(base_dir, default_filename="pacho_logistic.db"):
    cfg = load_config()
    custom = (cfg.get("db_path") or "").strip()
    if custom:
        return custom
    return os.path.join(base_dir, default_filename)
