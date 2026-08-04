# -*- coding: utf-8 -*-
"""База данни (SQLite) на ПачоЛогистик — схема, инициализация и номерация."""
import os
import sqlite3
import secrets
from datetime import date

from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "pacho_logistic.db")
SECRET_PATH = os.path.join(BASE_DIR, ".secret_key")

# Типове документи: префикс за баркода и заглавие на български
DOC_TYPES = {
    "cmr": {"prefix": "CMR", "title": "ЧМР товарителница"},
    "packing": {"prefix": "OPL", "title": "Опаковъчен лист"},
    "pallet": {"prefix": "PAL", "title": "Палетна карта"},
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
    con = sqlite3.connect(DB_PATH)
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
