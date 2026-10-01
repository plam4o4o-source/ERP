# -*- coding: utf-8 -*-
"""Възстановяване на база от архив от самата програма (одит 01.10.2026, O1).

Ръчното „копирай архива върху базата“ след срив оставя стария -wal до копието
и SQLite го прилага върху него — възстановяването тихо не става или базата се
поврежда. Възстановяването вече се насрочва от „Система“ и се изпълнява при
старт, преди базата да бъде отворена."""
import os
import sqlite3
import subprocess
import sys

import pytest

import backup
from conftest import post_with_csrf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _count_clients(path):
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT count(*) FROM clients").fetchone()[0]
    finally:
        con.close()


def _add_clients(db_module, n, prefix):
    con = db_module.get_db()
    try:
        for i in range(n):
            con.execute("INSERT INTO clients (name) VALUES (?)", ("%s-%d" % (prefix, i),))
        con.commit()
    finally:
        con.close()


_CRASHING_WRITER = r'''
import sqlite3, sys, time
con = sqlite3.connect(sys.argv[1])
con.execute("PRAGMA wal_autocheckpoint = 0")
for i in range(int(sys.argv[2])):
    con.execute("INSERT INTO clients (name, address) VALUES (?, ?)", ("after-%d" % i, "z" * 3000))
    con.commit()
con.execute("UPDATE settings SET value = 'CHANGED AFTER BACKUP' WHERE key = 'sender_name'")
con.commit()
print("DONE", flush=True)
time.sleep(60)
'''


def _crash_after_writes(db_path, n):
    """Друг процес пише след архива и „умира“ (kill -9): -wal/-shm остават."""
    proc = subprocess.Popen([sys.executable, "-c", _CRASHING_WRITER, db_path, str(n)],
                            stdout=subprocess.PIPE)
    try:
        assert proc.stdout.readline().strip() == b"DONE"
    finally:
        proc.kill()  # SIGKILL на POSIX, TerminateProcess на Windows — без почистване
        proc.wait()
    assert os.path.exists(db_path + "-wal")


@pytest.mark.parametrize("after", [5, 300])
def test_restore_after_crash_ignores_the_stale_wal(db_module, tmp_path, after):
    """Репро restore_stale_wal.py: при 5 документа след архива ръчното
    възстановяване тихо не ставаше, при 300 — „database disk image is
    malformed“. Тук: старият -wal се мести настрана заедно с базата."""
    _add_clients(db_module, 50, "before")
    folder = str(tmp_path / "bk")
    os.makedirs(folder)
    bpath = backup.local_backup(folder)
    _crash_after_writes(db_module.DB_PATH, after)

    backup.request_restore(folder, os.path.basename(bpath), "test")
    result = backup.apply_pending_restore()

    assert result["ok"], result
    con = sqlite3.connect(db_module.DB_PATH)
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert con.execute("SELECT count(*) FROM clients").fetchone()[0] == 50
        assert con.execute("SELECT value FROM settings WHERE key = 'sender_name'"
                           ).fetchone()[0] != "CHANGED AFTER BACKUP"
    finally:
        con.close()
    # Нищо не е изтрито: старата база и нейният -wal са в pre_restore_*.
    aside = result["aside"]
    assert os.path.basename(aside).startswith("pre_restore_")
    assert set(os.listdir(aside)) >= {os.path.basename(db_module.DB_PATH),
                                       os.path.basename(db_module.DB_PATH) + "-wal"}
    assert not os.path.exists(backup._restore_marker_path())
    assert os.path.exists(bpath), "архивът в папката не бива да се пипа"


