# -*- coding: utf-8 -*-
"""Одит (13.10.2026, група SEC, v3.81.0): задължителна SHA-256 проверка и
таван на изтеглянето при обновяване (S1), преводими грешки на обновяването
(S2), CSP script-src 'self' (S3), одитен дневник в базата (S4), логване на
погълнатите грешки (S5), мъртъв код (S6), етикети/alt (S7), TOCTOU при
преименуването на споделената база (S8) и непокрити критични пътища (S9)."""
import hashlib
import io
import json
import os
import re
import sqlite3
import sys
import time
import types
from datetime import datetime, timedelta

import pytest

import applog
import backup
import legacy_migration as lm
import updater
from conftest import ROOT, e2e_login, post_with_csrf, read_source


# ============================================================ помощни

class _Resp:
    """Изчерпващ се отговор на urlopen (read(n) на парчета)."""

    def __init__(self, data, length="auto"):
        self._buf = io.BytesIO(data)
        self.headers = {} if length is None else {
            "Content-Length": str(len(data) if length == "auto" else length)}
        self.reads = 0

    def read(self, n=-1):
        self.reads += 1
        return self._buf.read(n) if n is not None and n >= 0 else self._buf.read()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _frozen(monkeypatch, tmp_path, name="PHLogistics.exe"):
    exe = tmp_path / name
    exe.write_bytes(b"MZ" + b"\x00" * 100)
    monkeypatch.setattr(updater.sys, "executable", str(exe))
    monkeypatch.setattr(updater, "is_frozen_windows", lambda: True)
    monkeypatch.setattr(updater.threading, "Timer",
                        lambda *a, **k: types.SimpleNamespace(start=lambda: None))
    return str(exe)


PAYLOAD = b"MZ" + b"\x00" * 1_100_000


# ============================================================ S1 обновяване

