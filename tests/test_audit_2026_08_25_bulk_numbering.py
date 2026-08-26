# -*- coding: utf-8 -*-
"""Одит (25.08.2026, находка №6): изчерпаната номерация при ГРУПОВО издаване
на палетни карти вече показва конкретното си обяснение.

Единичното издаване (routes_documents) хваща db.NumberingExhaustedError и
показва точния му текст („първите 1000 поредни номера са заети…“). Груповото
издаване го хващаше с общия `except Exception` и показваше безполезното
„Възникна грешка… опитайте отново“ — повторният опит удря СЪЩАТА изчерпана
номерация. Това е класическата „непокрита половина“ от одита.
"""
import json

import db
from conftest import post_with_csrf


def test_bulk_pallet_issue_shows_specific_numbering_exhausted_message(
        admin_client, db_module, monkeypatch):
    monkeypatch.setattr(db, "_MAX_SEQ_SKIPS", 3)
    con = db_module.get_db()
    # Запълваме поредните номера на палетните карти, за да е изчерпан таванът.
    for n in range(1, 6):
        con.execute(
            "INSERT INTO documents (doc_type, number, year, seq, barcode, public_token,"
            " data, created_by) VALUES ('pallet', ?, 2026, ?, ?, ?, '{}', 1)",
            ("%04d/2026" % n, n, "B-pexh-%d" % n, "t-pexh-%d" % n))
    con.commit()
    before = con.execute(
        "SELECT COUNT(*) c FROM documents WHERE doc_type = 'pallet'").fetchone()["c"]
    con.close()

    items = [{"order_no": "1", "pos": "10", "reference": "R", "reference_desc": "стока", "qty": "5"}]
    resp = post_with_csrf(admin_client, "/pallet/bulk-issue", {
        "client_name": "Клиент",
        "groups": "g1",
        "items_format_g1": "orders",
        "items_json_g1": json.dumps(items, ensure_ascii=False),
        "gross_g1": "100",
    }, csrf_source_url="/pallet/new", follow_redirects=True)

    body = resp.get_data(as_text=True)
    # Конкретното обяснение на изключението, не общото „Възникна грешка“.
    assert "свободен номер" in body, "конкретното съобщение за изчерпана номерация трябва да се вижда"
    assert "Възникна грешка при масовото издаване" not in body, (
        "общото съобщение маскира конкретната причина")

    # All-or-nothing: нищо не бива да е записано.
    con = db_module.get_db()
    after = con.execute(
        "SELECT COUNT(*) c FROM documents WHERE doc_type = 'pallet'").fetchone()["c"]
    con.close()
    assert after == before, "при изчерпана номерация не бива да се запише нито една карта"
