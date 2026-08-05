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


def test_seed_admin_must_change_password(con):
    """C1: паролата по подразбиране (admin123) е публично документирана —
    първият вход трябва да задължи смяна на паролата."""
    row = con.execute(
        "SELECT must_change_password FROM users WHERE username = 'admin'"
    ).fetchone()
    assert row["must_change_password"] == 1


def test_new_user_default_does_not_require_change(con):
    con.execute(
        "INSERT INTO users (username, password_hash) VALUES ('ivan', 'x')"
    )
    row = con.execute(
        "SELECT must_change_password FROM users WHERE username = 'ivan'"
    ).fetchone()
    # Схемният default е 0 — app.py е този, който изрично налага 1 при
    # admin_user_new/admin_user_password (тествано на ниво маршрут в бъдещи
    # тестове с Flask test-client, след Фаза 3).
    assert row["must_change_password"] == 0


def test_existing_admin_with_default_password_flagged_on_upgrade(db_module, tmp_path, monkeypatch):
    """C1, случай на обновяване: съществуваща база отпреди тази версия,
    чийто 'admin' все още ползва фабричната парола, трябва да бъде
    задължена за смяна и при повторно init_db() (не само при нова инсталация)."""
    con = db_module.get_db()
    con.execute("DELETE FROM users")  # изчистваме засетия при create-fixture admin
    con.execute(
        "INSERT INTO users (username, password_hash, role, must_change_password)"
        " VALUES ('admin', ?, 'admin', 0)",
        (generate_password_hash("admin123"),),
    )
    con.commit()
    con.close()

    db_module.init_db()  # симулира рестарт на приложението след обновяване

    con = db_module.get_db()
    row = con.execute(
        "SELECT must_change_password FROM users WHERE username = 'admin'"
    ).fetchone()
    con.close()
    assert row["must_change_password"] == 1


def test_existing_admin_with_changed_password_not_flagged(db_module):
    """Ако администраторът вече е сменил паролата, init_db() не трябва да
    я задължава повторно (не сме пипали существуващи данни излишно)."""
    con = db_module.get_db()
    con.execute("DELETE FROM users")
    con.execute(
        "INSERT INTO users (username, password_hash, role, must_change_password)"
        " VALUES ('admin', ?, 'admin', 0)",
        (generate_password_hash("нещо-съвсем-различно"),),
    )
    con.commit()
    con.close()

    db_module.init_db()

    con = db_module.get_db()
    row = con.execute(
        "SELECT must_change_password FROM users WHERE username = 'admin'"
    ).fetchone()
    con.close()
    assert row["must_change_password"] == 0


def test_ensure_column_is_idempotent(con, db_module):
    # Повторно извикване на init_db (напр. при рестарт на приложението) не
    # трябва да гръмне с "duplicate column name".
    db_module.init_db()
    db_module.init_db()
    cols = [r["name"] for r in con.execute("PRAGMA table_info(users)")]
    assert cols.count("must_change_password") == 1
