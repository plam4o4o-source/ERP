# -*- coding: utf-8 -*-
"""Довършване на прехода PachoLogistic/pacho_ → PHLogistics/ph_ (одит 07.10.2026).

Покрива: вътрешните идентификатори с новите имена (и четенето на старите,
където две версии се срещат), преименуването НА МЯСТО на преносимата
инсталация (и отказа при споделена/мрежова папка), самовъзстановяването на
пътя до споделена база, преименувана от друг компютър, отчета на копията
(app_instances), действието „Преименувай споделената база на новото име“,
остатъците от старото име на машината и CI сценариите (по изходния код +
помощника на CI срещу истински SQLite файлове).

Всичко е във временни папки — истинските данни в корена не се пипат."""
import hashlib
import io
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta

import pytest

import config as appconfig
import legacy_migration as lm
import updater
from conftest import ROOT, post_with_csrf, read_source


# ---------------------------------------------------------------- помощни

@pytest.fixture
def tmp_db_path(tmp_path):
    """Тук временната база е със СТАРОТО име в отделна („споделена“) папка —
    както мрежовата база с изричен db_path."""
    folder = tmp_path / "share"
    folder.mkdir()
    return str(folder / "pacho_logistic.db")


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    import db
    monkeypatch.setitem(lm._runtime, "db_override", None)
    monkeypatch.setitem(lm._runtime, "result", None)
    monkeypatch.setattr(db, "_heartbeat_state", {"at": None, "path": None})
    monkeypatch.setattr(db, "_heal_state", {"checked_at": None})
    # Базата с изричен db_path (мрежова) никога не е в WAL — както на терен.
    monkeypatch.setattr(db, "_USE_WAL", False)


def _write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _make_db(path, marker="ok", wal_marker=None):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE marker (v TEXT)")
    con.execute("INSERT INTO marker VALUES (?)", (marker,))
    con.commit()
    if wal_marker is not None:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA wal_autocheckpoint=0")
        con.execute("UPDATE marker SET v = ?", (wal_marker,))
        con.commit()
    return con  # отворена — извикващият я затваря (при WAL пази -wal)


def _marker(path):
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT v FROM marker").fetchone()[0]
    finally:
        con.close()


def _portable(tmp_path, cfg=None, with_db=True):
    """Преносима папка: PachoLogistic.exe + данни със старите имена."""
    folder = tmp_path / "PH portable"
    (folder / "attachments" / "1").mkdir(parents=True)
    if with_db:
        _make_db(str(folder / "pacho_logistic.db"), marker="данни").close()
    if cfg is not None:
        _write(str(folder / "pacho_config.json"), json.dumps(cfg, ensure_ascii=False))
    _write(str(folder / ".secret_key"), "ab" * 32)
    _write(str(folder / "attachments" / "1" / "a.txt"), "прикачен")
    for name, data in (("PachoLogistic.exe", b"MZ"), ("pacho_startup_1a2b3c4d.log", b"log"),
                       ("pacho_update.log", b"OK"), ("pacho_logistic_20260101_010101.db", b"bk"),
                       ("pacho_restore_request.json", b"{}"), ("company_logo.png", b"PNG")):
        with open(str(folder / name), "wb") as fh:
            fh.write(data)
    return str(folder)


def _run_portable(folder, shares=(), network=False, in_use=False, machine="PC-1"):
    return lm.migrate_portable(
        folder, os.path.join(folder, "PachoLogistic.exe"),
        share_paths_func=lambda: list(shares) if shares is not None else None,
        network_func=lambda p: network, in_use_func=lambda p: in_use, machine=machine)


def _report(folder):
    return json.loads(_read(os.path.join(folder, lm.REPORT_NAME)))


# ---------------------------------------------------------------- 1. вътрешни идентификатори

def test_started_marker_is_confirmed_under_the_new_and_the_old_name(tmp_path, monkeypatch):
    """Новото .exe чете и PACHO_UPDATE_STARTED_MARKER — скриптът на v3.78/
    v3.79 подава старото име; и двете се махат от средата."""
    new_marker, old_marker = tmp_path / "new.txt", tmp_path / "old.txt"
    monkeypatch.setenv("PH_UPDATE_STARTED_MARKER", str(new_marker))
    assert updater.confirm_started()
    assert _read(str(new_marker)) == updater.__version__
    monkeypatch.setenv("PACHO_UPDATE_STARTED_MARKER", str(old_marker))
    assert updater.confirm_started()
    assert _read(str(old_marker)) == updater.__version__
    assert "PH_UPDATE_STARTED_MARKER" not in os.environ
    assert "PACHO_UPDATE_STARTED_MARKER" not in os.environ
    assert not updater.confirm_started()
    assert updater.STARTED_MARKER_ENV == "PH_UPDATE_STARTED_MARKER"


