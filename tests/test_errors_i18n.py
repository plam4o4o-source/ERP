# -*- coding: utf-8 -*-
"""Одит (04.10.2026, група BACKEND-CORE): спасяване на форма при изтекла
сесия (R1/F4), преведени страници за HTTP грешки (I2), „Опитай пак“ без
загуба на въведеното (R4), преводи на съобщенията при вход/импорт/тунел/път
до базата/резервен режим (I1, I4, I6, I8, I10, I11), защитни хедъри (S1),
политика за пароли (S2), заключване по източник в LAN (S3) и банерът за
насрочено възстановяване (pending_restore)."""
import json
import re
import sqlite3
import urllib.request

import pytest
from werkzeug.security import generate_password_hash

import appcore
import login_guard
from conftest import get_csrf_token, post_with_csrf

ORIGIN = {"Origin": "http://localhost"}
NAV = {"Sec-Fetch-Dest": "document", "Accept": "text/html,application/xhtml+xml"}


def _marker_gettext(monkeypatch, module_attr_pairs=()):
    """Подменя gettext с маркер «…», за да се види кои низове минават през
    превод (за нови msgid-и каталозите още нямат превод)."""
    def fake(msgid, **params):
        return "«%s»" % ((msgid % params) if params else msgid)
    for module, attr in module_attr_pairs:
        monkeypatch.setattr(module, attr, fake)
    return fake


def _form_csrf(client, url):
    return get_csrf_token(client, url)


def _logout(client):
    post_with_csrf(client, "/logout", {})


def _login(client, url, username, password):
    token = get_csrf_token(client, url)
    return client.post(url, data={"csrf_token": token, "username": username,
                                  "password": password})


def _add_user(db_module, username, password="other-pass-987", role="employee"):
    con = db_module.get_db()
    con.execute("INSERT INTO users (username, password_hash, full_name, role, active,"
                " must_change_password) VALUES (?, ?, ?, ?, 1, 0)",
                (username, generate_password_hash(password), username, role))
    con.commit()
    uid = con.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()[0]
    con.close()
    return uid


def _uid(db_module, username):
    con = db_module.get_db()
    try:
        return con.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()[0]
    finally:
        con.close()


# ============================================================ R1/F4 спасяване


def test_cmr_posted_after_logout_in_another_tab_is_restored_after_login(admin_client):
    token = _form_csrf(admin_client, "/cmr/new")
    _logout(admin_client)                       # „изход в друг раздел“
    resp = admin_client.post("/cmr/new", data={
        "csrf_token": token, "consignee_name": "Спасен Получател ООД",
        "sender_name": "Изпращач 1"}, headers=ORIGIN)
    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert location.startswith("/login?") and "rescue=" in location and "next=/cmr/new" in location
    page = admin_client.get(location).get_data(as_text=True)
    assert "попълнената форма е запазена" in page
    resp = _login(admin_client, location, "test_admin", "test-password-123")
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("/cmr/new?restore=")
    form = admin_client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "Спасен Получател ООД" in form
    assert "Попълнената форма е възстановена" in form


def test_expired_session_cookie_still_rescues_the_form_for_the_same_user(flask_app, db_module,
                                                                         admin_client):
    uid = _uid(db_module, "test_admin")
    token = _form_csrf(admin_client, "/packing/new")
    assert token.endswith(".u%d" % uid), "токенът на влязъл потребител носи номера му"
    fresh = flask_app.test_client()             # бисквитката е изтекла/изтрита
    items = json.dumps([{"description": "Ред от спасен лист", "qty": "3"}])
    resp = fresh.post("/packing/new", data={"csrf_token": token, "consignee_name": "Х",
                                            "items_json": items}, headers=ORIGIN)
    assert resp.status_code == 302 and "rescue=" in resp.headers["Location"]
    resp = _login(fresh, resp.headers["Location"], "test_admin", "test-password-123")
    assert resp.headers["Location"].startswith("/packing/new?restore=")
    assert "Ред от спасен лист" in fresh.get(resp.headers["Location"]).get_data(as_text=True)


