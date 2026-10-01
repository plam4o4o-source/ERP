# -*- coding: utf-8 -*-
"""Вход отвън без вградения тунел (пренасочен порт, собствен домейн, чужд
тунел) — R3 от анализа на 01.10.2026."""
import re

from werkzeug.security import generate_password_hash


def _token(client, addr):
    body = client.get("/login", environ_base={"REMOTE_ADDR": addr}).get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', body).group(1)


def _post_login(flask_app, addr, username, password):
    client = flask_app.test_client()
    return client.post("/login", data={"csrf_token": _token(client, addr),
                                        "username": username, "password": password},
                       environ_base={"REMOTE_ADDR": addr})


def _add_user(db_module, username, password):
    con = db_module.get_db()
    con.execute("INSERT INTO users (username, password_hash, full_name, role, active,"
                " must_change_password) VALUES (?, ?, 'B', 'admin', 1, 0)",
                (username, generate_password_hash(password)))
    con.commit()
    con.close()


def test_public_addresses_hit_the_global_limit_and_lockout(flask_app, db_module):
    _add_user(db_module, "boss", "Correct-Horse-9")
    throttled = 0
    for i in range(40):
        resp = _post_login(flask_app, "93.184.%d.%d" % (i // 200, i % 200 + 1), "boss", "guess%d" % i)
        throttled += "Твърде много опити" in resp.get_data(as_text=True)
    assert throttled > 0
    import login_guard
    login_guard._global_attempts.clear()  # само глобалният праг; заключването по име остава
    resp = _post_login(flask_app, "81.2.69.160", "boss", "Correct-Horse-9")
    assert resp.status_code == 200, "заключен акаунт пусна вход от интернет с правилна парола"


def test_lan_user_still_gets_in_with_the_correct_password_while_locked(flask_app, db_module):
    _add_user(db_module, "boss", "Correct-Horse-9")
    for i in range(12):
        _post_login(flask_app, "192.168.1.%d" % (i + 10), "boss", "guess%d" % i)
    import login_guard
    assert login_guard.is_locked_out("boss")[0]
    resp = _post_login(flask_app, "192.168.1.50", "boss", "Correct-Horse-9")
    assert resp.status_code == 302


def test_logout_from_a_tab_whose_session_expired_goes_to_login(flask_app):
    """R7: след изтичане на сесията (12 ч.) бисквитката и CSRF токенът ги няма
    — POST /logout даваше 400 „Невалидна или изтекла сесия на формата“."""
    client = flask_app.test_client()
    resp = client.post("/logout", data={"csrf_token": "stale"})
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]