@pytest.mark.parametrize("name", ["PH_DISABLE_AUTO_UPDATE", "PACHO_DISABLE_AUTO_UPDATE"])
def test_auto_update_is_disabled_by_the_new_and_the_old_name(monkeypatch, name):
    started = []
    monkeypatch.setattr(updater, "is_frozen_windows", lambda: True)
    monkeypatch.setattr(updater.threading, "Thread", lambda *a, **kw: started.append(kw))
    monkeypatch.delenv("PH_DISABLE_AUTO_UPDATE", raising=False)
    monkeypatch.delenv("PACHO_DISABLE_AUTO_UPDATE", raising=False)
    monkeypatch.setenv(name, "1")
    updater.start_auto_update_loop(lambda: False)
    assert started == []


def test_update_script_passes_only_the_new_marker_and_clears_both(tmp_path, monkeypatch):
    """Нашият скрипт подава новото име на новото .exe (наследеното старо се
    маха), а преди да пусне старото .exe чисти и двете."""
    from test_update_rollback import _generate
    monkeypatch.setenv("PACHO_UPDATE_STARTED_MARKER", "наследено")
    bat, popen, _exe = _generate(tmp_path, monkeypatch)
    assert "PACHO_UPDATE_STARTED_MARKER" not in popen["env"]
    assert popen["env"]["PH_UPDATE_STARTED_MARKER"].endswith(".txt")
    with open(bat, encoding="ascii", newline="") as fh:
        lines = fh.read().split("\r\n")
    launch = lines.index(":launch")
    assert lines[launch + 1:launch + 4] == [
        "set PH_UPDATE_STARTED_MARKER=", "set PACHO_UPDATE_STARTED_MARKER=", 'start "" "%~2"']
    rename = updater._rename_bat_content().split("\r\n")
    old = rename.index(":launchold")
    assert rename[old + 1:old + 3] == ["set PH_UPDATE_STARTED_MARKER=",
                                       "set PACHO_UPDATE_STARTED_MARKER="]
    src = read_source("updater.py")
    assert src.count("_env_for_new_exe(") == 3, "двата скрипта ползват общата среда"


def test_pdf_ready_cookie_has_the_new_name_and_clears_the_old(admin_client):
    from conftest import issue_cmr
    doc_id = issue_cmr(admin_client)
    admin_client.set_cookie("pacho_pdf_ready", "old")
    resp = admin_client.get("/doc/%d/export.pdf?dl=tok123" % doc_id)
    cookies = resp.headers.getlist("Set-Cookie")
    assert any(c.startswith("ph_pdf_ready=tok123;") for c in cookies), cookies
    assert any(c.startswith("pacho_pdf_ready=;") and "Max-Age=0" in c for c in cookies), cookies
    js = read_source("static", "app.js")
    assert 'indexOf("ph_pdf_ready=" + token)' in js
    assert 'indexOf("pacho_pdf_ready=' not in js


def test_internal_flags_and_fallback_repair_address(monkeypatch, db_module):
    import appcore
    assert appcore.ERROR_HOP_FLAG == "_ph_error_redirect"
    src = read_source("app.py")
    assert 'fallback.config["PH_DB_UNAVAILABLE"] = True' in src
    assert src.count('app.config.get("PH_DB_UNAVAILABLE")') == 2
    assert "PACHO_DB_UNAVAILABLE\"]" not in src
    from test_errors_i18n import _fallback_app
    client = _fallback_app(monkeypatch).test_client()
    page = client.get("/").get_data(as_text=True)
    assert "/ph-fix-db-path" in page
    for path in ("/ph-fix-db-path", "/pacho-fix-db-path"):  # старият адрес — синоним
        resp = client.get(path)
        assert resp.status_code == 200 and 'action="/ph-fix-db-path"' in resp.get_data(as_text=True)


def test_pdf_fonts_and_page_total_use_the_new_internal_names():
    import pdf_export
    from reportlab.pdfgen import canvas
    assert pdf_export._PDF_METRIC_FONT == "PHDejaVuSans"
    assert pdf_export._PDF_METRIC_FONT_BOLD == "PHDejaVuSans-Bold"
    assert pdf_export._footer_font() == "PHDejaVuSans"
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(100, 20, "1")
    c.showPage()
    c.save()
    stamped = pdf_export._stamp_page_total(buf.getvalue())
    assert b"/PHPageTotal0" in stamped and b"Pacho" not in stamped


def test_weak_password_list_has_the_new_name(flask_app):
    import appcore
    with flask_app.test_request_context():
        for word in ("phlogistics", "PHLogistic", "PH Logistics", "pachologistik"):
            assert appcore.password_policy_error(word) is not None, word
        assert appcore.password_policy_error("Зелена-Ябълка-42") is None


# ---------------------------------------------------------------- 2. преносима инсталация

