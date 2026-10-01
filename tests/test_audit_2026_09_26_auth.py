# -*- coding: utf-8 -*-
"""Регресионни тестове за одита от 26.09.2026 — вход/изход, служители,
настройки, одитен дневник (находки №1-№9 от този кръг).

Статус кодовете се проверяват БЕЗ follow_redirects: общият обработчик на
грешки (appcore._handle_unexpected_error) превръща изключенията в 302."""
import io
import re
from urllib.parse import urlsplit

import pytest
from werkzeug.security import generate_password_hash

import login_guard
import remote_tunnel
from conftest import get_csrf_token, post_with_csrf

PASSWORD = "password-123"
TUNNEL_IP = "203.0.113.9"
WRONG_CREDENTIALS = "Грешно потребителско име или парола"


def _make_user(db_module, name, password=PASSWORD, role="employee", must_change=0):
    con = db_module.get_db()
    con.execute(
        "INSERT INTO users (username, password_hash, full_name, role, active,"
        " must_change_password) VALUES (?, ?, ?, ?, 1, ?)",
        (name, generate_password_hash(password), name, role, must_change))
    con.commit()
    uid = con.execute("SELECT id FROM users WHERE username = ?", (name,)).fetchone()[0]
    con.close()
    return uid


def _login(test_client, username, password, query=""):
    token = get_csrf_token(test_client, "/login")
    return test_client.post("/login" + query, data={
        "username": username, "password": password, "csrf_token": token})


def _tunnel_client(flask_app, monkeypatch, ip=TUNNEL_IP):
    """Клиент, който идва „отвън“: работещ тунел + CF-Connecting-IP от
    loopback (точно така изглежда заявка през cloudflared)."""
    monkeypatch.setattr(remote_tunnel, "status", lambda: {"status": "running"})
    c = flask_app.test_client()
    c.environ_base["HTTP_CF_CONNECTING_IP"] = ip
    return c


def _lan_client(flask_app, ip="192.168.1.20"):
    c = flask_app.test_client()
    c.environ_base["REMOTE_ADDR"] = ip
    return c


# ---------------------------------------------------------------- №1 памет

def test_login_guard_key_for_huge_username_is_bounded():
    """Находка №1: login_guard пазеше ЦЕЛИЯ въведен низ като ключ."""
    huge = "A" * 200000
    login_guard.register_failure(huge)
    assert login_guard._attempts, "опитът изобщо не е отбелязан"
    assert max(len(k) for k in login_guard._attempts) <= 80
    # Заключването по такъв ключ продължава да работи.
    for _i in range(login_guard.MAX_ATTEMPTS):
        login_guard.register_failure(huge)
    assert login_guard.is_locked_out(huge)[0] is True
    # Кратките (реалните) имена остават четими и непроменени.
    login_guard.register_failure("Ivan")
    assert "ivan" in login_guard._attempts


def test_huge_username_on_login_does_not_stay_in_memory(client):
    """Находка №1: анонимен POST /login с огромно име задържаше низа."""
    resp = _login(client, "Б" * 300000, "x")
    assert resp.status_code == 200
    assert WRONG_CREDENTIALS in resp.data.decode("utf-8")
    assert sum(len(k) for k in login_guard._attempts) < 1000


def test_overlong_username_is_rejected_without_database_lookup(flask_app, db_module):
    """Находка №1: име над MAX_USERNAME_LENGTH се обработва направо като
    неуспешен опит — дори да има (исторически) такъв ред в базата."""
    import routes_auth
    long_name = "u" * (routes_auth.MAX_USERNAME_LENGTH + 1)
    _make_user(db_module, long_name)
    resp = _login(flask_app.test_client(), long_name, PASSWORD)
    assert resp.status_code == 200
    assert WRONG_CREDENTIALS in resp.data.decode("utf-8")


def test_admin_cannot_create_username_longer_than_login_accepts(admin_client, db_module):
    import routes_auth
    long_name = "u" * (routes_auth.MAX_USERNAME_LENGTH + 1)
    resp = post_with_csrf(admin_client, "/admin/users/new",
                          {"username": long_name, "password": "long-enough-1"},
                          csrf_source_url="/admin/users")
    assert resp.status_code == 302
    con = db_module.get_db()
    assert con.execute("SELECT 1 FROM users WHERE username = ?", (long_name,)).fetchone() is None
    con.close()


# ---------------------------------------------------------------- №2 open redirect

