# -*- coding: utf-8 -*-
"""Тестове за автентикацията на ниво данни (хеширане на пароли, роли).

Проверките на маршрутите (login_required/admin_required) ще се добавят
след разделянето на app.py на blueprints във Фаза 3 — тогава ще е лесно
да се вдигне Flask test-client без страничните ефекти на текущия app.py.
"""
from werkzeug.security import check_password_hash, generate_password_hash


def test_seed_admin_created(con):
    row = con.execute(
        "SELECT username, role, active FROM users WHERE username = 'admin'"
    ).fetchone()
    assert row is not None
    assert row["role"] == "admin"
    assert row["active"] == 1


def test_seed_admin_password_is_hashed_not_plaintext(con):
    row = con.execute(
        "SELECT password_hash FROM users WHERE username = 'admin'"
    ).fetchone()
    # Паролата никога не се пази в чист текст.
    assert row["password_hash"] != "admin123"
    assert check_password_hash(row["password_hash"], "admin123") is True
    assert check_password_hash(row["password_hash"], "грешна") is False


def test_password_hash_roundtrip():
    h = generate_password_hash("СуперТайна42")
    assert check_password_hash(h, "СуперТайна42") is True
    assert check_password_hash(h, "супертайна42") is False  # чувствителна към регистър


def test_username_is_unique(con):
    import sqlite3
    with __import__("pytest").raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO users (username, password_hash) VALUES ('admin', 'x')"
        )


def test_default_role_is_employee(con):
    con.execute(
        "INSERT INTO users (username, password_hash) VALUES ('ivan', 'x')"
    )
    row = con.execute(
        "SELECT role FROM users WHERE username = 'ivan'"
    ).fetchone()
    assert row["role"] == "employee"
