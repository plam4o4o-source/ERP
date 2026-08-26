# -*- coding: utf-8 -*-
"""Одит (25.08.2026, предложение Д): валидация на постоянния публичен адрес.

Този адрес влиза буквално в QR кода на ПЕЧАТНАТА бланка. Невалиден хост
(без домейн, с път/параметри, с невалидни знаци) означаваше траен неработещ
QR върху официален документ. Проверката отсича очевидно невалидните адреси
при запис, вместо да ги открием чак когато линкът не се отваря.
"""
import db
from conftest import post_with_csrf


def _post_url(admin_client, value):
    return post_with_csrf(admin_client, "/admin/system", {
        "form": "public_base_url", "public_base_url": value,
    }, csrf_source_url="/my-settings", follow_redirects=True)


def _saved(db_module):
    con = db_module.get_db()
    try:
        return db.get_settings(con).get("public_base_url", "")
    finally:
        con.close()


def test_valid_url_is_saved(admin_client, db_module):
    resp = _post_url(admin_client, "https://pacho.example.com")
    assert resp.status_code == 200
    assert _saved(db_module) == "https://pacho.example.com"


def test_bare_domain_gets_https_and_is_saved(admin_client, db_module):
    _post_url(admin_client, "pacho.example.com")
    assert _saved(db_module) == "https://pacho.example.com"


def test_url_with_a_port_is_accepted(admin_client, db_module):
    _post_url(admin_client, "https://pacho.example.com:8443")
    assert _saved(db_module) == "https://pacho.example.com:8443"


def test_scheme_without_a_host_is_rejected(admin_client, db_module):
    resp = _post_url(admin_client, "https://")
    assert "домейн" in resp.data.decode()
    assert _saved(db_module) == "", "адрес без домейн не бива да се записва"


def test_url_with_a_path_is_rejected(admin_client, db_module):
    resp = _post_url(admin_client, "https://pacho.example.com/some/path")
    assert "без път" in resp.data.decode()
    assert _saved(db_module) == ""


def test_url_with_invalid_host_characters_is_rejected(admin_client, db_module):
    resp = _post_url(admin_client, "https://pacho|bad.example")
    body = resp.data.decode()
    assert "валиден" in body
    assert _saved(db_module) == ""


def test_clearing_the_url_is_allowed(admin_client, db_module):
    _post_url(admin_client, "https://pacho.example.com")
    assert _saved(db_module) == "https://pacho.example.com"
    _post_url(admin_client, "")
    assert _saved(db_module) == "", "изчистването на адреса трябва да е позволено"