def test_restore_refuses_corrupt_backup_and_keeps_current_db(db_module, tmp_path):
    _add_clients(db_module, 3, "now")
    folder = str(tmp_path / "bk")
    os.makedirs(folder)
    bpath = backup.local_backup(folder)
    backup.request_restore(folder, os.path.basename(bpath))
    with open(bpath, "r+b") as fh:  # архивът се поврежда след насрочването
        fh.seek(100)
        fh.write(b"\xff" * 4000)
    _add_clients(db_module, 2, "later")
    before = open(db_module.DB_PATH, "rb").read()

    result = backup.apply_pending_restore()

    assert result["ok"] is False and result["error"]
    assert open(db_module.DB_PATH, "rb").read() == before
    assert _count_clients(db_module.DB_PATH) == 5
    assert not os.path.exists(backup._restore_marker_path())
    assert not [n for n in os.listdir(os.path.dirname(db_module.DB_PATH))
                if n.startswith("pre_restore_")]


def test_restore_refuses_missing_backup(db_module, tmp_path):
    folder = str(tmp_path / "bk")
    os.makedirs(folder)
    bpath = backup.local_backup(folder)
    backup.request_restore(folder, os.path.basename(bpath))
    os.remove(bpath)
    result = backup.apply_pending_restore()
    assert result["ok"] is False and "липсва" in result["error"]
    assert _count_clients(db_module.DB_PATH) == 0


def test_request_restore_accepts_only_backup_names_in_the_folder(db_module, tmp_path):
    folder = str(tmp_path / "bk")
    os.makedirs(folder)
    for bad in ("../pacho_logistic.db", "x.db", ""):
        with pytest.raises(ValueError):
            backup.request_restore(folder, bad)
    assert backup.pending_restore() is None


def test_restore_unzips_files_without_deleting_existing_ones(db_module, tmp_path):
    base = os.path.dirname(db_module.DB_PATH)
    att = os.path.join(base, "attachments", "7")
    os.makedirs(att)
    with open(os.path.join(att, "old.pdf"), "wb") as fh:
        fh.write(b"%PDF-backup")
    with open(os.path.join(base, "company_logo.png"), "wb") as fh:
        fh.write(b"LOGO-IN-BACKUP")
    folder = str(tmp_path / "bk")
    os.makedirs(folder)
    bpath = backup.local_backup(folder)
    # След архива: файлът е изтрит, качен е нов, логото е сменено с .jpg.
    os.remove(os.path.join(att, "old.pdf"))
    with open(os.path.join(att, "new.pdf"), "wb") as fh:
        fh.write(b"%PDF-new")
    os.remove(os.path.join(base, "company_logo.png"))
    with open(os.path.join(base, "company_logo.jpg"), "wb") as fh:
        fh.write(b"NEW-LOGO")

    backup.request_restore(folder, os.path.basename(bpath))
    result = backup.apply_pending_restore()

    assert result["ok"] and result["files"] == 2
    assert open(os.path.join(att, "old.pdf"), "rb").read() == b"%PDF-backup"
    assert open(os.path.join(att, "new.pdf"), "rb").read() == b"%PDF-new"
    assert open(os.path.join(base, "company_logo.png"), "rb").read() == b"LOGO-IN-BACKUP"
    assert not os.path.exists(os.path.join(base, "company_logo.jpg"))
    assert open(os.path.join(result["aside"], "company_logo.jpg"), "rb").read() == b"NEW-LOGO"


def test_init_db_applies_a_pending_restore_before_opening(db_module, tmp_path):
    """Защитата в db.init_db — дори ако някой път стигне до базата без app.py."""
    _add_clients(db_module, 4, "a")
    folder = str(tmp_path / "bk")
    os.makedirs(folder)
    bpath = backup.local_backup(folder)
    _add_clients(db_module, 6, "b")
    backup.request_restore(folder, os.path.basename(bpath))
    db_module.init_db()
    assert _count_clients(db_module.DB_PATH) == 4


def test_app_applies_restore_before_creating_the_app():
    src = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    assert src.index("backup.apply_pending_restore()") < src.index("app = _create_app_or_explain()")


# ---------------------------------------------------------------- през интерфейса

def _set_folder(db_module, folder):
    con = db_module.get_db()
    db_module.save_settings(con, {"backup_folder": folder})
    con.commit()
    con.close()