def test_rescued_form_is_not_handed_to_another_user(flask_app, db_module, admin_client):
    _add_user(db_module, "drugiyat")
    token = _form_csrf(admin_client, "/cmr/new")
    _logout(admin_client)
    resp = admin_client.post("/cmr/new", data={"csrf_token": token,
                                               "consignee_name": "Тайна Фирма"}, headers=ORIGIN)
    login_url = resp.headers["Location"]
    other = flask_app.test_client()
    resp = _login(other, login_url, "drugiyat", "other-pass-987")
    assert resp.status_code == 302 and "restore=" not in resp.headers["Location"]
    assert "Тайна Фирма" not in other.get(resp.headers["Location"],
                                          follow_redirects=True).get_data(as_text=True)
    # Собственикът още може да си я вземе.
    resp = _login(admin_client, login_url, "test_admin", "test-password-123")
    assert "Тайна Фирма" in admin_client.get(resp.headers["Location"]).get_data(as_text=True)


def test_stale_token_with_a_valid_session_goes_straight_back_to_the_form(admin_client):
    old = _form_csrf(admin_client, "/cmr/new")
    _logout(admin_client)
    _login(admin_client, "/login", "test_admin", "test-password-123")   # нова сесия
    resp = admin_client.post("/cmr/new", data={"csrf_token": old,
                                               "consignee_name": "Стар Раздел ЕООД"},
                             headers=ORIGIN)
    assert resp.status_code == 302 and resp.headers["Location"].startswith("/cmr/new?restore=")
    assert "Стар Раздел ЕООД" in admin_client.get(resp.headers["Location"]).get_data(as_text=True)


def test_password_changed_elsewhere_rescues_the_edit_form(admin_client, db_module):
    from conftest import issue_cmr
    doc_id = issue_cmr(admin_client, consignee_name="Оригинал")
    edit_url = "/doc/%d/edit" % doc_id
    token = _form_csrf(admin_client, edit_url)
    con = db_module.get_db()
    con.execute("UPDATE users SET session_epoch = session_epoch + 1 WHERE username = 'test_admin'")
    con.commit()
    con.close()
    resp = admin_client.post(edit_url, data={"csrf_token": token, "consignee_name": "Редакция",
                                             "edit_doc_version": "1"}, headers=ORIGIN)
    assert resp.status_code == 302 and "rescue=" in resp.headers["Location"]
    resp = _login(admin_client, resp.headers["Location"], "test_admin", "test-password-123")
    assert resp.headers["Location"].startswith(edit_url + "?restore=")
    assert "Редакция" in admin_client.get(resp.headers["Location"]).get_data(as_text=True)


def test_cross_site_post_is_not_rescued_and_gets_a_styled_page(client):
    resp = client.post("/cmr/new", data={"csrf_token": "x.u1", "consignee_name": "Чужд"},
                       headers={"Origin": "http://evil.example"})
    assert resp.status_code == 400
    body = resp.get_data(as_text=True)
    assert "Bad Request" not in body and "Сесията Ви е изтекла" in body
    assert 'href="/login' in body


def test_non_document_post_without_session_gets_translated_page_with_login(client, flask_app):
    client.get("/login?lang=en")
    resp = client.post("/clients/new", data={"csrf_token": "stale", "name": "X"},
                       headers=dict(NAV, **ORIGIN))
    assert resp.status_code == 400
    body = resp.get_data(as_text=True)
    assert "Bad Request" not in body and "Werkzeug" not in body
    assert '<html lang="en"' in body
    assert ">Login<" in body, "бутонът „Вход“ (с наличен превод) трябва да е на английски"


def test_csrf_failure_from_fetch_returns_json(client):
    client.get("/login?lang=en")
    resp = client.post("/invoice/pull-pallet", data={"code": "1"},
                       headers={"Sec-Fetch-Dest": "empty"})
    assert resp.status_code == 400
    payload = resp.get_json()
    assert payload["ok"] is False and payload["session_expired"] is True
    assert payload["error"] == "Your session has expired — reload the page and log in again."


def test_rescue_store_is_bounded():
    appcore._reset_rescue_store()
    for i in range(appcore._RESCUE_MAX_ENTRIES + 20):
        with appcore._rescue_lock:
            appcore._rescue_store["t%d" % i] = {"expires": 1e18, "size": 10}
            appcore._rescue_evict(0)
    assert len(appcore._rescue_store) == appcore._RESCUE_MAX_ENTRIES
    appcore._reset_rescue_store()


# ============================================================ I2 HTTP грешки


def test_404_is_a_styled_translated_page(admin_client):
    admin_client.get("/my-settings")  # нищо — просто жива сесия
    resp = admin_client.get("/no-such-page", headers=NAV)
    assert resp.status_code == 404
    body = resp.get_data(as_text=True)
    assert "Not Found" not in body and "Страницата не е намерена" in body
    assert 'class="sidebar' in body, "влязъл потребител — общият изглед със страничната лента"