def test_portable_install_is_renamed_in_place(tmp_path):
    folder = _portable(tmp_path, cfg={"network_port": 5050, "x": "у"})
    report = _run_portable(folder)
    assert report["status"] == "renamed" and report["kind"] == "in_place", report
    assert _marker(os.path.join(folder, "ph_logistics.db")) == "данни"
    assert json.loads(_read(os.path.join(folder, "ph_config.json"))) == {"network_port": 5050,
                                                                         "x": "у"}
    for name in ("pacho_logistic.db", "pacho_config.json", "pacho_startup_1a2b3c4d.log",
                 "pacho_update.log"):
        assert not os.path.exists(os.path.join(folder, name)), name
    for name in ("ph_startup_1a2b3c4d.log", "ph_update.log", ".secret_key",
                 "PachoLogistic.exe",                      # .exe-то НЕ се преименува
                 "pacho_logistic_20260101_010101.db",      # архивите — също
                 "pacho_restore_request.json", "company_logo.png"):
        assert os.path.exists(os.path.join(folder, name)), name
    assert _read(os.path.join(folder, "attachments", "1", "a.txt")) == "прикачен"
    assert _report(folder)["status"] == "renamed"
    # Програмата вече намира данните по новите имена.
    assert lm.resolve_config_path(folder).endswith("ph_config.json")
    assert lm.resolve_default_db_path(folder).endswith("ph_logistics.db")
    assert _run_portable(folder) is None, "второ пускане — нищо за правене"


def test_portable_wal_content_survives_the_rename(tmp_path):
    folder = _portable(tmp_path, with_db=False)
    con = _make_db(os.path.join(folder, "pacho_logistic.db"), marker="first", wal_marker="in-wal")
    try:
        assert os.path.exists(os.path.join(folder, "pacho_logistic.db-wal"))
    finally:
        con.close()
    assert _run_portable(folder)["status"] == "renamed"
    assert _marker(os.path.join(folder, "ph_logistics.db")) == "in-wal"
    assert not [n for n in os.listdir(folder) if n.startswith("pacho_logistic.db")]


@pytest.mark.parametrize("kwargs, reason", [
    ({"shares": None}, "registry"),
    ({"network": True}, "network"),
    ({"in_use": True}, "running"),
])
def test_portable_in_doubt_nothing_is_renamed(tmp_path, kwargs, reason):
    folder = _portable(tmp_path, cfg={"a": 1})
    with open(os.path.join(folder, "PHLogistics.exe"), "wb") as fh:
        fh.write(b"MZ")  # друго .exe в папката („работи“ при in_use)
    before = sorted(os.listdir(folder))
    report = _run_portable(folder, **kwargs)
    assert report["status"] == "kept" and report["reason"] == reason, report
    assert sorted(os.listdir(folder)) == sorted(before + [lm.REPORT_NAME])
    assert _marker(os.path.join(folder, "pacho_logistic.db")) == "данни"


def test_portable_shared_folder_is_left_alone_and_reported_once(tmp_path):
    folder = _portable(tmp_path, cfg={"a": 1})
    report = _run_portable(folder, shares=[os.path.dirname(folder)])
    assert report["status"] == "kept" and report["reason"] == "shared"
    assert os.path.exists(os.path.join(folder, "pacho_logistic.db"))
    assert os.path.exists(os.path.join(folder, "pacho_config.json"))
    assert not os.path.exists(os.path.join(folder, "ph_logistics.db"))
    assert lm.take_report_for_admin(folder)["reason"] == "shared"
    _run_portable(folder, shares=[folder])  # същата причина — отчетът не се подновява
    assert lm.take_report_for_admin(folder) is None


def test_portable_explicit_db_path_to_the_default_file_follows_the_rename(tmp_path):
    folder = _portable(tmp_path, cfg={})
    _write(os.path.join(folder, "pacho_config.json"),
           json.dumps({"db_path": os.path.join(folder, "pacho_logistic.db"), "k": "v"}))
    assert _run_portable(folder)["status"] == "renamed"
    cfg = json.loads(_read(os.path.join(folder, "ph_config.json")))
    assert cfg == {"db_path": os.path.join(folder, "ph_logistics.db"), "k": "v"}
    assert _marker(cfg["db_path"]) == "данни"


def test_portable_db_path_elsewhere_keeps_that_db_untouched(tmp_path):
    other = tmp_path / "server"
    other.mkdir()
    remote = str(other / "pacho_logistic.db")
    _make_db(remote, marker="мрежа").close()
    folder = _portable(tmp_path, cfg={"db_path": remote})
    report = _run_portable(folder)
    assert report["status"] == "renamed" and report["renamed"] == ["pacho_config.json"]
    assert json.loads(_read(os.path.join(folder, "ph_config.json")))["db_path"] == remote
    assert _marker(remote) == "мрежа"
    assert os.path.exists(os.path.join(folder, "pacho_logistic.db")), "неактивна база — не се пипа"


