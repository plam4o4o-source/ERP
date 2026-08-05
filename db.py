# -*- coding: utf-8 -*-
"""База данни (SQLite) на ПачоЛогистик — схема, инициализация и номерация."""
import os
import sqlite3
import secrets
import sys
from datetime import date

from werkzeug.security import generate_password_hash

import config as appconfig

# В компилираната .exe версия базата данни стои до самия .exe файл,
# а не във временната папка на PyInstaller.
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Пътят може да бъде пренасочен към мрежов диск чрез pacho_config.json
# (виж config.py и „Системни настройки“ в програмата).
DB_PATH = appconfig.resolve_db_path(BASE_DIR)
SECRET_PATH = os.path.join(BASE_DIR, ".secret_key")

# Типове документи: префикс за баркода и заглавие на български
DOC_TYPES = {
    "cmr": {"prefix": "CMR", "title": "ЧМР товарителница"},
    "packing": {"prefix": "OPL", "title": "Опаковъчен лист"},
    "pallet": {"prefix": "PAL", "title": "Палетна карта"},
    "waybill": {"prefix": "TOV", "title": "Товарителница (вътрешен превоз)"},
    "dualuse": {"prefix": "DUD", "title": "Декларация за стоки с двойна употреба"},
    "export_it": {"prefix": "EXI", "title": "Декларация за износ (Италия)"},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    full_name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'employee',      -- 'admin' или 'employee'
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

-- Индивидуални настройки за всеки служител (тема и др.), не общи за фирмата.
CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (user_id, key),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    address TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    postcode TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT '',
    eik TEXT NOT NULL DEFAULT '',
    vat TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    contact TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Пунктове за разтоварване на клиент — неограничен брой на клиент (напр.
-- различни складове/обекти на една и съща фирма), за избор в ЧМР вместо
-- ръчно въвеждане на адреса всеки път.
CREATE TABLE IF NOT EXISTS client_unload_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    postcode TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_unload_points_client ON client_unload_points(client_id);

CREATE TABLE IF NOT EXISTS counters (
    doc_type TEXT NOT NULL,
    year INTEGER NOT NULL,
    last INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (doc_type, year)
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_type TEXT NOT NULL,
    number TEXT NOT NULL,
    year INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    barcode TEXT NOT NULL UNIQUE,
    data TEXT NOT NULL DEFAULT '{}',
    created_by INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (created_by) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_documents_type_year ON documents(doc_type, year, seq);
"""


def get_db():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir and not os.path.isdir(db_dir):
        raise RuntimeError(
            "Папката за базата данни не съществува или мрежовият диск не е "
            "достъпен: %s — проверете пътя в „Системни настройки“." % db_dir
        )
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def get_secret_key():
    """Постоянен таен ключ за сесиите, пази се във файл до базата."""
    if os.path.exists(SECRET_PATH):
        with open(SECRET_PATH, "r", encoding="utf-8") as f:
            key = f.read().strip()
            if key:
                return key
    key = secrets.token_hex(32)
    with open(SECRET_PATH, "w", encoding="utf-8") as f:
        f.write(key)
    return key


def init_db():
    con = get_db()
    con.executescript(SCHEMA)
    # Първоначален администраторски акаунт (сменете паролата след първия вход!)
    row = con.execute("SELECT COUNT(*) AS c FROM users").fetchone()
    if row["c"] == 0:
        con.execute(
            "INSERT INTO users (username, password_hash, full_name, role, active)"
            " VALUES (?, ?, ?, 'admin', 1)",
            ("admin", generate_password_hash("admin123"), "Администратор"),
        )
    # Данни на фирмата изпращач по подразбиране (само при чисто нова база —
    # редактируеми по-късно от „Фирма изпращач“). Взети от реални документи
    # на фирмата (ЧМР и декларация за двойна употреба).
    settings_row = con.execute("SELECT COUNT(*) AS c FROM settings").fetchone()
    if settings_row["c"] == 0:
        save_settings(con, {
            "sender_name": "ББС - България ООД / BBS Bulgaria Ltd",
            "sender_address": "ул. Георги Димитров 47",
            "sender_city": "Яворец",
            "sender_postcode": "5334",
            "sender_country": "България",
            "sender_eik": "205284599",
            "sender_vat": "",
            "sender_phone": "",
            "sender_email": "",
            "sender_person": "",
        })
    con.commit()
    con.close()


def next_number(con, doc_type):
    """Следващ пореден номер за типа документ в текущата година.

    Годината се взима автоматично от системната дата, така че всяка нова
    година номерацията започва отначало от 0001.
    Връща (number, year, seq, barcode), напр. ('0001/2026', 2026, 1, 'CMR-2026-0001').
    """
    if doc_type not in DOC_TYPES:
        raise ValueError("Непознат тип документ: %r" % doc_type)
    year = date.today().year
    row = con.execute(
        "SELECT last FROM counters WHERE doc_type = ? AND year = ?",
        (doc_type, year),
    ).fetchone()
    if row is None:
        seq = 1
        con.execute(
            "INSERT INTO counters (doc_type, year, last) VALUES (?, ?, 1)",
            (doc_type, year),
        )
    else:
        seq = row["last"] + 1
        con.execute(
            "UPDATE counters SET last = ? WHERE doc_type = ? AND year = ?",
            (seq, doc_type, year),
        )
    number = "%04d/%d" % (seq, year)
    barcode = "%s-%d-%04d" % (DOC_TYPES[doc_type]["prefix"], year, seq)
    return number, year, seq, barcode


def get_settings(con):
    return {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM settings")}


def save_settings(con, values):
    for key, value in values.items():
        con.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_unload_points(con, client_id):
    return con.execute(
        "SELECT * FROM client_unload_points WHERE client_id = ? ORDER BY id",
        (client_id,),
    ).fetchall()


def get_unload_points_map(con, client_ids=None):
    """Всички пунктове за разтоварване, групирани по client_id — за
    еднократно вграждане в JSON списъка с клиенти (за автоматично
    попълване във формите), вместо отделна заявка за всеки клиент."""
    sql = "SELECT * FROM client_unload_points"
    params = ()
    if client_ids is not None:
        client_ids = list(client_ids)
        if not client_ids:
            return {}
        sql += " WHERE client_id IN (%s)" % ",".join("?" * len(client_ids))
        params = tuple(client_ids)
    sql += " ORDER BY id"
    result = {}
    for r in con.execute(sql, params):
        result.setdefault(r["client_id"], []).append(dict(r))
    return result


def save_unload_points(con, client_id, points):
    """Заменя всички пунктове за разтоварване на клиента с подадения
    списък (изтрива старите, вмъква новите) — прост и надежден начин да
    се поддържа неограничен брой редове, подадени от формата като JSON."""
    con.execute("DELETE FROM client_unload_points WHERE client_id = ?", (client_id,))
    for p in points or []:
        if not isinstance(p, dict):
            continue
        row = {k: (p.get(k) or "").strip() for k in
               ("label", "address", "city", "postcode", "country")}
        if not any(row.values()):
            continue
        con.execute(
            "INSERT INTO client_unload_points"
            " (client_id, label, address, city, postcode, country)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (client_id, row["label"], row["address"], row["city"],
             row["postcode"], row["country"]),
        )


THEMES = {
    "light": "Светла (по подразбиране)",
    "dark": "Тъмна",
    "blue": "Синя / корпоративна",
    "green": "Зелена",
    "contrast": "Висок контраст",
    "sepia": "Кафява / топла",
}
DEFAULT_THEME = "light"


def get_user_settings(con, user_id):
    rows = con.execute(
        "SELECT key, value FROM user_settings WHERE user_id = ?", (user_id,)
    ).fetchall()
    return {r["key"]: r["value"] for r in rows}


def get_user_theme(con, user_id):
    row = con.execute(
        "SELECT value FROM user_settings WHERE user_id = ? AND key = 'theme'",
        (user_id,),
    ).fetchone()
    theme = row["value"] if row else DEFAULT_THEME
    return theme if theme in THEMES else DEFAULT_THEME


def save_user_settings(con, user_id, values):
    for key, value in values.items():
        con.execute(
            "INSERT INTO user_settings (user_id, key, value) VALUES (?, ?, ?)"
            " ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
            (user_id, key, value),
        )