def test_install_update_refuses_without_checksum_before_any_download(tmp_path, monkeypatch):
    """S1: до v3.80 липсващата сума значеше „провери само размер + MZ“."""
    _frozen(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(updater.net, "urlopen", lambda *a, **k: calls.append(a) or _Resp(PAYLOAD))
    monkeypatch.setattr(updater.subprocess, "Popen", lambda *a, **k: calls.append("popen"))
    for missing in (None, ""):
        with pytest.raises(updater.UpdateError, match="контролна сума"):
            updater.install_update("https://example.invalid/x.exe", missing, version="9.9.9")
    assert calls == [], "без сума не се тегли и не се пуска нищо"


def test_release_without_manifest_is_shown_but_not_installed(tmp_path, monkeypatch):
    release = {"tag_name": "v99.0.0", "html_url": "https://example.invalid/r",
               "assets": [{"name": "PHLogistics.exe",
                           "browser_download_url": "https://example.invalid/PHLogistics.exe"}]}
    monkeypatch.setattr(updater.net, "urlopen",
                        lambda req, timeout=8: _Resp(json.dumps(release).encode()))
    info = updater.check_for_update()
    assert info["available"] and info["expected_sha256"] is None
    _frozen(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match="контролна сума"):
        updater.install_update(info["download"], info["expected_sha256"], version=info["latest"])


def test_legacy_alias_lines_stay_in_the_published_manifest():
    """Старите клиенти (до v3.78) търсят PachoLogistic*.exe и реда им."""
    wf = read_source(".github", "workflows", "release.yml")
    for name in ("PHLogistics.exe", "PHLogistics-Setup.exe", "PachoLogistic.exe",
                 "PachoLogistic-Setup.exe"):
        assert '"  %s"' % name in wf, name
    sums = "\n".join("%s  %s" % (c * 64, n) for c, n in (
        ("a", "PHLogistics.exe"), ("b", "PHLogistics-Setup.exe"),
        ("c", "PachoLogistic.exe"), ("d", "PachoLogistic-Setup.exe")))
    assert updater.parse_sha256sums(sums, "PachoLogistic.exe") == "c" * 64
    assert updater.parse_sha256sums(sums, "PHLogistics.exe") == "a" * 64


def test_download_over_the_content_length_cap_is_refused(tmp_path, monkeypatch):
    exe = _frozen(monkeypatch, tmp_path)
    resp = _Resp(PAYLOAD, length=updater.MAX_DOWNLOAD_BYTES + 1)
    monkeypatch.setattr(updater.net, "urlopen", lambda req, timeout=120: resp)
    with pytest.raises(updater.UpdateError, match="твърде голям"):
        updater.install_update("https://example.invalid/x.exe",
                               hashlib.sha256(PAYLOAD).hexdigest())
    assert resp.reads == 0, "при обявен прекалено голям размер не се чете нищо"
    assert not [n for n in os.listdir(os.path.dirname(exe)) if n.endswith(".new")]


def test_streamed_download_is_cut_at_the_cap_and_removed(tmp_path, monkeypatch):
    exe = _frozen(monkeypatch, tmp_path)
    monkeypatch.setattr(updater, "MAX_DOWNLOAD_BYTES", 2 * 1024 * 1024)
    big = b"MZ" + b"\x00" * (5 * 1024 * 1024)
    resp = _Resp(big, length=None)  # без Content-Length
    monkeypatch.setattr(updater.net, "urlopen", lambda req, timeout=120: resp)
    with pytest.raises(updater.UpdateError, match="по-голям от допустимите 2 MB"):
        updater.install_update("https://example.invalid/x.exe", hashlib.sha256(big).hexdigest())
    assert resp.reads <= 4, "четенето спира веднага след тавана"
    assert not [n for n in os.listdir(os.path.dirname(exe)) if n.endswith(".new")]


def test_partial_download_is_removed_when_the_connection_breaks(tmp_path, monkeypatch):
    exe = _frozen(monkeypatch, tmp_path)

    class _Broken(_Resp):
        def read(self, n=-1):
            if self.reads:
                raise ConnectionResetError("прекъсната връзка")
            return super().read(n)
    monkeypatch.setattr(updater.net, "urlopen", lambda req, timeout=120: _Broken(PAYLOAD))
    with pytest.raises(ConnectionResetError):
        updater.install_update("https://example.invalid/x.exe", "0" * 64)
    assert not [n for n in os.listdir(os.path.dirname(exe)) if n.endswith(".new")]


def test_checksum_manifest_is_read_with_a_cap(monkeypatch):
    monkeypatch.setattr(updater, "_MAX_SUMS_BYTES", 100)
    monkeypatch.setattr(updater.net, "urlopen", lambda req, timeout=8: _Resp(b"a" * 500))
    assets = [{"name": "SHA256SUMS.txt", "browser_download_url": "https://example.invalid/s"}]
    with pytest.raises(updater.UpdateError, match="SHA256SUMS.txt"):
        updater._fetch_checksums_text(assets, 1)


def test_both_scripts_are_written_and_started_the_same_way(tmp_path, monkeypatch):
    """S1 (дедупликация): двата скрипта — ASCII, CRLF (newline=""), без
    `timeout`/конвейери, пуснати откачени със същата среда и флагове."""
    exe = _frozen(monkeypatch, tmp_path)
    popens = []
    monkeypatch.setattr(updater.subprocess, "Popen", lambda args, **kw: popens.append((args, kw)))
    monkeypatch.setattr(updater.net, "urlopen", lambda req, timeout=120: _Resp(PAYLOAD))
    updater.install_update("https://example.invalid/x.exe", hashlib.sha256(PAYLOAD).hexdigest(),
                           version="3.99.0")
    bat = os.path.join(os.path.dirname(exe), "ph_update_%s.bat" % updater._machine_suffix())
    setup = tmp_path / "work" / updater.SETUP_NAME
    setup.parent.mkdir()
    setup.write_bytes(PAYLOAD)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "lad"))
    updater._launch_rename(str(setup), "3.99.0")
    rename_bat = str(setup.parent / ("ph_update_%s.bat" % updater._machine_suffix()))
    for path in (bat, rename_bat):
        raw = open(path, "rb").read()
        assert raw.isascii(), path
        assert raw.count(b"\r\n") == raw.count(b"\n") and b"\r\r" not in raw, path
        text = raw.decode("ascii")
        assert not re.search(r"(?<!\|)\|(?!\|)", text), "конвейер (|) виси без конзола"
        assert not re.search(r"\btimeout\b", text, re.I) and "tasklist" not in text
    assert '2>nul ( >>"%~2" (call ) ) && goto exited' in open(rename_bat, encoding="ascii").read()
    assert len(popens) == 2
    for args, kw in popens:
        assert args.startswith('cmd.exe /d /s /c ""') and args.endswith('""')
        assert kw["creationflags"] == updater.DETACHED_PROCESS == 0x00000008
        assert kw["close_fds"] is True and updater.STARTED_MARKER_ENV in kw["env"]


def test_command_lines_refuse_quotes_with_a_translatable_error():
    with pytest.raises(updater.UpdateError, match="кавички"):
        updater._restart_command_line('C:\\a"b.bat', "n", "e", "1.0")
    with pytest.raises(updater.UpdateError, match="прехода"):
        updater._rename_command_line("b", 's"', "e", "n", "1.0", 1, 2)
    assert updater._restart_command_line("b", "n", "e", "x y") == \
        'cmd.exe /d /s /c ""b" "n" "e" "?""'


# ============================================================ S2 преводими грешки

