# -*- coding: utf-8 -*-
"""Общи fixtures за тестовете.

Основната цел тук е ИЗОЛАЦИЯ: тестовете никога не пипат реалната база
данни или `pacho_config.json` на разработчика. Всеки тест, който има нужда
от база, получава чисто нова временна SQLite база във временна папка.

Модулът `db` изчислява `DB_PATH` при импорт (от `config.resolve_db_path`),
затова го пренасочваме към временния файл чрез monkeypatch на атрибута на
модула, преди да извикаме `db.init_db()`.
"""
import os
import sys

import pytest

# Коренът на проекта (папката над tests/) трябва да е в пътя, за да се
# импортират db, config, barcode128, updater и т.н.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture
def tmp_db_path(tmp_path):
    """Път до нова временна база данни (файлът още не съществува)."""
    return os.path.join(str(tmp_path), "test_pacho.db")


@pytest.fixture
def db_module(tmp_db_path, monkeypatch):
    """Модулът `db`, пренасочен към временна база и инициализиран със схемата.

    Връща самия модул, за да могат тестовете да ползват db.next_number,
    db.get_db, db.save_settings и т.н. срещу изолирана база.
    """
    import db as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_db_path)
    monkeypatch.setattr(db_mod, "SECRET_PATH", tmp_db_path + ".secret")
    db_mod.init_db()
    return db_mod


@pytest.fixture
def con(db_module):
    """Отворена връзка към временната база; затваря се автоматично след теста."""
    c = db_module.get_db()
    try:
        yield c
    finally:
        c.close()