@pytest.mark.parametrize("setup, reason", [
    ("both_db", "conflict"), ("both_cfg", "conflict"), ("bad_cfg", "config_unreadable"),
    ("bad_db", "db_unreadable"), ("others", "used_by_others"),
])
def test_portable_refusals(tmp_path, setup, reason):
    folder = _portable(tmp_path, cfg={"a": 1})
    if setup == "both_db":
        _make_db(os.path.join(folder, "ph_logistics.db")).close()
    elif setup == "both_cfg":
        _write(os.path.join(folder, "ph_config.json"), "{}")
    elif setup == "bad_cfg":
        _write(os.path.join(folder, "pacho_config.json"), "{не е json")
    elif setup == "bad_db":
        with open(os.path.join(folder, "pacho_logistic.db"), "wb") as fh:
            fh.write(b"not a database at all" * 100)
    elif setup == "others":
        con = sqlite3.connect(os.path.join(folder, "pacho_logistic.db"))
        con.execute("CREATE TABLE app_instances (machine TEXT PRIMARY KEY, version TEXT,"
                    " exe_name TEXT, last_seen TEXT)")
        con.execute("INSERT INTO app_instances VALUES ('PC-2', '3.80.0', 'x.exe', ?)",
                    (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),))
        con.commit()
        con.close()
    report = _run_portable(folder)
    assert report["status"] == "kept" and report["reason"] == reason, report
    assert os.path.exists(os.path.join(folder, "pacho_config.json"))
    assert os.path.exists(os.path.join(folder, "pacho_logistic.db"))


def test_portable_failure_in_the_middle_rolls_everything_back(tmp_path, monkeypatch):
    """Под Windows отворена база не може да бъде преименувана — os.replace
    гърми; всичко се връща, дневникът изчезва."""
    folder = _portable(tmp_path, cfg={"a": 1})
    real = os.replace

    def flaky(src, dst):
        if os.path.basename(src) == "pacho_config.json":
            raise PermissionError(13, "в употреба")
        return real(src, dst)
    monkeypatch.setattr(lm.os, "replace", flaky)
    report = _run_portable(folder)
    assert report["status"] == "kept" and report["reason"] == "move_failed", report
    assert _marker(os.path.join(folder, "pacho_logistic.db")) == "данни"
    assert not os.path.exists(os.path.join(folder, "ph_logistics.db"))
    assert _report(folder)["status"] == "kept"


def test_portable_interrupted_rename_is_recovered_and_redone(tmp_path):
    folder = _portable(tmp_path, cfg={"a": 1})
    src, dst = os.path.join(folder, "pacho_logistic.db"), os.path.join(folder, "ph_logistics.db")
    _write(os.path.join(folder, lm.REPORT_NAME), json.dumps({
        "status": "in_progress", "moves": [[src, dst],
                                           [os.path.join(folder, "pacho_config.json"),
                                            os.path.join(folder, "ph_config.json")]]}))
    os.replace(src, dst)  # „токът спря“ след първото преименуване
    report = _run_portable(folder)
    assert report["status"] == "renamed", report
    assert _marker(dst) == "данни"
    assert not os.path.exists(src)


def test_portable_only_outside_the_default_install_folders(tmp_path):
    local = str(tmp_path)
    new_dir, legacy_dir = lm.install_dirs(local)
    assert not lm.is_portable_dir(new_dir, local)
    assert not lm.is_portable_dir(legacy_dir, local)
    assert not lm.is_portable_dir(str(tmp_path / "D"), "")
    assert lm.is_portable_dir(str(tmp_path / "D"), local)


def test_run_at_startup_runs_the_portable_rename_and_the_cleanup(tmp_path, monkeypatch):
    calls = []
    exe = tmp_path / "PH" / "PachoLogistic.exe"
    monkeypatch.setattr(lm, "_is_frozen_windows", lambda: True)
    monkeypatch.setattr(lm.sys, "executable", str(exe))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("ProgramData", str(tmp_path / "pd"))
    monkeypatch.setattr(lm, "migrate_user_dir", lambda local: None)
    monkeypatch.setattr(lm, "migrate_install_dir", lambda d, local: None)
    monkeypatch.setattr(lm, "migrate_portable", lambda d, e: calls.append(("portable", d, e))
                        or {"status": "renamed"})
    monkeypatch.setattr(lm, "cleanup_legacy_leftovers",
                        lambda local, pd: calls.append(("cleanup", local, pd)))
    assert lm.run_at_startup() == {"status": "renamed"}
    assert calls == [("portable", str(tmp_path / "PH"), str(exe)),
                     ("cleanup", str(tmp_path / "local"), str(tmp_path / "pd"))]
    assert lm.last_result() == {"status": "renamed"}


def test_migration_reports_for_the_admin(admin_client, monkeypatch):
    import routes_admin
    folder = os.path.dirname(appconfig.CONFIG_PATH)
    for status, extra, text in (
            ("renamed", {}, "са преименувани на новите имена"),
            ("kept", {"reason": "used_by_others"}, "базата се ползва и от други компютри")):
        monkeypatch.setattr(routes_admin, "_migration_report_checked", set())
        report = {"status": status, "legacy_dir": r"D:\PH", "new_dir": r"D:\PH", "shown": False}
        report.update(extra)
        _write(os.path.join(folder, lm.REPORT_NAME), json.dumps(report))
        assert text in admin_client.get("/").get_data(as_text=True)