def test_update_errors_are_translated_at_display(flask_app, monkeypatch):
    import flask_babel
    err = updater.UpdateError(updater.N_(
        "Изтегленият файл изглежда повреден — %(problem)s. Обновяването е прекратено — "
        "старата версия остава активна и работеща; ще се пробва пак автоматично "
        "по-късно."), problem=updater.UpdateError(
            updater.N_("файлът е твърде малък (%(size)d байта)"), size=5))
    assert "файлът е твърде малък (5 байта)" in str(err)        # извън заявка — български
    assert updater.describe_error(err) == str(err), "без „Неочаквана грешка: RuntimeError“"
    monkeypatch.setattr(flask_babel, "gettext", lambda m, **p: "«%s»" % m)
    with flask_app.test_request_context("/"):
        text = updater.describe_error(err)
        assert text.startswith("«Изтегленият файл") and "«файлът е твърде малък" in text
        assert updater.describe_error(TimeoutError()).startswith("«Заявката към GitHub")


def test_update_install_route_shows_the_translated_reason(admin_client, monkeypatch):
    import flask_babel
    monkeypatch.setattr(updater, "check_for_update", lambda: {
        "available": True, "latest": "9.9.9", "current": "1.0.0", "download": "x",
        "expected_sha256": None})
    monkeypatch.setattr(updater, "is_frozen_windows", lambda: True)
    monkeypatch.setattr(updater, "clear_failed_install_marker", lambda: None)
    monkeypatch.setattr(flask_babel, "gettext", lambda m, **p: "«%s»" % ((m % p) if p else m))
    resp = post_with_csrf(admin_client, "/update/install", {}, follow_redirects=True)
    body = resp.get_data(as_text=True)
    assert "«Релийзът няма контролна сума" in body
    assert "Неочаквана грешка" not in body


# ============================================================ S3 CSP

_TEMPLATES = os.path.join(ROOT, "templates")


def _template_sources():
    for name in sorted(os.listdir(_TEMPLATES)):
        if name.endswith(".html"):
            text = open(os.path.join(_TEMPLATES, name), encoding="utf-8").read()
            yield name, re.sub(r"\{#.*?#\}", "", text, flags=re.S)  # без Jinja коментари


def test_templates_have_no_inline_scripts_handlers_or_javascript_urls():
    for name, text in _template_sources():
        for tag in re.findall(r"<script\b[^>]*>", text):
            assert "src=" in tag, "%s: вграден <script>: %s" % (name, tag)
        assert not re.search(r"\son[a-z]+\s*=", text), "%s: on*= атрибут" % name
        assert "javascript:" not in text, "%s: javascript: адрес" % name


def test_csp_has_script_src_self_and_allows_speculation_rules(admin_client):
    csp = admin_client.get("/").headers["Content-Security-Policy"]
    assert "script-src 'self' 'inline-speculation-rules'" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp


@pytest.mark.parametrize("url", ["/my-settings", "/clients/new", "/pallet/new", "/docs", "/"])
def test_pages_render_without_inline_scripts(admin_client, url):
    body = admin_client.get(url).get_data(as_text=True)
    for tag in re.findall(r"<script\b[^>]*>", body):
        assert "src=" in tag, (url, tag)


def test_updating_page_redirects_via_data_attribute(flask_app):
    from flask import render_template
    with flask_app.test_request_context("/"):
        html = render_template("updating.html", latest="9.9.9", local_port=5000)
    assert 'data-redirect-after="15000"' in html
    assert not [t for t in re.findall(r"<script\b[^>]*>", html) if "src=" not in t]


@pytest.mark.e2e
def test_csp_pages_have_no_violations_in_chromium(live_server, e2e_context_factory):
    """S3: в истински Chromium — никакви CSP нарушения по основните страници;
    вграден скрипт е блокиран, а speculationrules от DOM — разрешен. Без
    bypass_csp (общите e2e тестове го заобикалят — виж conftest)."""
    page = e2e_context_factory(bypass_csp=False).new_page()
    page.context.add_init_script(
        "window.__csp = []; document.addEventListener('securitypolicyviolation',"
        " function (e) { window.__csp.push(e.violatedDirective + ' ' + e.lineNumber); });")
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(live_server + "/login")
    assert page.evaluate("window.__csp") == []
    e2e_login(page, live_server)
    for url in ("/", "/docs", "/cmr/new", "/pallet/new", "/packing/new", "/clients/new",
                "/my-settings", "/settings", "/admin/users", "/admin/audit", "/invoices",
                "/invoice-br/new", "/materials", "/няма-такава"):
        page.goto(live_server + url)
        page.wait_for_timeout(150)
        assert page.evaluate("window.__csp") == [], url
    page.goto(live_server + "/cmr/new")
    page.fill('input[name="consignee_name"]', "Получател ЕООД")
    page.click('#main-doc-form button[type="submit"]:not([formaction])')
    page.wait_for_url(re.compile(r".*/doc/\d+$"))
    page.evaluate("() => { window.__printed = 0; window.print = () => { window.__printed += 1; }; }")
    page.click("a[data-print]")
    assert page.evaluate("window.__printed") == 1, "бившият javascript:window.print()"
    assert page.evaluate("window.__csp") == []
    page.evaluate("""() => { const s = document.createElement('script'); s.type = 'speculationrules';
        s.textContent = JSON.stringify({prefetch: [{source: 'list', urls: ['/docs']}]});
        document.head.appendChild(s); }""")
    page.wait_for_timeout(100)
    assert page.evaluate("window.__csp") == [], "speculationrules трябва да са разрешени"
    page.evaluate("() => { const s = document.createElement('script'); s.textContent ="
                  " 'window.__inl = 1'; document.head.appendChild(s); }")
    page.wait_for_timeout(100)
    assert page.evaluate("window.__inl") is None and len(page.evaluate("window.__csp")) == 1
    assert errors == []


