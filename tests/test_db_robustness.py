# -*- coding: utf-8 -*-
"""db.py при споделена база, заключване, разминат часовник, пълен диск и
временно нечетим .secret_key (одит 01.10.2026: O2, O5, O7, O9, R6)."""
import builtins
import os
import sqlite3
import time
from datetime import date, datetime, timedelta

import pytest


def _mode(path):
    con = sqlite3.connect(path)
    try:
        return con.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        con.close()


def _as_client(db_module, monkeypatch, local):
    """Като друг компютър: локален (WAL позволен) или през мрежов път."""
    monkeypatch.setattr(db_module, "_USE_WAL", local)
    getattr(db_module, "_journal_settled", set()).discard(db_module.DB_PATH)
    con = db_module.get_db()
    con.execute("SELECT 1 FROM settings").fetchall()
    con.close()


# ---------------------------------------------------------------- O2: WAL флип-флоп

def test_new_local_database_uses_wal(db_module):
    assert _mode(db_module.DB_PATH) == "wal"


def test_network_client_marks_the_db_shared_and_local_does_not_flip_it_back(
        db_module, monkeypatch):
    """Репро mixed_wal.py: сървърът (локален път) включваше WAL, станциите
    (UNC) — DELETE, при всяка връзка; файлът свършваше в WAL на мрежов дял."""
    _as_client(db_module, monkeypatch, local=False)
    assert _mode(db_module.DB_PATH) == "delete"
    for _ in range(3):
        _as_client(db_module, monkeypatch, local=True)
        assert _mode(db_module.DB_PATH) == "delete"
    con = sqlite3.connect(db_module.DB_PATH)
    assert con.execute("SELECT value FROM settings WHERE key = 'shared_db'").fetchone()
    con.close()


def test_existing_delete_mode_file_is_never_switched_into_wal(db_module, monkeypatch):
    con = sqlite3.connect(db_module.DB_PATH)
    con.execute("PRAGMA journal_mode = DELETE")
    con.close()
    _as_client(db_module, monkeypatch, local=True)
    assert _mode(db_module.DB_PATH) == "delete"


def test_journal_mode_is_not_touched_on_every_connection(db_module, monkeypatch):
    statements = []
    real_connect = sqlite3.connect

    def tracing_connect(*args, **kwargs):
        con = real_connect(*args, **kwargs)
        con.set_trace_callback(statements.append)
        return con

    monkeypatch.setattr(db_module.sqlite3, "connect", tracing_connect)
    for _ in range(5):
        db_module.get_db().close()
    assert not [s for s in statements if "journal_mode" in s.lower()]


# ---------------------------------------------------------------- O5: ограничено чакане

def test_next_number_gives_up_within_the_total_budget(db_module, monkeypatch):
    """Репро lockhold.py: друг компютър държи писателския катинар — операторът
    чакаше 122 с (8 опита × 15 с busy_timeout)."""
    monkeypatch.setattr(db_module, "_NUMBER_BUSY_BUDGET_SECONDS", 1.5, raising=False)
    holder = sqlite3.connect(db_module.DB_PATH)
    holder.execute("BEGIN IMMEDIATE")
    con = db_module.get_db()
    try:
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="заета"):
            db_module.next_number(con, "cmr")
        assert time.monotonic() - started < 6
        assert not con.in_transaction
        assert con.execute("PRAGMA busy_timeout").fetchone()[0] == 15000
    finally:
        con.close()
        holder.rollback()
        holder.close()


# ---------------------------------------------------------------- O7: часовник

class _FakeDate(date):
    fixed = (2026, 12, 31)

    @classmethod
    def today(cls):
        return cls(*cls.fixed)


def test_next_number_refuses_a_year_older_than_the_newest_counter(db_module, monkeypatch):
    """Репро clock.py: изостанал компютър издаде 0004/2026 след 0001/2027,
    нулиран часовник — 0001/2020."""
    con = db_module.get_db()
    con.execute("INSERT INTO counters (doc_type, year, last) VALUES ('invoice_br', 2027, 1)")
    con.commit()
    monkeypatch.setattr(db_module, "date", _FakeDate)
    with pytest.raises(db_module.NumberingExhaustedError, match="Часовникът"):
        db_module.next_number(con, "invoice_br")
    assert not con.in_transaction
    assert con.execute("SELECT count(*) FROM counters WHERE year < 2027").fetchone()[0] == 0
    con.close()


def test_next_number_still_works_in_the_newest_year(db_module, monkeypatch):
    con = db_module.get_db()
    con.execute("INSERT INTO counters (doc_type, year, last) VALUES ('cmr', 2026, 4)")
    con.commit()
    monkeypatch.setattr(db_module, "date", _FakeDate)
    assert db_module.next_number(con, "cmr")[0] == "0005/2026"
    con.rollback()
    con.close()