def test_system_page_offers_the_manual_exe_rename(admin_client, monkeypatch):
    import routes_settings
    monkeypatch.setattr(routes_settings, "_exe_rename_hint",
                        lambda: {"old": "PachoLogistic.exe", "new": "PHLogistics.exe"})
    page = admin_client.get("/admin/system").get_data(as_text=True)
    assert "Може да го преименувате ръчно на PHLogistics.exe" in page


def test_exe_rename_hint_only_for_a_legacy_named_portable_exe(tmp_path, monkeypatch):
    import routes_settings
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater, "is_legacy_local_install", lambda: False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "PachoLogistic.exe"))
    assert routes_settings._exe_rename_hint() == {"old": "PachoLogistic.exe",
                                                  "new": "PHLogistics.exe"}
    monkeypatch.setattr(sys, "executable", str(tmp_path / "PHLogistics.exe"))
    assert routes_settings._exe_rename_hint() is None
    monkeypatch.setattr(sys, "executable", str(tmp_path / "PachoLogistic.exe"))
    monkeypatch.setattr(updater, "is_legacy_local_install", lambda: True)
    assert routes_settings._exe_rename_hint() is None


# ---------------------------------------------------------------- 3а. самовъзстановяване на пътя

def test_heal_db_path_cases(tmp_path):
    old, new = str(tmp_path / "pacho_logistic.db"), str(tmp_path / "ph_logistics.db")
    assert lm.heal_db_path(old) == (None, None), "нито едното — нищо"
    assert lm.heal_db_path(str(tmp_path / "other.db")) == (None, None)
    _make_db(new).close()
    assert lm.heal_db_path(old) == (new, "missing")
    os.replace(new, old)
    assert lm.heal_db_path(new) == (old, "missing"), "и в обратната посока"
    os.replace(old, new)
    open(old, "wb").close()  # стара версия създаде празен файл след преименуването
    assert lm.heal_db_path(old) == (None, None), "без бележката — нищо не се сменя"
    _write(str(tmp_path / lm.SHARED_RENAME_MARKER),
           json.dumps({"from": "pacho_logistic.db", "to": "ph_logistics.db"}))
    assert lm.heal_db_path(old) == (new, "recreated")
    os.remove(old)
    con = sqlite3.connect(old)  # пресъздадена, но вече с документ — не се пипа
    con.execute("CREATE TABLE documents (id INTEGER)")
    con.execute("INSERT INTO documents VALUES (1)")
    con.commit()
    con.close()
    assert lm.heal_db_path(old) == (None, None)


def test_config_resolve_heals_and_rewrites_this_pcs_config(tmp_path, monkeypatch):
    cfg_path = str(tmp_path / "ph_config.json")
    monkeypatch.setattr(appconfig, "CONFIG_PATH", cfg_path)
    share = tmp_path / "share"
    share.mkdir()
    old, new = str(share / "pacho_logistic.db"), str(share / "ph_logistics.db")
    _write(cfg_path, json.dumps({"db_path": old, "network_mode": True, "network_port": 5001}))
    _make_db(new, marker="споделена").close()
    assert appconfig.resolve_db_path(str(tmp_path)) == new
    assert json.loads(_read(cfg_path)) == {"db_path": new, "network_mode": True,
                                           "network_port": 5001}
    assert not os.path.exists(old), "никога нова празна база под старото име"


def test_running_instance_follows_the_rename_by_another_pc(db_module, monkeypatch):
    """Друг компютър преименува базата, докато тази програма работи:
    следващото отваряне намира новото име (без нова празна база)."""
    old = db_module.DB_PATH
    appconfig.save_config({"db_path": old, "network_port": 5002})
    new = os.path.join(os.path.dirname(old), "ph_logistics.db")
    for src, dst in lm.family_moves(old, new):
        os.replace(src, dst)
    con = db_module.get_db()
    try:
        assert con.execute("SELECT COUNT(*) FROM users").fetchone()[0] >= 1
    finally:
        con.close()
    assert db_module.DB_PATH == new
    assert not os.path.exists(old)
    assert appconfig.load_config()["db_path"] == new
    assert appconfig.load_config()["network_port"] == 5002


def test_running_instance_ignores_an_empty_db_recreated_by_an_old_version(db_module):
    old = db_module.DB_PATH
    new = os.path.join(os.path.dirname(old), "ph_logistics.db")
    for src, dst in lm.family_moves(old, new):
        os.replace(src, dst)
    open(old, "wb").close()
    _write(os.path.join(os.path.dirname(old), lm.SHARED_RENAME_MARKER),
           json.dumps({"to": "ph_logistics.db"}))
    db_module.get_db().close()
    assert db_module.DB_PATH == new


# ---------------------------------------------------------------- 3б. отчет на копията

