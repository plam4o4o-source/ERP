# -*- coding: utf-8 -*-
"""Одит (01.10.2026, Q1): тестовете ползват евтин хеш на паролите
(conftest._fast_password_hashing) — тук пазим, че това не изтича в
продукцията и че подмяната наистина е активна в тестовете."""
import subprocess
import sys

import pytest
from werkzeug.security import check_password_hash, generate_password_hash

from conftest import ROOT, post_with_csrf


def _stored_hash(db_module, username):
    con = db_module.get_db()
    try:
        return con.execute("SELECT password_hash FROM users WHERE username = ?",
                           (username,)).fetchone()["password_hash"]
    finally:
        con.close()


@pytest.mark.real_password_hash
def test_production_hashes_passwords_with_scrypt(admin_client, db_module):
    assert generate_password_hash("x").startswith("scrypt:")
    # dummy хешът се смята при импорт — в процеса на тестовете може вече да е
    # подменен, затова го проверяваме в чист процес.
    out = subprocess.run(
        [sys.executable, "-c", "import routes_auth; print(routes_auth._DUMMY_PASSWORD_HASH)"],
        cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert out.stdout.startswith("scrypt:"), out.stderr[-2000:]
    assert _stored_hash(db_module, "admin").startswith("scrypt:")  # db.init_db

    resp = post_with_csrf(admin_client, "/admin/users/new", {
        "username": "scrypt_user", "full_name": "Скрипт", "password": "дълга-парола-123",
        "role": "employee"}, csrf_source_url="/admin/users")
    assert resp.status_code == 302
    stored = _stored_hash(db_module, "scrypt_user")
    assert stored.startswith("scrypt:")
    assert check_password_hash(stored, "дълга-парола-123")


def test_tests_use_the_cheap_hash_by_default(db_module):
    import routes_auth

    assert generate_password_hash("x").startswith("pbkdf2:sha256:1$")
    assert routes_auth._DUMMY_PASSWORD_HASH.startswith("pbkdf2:sha256:1$")
    assert _stored_hash(db_module, "admin").startswith("pbkdf2:sha256:1$")
