# -*- coding: utf-8 -*-
"""Архивиране под натоварване, видими провали и остатъци (одит 01.10.2026:
O3, O4, O6, O10, O11, P12)."""
import io
import os
import sqlite3
import time
import zipfile

import pytest

import backup
from conftest import post_with_csrf


def _fill(db_module, rows=3000):
    con = sqlite3.connect(db_module.DB_PATH)
    con.executemany("INSERT INTO clients (name, address) VALUES (?, ?)",
                    [("c%d" % i, "x" * 1000) for i in range(rows)])
    con.commit()
    con.close()


def _settings_func(db_module, folder):
    def get():
        con = db_module.get_db()
        try:
            s = db_module.get_settings(con)
        finally:
            con.close()
        s.update(backup_folder=folder, backup_auto="on")
        return s
    return get


# ---------------------------------------------------------------- O3: оживена база

class _ConnWithForeignWrites:
    """Връзка към живата база, при която ДРУГА връзка пише между стъпките на
    копирането (детерминиран вариант на репро bk_slow.py)."""

    def __init__(self, con, write):
        self._con, self._write = con, write

    def __getattr__(self, name):
        return getattr(self._con, name)

    def set_progress_handler(self, handler, n):
        def wrapped():
            self._write()
            return handler()
        return self._con.set_progress_handler(wrapped, n)

    def backup(self, target, pages=-1, progress=None, sleep=0.25):
        def wrapped(status, remaining, total):
            self._write()
            if progress:
                progress(status, remaining, total)
        return self._con.backup(target, pages=pages, progress=wrapped, sleep=sleep)


def test_backup_finishes_while_another_connection_keeps_writing(db_module, tmp_path,
                                                                monkeypatch):
    """Репро bk_slow.py: backup() с pages=100 почваше отначало при всеки чужд
    запис и при оживена база удряше тавана — архив не се получаваше никога."""
    _fill(db_module)
    monkeypatch.setattr(backup, "_BACKUP_MAX_SECONDS", 4)
    real_connect = sqlite3.connect
    writer = real_connect(db_module.DB_PATH, timeout=15)
    writes = []

    def write():
        writer.execute("INSERT INTO clients (name) VALUES ('w')")
        writer.commit()
        writes.append(1)

    def connect(path, *args, **kwargs):
        con = real_connect(path, *args, **kwargs)
        return _ConnWithForeignWrites(con, write) if path == db_module.DB_PATH else con

    monkeypatch.setattr(backup.sqlite3, "connect", connect)
    try:
        path = backup.local_backup(str(tmp_path))
    finally:
        monkeypatch.setattr(backup.sqlite3, "connect", real_connect)
        writer.close()
    assert writes, "другата връзка трябва реално да е писала по време на архива"
    con = sqlite3.connect(path)
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert con.execute("SELECT count(*) FROM clients").fetchone()[0] >= 3000
        # Копието не носи WAL флага — отварянето му не оставя -wal/-shm.
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        con.close()


# ---------------------------------------------------------------- O4: видим провал

def test_failed_auto_backup_is_recorded_and_releases_the_slot(db_module, tmp_path,
                                                              monkeypatch):
    """Репро autobk_silent.py: провал на часовия архив беше само ред в лога, а
    заетият предварително ред спираше опитите на ВСИЧКИ компютри за час."""
    folder = str(tmp_path)
    get = _settings_func(db_module, folder)
    real = backup.local_backup

    def broken(dest):
        with backup._local_backup_lock:
            try:
                raise TimeoutError("симулиран провал")
            except Exception as exc:
                backup._record_result(error=exc)
                raise

    monkeypatch.setattr(backup, "local_backup", broken)
    with pytest.raises(TimeoutError):
        backup.run_scheduled_backup(get)
    monkeypatch.setattr(backup, "local_backup", real)

    con = db_module.get_db()
    status = backup.status(con)
    con.close()
    assert status["failing"] and "симулиран провал" in status["last_error"]
    # Друг компютър веднага след това — архивира, не чака час.
    assert backup.run_scheduled_backup(get) is not None
    con = db_module.get_db()
    status = backup.status(con)
    con.close()
    assert status["last_ok_at"] and not status["failing"]


def test_manual_backup_failure_is_recorded(db_module, tmp_path):
    with pytest.raises(RuntimeError):
        backup.local_backup(str(tmp_path / "липсва"))
    con = db_module.get_db()
    status = backup.status(con)
    con.close()
    assert status["failing"] and "не съществува" in status["last_error"]


def test_backup_panel_shows_last_success_last_error_and_disabled_button(
        admin_client, db_module, tmp_path):
    body = admin_client.get("/admin/system").data.decode()
    assert "Последен успешен архив:" in body
    import re
    button = re.search(r'<button type="submit" class="btn-secondary"[^>]*>[^<]*<svg[^>]*>.*?'
                       r'Архивирай сега', body, re.S)
    assert button and "disabled" in button.group(0)
    con = db_module.get_db()
    db_module.save_settings(con, {"backup_folder": str(tmp_path)})
    con.commit()
    con.close()
    backup.local_backup(str(tmp_path))
    with pytest.raises(RuntimeError):
        backup.local_backup(str(tmp_path / "липсва"))
    body = admin_client.get("/admin/system").data.decode()
    assert "Последна грешка при архивиране" in body
    assert "Налични архиви" in body
    button = re.search(r'<button type="submit" class="btn-secondary"[^>]*>[^<]*<svg[^>]*>.*?'
                       r'Архивирай сега', body, re.S)
    assert "disabled" not in button.group(0)


