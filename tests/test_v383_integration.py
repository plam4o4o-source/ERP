# -*- coding: utf-8 -*-
"""Заявка (06.11.2026, интеграция v3.83): връзки между групите — колоната
„VIES“ в Intrastat (CUSTOMS) чете кеша на проверките във VIES (PARTIES), без
да вика услугата."""
import customs
import vies


def test_intrastat_vies_column_reads_the_cached_check(flask_app, db_module):
    con = db_module.get_db()
    con.execute("INSERT INTO clients (name, vies_vat, vies_valid, vies_checked_at)"
                " VALUES ('Rossi SpA', 'IT01234567890', 1, '2026-11-02 10:15:00')")
    con.commit()
    assert customs.VIES_STATUS_HOOK is vies.cached_status_text
    assert customs._vies(con, "IT 01234567890") == "валиден (02.11.2026)"
    # Непроверен номер — празно, без мрежа.
    assert customs._vies(con, "DE123456789") == ""
    assert customs._vies(con, "") == ""


def test_vies_cache_prefers_the_latest_check(flask_app, db_module):
    con = db_module.get_db()
    con.execute("INSERT INTO clients (name, vies_vat, vies_valid, vies_checked_at)"
                " VALUES ('A', 'DE123456789', 1, '2026-01-01 08:00:00')")
    con.execute("INSERT INTO carriers (name, vies_vat, vies_valid, vies_checked_at)"
                " VALUES ('B', 'DE123456789', 0, '2026-10-01 08:00:00')")
    con.commit()
    assert vies.cached_status_text(con, "DE123456789") == "невалиден (01.10.2026)"


def test_vies_cache_never_calls_the_service(flask_app, db_module, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("мрежа")
    monkeypatch.setattr(vies, "_http_post_json", boom)
    con = db_module.get_db()
    assert vies.cached_status_text(con, "BG123456789") == ""