@pytest.mark.e2e
def test_former_inline_scripts_still_work(page, live_server):
    e2e_login(page, live_server)
    page.goto(live_server + "/clients/new")
    assert page.locator("#unload-points tbody tr").count() >= 1   # initItemsTable от data-*
    page.goto(live_server + "/my-settings")
    page.wait_for_function(
        "document.getElementById('remote-status-box').textContent.indexOf('не е активен') >= 0")
    # S7: етикетите на клонираните палетни карти — уникални id и for=.
    page.goto(live_server + "/pallet/new")
    page.click("#pallet-add-card-btn")
    page.click("#pallet-add-card-btn")
    pairs = page.evaluate("""Array.from(document.querySelectorAll('#pallet-cards label[data-for-field]'))
        .map(l => [l.htmlFor, !!l.htmlFor && document.getElementById(l.htmlFor) !== null])""")
    ids = [p[0] for p in pairs]
    # PALLET-7 (15.10.2026): + „Нето, кг“ — 5 етикета на клонирана карта (бяха 4).
    assert len(pairs) == 10 and all(p[1] for p in pairs) and len(set(ids)) == len(ids), pairs
    # Тема — бившият onchange="this.form.submit()".
    page.goto(live_server + "/my-settings")
    radios = page.locator('input[name="theme"]')
    target = radios.nth(1)
    value = target.get_attribute("value")
    with page.expect_navigation():
        target.check()
    assert page.locator('input[name="theme"][value="%s"]' % value).is_checked()


# ============================================================ S4 одитен дневник

def _audit_rows(db_module):
    con = db_module.get_db()
    try:
        return [dict(r) for r in con.execute("SELECT * FROM audit_log ORDER BY id")]
    finally:
        con.close()


def test_audit_log_table_comes_from_a_migration(db_module):
    assert db_module._m014_audit_log in db_module.MIGRATIONS
    assert db_module.MIGRATIONS.index(db_module._m014_audit_log) == \
        db_module.MIGRATIONS.index(db_module._m013_app_instances) + 1
    con = db_module.get_db()
    try:
        cols = [r["name"] for r in con.execute("PRAGMA table_info(audit_log)")]
    finally:
        con.close()
    assert cols == ["id", "at", "user_id", "username", "ip", "machine", "action", "detail"]


def test_audit_is_written_to_the_db_and_still_to_the_file(admin_client, db_module, capsys):
    resp = post_with_csrf(admin_client, "/admin/users/new", {
        "username": "нов_служител", "password": "Много-Сложна-Парола-17", "role": "employee"},
        csrf_source_url="/admin/users")
    assert resp.status_code == 302
    rows = _audit_rows(db_module)
    actions = [r["action"] for r in rows]
    assert "успешен вход" in actions and "създаден служител" in actions
    row = rows[actions.index("създаден служител")]
    assert row["username"] == "test_admin" and row["user_id"] and row["ip"] == "127.0.0.1"
    assert row["machine"] == lm.machine_name() and "нов_служител" in row["detail"]
    assert "ОДИТ: test_admin" in capsys.readouterr().out, "файлът (stdout лога) остава"


def test_audit_never_touches_a_db_this_process_did_not_open(tmp_path, monkeypatch):
    import db
    foreign = str(tmp_path / "foreign.db")
    sqlite3.connect(foreign).close()
    before = open(foreign, "rb").read()
    monkeypatch.setattr(db, "DB_PATH", foreign)
    applog.log_audit("нещо")
    assert applog.flush_audit(2)
    assert open(foreign, "rb").read() == before


def test_busy_db_never_slows_or_fails_the_action_and_is_retried(db_module, monkeypatch):
    monkeypatch.setattr(applog, "_AUDIT_RETRY_DELAYS", (0.3, 0.3, 0.3, 0.3))
    blocker = sqlite3.connect(db_module.DB_PATH)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        started = time.monotonic()
        applog.log_audit("действие при заета база", "x=1")
        assert time.monotonic() - started < 1.0, "действието не чака дневника"
    finally:
        time.sleep(0.2)
        blocker.rollback()
        blocker.close()
    assert applog.flush_audit(10)
    assert [r["action"] for r in _audit_rows(db_module)] == ["действие при заета база"]