def test_system_page_lists_backups_with_restore_buttons(admin_client, db_module, tmp_path):
    folder = str(tmp_path / "bk")
    os.makedirs(folder)
    _set_folder(db_module, folder)
    first = backup.local_backup(folder)
    second = backup.local_backup(folder)
    body = admin_client.get("/admin/system").data.decode()
    assert body.count('name="backup_name"') == 2
    assert os.path.basename(first) in body and os.path.basename(second) in body
    assert 'action="/admin/system/restore"' in body


def test_list_backups_newest_first_and_limited(tmp_path):
    folder = str(tmp_path)
    for day in range(1, 26):
        open(os.path.join(folder, "pacho_logistic_202609%02d_120000.db" % day), "wb").close()
    open(os.path.join(folder, "other.db"), "wb").close()
    items = backup.list_backups(folder)
    assert len(items) == 20
    assert items[0]["name"] == "pacho_logistic_20260925_120000.db"
    assert items[-1]["name"] == "pacho_logistic_20260906_120000.db"


def test_restore_request_route_writes_marker_and_cancel_removes_it(admin_client, db_module,
                                                                   tmp_path):
    folder = str(tmp_path / "bk")
    os.makedirs(folder)
    _set_folder(db_module, folder)
    bpath = backup.local_backup(folder)
    resp = post_with_csrf(admin_client, "/admin/system/restore",
                          {"backup_name": os.path.basename(bpath)},
                          csrf_source_url="/admin/system", follow_redirects=True)
    assert "ВСИЧКИ компютри" in resp.data.decode()
    assert backup.pending_restore()["backup"] == os.path.abspath(bpath)
    post_with_csrf(admin_client, "/admin/system/restore/cancel", {},
                   csrf_source_url="/admin/system")
    assert backup.pending_restore() is None


def test_restore_request_route_rejects_foreign_paths(admin_client, db_module, tmp_path):
    folder = str(tmp_path / "bk")
    os.makedirs(folder)
    _set_folder(db_module, folder)
    post_with_csrf(admin_client, "/admin/system/restore",
                   {"backup_name": "../" + os.path.basename(db_module.DB_PATH)},
                   csrf_source_url="/admin/system")
    assert backup.pending_restore() is None


def test_employee_cannot_request_restore(employee_client, db_module, tmp_path):
    resp = post_with_csrf(employee_client, "/admin/system/restore", {"backup_name": "x"},
                          csrf_source_url="/")
    assert resp.status_code == 403


def test_restore_result_is_shown_to_the_admin_once(admin_client, db_module, tmp_path):
    import routes_admin
    folder = str(tmp_path / "bk")
    os.makedirs(folder)
    bpath = backup.local_backup(folder)
    backup.request_restore(folder, os.path.basename(bpath))
    backup.apply_pending_restore()
    routes_admin._restore_result_checked.discard(db_module.DB_PATH)
    first = admin_client.get("/my-settings").data.decode()
    assert "Базата е възстановена от архива" in first
    second = admin_client.get("/my-settings").data.decode()
    assert "Базата е възстановена от архива" not in second


# ---------------------------------------------------------------- R2: лого с версия в адреса

def test_logo_url_changes_when_a_new_logo_is_uploaded(flask_app, db_module):
    """Одит (01.10.2026, R2): /logo.img се кешираше 5 мин. при НЕПРОМЕНЕН
    адрес — след ново лого старото се показваше и печаташе."""
    from flask import url_for
    base = os.path.dirname(db_module.DB_PATH)
    path = os.path.join(base, "company_logo.png")
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\nA")
    with flask_app.test_request_context():
        first = url_for("company_logo_image")
    os.utime(path, ns=(1, 1_000_000_000))
    with flask_app.test_request_context():
        second = url_for("company_logo_image")
    assert first != second and "v=" in second
    client = flask_app.test_client()
    versioned = client.get(second)
    assert "max-age" in versioned.headers.get("Cache-Control", "")
    plain = client.get("/logo.img")
    assert "max-age" not in plain.headers.get("Cache-Control", "")
    assert "no-cache" in plain.headers.get("Cache-Control", "")