def test_migration_creates_app_instances_and_startup_records_this_pc(db_module):
    con = db_module.get_db()
    try:
        assert db_module._m013_app_instances in db_module.MIGRATIONS
        cols = [r["name"] for r in con.execute("PRAGMA table_info(app_instances)")]
        assert cols == ["machine", "version", "exe_name", "last_seen"]
        assert db_module.record_instance(force=True)
        row = con.execute("SELECT * FROM app_instances").fetchone()
        assert row["machine"] == lm.machine_name()
        assert row["version"] == updater.__version__ and row["exe_name"] == "source"
    finally:
        con.close()


def test_heartbeat_is_throttled_cheap_and_never_fails(db_module, monkeypatch, tmp_path):
    assert db_module.record_instance(version="3.80.0", force=True)
    assert not db_module.record_instance(version="3.80.1"), "до 10 минути — без запис"
    monkeypatch.setitem(db_module._heartbeat_state, "at",
                        time.monotonic() - db_module.INSTANCE_HEARTBEAT_SECONDS - 1)
    assert db_module.record_instance(version="3.80.1")
    # Заета база: кратко изчакване и тихо „не“, без изключение.
    blocker = sqlite3.connect(db_module.DB_PATH)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        started = time.monotonic()
        assert not db_module.record_instance(force=True)
        assert time.monotonic() - started < 10
    finally:
        blocker.rollback()
        blocker.close()
    # Липсваща база: файл не се създава.
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "missing" / "x.db"))
    assert not db_module.record_instance(force=True)
    assert not os.path.exists(db_module.DB_PATH)


def test_requests_record_the_instance_without_failing(admin_client, db_module, monkeypatch):
    monkeypatch.setattr(db_module, "record_instance",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("x")))
    assert admin_client.get("/").status_code == 200


def _set_instances(db_module, rows):
    con = db_module.get_db()
    con.execute("DELETE FROM app_instances")
    for machine, version, days_ago in rows:
        seen = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
        con.execute("INSERT INTO app_instances VALUES (?, ?, 'PHLogistics.exe', ?)",
                    (machine, version, seen))
    con.commit()
    con.close()


def test_shared_rename_state_and_instance_flags(db_module, monkeypatch):
    _set_instances(db_module, [("A", "3.80.0", 1), ("B", "3.79.0", 2), ("C", "3.70.1", 40)])
    con = db_module.get_db()
    try:
        items = {i["machine"]: i for i in db_module.list_instances(con)}
        assert items["A"]["recent"] and not items["A"]["below_rename"]
        assert items["B"]["below_rename"] and not items["B"]["below_alias"]
        assert items["C"]["below_alias"] and not items["C"]["recent"]
        state = db_module.shared_rename_state(con, {"db_path": ""})
        assert not state["applicable"], "без изричен db_path — няма действие"
        cfg = {"db_path": db_module.DB_PATH}
        state = db_module.shared_rename_state(con, cfg)
        assert state["applicable"] and not state["allowed"]
        assert [b["machine"] for b in state["blockers"]] == ["B"], "старият C (40 дни) не пречи"
        _set_instances(db_module, [("A", "3.80.0", 1), ("C", "3.70.1", 40)])
        assert db_module.shared_rename_state(con, cfg)["allowed"]
        open(state["target"] + "-wal", "wb").close()
        state = db_module.shared_rename_state(con, cfg)
        assert state["target_exists"] and not state["allowed"]
    finally:
        con.close()


# ---------------------------------------------------------------- 3в. преименуване на споделената база

def _allow_rename(db_module, monkeypatch):
    appconfig.save_config({"db_path": db_module.DB_PATH, "network_port": 5003})
    monkeypatch.setattr(db_module, "SHARED_RENAME_MIN_VERSION", "3.0.0")
    assert db_module.record_instance(force=True)  # това копие се е отчело


def test_shared_db_rename_moves_the_family_and_leaves_a_note(db_module, monkeypatch):
    _allow_rename(db_module, monkeypatch)
    old = db_module.DB_PATH
    con = db_module.get_db()
    con.execute("INSERT INTO clients (name) VALUES ('Клиент на сървъра')")
    con.commit()
    con.close()
    open(old + "-shm", "wb").close()  # празен придружаващ файл — мести се заедно
    new = db_module.rename_shared_database()
    folder = os.path.dirname(old)
    assert new == os.path.join(folder, "ph_logistics.db") == db_module.DB_PATH
    assert not [n for n in os.listdir(folder) if n.startswith("pacho_logistic.db")]
    assert os.path.exists(new + "-shm")
    note = json.loads(_read(os.path.join(folder, lm.SHARED_RENAME_MARKER)))
    assert note["from"] == "pacho_logistic.db" and note["to"] == "ph_logistics.db"
    assert appconfig.load_config() == dict(appconfig.DEFAULTS, db_path=new, network_port=5003)
    con = db_module.get_db()
    try:
        assert con.execute("SELECT name FROM clients").fetchone()[0] == "Клиент на сървъра"
    finally:
        con.close()