def test_404_for_guest_is_minimal(client):
    body = client.get("/no-such-page").get_data(as_text=True)
    assert "Страницата не е намерена" in body and 'class="sidebar' not in body


def test_403_405_414_are_styled(employee_client, admin_client):
    resp = employee_client.get("/admin/users")
    assert resp.status_code == 403 and "Нямате достъп" in resp.get_data(as_text=True)
    resp = admin_client.get("/admin/users/new")
    assert resp.status_code == 405 and "POST" in resp.headers.get("Allow", "")
    assert "Method Not Allowed" not in resp.get_data(as_text=True)


def test_http_error_texts_go_through_gettext(admin_client, monkeypatch):
    _marker_gettext(monkeypatch, [(appcore, "_")])
    body = admin_client.get("/no-such-page").get_data(as_text=True)
    assert "«Страницата не е намерена»" in body


def test_404_from_fetch_is_json(admin_client):
    resp = admin_client.get("/no-such-page", headers={"Sec-Fetch-Dest": "empty"})
    assert resp.status_code == 404 and resp.get_json()["ok"] is False


def test_413_from_fetch_is_json(flask_app, admin_client):
    flask_app.config["MAX_CONTENT_LENGTH"] = 100
    resp = admin_client.post("/invoice/import-items", data={"x": "y" * 500},
                             headers={"Sec-Fetch-Dest": "empty"})
    assert resp.status_code == 413 and "25 MB" in resp.get_json()["error"]


# ============================================================ R4 „Опитай пак“


def test_busy_database_on_client_save_keeps_the_input(admin_client, monkeypatch):
    import routes_clients
    real = appcore.get_db

    class Busy:
        def __init__(self, con):
            self._con = con

        def execute(self, sql, *a, **k):
            if sql.lstrip().upper().startswith(("INSERT", "UPDATE")):
                raise sqlite3.OperationalError("database is locked")
            return self._con.execute(sql, *a, **k)

        def __getattr__(self, name):
            return getattr(self._con, name)

    token = _form_csrf(admin_client, "/clients/new")
    monkeypatch.setattr(routes_clients, "get_db", lambda: Busy(real()))
    resp = admin_client.post("/clients/new", data={"csrf_token": token,
                                                   "name": "Заета База ООД",
                                                   "city": "Пловдив"})
    assert resp.status_code == 503
    body = resp.get_data(as_text=True)
    assert resp.headers.get("Cache-Control") == "no-store"
    assert re.search(r'<form class="repost" method="post" action="/clients/new">', body)
    assert 'name="name" value="Заета База ООД"' in body
    assert 'name="city" value="Пловдив"' in body
    # Повторно изпращане (базата вече е свободна) записва клиента.
    monkeypatch.setattr(routes_clients, "get_db", real)
    fields = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', body))
    resp = admin_client.post("/clients/new", data=fields)
    assert resp.status_code == 302
    assert "Заета База ООД" in admin_client.get("/clients").get_data(as_text=True)


def test_db_unavailable_page_is_fully_translated(admin_client, monkeypatch):
    import routes_dashboard
    admin_client.get("/my-settings")
    with admin_client.session_transaction() as sess:
        sess["lang"] = "en"

    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(routes_dashboard, "get_db", boom)
    body = admin_client.get("/").get_data(as_text=True)
    assert '<html lang="en">' in body
    assert "The database is busy right now" in body


def test_db_unavailable_template_strings_go_through_gettext(flask_app):
    seen = []

    def fake(msgid, **kw):
        seen.append(msgid)
        return "«%s»" % msgid
    flask_app.jinja_env.globals["gettext"] = fake
    with flask_app.test_request_context("/"):
        html = appcore.render_template("db_unavailable.html", app_name="X", message="m",
                                       retry_url="/")
    assert "«Опитай пак»" in html and "«Базата данни временно не е достъпна»" in html
    assert "«Ако базата е на мрежов диск" in html


# ============================================================ I1/I4/I8/I10/I11


def test_login_errors_are_translated(client, monkeypatch):
    import routes_auth
    _marker_gettext(monkeypatch, [(routes_auth, "_")])
    body = _login(client, "/login", "nobody", "wrong").get_data(as_text=True)
    assert "«Грешно потребителско име или парола" in body


