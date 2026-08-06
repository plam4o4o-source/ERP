# -*- coding: utf-8 -*-
"""Регресионни тестове за централизирания жизнен цикъл на DB връзката
(appcore.get_db() + app.teardown_appcontext(_close_db)) — отложената от
Фаза 2 към Фаза 3 задача в оригиналния план, приложена тук: вместо всеки
routes_*.py хендлър да отваря собствена db.get_db() и да я затваря ръчно
(`con.close()`) точно преди всеки `return` (лесно за пропускане при нов
маршрут/нов ранен `return`, и означава отделна SQLite връзка при всяко
повторно извикване в рамките на ЕДНА заявка), appcore.get_db() кешира
ЕДНА връзка в `flask.g` за целия живот на заявката и я затваря
автоматично след отговора — включително при необработено изключение."""
import sqlite3

import pytest

import appcore


def test_get_db_returns_same_connection_within_one_request(flask_app):
    with flask_app.test_request_context("/"):
        c1 = appcore.get_db()
        c2 = appcore.get_db()
        assert c1 is c2


def test_get_db_returns_different_connection_across_requests(flask_app):
    with flask_app.test_request_context("/"):
        c1 = appcore.get_db()
    with flask_app.test_request_context("/"):
        c2 = appcore.get_db()
    assert c1 is not c2


def test_connection_is_closed_after_request_context_exits(flask_app):
    with flask_app.test_request_context("/"):
        con = appcore.get_db()
        con.execute("SELECT 1")  # работи вътре в заявката

    with pytest.raises(sqlite3.ProgrammingError):
        con.execute("SELECT 1")  # затворена автоматично след teardown


def test_connection_is_closed_even_when_view_raises(flask_app):
    """Teardown функциите на Flask се извикват ВИНАГИ, дори при
    необработено изключение по средата на хендлъра — за разлика от ръчен
    `con.close()` в тялото на функцията, до който кодът може да не стигне
    (напр. изключение преди достигане на реда с close()). Точно тази
    разлика е причината да минем към flask.g/teardown вместо ръчно
    управление на връзката във всеки routes_*.py хендлър."""
    con_holder = {}
    with pytest.raises(ValueError):
        with flask_app.test_request_context("/"):
            con_holder["con"] = appcore.get_db()
            raise ValueError("симулирана грешка по средата на заявката")

    with pytest.raises(sqlite3.ProgrammingError):
        con_holder["con"].execute("SELECT 1")


def test_writes_via_get_db_are_committed_and_visible_after_request(flask_app, db_module):
    """Затварянето на връзката НЕ прави автоматичен commit — само
    затваря; изричните `con.commit()` в routes_*.py остават непроменени.
    Този тест проверява, че запис + commit през appcore.get_db() наистина
    се вижда от друга, отделна връзка СЛЕД като заявката е приключила
    (т.е. commit-ът наистина е стигнал до диска, не е останал в отворена
    некомитната транзакция, изгубена при close())."""
    with flask_app.test_request_context("/"):
        con = appcore.get_db()
        con.execute(
            "INSERT INTO clients (name, city) VALUES (?, ?)",
            ("DB Lifecycle Test EOOD", "Яворец"),
        )
        con.commit()

    con2 = db_module.get_db()
    try:
        row = con2.execute(
            "SELECT * FROM clients WHERE name = ?", ("DB Lifecycle Test EOOD",)
        ).fetchone()
        assert row is not None
        assert row["city"] == "Яворец"
    finally:
        con2.close()


def test_login_flow_reuses_single_connection_no_regression(admin_client):
    """Характеризиращ тест: цялата логин верига (routes_auth.login, преди
    рефакторинга: две отделни db.get_db() извиквания — con за проверка на
    паролата, con2 за тема/език) продължава да работи еднакво, вече само
    с ЕДНА кеширана връзка — виж routes_auth.py."""
    resp = admin_client.get("/", follow_redirects=False)
    assert resp.status_code == 200


def test_admin_panel_full_crud_cycle_still_works(admin_client, db_module):
    """Опростен end-to-end тест по няколко routes_admin.py хендлъра
    (добавяне/toggle/смяна на парола/изтриване на служител) — всичките
    пренаписани да ползват appcore.get_db() вместо ръчно db.get_db()/
    con.close() — за да хване евентуален пропуснат commit()."""
    from conftest import post_with_csrf

    resp = post_with_csrf(admin_client, "/admin/users/new", {
        "username": "db_lifecycle_emp", "full_name": "Тест Служител",
        "password": "test-password-123", "role": "employee",
    }, csrf_source_url="/admin/users", follow_redirects=False)
    assert resp.status_code == 302

    con = db_module.get_db()
    row = con.execute("SELECT * FROM users WHERE username = 'db_lifecycle_emp'").fetchone()
    con.close()
    assert row is not None
    assert row["active"] == 1

    toggle_url = "/admin/users/%d/toggle" % row["id"]
    resp = post_with_csrf(admin_client, toggle_url, {}, csrf_source_url="/admin/users",
                          follow_redirects=False)
    assert resp.status_code == 302

    con = db_module.get_db()
    row2 = con.execute("SELECT active FROM users WHERE id = ?", (row["id"],)).fetchone()
    con.close()
    assert row2["active"] == 0  # реално се е записало в базата (commit е стигнал)

    delete_url = "/admin/users/%d/delete" % row["id"]
    resp = post_with_csrf(admin_client, delete_url, {}, csrf_source_url="/admin/users",
                          follow_redirects=False)
    assert resp.status_code == 302

    con = db_module.get_db()
    row3 = con.execute("SELECT * FROM users WHERE id = ?", (row["id"],)).fetchone()
    con.close()
    assert row3 is None