@pytest.mark.parametrize("next_param", [
    "/%09/evil.example.com/x",
    "/%0a/evil.example.com/x",
    "/%0d/evil.example.com/x",
    "/%20/evil.example.com/x",
    "/%5C/evil.example.com/x",
    "/%5Cevil.example.com/x",
    "/%00/evil.example.com/x",
    "//evil.example.com/x",
    "https://evil.example.com/x",
])
def test_login_next_cannot_leave_the_site(flask_app, db_module, next_param):
    """Находка №2: `next=/%09/evil.example.com/x` → Location: //evil…"""
    _make_user(db_module, "bob")
    resp = _login(flask_app.test_client(), "bob", PASSWORD, "?next=" + next_param)
    assert resp.status_code == 302
    location = resp.headers["Location"]
    # Както браузърът: маха табулации/нови редове, крайни интервали и
    # третира „\\“ като „/“.
    as_browser = re.sub(r"[\t\n\r]", "", location).strip("\x00 ").replace("\\", "/")
    assert not as_browser.startswith("//"), location
    parts = urlsplit(as_browser)
    assert parts.netloc in ("", "localhost"), location
    assert "evil.example.com" not in location


def test_login_next_still_accepts_normal_internal_paths(flask_app, db_module):
    _make_user(db_module, "bob")
    resp = _login(flask_app.test_client(), "bob", PASSWORD, "?next=/docs%3Fq%3Dabc")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/docs?q=abc")


# ---------------------------------------------------------------- №3 деактивиране

def test_reactivating_a_user_does_not_revive_old_sessions(flask_app, db_module, admin_client):
    """Находка №3: деактивиране → повторно активиране съживяваше старите
    (вкл. откраднати) бисквитки на служителя."""
    uid = _make_user(db_module, "bob")
    victim = flask_app.test_client()
    assert _login(victim, "bob", PASSWORD).status_code == 302
    stolen = victim.get_cookie("session").value
    assert victim.get("/").status_code == 200

    for expected in ("1", "0"):  # деактивиране, после активиране
        resp = post_with_csrf(admin_client, "/admin/users/%d/toggle" % uid,
                              {"expected_active": expected}, csrf_source_url="/admin/users")
        assert resp.status_code == 302
    con = db_module.get_db()
    assert con.execute("SELECT active FROM users WHERE id = ?", (uid,)).fetchone()[0] == 1
    con.close()

    thief = flask_app.test_client()
    thief.set_cookie("session", stolen, domain="localhost")
    resp = thief.get("/")
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]


def test_toggle_without_expected_field_also_ends_sessions(flask_app, db_module, admin_client):
    uid = _make_user(db_module, "bob")
    victim = flask_app.test_client()
    _login(victim, "bob", PASSWORD)
    stolen = victim.get_cookie("session").value
    for _i in range(2):
        post_with_csrf(admin_client, "/admin/users/%d/toggle" % uid, {},
                       csrf_source_url="/admin/users")
    thief = flask_app.test_client()
    thief.set_cookie("session", stolen, domain="localhost")
    assert thief.get("/").status_code == 302


# ---------------------------------------------------------------- №4 същата парола

def test_forced_password_change_rejects_the_same_password(flask_app, db_module):
    """Находка №4: задължителната смяна приемаше СЪЩАТА парола."""
    uid = _make_user(db_module, "bob", password="admin-knows-1", must_change=1)
    c = flask_app.test_client()
    assert _login(c, "bob", "admin-knows-1").status_code == 302
    resp = post_with_csrf(c, "/password", {
        "current": "admin-knows-1", "new": "admin-knows-1", "repeat": "admin-knows-1",
    }, csrf_source_url="/password")
    assert resp.status_code == 200
    assert "Новата парола трябва да е различна от текущата." in resp.data.decode("utf-8")
    con = db_module.get_db()
    row = con.execute("SELECT must_change_password FROM users WHERE id = ?", (uid,)).fetchone()
    con.close()
    assert row[0] == 1
    # Истинска смяна минава.
    resp = post_with_csrf(c, "/password", {
        "current": "admin-knows-1", "new": "my-own-secret-2", "repeat": "my-own-secret-2",
    }, csrf_source_url="/password")
    assert resp.status_code == 302


# ---------------------------------------------------------------- №5 публично лого

_PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
        b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01"
        b"\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")


def _upload_logo(admin_client):
    return post_with_csrf(admin_client, "/settings/logo",
                          {"logo_file": (io.BytesIO(_PNG), "logo.png")},
                          csrf_source_url="/settings", content_type="multipart/form-data")


def test_company_logo_is_served_to_anonymous_viewers(flask_app, admin_client):
    """Находка №5: /p/<token> вгражда /logo.img, а той изискваше вход."""
    assert _upload_logo(admin_client).status_code == 302
    # Одит (01.10.2026, R2): кешира се само версионираният адрес (?v=<mtime>,
    # който url_for добавя сам); голият адрес е no-cache, за да се види новото лого.
    resp = flask_app.test_client().get("/logo.img?v=1")
    assert resp.status_code == 200
    assert resp.mimetype == "image/png"
    assert resp.data == _PNG
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.cache_control.max_age and resp.cache_control.max_age > 0