def _insert_doc(con, created_at):
    con.execute(
        "INSERT INTO documents (doc_type, number, year, seq, barcode, data, created_at)"
        " VALUES ('cmr', ?, 2026, 1, ?, '{}', ?)", (created_at, created_at, created_at))
    con.commit()


def test_clock_skew_warning(db_module):
    now = datetime(2026, 10, 1, 12, 0, 0)
    con = db_module.get_db()
    assert db_module.clock_skew_warning(con, now=now) is None
    _insert_doc(con, (now + timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S"))
    assert db_module.clock_skew_warning(con, now=now) is None
    _insert_doc(con, (now + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S"))
    warning = db_module.clock_skew_warning(con, now=now)
    assert warning["behind_seconds"] == 600
    assert warning["newest"] == "2026-10-01 12:10:00"
    con.close()


# ---------------------------------------------------------------- O9: пълен диск

def test_disk_full_errors_are_recognised(db_module, monkeypatch):
    import shutil
    usage = shutil.disk_usage(os.path.dirname(db_module.DB_PATH))
    assert db_module.is_disk_full_error(sqlite3.OperationalError("database or disk is full"))
    io_error = sqlite3.OperationalError("disk I/O error")  # WAL: SQLITE_IOERR_SHMSIZE
    assert not db_module.is_disk_full_error(io_error)
    monkeypatch.setattr(shutil, "disk_usage", lambda p: usage._replace(free=4096))
    assert db_module.is_disk_full_error(io_error)
    assert not db_module.is_disk_full_error(sqlite3.OperationalError("database is locked"))
    assert not db_module.is_disk_full_error(RuntimeError("disk is full"))


def test_disk_space_warning_below_twice_the_db_size(db_module, monkeypatch):
    import shutil
    usage = shutil.disk_usage(os.path.dirname(db_module.DB_PATH))
    assert db_module.disk_space_warning() is None
    size = os.path.getsize(db_module.DB_PATH)
    monkeypatch.setattr(shutil, "disk_usage", lambda p: usage._replace(free=size))
    warning = db_module.disk_space_warning()
    assert warning["free"] == size and warning["needed"] >= 2 * size


def test_backup_panel_shows_the_disk_space_warning(admin_client, db_module, monkeypatch):
    monkeypatch.setattr(db_module, "disk_space_warning",
                        lambda: {"free": 1_000_000, "needed": 8_000_000})
    body = admin_client.get("/admin/system").data.decode()
    assert "Малко свободно място на диска с базата данни" in body


# ---------------------------------------------------------------- R6: .secret_key

def test_transient_read_error_does_not_replace_the_secret_key(db_module, monkeypatch):
    """Регресия от v3.75.0 (scratchpad/regr/test_secret.py): всяка OSError при
    четене (заключен файл под Windows, SMB) ставаше „празен ключ“ и след
    5×50 ms файлът се презаписваше с НОВ ключ — всички излизаха от системата."""
    key1 = db_module.get_secret_key()
    real_open = builtins.open
    calls = {"n": 0}

    def flaky_open(path, *a, **k):
        if os.path.abspath(str(path)) == os.path.abspath(db_module.SECRET_PATH) and calls["n"] < 5:
            calls["n"] += 1
            raise PermissionError(13, "being used by another process")
        return real_open(path, *a, **k)

    monkeypatch.setattr(builtins, "open", flaky_open)
    key2 = db_module.get_secret_key()
    monkeypatch.setattr(builtins, "open", real_open)
    on_disk = real_open(db_module.SECRET_PATH, encoding="utf-8").read().strip()
    assert on_disk == key1
    assert key2 == key1


def test_permanently_unreadable_key_gives_temporary_key_without_overwriting(
        db_module, monkeypatch):
    key1 = db_module.get_secret_key()
    real_open = builtins.open

    def locked_open(path, *a, **k):
        if os.path.abspath(str(path)) == os.path.abspath(db_module.SECRET_PATH):
            raise PermissionError(13, "locked")
        return real_open(path, *a, **k)

    monkeypatch.setattr(builtins, "open", locked_open)
    monkeypatch.setattr(db_module.time, "sleep", lambda s: None)
    temp = db_module.get_secret_key()
    monkeypatch.setattr(builtins, "open", real_open)
    assert temp and temp != key1
    assert real_open(db_module.SECRET_PATH, encoding="utf-8").read().strip() == key1


def test_empty_secret_key_file_is_still_repaired(db_module, monkeypatch):
    with open(db_module.SECRET_PATH, "w", encoding="utf-8") as fh:
        fh.write("")
    monkeypatch.setattr(db_module.time, "sleep", lambda s: None)
    key = db_module.get_secret_key()
    assert len(key) >= 32
    assert open(db_module.SECRET_PATH, encoding="utf-8").read().strip() == key