def test_unwritable_audit_db_is_logged_once_not_raised(db_module, monkeypatch, capsys):
    import db
    monkeypatch.setattr(applog, "_AUDIT_RETRY_DELAYS", (0.01,))
    monkeypatch.setattr(applog, "_throttle_last", {})

    def broken(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(db, "write_audit_rows", broken)
    applog.log_audit("първо")
    applog.log_audit("второ")
    assert applog.flush_audit(5)
    out = capsys.readouterr().out
    assert out.count("не са записани в одитния дневник") == 1


def test_audit_retention_and_row_cap(db_module, monkeypatch):
    now = datetime(2026, 10, 13, 12, 0, 0)
    old = (now - timedelta(days=db_module.AUDIT_RETENTION_DAYS + 1)).strftime("%Y-%m-%d %H:%M:%S")
    fresh = now.strftime("%Y-%m-%d %H:%M:%S")
    row = {"user_id": None, "username": "u", "ip": None, "machine": None, "detail": None}
    db_module.write_audit_rows(db_module.DB_PATH, [dict(row, at=old, action="старо")])
    db_module.write_audit_rows(db_module.DB_PATH, [dict(row, at=fresh, action="ново")],
                               now=now, prune=True)
    assert [r["action"] for r in _audit_rows(db_module)] == ["ново"]
    monkeypatch.setattr(db_module, "AUDIT_MAX_ROWS", 3)
    db_module.write_audit_rows(db_module.DB_PATH, [dict(row, at=fresh, action="n%d" % i)
                                                   for i in range(6)], now=now, prune=True)
    assert [r["action"] for r in _audit_rows(db_module)] == ["n3", "n4", "n5"]


def _seed_audit(db_module, n=60):
    rows = []
    for i in range(n):
        rows.append({"at": "2026-10-%02d 10:00:%02d" % (1 + i // 30, i % 60),
                     "user_id": 1, "username": "иван" if i % 2 else "мария", "ip": "10.0.0.%d" % i,
                     "machine": "PC", "action": "изтрит документ" if i % 3 == 0 else "успешен вход",
                     "detail": "№%d <b>" % i})
    db_module.write_audit_rows(db_module.DB_PATH, rows)


def test_admin_audit_page_is_paged_filtered_and_newest_first(admin_client, db_module):
    applog.flush_audit(2)
    con = db_module.get_db()
    con.execute("DELETE FROM audit_log")
    con.commit()
    con.close()
    _seed_audit(db_module)
    body = admin_client.get("/admin/audit").get_data(as_text=True)
    assert "№59 &lt;b&gt;" in body and "№0 &lt;b&gt;" not in body, "най-новите първи, екранирано"
    assert "Страница" in body and "/admin/audit?page=2" in body.replace("&amp;", "&")
    page2 = admin_client.get("/admin/audit?page=2").get_data(as_text=True)
    assert "№0 &lt;b&gt;" in page2
    only = admin_client.get("/admin/audit?user=иван&action=ИЗТРИТ").get_data(as_text=True)
    found = re.findall(r"№(\d+) &lt;b&gt;", only)
    assert found and all(int(n) % 2 == 1 and int(n) % 3 == 0 for n in found)
    assert admin_client.get("/admin/audit?page=abc").status_code == 200
    assert 'href="/admin/audit"' in admin_client.get("/admin/users").get_data(as_text=True)


def test_admin_audit_page_is_admin_only(employee_client):
    resp = employee_client.get("/admin/audit")
    assert resp.status_code in (302, 403)
    assert "Одитен дневник" not in resp.get_data(as_text=True)


# ============================================================ S5 погълнати грешки

def test_throttled_warning_is_written_once_per_interval(monkeypatch, capsys):
    monkeypatch.setattr(applog, "_throttle_last", {})
    assert applog.log_warning_throttled("k", "ctx", "съобщение", interval=600)
    assert not applog.log_warning_throttled("k", "ctx", "съобщение", interval=600)
    assert applog.log_warning_throttled("друг", "ctx", "съобщение", interval=600)
    assert capsys.readouterr().out.count("ctx: съобщение") == 2


def test_heartbeat_uses_a_short_timeout_and_logs_failures(admin_client, db_module, monkeypatch,
                                                          capsys):
    import appcore
    seen = []
    monkeypatch.setattr(applog, "_throttle_last", {})
    monkeypatch.setattr(db_module, "record_instance", lambda **kw: seen.append(kw))
    assert admin_client.get("/docs").status_code == 200
    assert seen and seen[-1]["timeout"] == db_module.INSTANCE_REQUEST_TIMEOUT <= 0.5

    def boom(**kw):
        raise RuntimeError("заета мрежа")
    monkeypatch.setattr(db_module, "record_instance", boom)
    for _ in range(3):
        assert admin_client.get("/docs").status_code == 200
    out = capsys.readouterr().out
    assert out.count("appcore._instance_heartbeat: отчетът на копието не мина") == 1
    assert hasattr(appcore, "_instance_heartbeat")


def test_busy_shared_db_heartbeat_returns_quickly(db_module):
    blocker = sqlite3.connect(db_module.DB_PATH)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        started = time.monotonic()
        assert not db_module.record_instance(force=True, timeout=db_module.INSTANCE_REQUEST_TIMEOUT)
        assert time.monotonic() - started < 1.5
    finally:
        blocker.rollback()
        blocker.close()


def test_cleanup_errors_are_logged_not_swallowed(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(applog, "_throttle_last", {})
    programdata = tmp_path / "pd"
    folder = programdata / lm.LEGACY_INSTALL_DIR_NAME
    folder.mkdir(parents=True)
    (folder / "keep.txt").write_text("x", encoding="utf-8")  # не е празна → rmdir гърми
    assert lm.cleanup_legacy_leftovers("", str(programdata)) == []
    assert "не е почистена" in capsys.readouterr().out
    real_listdir = os.listdir
    monkeypatch.setattr(lm, "cleanup_legacy_leftovers",
                        lambda *a: (_ for _ in ()).throw(ValueError("неочаквано")))
    monkeypatch.setattr(lm, "_is_frozen_windows", lambda: True)
    monkeypatch.setattr(lm, "migrate_user_dir", lambda d: None)
    monkeypatch.setattr(lm, "migrate_install_dir", lambda *a: None)
    monkeypatch.setattr(lm, "is_portable_dir", lambda *a: False)
    monkeypatch.setitem(lm._runtime, "result", lm._runtime.get("result"))
    lm.run_at_startup()
    assert "почистване на остатъците" in capsys.readouterr().out
    assert os.listdir is real_listdir


def test_backup_temp_sweep_errors_are_logged(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(applog, "_throttle_last", {})
    monkeypatch.setattr(backup.tempfile, "gettempdir", lambda: str(tmp_path))
    (tmp_path / "ph_bk_old").mkdir()

    def bad_mtime(p):
        raise PermissionError(13, "отказан достъп")
    monkeypatch.setattr(backup.os.path, "getmtime", bad_mtime)
    backup._sweep_stale_temp_dirs()
    assert "временната папка не е почистена" in capsys.readouterr().out


# ============================================================ S6 мъртъв код

def test_dead_code_is_gone():
    import inspect

    import appcore
    import client_export
    import db
    import routes_documents
    assert not hasattr(routes_documents, "_CLIENT_NAME_SQL")
    assert not hasattr(appcore, "_fmt_amount")
    assert not hasattr(client_export, "save_client_export_copy")
    assert not hasattr(db, "get_document_id_by_public_token")
    assert list(inspect.signature(appcore.create_app).parameters) == []


@pytest.mark.parametrize("data", [
    {"consignee_name": "А", "receiver_name": "Б", "client_name": "В"},
    {"consignee_name": "   ", "receiver_name": " Б ", "client_name": "В"},
    {"client_name": "В", "dest_name": "Г"},
    {"dest_name": " Г "},
    {"consignee_name": ""},
    {},
])
def test_client_name_priority_is_one_rule_for_sql_and_python(data):
    """S6: SQL изразът (колоната documents.client_name) и Python
    (client_export.resolve_client_name — папките) идват от един списък."""
    import client_export
    import db
    con = sqlite3.connect(":memory:")
    try:
        sql = con.execute("SELECT " + db._CLIENT_NAME_EXPR_TMPL.format(":"),
                          {"data": json.dumps(data)}).fetchone()[0]
    finally:
        con.close()
    assert client_export.resolve_client_name(data) == sql


def test_documents_list_shows_the_client_name_column(admin_client, db_module):
    from conftest import issue_cmr
    issue_cmr(admin_client, consignee_name="Колона Клиент ООД")
    body = admin_client.get("/docs?group=1").get_data(as_text=True)
    assert "Колона Клиент ООД" in body


# ============================================================ S7 достъпност

def test_pdf_barcode_image_has_alt_text():
    src = read_source("templates", "pdf_export.html")
    img = re.search(r"<img [^>]*barcode_uri[^>]*>", src).group(0)
    assert 'alt="' in img


def test_pallet_card_template_labels_point_to_fields():
    src = read_source("templates", "pallet_form.html")
    template = src[src.index('<template id="pallet-card-template">'):src.index("</template>")]
    for field in ("pallet_no", "packaging_type", "pallet_type", "gross"):
        assert 'data-for-field="%s"' % field in template
        assert 'data-field="%s"' % field in template
    assert "linkCardLabels(block)" in read_source("static", "app.js")


# ============================================================ S8 преименуване на споделената база

class TestSharedRename:
    @pytest.fixture
    def tmp_db_path(self, tmp_path):
        folder = tmp_path / "share"  # базата е със СТАРОТО име (като в test_rename_finish)
        folder.mkdir()
        return str(folder / "pacho_logistic.db")

    def test_rename_is_undone_when_a_write_starts_during_the_move(self, db_module, monkeypatch):
        _shared_rename_race(db_module, monkeypatch)


def _shared_rename_race(db_module, monkeypatch):
    """S8: катинарът се пуска преди os.replace (под Windows собствената връзка
    би спряла преименуването); журнал под СТАРОТО име след местенето значи
    чужд запис в прозореца — всичко се връща."""
    import config as appconfig
    from test_rename_finish import _allow_rename
    monkeypatch.setitem(lm._runtime, "db_override", None)
    monkeypatch.setattr(db_module, "_heartbeat_state", {"at": None, "path": None})
    monkeypatch.setattr(db_module, "_heal_state", {"checked_at": None})
    monkeypatch.setattr(db_module, "_USE_WAL", False)
    _allow_rename(db_module, monkeypatch)
    old = db_module.DB_PATH
    new = os.path.join(os.path.dirname(old), "ph_logistics.db")
    real = os.replace

    def replace_and_race(src, dst):
        real(src, dst)
        if src == old:  # друг компютър отваря старото име и започва запис
            with open(old + "-journal", "wb") as fh:
                fh.write(b"x" * 512)
    monkeypatch.setattr(lm.os, "replace", replace_and_race)
    with pytest.raises(db_module.TranslatableError, match="по време на преименуването"):
        db_module.rename_shared_database()
    monkeypatch.setattr(lm.os, "replace", real)
    assert os.path.exists(old) and not os.path.exists(new)
    assert db_module.DB_PATH == old
    assert appconfig.load_config()["db_path"] == old
    assert not os.path.exists(os.path.join(os.path.dirname(old), lm.SHARED_RENAME_MARKER))


# ============================================================ S9 непокрити критични пътища

def _scheduled_restore(db_module, tmp_path):
    from test_restore import _add_clients
    _add_clients(db_module, 3, "архив")
    folder = str(tmp_path / "bk")
    os.makedirs(folder)
    bpath = backup.local_backup(folder)
    backup.request_restore(folder, os.path.basename(bpath))
    _add_clients(db_module, 2, "после")
    return bpath


def _clients(path):
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT count(*) FROM clients").fetchone()[0]
    finally:
        con.close()


def test_restore_rolls_back_when_the_current_db_cannot_be_moved_aside(db_module, tmp_path,
                                                                      monkeypatch):
    _scheduled_restore(db_module, tmp_path)
    open(db_module.DB_PATH + "-shm", "wb").close()
    real = os.replace

    def busy(src, dst):
        if src.endswith("-shm") and "pre_restore_" in dst:
            raise PermissionError(32, "заета от друг процес")
        return real(src, dst)
    monkeypatch.setattr(backup.os, "replace", busy)
    result = backup.apply_pending_restore()
    assert result["ok"] is False and "заета" in result["error"]
    assert os.path.exists(db_module.DB_PATH + "-shm")
    assert not [n for n in os.listdir(os.path.dirname(db_module.DB_PATH))
                if n.startswith("pre_restore_") and os.listdir(
                    os.path.join(os.path.dirname(db_module.DB_PATH), n))], "нищо не остава настрана"
    assert _clients(db_module.DB_PATH) == 5, "текущата база е върната на мястото си"
    assert not os.path.exists(db_module.DB_PATH + ".restore_tmp")
    assert not os.path.exists(backup._restore_marker_path())


def test_restore_rolls_back_when_the_staged_copy_cannot_take_its_place(db_module, tmp_path,
                                                                       monkeypatch):
    _scheduled_restore(db_module, tmp_path)
    real = os.replace

    def fail_final(src, dst):
        if src.endswith(".restore_tmp"):
            raise PermissionError(5, "отказан достъп")
        return real(src, dst)
    monkeypatch.setattr(backup.os, "replace", fail_final)
    result = backup.apply_pending_restore()
    assert result["ok"] is False and result["error"]
    assert _clients(db_module.DB_PATH) == 5
    assert not os.path.exists(db_module.DB_PATH + ".restore_tmp")
    taken = backup.take_restore_result()
    assert taken and taken["ok"] is False


def test_restore_with_a_corrupt_marker_changes_nothing(db_module, tmp_path):
    from test_restore import _add_clients
    _add_clients(db_module, 2, "x")
    with open(backup._restore_marker_path(), "w", encoding="utf-8") as fh:
        fh.write("{не е json")
    result = backup.apply_pending_restore()
    assert result["ok"] is False and "повреден" in result["error"]
    assert _clients(db_module.DB_PATH) == 2
    assert not os.path.exists(backup._restore_marker_path())


def _portable_with_db_path(tmp_path):
    from test_rename_finish import _portable, _write
    folder = _portable(tmp_path, cfg={})
    _write(os.path.join(folder, "pacho_config.json"),
           json.dumps({"db_path": os.path.join(folder, "pacho_logistic.db"), "k": "v"}))
    return folder


def test_rename_in_place_rolls_back_when_the_new_config_cannot_be_written(tmp_path, monkeypatch):
    from test_rename_finish import _marker, _read, _run_portable
    folder = _portable_with_db_path(tmp_path)
    real = lm._write_json_atomic

    def no_config(path, data):
        if os.path.basename(path) == lm.CONFIG_NAME:
            raise OSError(28, "няма място на диска")
        return real(path, data)
    monkeypatch.setattr(lm, "_write_json_atomic", no_config)
    report = _run_portable(folder)
    assert report["status"] == "kept" and report["reason"] == "move_failed", report
    assert "config" in report.get("error", "")
    assert _marker(os.path.join(folder, "pacho_logistic.db")) == "данни"
    assert json.loads(_read(os.path.join(folder, "pacho_config.json")))["k"] == "v"
    assert not os.path.exists(os.path.join(folder, "ph_logistics.db"))
    assert not os.path.exists(os.path.join(folder, "ph_config.json"))


def test_rename_in_place_reports_incomplete_when_the_rollback_fails(tmp_path, monkeypatch):
    from test_rename_finish import _run_portable
    folder = _portable_with_db_path(tmp_path)
    real = lm._write_json_atomic

    def no_config(path, data):
        if os.path.basename(path) == lm.CONFIG_NAME:
            raise OSError(28, "пълен диск")
        return real(path, data)
    monkeypatch.setattr(lm, "_write_json_atomic", no_config)
    monkeypatch.setattr(lm, "_undo", lambda moves: [moves[0]])
    report = _run_portable(folder)
    assert report["status"] == "incomplete" and "не се върнаха" in report["error"], report


class _FakeWinreg(types.ModuleType):
    HKEY_LOCAL_MACHINE = object()

    def __init__(self, open_error=None, values=(), enum_error=None):
        super().__init__("winreg")
        self.open_error, self.values, self.enum_error = open_error, list(values), enum_error
        self.closed = False

    def OpenKey(self, root, path):
        if self.open_error:
            raise self.open_error
        return "key"

    def EnumValue(self, key, index):
        if index < len(self.values):
            return ("Share%d" % index, self.values[index], 7)
        raise self.enum_error or _winerror(259)

    def CloseKey(self, key):
        self.closed = True


def _winerror(code):
    exc = OSError("winerror %d" % code)
    exc.winerror = code
    return exc


def test_read_share_paths_cases(monkeypatch):
    def run(fake):
        monkeypatch.setitem(sys.modules, "winreg", fake)
        return lm.read_share_paths()
    assert run(_FakeWinreg(open_error=FileNotFoundError())) == [], "липсващ ключ — няма дялове"
    assert run(_FakeWinreg(open_error=PermissionError())) is None, "нечетим регистър — не"
    fake = _FakeWinreg(values=[["Path=C:\\Share", "Permissions=0"], "Path=D:\\Data\x00Type=0"])
    assert run(fake) == ["C:\\Share", "D:\\Data"] and fake.closed
    fake = _FakeWinreg(values=[["Path=C:\\X"]], enum_error=_winerror(5))
    assert run(fake) is None and fake.closed, "друга грешка при четене — не е безопасно"
    monkeypatch.setitem(sys.modules, "winreg", None)  # не е Windows
    assert lm.read_share_paths() is None


def _legacy_setup(tmp_path, monkeypatch):
    from test_rename_migration import _as_legacy_local
    return _as_legacy_local(tmp_path, monkeypatch)


def test_install_via_setup_refusals(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "is_frozen_windows", lambda: False)
    with pytest.raises(updater.UpdateError, match="компилираната"):
        updater.install_via_setup("https://example.invalid/s.exe", "a" * 64)
    _legacy_setup(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(updater.net, "urlopen", lambda *a, **k: calls.append(a))
    # Провален преход за тази версия — не се опитва пак автоматично.
    with open(updater._rename_failed_marker_path(), "w", encoding="utf-8") as fh:
        fh.write("3.99.0 setup")
    with pytest.raises(updater.UpdateError, match="вече беше опитан"):
        updater.install_via_setup("https://example.invalid/s.exe", "a" * 64, version="3.99.0")
    # Друго обновяване тече — отказ, без изтегляне.
    assert updater._install_lock.acquire(blocking=False)
    try:
        with pytest.raises(updater.UpdateError, match="вече тече"):
            updater.install_via_setup("https://example.invalid/s.exe", "a" * 64, version="4.0.0")
    finally:
        updater._install_lock.release()
    assert calls == []
    # Твърде голям инсталатор — отказ и без остатък в работната папка.
    monkeypatch.setattr(updater.net, "urlopen", lambda req, timeout=120: _Resp(
        PAYLOAD, length=updater.MAX_DOWNLOAD_BYTES + 1))
    monkeypatch.setattr(updater, "_launch_rename", lambda *a: calls.append("launch"))
    with pytest.raises(updater.UpdateError, match="инсталатор изглежда повреден"):
        updater.install_via_setup("https://example.invalid/s.exe", "a" * 64, version="4.0.0")
    assert calls == [] and not os.path.exists(
        os.path.join(updater._rename_work_dir(), updater.SETUP_NAME))
    assert updater._install_lock.acquire(blocking=False), "заключването е освободено"
    updater._install_lock.release()