def test_xlsx_bomb_message_is_translated(flask_app, monkeypatch):
    err = appcore.XlsxTooLargeError(
        appcore.N_("Файлът се разархивира до над %(limit).0f MB общо — твърде "
                   "голям за обработка. Ако това е истинска справка, разделете "
                   "я на по-малки файлове."), limit=200.0)
    assert "200 MB" in str(err)                        # извън заявка — български
    _marker_gettext(monkeypatch, [(appcore, "_")])
    with flask_app.test_request_context("/"):
        assert str(err).startswith("«Файлът се разархивира до над 200 MB")


def test_tunnel_error_is_translated_at_display(flask_app, monkeypatch):
    import remote_tunnel
    import flask_babel
    saved = dict(remote_tunnel._state)
    try:
        remote_tunnel._state["error"] = (
            "Неуспешно изтегляне на компонента за отдалечен достъп: %(reason)s",
            {"reason": ("файлът е твърде малък (%(size)d байта)", {"size": 5})})
        assert remote_tunnel.status()["error"].endswith("файлът е твърде малък (5 байта)")
        monkeypatch.setattr(flask_babel, "gettext", lambda m, **p: "«%s»" % (m % p if p else m))
        with flask_app.test_request_context("/"):
            assert remote_tunnel.status()["error"].startswith("«Неуспешно изтегляне")
    finally:
        remote_tunnel._state.update(saved)


def test_tunnel_binary_error_keeps_bulgarian_str():
    import remote_tunnel
    err = remote_tunnel.TunnelError(
        "файлът изглежда повреден (%(problem)s) — опитайте отново",
        problem=("непълно изтегляне (%(size)d от общо %(total)d байта)", {"size": 1, "total": 2}))
    assert "непълно изтегляне (1 от общо 2 байта)" in str(err)


def test_db_path_validation_message_is_translated(flask_app, tmp_path, monkeypatch):
    import config as appconfig
    import flask_babel
    missing = str(tmp_path / "няма" / "a.db")
    assert "не съществува" in appconfig.validate_db_path(missing)[0]
    monkeypatch.setattr(flask_babel, "gettext", lambda m, **p: "«%s»" % (m % p if p else m))
    with flask_app.test_request_context("/"):
        assert appconfig.validate_db_path(missing)[0].startswith("«Папката")


def _fallback_app(monkeypatch):
    """Резервното приложение от app.py (базата е недостъпна при старт) —
    функцията се изпълнява сама, без да се внася app.py (той стартира
    сървър при внасяне)."""
    import os
    import hmac
    import secrets
    import time
    import flask
    import applog
    import config as appconfig
    import db
    from flask_babel import gettext
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "app.py"), encoding="utf-8").read()
    m = re.search(r"def _create_app_or_explain\(\):.*?\n    return fallback\n", src, re.S)
    ns = dict(flask=flask, hmac=hmac, secrets=secrets, time=time, appcore=appcore,
              applog=applog, appconfig=appconfig, db=db, os=os, _=gettext,
              __name__="app")
    exec(m.group(0), ns)  # nosec B102 -- собственият изходен код на програмата

    def fail():
        raise sqlite3.OperationalError("unable to open database file")
    monkeypatch.setattr(appcore, "create_app", fail)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return ns["_create_app_or_explain"]()


def test_fallback_mode_follows_the_browser_language(monkeypatch, db_module):
    app = _fallback_app(monkeypatch)
    c = app.test_client()
    resp = c.get("/", headers={"Accept-Language": "en-US,en;q=0.9"})
    assert resp.status_code == 503
    body = resp.get_data(as_text=True)
    assert '<html lang="en">' in body
    repair = c.get("/pacho-fix-db-path", headers={"Accept-Language": "en"}).get_data(as_text=True)
    assert ">Save<" in repair, "„Запази“ има превод — резервният режим вече ползва Babel"
    bg = c.get("/pacho-fix-db-path").get_data(as_text=True)
    assert ">Запази<" in bg and '<html lang="bg">' in bg


# ============================================================ S1 хедъри


def test_security_headers_and_no_server_versions(live_server):
    with urllib.request.urlopen(live_server + "/login") as resp:
        headers = resp.headers
    assert headers["Server"] == appcore.SERVER_IDENT
    assert "Werkzeug" not in headers["Server"] and "Python" not in headers["Server"]
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "same-origin"
    assert headers["X-Frame-Options"] == "DENY"
    csp = headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp
    assert "script-src" not in csp and "default-src" not in csp, "вградените скриптове остават"