def test_company_logo_404_without_upload(client):
    assert client.get("/logo.img").status_code == 404


# ---------------------------------------------------------------- №6 одитен дневник

def _audit_lines(capsys):
    return [ln for ln in capsys.readouterr().out.splitlines() if "ОДИТ" in ln]


def test_creating_a_user_is_audited(admin_client, capsys):
    capsys.readouterr()
    post_with_csrf(admin_client, "/admin/users/new",
                   {"username": "novak", "password": "long-enough-1", "role": "admin"},
                   csrf_source_url="/admin/users")
    lines = _audit_lines(capsys)
    assert any("създаден служител" in ln and "novak" in ln and "admin" in ln for ln in lines)
    assert not any("long-enough-1" in ln for ln in lines)


def test_backup_and_export_folder_changes_are_audited(admin_client, tmp_path, capsys):
    capsys.readouterr()
    post_with_csrf(admin_client, "/admin/system",
                   {"form": "backup_folder", "backup_folder": str(tmp_path / "arhiv")},
                   csrf_source_url="/my-settings")
    post_with_csrf(admin_client, "/admin/system",
                   {"form": "client_export", "client_export_dir": str(tmp_path / "klienti")},
                   csrf_source_url="/my-settings")
    lines = _audit_lines(capsys)
    assert any("променени настройки за архив" in ln and "arhiv" in ln for ln in lines)
    assert any("променени настройки за клиентски папки" in ln and "klienti" in ln
               for ln in lines)


def test_company_data_change_is_audited_with_masked_iban(admin_client, capsys):
    capsys.readouterr()
    post_with_csrf(admin_client, "/settings",
                   {"sender_iban": "BG80BNBG96611020345678", "sender_name": "Фирма"},
                   csrf_source_url="/settings")
    lines = _audit_lines(capsys)
    entry = [ln for ln in lines if "променени данни на фирмата изпращач" in ln]
    assert entry, lines
    assert "sender_iban" in entry[0] and "5678" in entry[0]
    assert "BG80BNBG96611020345678" not in "\n".join(lines)


def test_logo_upload_is_audited(admin_client, capsys):
    capsys.readouterr()
    _upload_logo(admin_client)
    assert any("качено лого на фирмата" in ln for ln in _audit_lines(capsys))


def test_client_delete_is_audited(admin_client, db_module, capsys):
    con = db_module.get_db()
    con.execute("INSERT INTO clients (name) VALUES (?)", ("Клиент За Триене",))
    con.commit()
    cid = con.execute("SELECT id FROM clients WHERE name = ?",
                      ("Клиент За Триене",)).fetchone()[0]
    con.close()
    capsys.readouterr()
    resp = post_with_csrf(admin_client, "/clients/%d/delete" % cid, {},
                          csrf_source_url="/clients")
    assert resp.status_code == 302
    assert any("изтрит клиент" in ln and "id=%d" % cid in ln for ln in _audit_lines(capsys))


def test_audit_log_records_real_ip_of_tunnel_clients(flask_app, monkeypatch, capsys):
    """Находка №6: през тунела remote_addr е винаги 127.0.0.1."""
    c = _tunnel_client(flask_app, monkeypatch)
    capsys.readouterr()
    _login(c, "nobody", "x")
    lines = _audit_lines(capsys)
    assert any(TUNNEL_IP in ln for ln in lines), lines


def test_audit_log_ignores_cf_header_when_tunnel_is_not_running(flask_app, capsys):
    """Същото правило за доверие като при лимита: без работещ тунел
    заглавието е подправимо от всеки в мрежата и НЕ се записва."""
    c = flask_app.test_client()
    c.environ_base["HTTP_CF_CONNECTING_IP"] = "198.51.100.66"
    capsys.readouterr()
    _login(c, "nobody", "x")
    assert not any("198.51.100.66" in ln for ln in _audit_lines(capsys))


# ---------------------------------------------------------------- №7 изход

def test_get_logout_does_not_end_the_session(admin_client):
    """Находка №7: всяка чужда страница можеше да изкара служителя (от
    всички устройства) с обикновен GET."""
    resp = admin_client.get("/logout")
    assert resp.status_code in (302, 405)
    assert admin_client.get("/").status_code == 200


def test_logout_post_without_csrf_is_rejected(admin_client):
    resp = admin_client.post("/logout", data={})
    assert resp.status_code == 400
    assert admin_client.get("/").status_code == 200


def test_logout_post_with_csrf_logs_out(admin_client):
    resp = post_with_csrf(admin_client, "/logout", {})
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]
    assert admin_client.get("/").status_code == 302