def test_system_page_is_a_real_page_for_admins(admin_client, employee_client):
    """P12: /admin/system вече не пренасочва към личните настройки."""
    resp = admin_client.get("/admin/system")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Резервно копие" in body and "Тема на оформлението" not in body
    assert employee_client.get("/admin/system").status_code == 403


def test_system_form_returns_to_the_system_page(admin_client):
    resp = post_with_csrf(admin_client, "/admin/system", {"form": "login_scene",
                                                         "login_scene": "classic"},
                          csrf_source_url="/admin/system",
                          headers={"Referer": "http://localhost/admin/system"})
    assert resp.headers["Location"].endswith("/admin/system")


# ---------------------------------------------------------------- O6: файл, качен по време на архива

def test_attachment_uploaded_during_the_backup_is_in_the_files_zip(db_module, tmp_path,
                                                                   monkeypatch):
    base = os.path.dirname(db_module.DB_PATH)
    os.makedirs(os.path.join(base, "attachments", "1"))
    with open(os.path.join(base, "attachments", "1", "a.pdf"), "wb") as fh:
        fh.write(b"%PDF-a")
    real_snapshot = backup._snapshot_db

    def snapshot_with_upload(dst, deadline):
        # Качване, завършило точно преди снимката (редът му е в архива).
        with open(os.path.join(base, "attachments", "1", "b.pdf"), "wb") as fh:
            fh.write(b"%PDF-b")
        real_snapshot(dst, deadline)

    monkeypatch.setattr(backup, "_snapshot_db", snapshot_with_upload)
    path = backup.local_backup(str(tmp_path))
    with zipfile.ZipFile(backup.companion_path(path)) as zf:
        assert set(zf.namelist()) == {"attachments/1/a.pdf", "attachments/1/b.pdf"}


# ---------------------------------------------------------------- O10: остатъци

def test_rotation_removes_stale_partial_siblings(tmp_path):
    folder = str(tmp_path)
    names = ["pacho_logistic_20260101_010101_abcdef.db.partial-journal",
             "pacho_logistic_20260101_010101_abcdef.db.partial-wal",
             "pacho_logistic_20260101_010101_abcdef.db.partial-shm",
             "pacho_logistic_20260101_010101_abcdef.files.zip.partial"]
    old = time.time() - 7200
    for name in names + ["pacho_logistic_young.db.partial-journal", "notes.txt"]:
        path = os.path.join(folder, name)
        open(path, "wb").close()
        if name in names:
            os.utime(path, (old, old))
    backup._rotate_local_backups(folder)
    assert sorted(os.listdir(folder)) == ["notes.txt", "pacho_logistic_young.db.partial-journal"]


def test_attachment_write_failure_leaves_no_empty_file(db_module, monkeypatch):
    """Репро diskfull.py: пълен диск оставяше 0-байтов файл на мястото на
    прикачения."""
    import attachments
    con = db_module.get_db()
    con.execute("INSERT INTO documents (doc_type, number, year, seq, barcode, data)"
                " VALUES ('cmr', '1', 2026, 1, 'B1', '{}')")
    con.commit()
    real_open = open

    class _Full(io.RawIOBase):
        def __init__(self, path):
            self.path = path
            real_open(path, "wb").close()

        def writable(self):
            return True

        def write(self, data):
            raise OSError(28, "No space left on device")

        def fileno(self):
            raise OSError(28, "No space left on device")

    def full_open(path, mode="r", *a, **k):
        if "attachments" in str(path) and "w" in mode:
            return _Full(path)
        return real_open(path, mode, *a, **k)

    monkeypatch.setattr("builtins.open", full_open)
    upload = type("U", (), {"filename": "x.pdf", "read": lambda self: b"%PDF-1.4 data"})()
    with pytest.raises(OSError):
        attachments.save_attachment(con, 1, upload)
    monkeypatch.setattr("builtins.open", real_open)
    folder = os.path.join(os.path.dirname(db_module.DB_PATH), "attachments", "1")
    assert os.listdir(folder) == []
    con.close()


# ---------------------------------------------------------------- O11: папка без право на запис

@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root пише и в папка без права")
@pytest.mark.skipif(os.name == "nt",
                    reason="атрибутът „само за четене“ на папка не спира запис в нея на Windows")
def test_read_only_backup_folder_gives_a_clear_message(db_module, tmp_path):
    folder = tmp_path / "ro"
    folder.mkdir()
    os.chmod(str(folder), 0o500)
    try:
        with pytest.raises(RuntimeError, match="Няма право на запис"):
            backup.local_backup(str(folder))
    finally:
        os.chmod(str(folder), 0o700)


def test_unwritable_backup_folder_message(db_module, tmp_path, monkeypatch):
    def denied(*a, **k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(backup.tempfile, "mkstemp", denied)
    with pytest.raises(RuntimeError, match="Няма право на запис"):
        backup.local_backup(str(tmp_path))


# ---------------------------------------------------------------- U12: „Фирма изпращач“

def test_sender_settings_have_en_postcode_and_dubai_bank_hint(admin_client, db_module):
    body = admin_client.get("/settings").data.decode()
    assert 'name="sender_postcode_en"' in body
    assert "Дубай" in body
    post_with_csrf(admin_client, "/settings", {"sender_name": "X", "sender_postcode_en": "5334"},
                   csrf_source_url="/settings")
    con = db_module.get_db()
    assert db_module.get_settings(con).get("sender_postcode_en") == "5334"
    con.close()