# ============================================================ S2 пароли


@pytest.mark.parametrize("pw", ["12345678", "87654321", "password", "PASSWORD", "aaaaaaaa",
                                "qwertyui", "йцукенгш", "abcdefgh", "test_admin99"])
def test_weak_passwords_are_rejected(pw):
    assert appcore.password_policy_error(pw, "test_admin") is not None


@pytest.mark.parametrize("pw", ["Correct-Horse-9", "kamion-sofia-77", "Ябълка-2026!"])
def test_reasonable_passwords_pass(pw):
    assert appcore.password_policy_error(pw, "test_admin") is None


def test_change_password_rejects_weak_but_old_weak_password_still_logs_in(flask_app, db_module):
    _add_user(db_module, "slab", password="12345678")       # стара слаба парола
    c = flask_app.test_client()
    assert _login(c, "/login", "slab", "12345678").status_code == 302
    token = get_csrf_token(c, "/password")
    c.post("/password", data={"csrf_token": token, "current": "12345678",
                              "new": "password", "repeat": "password"})
    con = db_module.get_db()
    row = con.execute("SELECT password_hash FROM users WHERE username='slab'").fetchone()
    con.close()
    from werkzeug.security import check_password_hash
    assert check_password_hash(row["password_hash"], "12345678"), "слабата нова парола мина"


# ============================================================ S3 заключване в LAN


def _lan_login(flask_app, ip, username, password):
    c = flask_app.test_client()
    c.environ_base["REMOTE_ADDR"] = ip
    return _login(c, "/login", username, password)


def test_lan_guessing_is_stopped_even_with_the_right_password(flask_app, db_module, monkeypatch):
    monkeypatch.setattr(login_guard, "_IP_MAX_ATTEMPTS", 10 ** 6)
    _add_user(db_module, "boss", password="Correct-Horse-9", role="admin")
    for i in range(login_guard.MAX_ATTEMPTS + 3):
        _lan_login(flask_app, "192.168.1.20", "boss", "guess%d" % i)
    resp = _lan_login(flask_app, "192.168.1.20", "boss", "Correct-Horse-9")
    assert resp.status_code == 200
    assert "Твърде много неуспешни опити" in resp.get_data(as_text=True)
    # Собственикът от своя компютър влиза както досега (не е DoS).
    assert _lan_login(flask_app, "192.168.1.21", "boss", "Correct-Horse-9").status_code == 302


def test_lan_lockout_expires_with_the_existing_policy(flask_app, db_module, monkeypatch):
    import time as _time
    monkeypatch.setattr(login_guard, "_IP_MAX_ATTEMPTS", 10 ** 6)
    _add_user(db_module, "boss", password="Correct-Horse-9", role="admin")
    for i in range(login_guard.MAX_ATTEMPTS):
        _lan_login(flask_app, "192.168.1.20", "boss", "guess%d" % i)
    later = _time.time() + login_guard.LOCKOUT_SECONDS + 5
    monkeypatch.setattr(login_guard.time, "time", lambda: later)
    assert _lan_login(flask_app, "192.168.1.20", "boss", "Correct-Horse-9").status_code == 302


# ============================================================ pending_restore


def test_pending_restore_banner_is_formatted_and_cached(flask_app, monkeypatch):
    import backup
    calls = []

    def fake():
        calls.append(1)
        return {"requested_at": "2026-10-04 17:05:00", "backup": "x"}
    monkeypatch.setattr(backup, "pending_restore", fake)
    appcore.invalidate_pending_restore_banner()
    with flask_app.test_request_context("/"):
        from flask import render_template_string
        html = render_template_string("{{ pending_restore.requested_at }}")
    assert html == "04.10.2026 17:05"
    assert appcore.pending_restore_banner() == {"requested_at": "04.10.2026 17:05"}
    assert len(calls) == 1, "проверката трябва да е кеширана"
    monkeypatch.setattr(backup, "pending_restore", lambda: None)
    appcore.invalidate_pending_restore_banner()
    assert appcore.pending_restore_banner() is None


def test_stale_login_page_is_simply_shown_again(client):
    resp = client.post("/login?next=/docs", data={"csrf_token": "stale", "username": "a",
                                                  "password": "b"}, headers=NAV)
    assert resp.status_code == 302 and resp.headers["Location"] == "/login?next=/docs"
    assert "Страницата за вход беше остаряла" in client.get(
        resp.headers["Location"]).get_data(as_text=True)