def test_sidebar_logout_is_a_post_form_with_csrf(admin_client):
    html = admin_client.get("/").data.decode("utf-8")
    assert 'href="/logout"' not in html
    m = re.search(r'<form method="post" action="/logout"[^>]*>(.*?)</form>', html, re.S)
    assert m, "формата за изход липсва"
    assert 'name="csrf_token"' in m.group(1)
    assert 'type="submit"' in m.group(1)


# ---------------------------------------------------------------- №8 заключване

def _lock_account(test_client, username):
    for _i in range(login_guard.MAX_ATTEMPTS):
        _login(test_client, username, "wrong-guess")
    assert login_guard.is_locked_out(username)[0]


def test_locked_account_refuses_correct_password_through_tunnel(flask_app, db_module,
                                                                monkeypatch):
    """Находка №8: при заключен акаунт отвън и правилната парола се отказва
    (иначе заключването не спира отгатването)."""
    monkeypatch.setattr(login_guard, "_IP_MAX_ATTEMPTS", 10 ** 6)
    monkeypatch.setattr(login_guard, "_GLOBAL_MAX_ATTEMPTS", 10 ** 6)
    _make_user(db_module, "bob")
    c = _tunnel_client(flask_app, monkeypatch)
    _lock_account(c, "bob")
    wrong = _login(c, "bob", "another-wrong")
    right = _login(c, "bob", PASSWORD)
    assert right.status_code == 200
    assert "Твърде много неуспешни опити" in right.data.decode("utf-8")
    # Отговорът не издава, че паролата е била правилна.
    assert (re.sub(rb'value="[0-9a-f]+"', b"", right.data)
            == re.sub(rb'value="[0-9a-f]+"', b"", wrong.data))


def test_locked_account_still_admits_correct_password_on_the_lan(flask_app, db_module,
                                                                 monkeypatch):
    monkeypatch.setattr(login_guard, "_IP_MAX_ATTEMPTS", 10 ** 6)
    _make_user(db_module, "bob")
    attacker = _tunnel_client(flask_app, monkeypatch)
    _lock_account(attacker, "bob")
    owner = _lan_client(flask_app)
    resp = _login(owner, "bob", PASSWORD)
    assert resp.status_code == 302


# ---------------------------------------------------------------- №9 глобален праг / IPv6

def test_ipv6_addresses_in_one_64_share_a_rate_limit_bucket():
    """Находка №9 (а): сменящ се IPv6 адрес от една /64 мрежа беше всеки път
    нова, празна кофа."""
    for i in range(login_guard._IP_MAX_ATTEMPTS + 1):
        login_guard.register_ip_attempt("cf:2001:db8:1:2::%x" % (i + 1))
    assert login_guard.is_ip_throttled("cf:2001:db8:1:2:ffff::1") is True
    assert login_guard.is_ip_throttled("cf:2001:db8:1:3::1") is False
    assert login_guard.is_ip_throttled("cf:203.0.113.1") is False


def test_rotating_ipv6_through_tunnel_is_throttled_per_prefix(flask_app, monkeypatch):
    monkeypatch.setattr(remote_tunnel, "status", lambda: {"status": "running"})
    last = None
    for i in range(login_guard._IP_MAX_ATTEMPTS + 1):
        c = flask_app.test_client()
        c.environ_base["HTTP_CF_CONNECTING_IP"] = "2001:db8:aa:bb::%x" % (i + 1)
        last = _login(c, "nobody", "x")
    assert "Твърде много опити за вход в момента" in last.data.decode("utf-8")


def test_remote_flood_does_not_block_lan_users(flask_app, db_module, monkeypatch):
    """Находка №9 (б): глобалният праг (общ за всички) се препълваше от
    няколко адреса отвън и спираше служител в офиса."""
    _make_user(db_module, "bob")
    for i in range(login_guard._GLOBAL_MAX_ATTEMPTS + 5):
        c = _tunnel_client(flask_app, monkeypatch, ip="198.51.100.%d" % (i + 1))
        _login(c, "nobody", "x")
    assert login_guard.is_globally_throttled() is True
    remote = _tunnel_client(flask_app, monkeypatch, ip="198.51.100.250")
    assert "Твърде много опити за вход в момента" in _login(
        remote, "bob", PASSWORD).data.decode("utf-8")
    resp = _login(_lan_client(flask_app), "bob", PASSWORD)
    assert resp.status_code == 302


def test_lan_attempts_do_not_count_towards_global_limit(flask_app, monkeypatch):
    for i in range(login_guard._GLOBAL_MAX_ATTEMPTS + 5):
        _login(_lan_client(flask_app, ip="192.168.1.%d" % (i + 2)), "nobody", "x")
    assert login_guard.is_globally_throttled() is False