def test_shared_db_rename_refuses_when_in_use_or_unfinished(db_module, monkeypatch):
    _allow_rename(db_module, monkeypatch)
    old = db_module.DB_PATH
    monkeypatch.setattr(db_module, "_RENAME_LOCK_TIMEOUT", 0.2)
    reader = sqlite3.connect(old)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM users").fetchone()  # SHARED катинар
    try:
        with pytest.raises(db_module.TranslatableError) as err:
            db_module.rename_shared_database()
        assert "заета" in err.value.message_bg
    finally:
        reader.rollback()
        reader.close()
    # Незавършен журнал, появил се веднага след проверката с BEGIN EXCLUSIVE
    # (друг компютър е започнал запис) — нищо не се пипа.
    real_sqlite = db_module.sqlite3

    class _Shim(object):
        Error = real_sqlite.Error
        Row = real_sqlite.Row
        OperationalError = real_sqlite.OperationalError

        def connect(self, path, timeout=5.0):
            con = real_sqlite.connect(path, timeout=timeout)
            if timeout != 0.2:
                return con

            class _Con(object):
                def execute(self, *args):
                    return con.execute(*args)

                def rollback(self):
                    con.rollback()

                def close(self):
                    con.close()
                    _write(old + "-journal", "чужда транзакция")
            return _Con()
    monkeypatch.setattr(db_module, "sqlite3", _Shim())
    with pytest.raises(db_module.TranslatableError, match="журнал"):
        db_module.rename_shared_database()
    monkeypatch.setattr(db_module, "sqlite3", real_sqlite)
    os.remove(old + "-journal")
    assert db_module.DB_PATH == old and os.path.exists(old)
    assert not os.path.exists(os.path.join(os.path.dirname(old), "ph_logistics.db"))


def test_shared_db_rename_failure_changes_nothing(db_module, monkeypatch):
    _allow_rename(db_module, monkeypatch)
    old = db_module.DB_PATH
    open(old + "-shm", "wb").close()
    real = os.replace

    def flaky(src, dst):
        if src.endswith("-shm"):
            raise PermissionError(32, "файлът е отворен от друг процес")
        return real(src, dst)
    monkeypatch.setattr(lm.os, "replace", flaky)
    with pytest.raises(db_module.TranslatableError, match="нищо не е променено"):
        db_module.rename_shared_database()
    assert os.path.exists(old) and os.path.exists(old + "-shm")
    assert not os.path.exists(os.path.join(os.path.dirname(old), "ph_logistics.db"))
    assert db_module.DB_PATH == old
    assert appconfig.load_config()["db_path"] == old
    assert not os.path.exists(os.path.join(os.path.dirname(old), lm.SHARED_RENAME_MARKER))


def test_shared_db_rename_blocked_by_old_versions(db_module, monkeypatch):
    appconfig.save_config({"db_path": db_module.DB_PATH})
    _set_instances(db_module, [("СКЛАД-2", "3.79.0", 3)])
    with pytest.raises(db_module.TranslatableError, match="не е разрешено"):
        db_module.rename_shared_database()


def test_system_page_lists_instances_and_the_rename_action(admin_client, db_module, monkeypatch):
    page = admin_client.get("/admin/system").get_data(as_text=True)
    assert "Компютри и версии" in page
    assert "Преименувай споделената база" not in page, "без изричен db_path — не се показва"
    appconfig.save_config({"db_path": db_module.DB_PATH})
    _set_instances(db_module, [("СКЛАД-2", "3.79.0", 3), ("ОФИС", "3.80.0", 0)])
    monkeypatch.setattr(db_module, "record_instance", lambda **kw: False)
    page = admin_client.get("/admin/system").get_data(as_text=True)
    assert "Преименувай споделената база на новото име" in page
    assert "Първо обновете тези компютри до 3.80 или по-нова: СКЛАД-2" in page
    assert "Компютри с версия под 3.80 през последните 30 дни: 1" in page
    assert "Няма компютър с версия под 3.79" in page
    assert 'name="confirm_all_updated" style="width:auto;display:inline-block" disabled' in page


def test_shared_rename_route(admin_client, employee_client, db_module, monkeypatch):
    import applog
    _allow_rename(db_module, monkeypatch)
    old = db_module.DB_PATH
    audit = []
    monkeypatch.setattr(applog, "log_audit", lambda action, detail="": audit.append(action))
    seen = {}
    real = db_module.rename_shared_database

    def spy():
        import flask
        seen["db_open"] = "db" in flask.g  # връзката на заявката е затворена
        return real()
    monkeypatch.setattr(db_module, "rename_shared_database", spy)
    post_with_csrf(employee_client, "/admin/system/shared-rename", {"confirm_all_updated": "on"})
    assert seen == {} and os.path.exists(old), "само администратор"
    resp = post_with_csrf(admin_client, "/admin/system/shared-rename", {},
                          csrf_source_url="/admin/system", follow_redirects=True)
    assert "Отбележете, че всички компютри са обновени" in resp.get_data(as_text=True)
    assert seen == {} and os.path.exists(old)
    resp = post_with_csrf(admin_client, "/admin/system/shared-rename",
                          {"confirm_all_updated": "on"}, csrf_source_url="/admin/system",
                          follow_redirects=True)
    assert seen == {"db_open": False}
    assert "Споделената база вече е" in resp.get_data(as_text=True)
    assert db_module.DB_PATH.endswith("ph_logistics.db") and not os.path.exists(old)
    assert "преименувана споделената база" in audit


# ---------------------------------------------------------------- 4. остатъци на машината

def _age(path, days):
    stamp = time.time() - days * 86400
    for root, dirs, files in os.walk(path, topdown=False):
        for name in dirs + files:
            os.utime(os.path.join(root, name), (stamp, stamp))
    os.utime(path, (stamp, stamp))


def test_cleanup_of_legacy_leftovers(tmp_path):
    pd, local = tmp_path / "ProgramData", tmp_path / "Local"
    locks = pd / "PachoLogistic"
    locks.mkdir(parents=True)
    (locks / "pacho_logistic_abc.lock").write_bytes(b" pid=1")
    profile = local / "PachoLogistic"
    (profile / "webview" / "Default").mkdir(parents=True)
    (profile / "webview" / "Default" / "Cookies").write_bytes(b"c")
    # новата потребителска папка още я няма — старата остава
    assert lm.cleanup_legacy_leftovers(str(local), str(pd)) == [str(locks)]
    assert profile.exists()
    (local / "PHLogistics").mkdir()
    assert lm.cleanup_legacy_leftovers(str(local), str(pd)) == [], "пипана наскоро — остава"
    _age(str(profile), 40)
    assert lm.cleanup_legacy_leftovers(str(local), str(pd)) == [str(profile)]
    assert not profile.exists()
    (profile / "notes").mkdir(parents=True)  # непознато съдържание — не се пипа
    _age(str(profile), 40)
    assert lm.cleanup_legacy_leftovers(str(local), str(pd)) == []
    assert (profile / "notes").exists()
    locks.mkdir()
    (locks / "other.txt").write_bytes(b"x")  # не е катинар — папката остава
    assert lm.cleanup_legacy_leftovers(str(local), "") == []
    assert lm.cleanup_legacy_leftovers("", str(pd)) == []
    assert (locks / "other.txt").exists()


# ---------------------------------------------------------------- 5. релийз и CI

def test_release_keeps_alias_assets_and_gates_on_portable_and_shared():
    src = read_source(".github", "workflows", "release.yml")
    smoke = src[src.index("- name: Smoke test the exe"):src.index("- name: Install Inno Setup")]
    assert 'PH_DISABLE_AUTO_UPDATE: "1"' in smoke
    publish = src.index("- name: Publish release")
    for step in ("-Scenario portable", "-Scenario shared"):
        assert src.index(step) < publish, step
    alias = src[src.index("- name: Legacy download names"):src.index("- name: Generate SHA256SUMS")]
    assert "под 3.79" in alias and "30 дни" in alias
    assert "Copy-Item dist\\PHLogistics.exe dist\\PachoLogistic.exe" in alias
    ps1 = read_source("scripts", "ci_rename_migration_test.ps1")
    for text in ('ValidateSet("setup", "cli", "portable", "shared")', "New-SmbShare",
                 "Remove-SmbShare -Name $ShareName -Force", "check-portable", "check-shared",
                 "$env:PH_DISABLE_AUTO_UPDATE", "$env:PACHO_DISABLE_AUTO_UPDATE",
                 "LanmanServer\\Shares"):
        assert text in ps1, text
    assert ps1.index("function Remove-TestShare") < ps1.index("function Fail")
    fail = ps1[ps1.index("function Fail"):]
    assert fail.index("Remove-TestShare") < fail.index("exit 1")


@pytest.mark.parametrize("shared", [False, True])
def test_ci_fixture_portable_and_shared_checks_against_the_real_rename(tmp_path, shared):
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    try:
        import ci_rename_fixture as fx
    finally:
        sys.path.pop(0)
    folder = tmp_path / "phl_portable"
    folder.mkdir()
    fx.create_portable(str(folder))
    with open(str(folder / "PachoLogistic.exe"), "wb") as fh:
        fh.write(b"MZ")
    assert fx.check_portable(str(folder)) == 1 and fx.check_shared(str(folder)) == 1, \
        "без отчет нито една проверка не минава"
    report = _run_portable(str(folder), shares=[str(folder)] if shared else [])
    assert report["status"] == ("kept" if shared else "renamed")
    if shared:
        assert fx.check_shared(str(folder)) == 0 and fx.check_portable(str(folder)) == 1
    else:
        assert fx.check_portable(str(folder)) == 0 and fx.check_shared(str(folder)) == 1

