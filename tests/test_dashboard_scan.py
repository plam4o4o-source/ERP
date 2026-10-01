# -*- coding: utf-8 -*-
"""Одит (01.10.2026, U2): полето за сканиране на таблото. Номер като
„0001/2026“ съществува във всеки тип документ — при няколко съвпадения
операторът избира, вместо да се отвори тихо най-новият от който и да е тип."""
import json
import re
from datetime import date

from conftest import post_with_csrf


def _insert(db_module, doc_type, number, barcode, client="Клиент"):
    con = db_module.get_db()
    cur = con.execute(
        "INSERT INTO documents (doc_type, number, year, seq, barcode, public_token, data, created_by)"
        " VALUES (?, ?, ?, 1, ?, ?, ?, 1)",
        (doc_type, number, date.today().year, barcode, "t-" + barcode,
         json.dumps({"consignee_name": client}, ensure_ascii=False)))
    con.commit()
    doc_id = cur.lastrowid
    con.close()
    return doc_id


def _scan(admin_client, code):
    return post_with_csrf(admin_client, "/scan", {"code": code})


def test_number_shared_by_several_types_shows_a_chooser(admin_client, db_module):
    year = date.today().year
    number = "0001/%d" % year
    cmr = _insert(db_module, "cmr", number, "CMR-01012026-0001", "ЧМР Клиент")
    pallet = _insert(db_module, "pallet", number, "PAL-01012026-0001", "Палетен Клиент")
    invoice = _insert(db_module, "invoice_br", number, "INVBR-01012026-0001", "Фактурен Клиент")
    resp = _scan(admin_client, number)
    assert resp.status_code == 200, resp.headers.get("Location")
    body = resp.get_data(as_text=True)
    links = set(int(x) for x in re.findall(r'href="/doc/(\d+)"', body))
    assert links == {cmr, pallet, invoice}
    for text in ("ЧМР Клиент", "Палетен Клиент", "Фактурен Клиент", "ЧМР товарителница",
                 "Палетна карта", "Фактура за Бразилия"):
        assert text in body, text


def test_unique_barcode_and_unique_number_open_directly(admin_client, db_module):
    year = date.today().year
    cmr = _insert(db_module, "cmr", "0001/%d" % year, "CMR-01012026-0001")
    _insert(db_module, "pallet", "0001/%d" % year, "PAL-01012026-0001")
    resp = _scan(admin_client, "CMR-01012026-0001")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/doc/%d" % cmr)
    only = _insert(db_module, "waybill", "0007/%d" % year, "TOV-01012026-0007")
    resp = _scan(admin_client, "0007/%d" % year)
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/doc/%d" % only)


def test_short_number_is_accepted_like_the_pallet_lookup(admin_client, db_module):
    year = date.today().year
    doc = _insert(db_module, "waybill", "0042/%d" % year, "TOV-01012026-0042")
    for code in ("42/%d" % year, "42"):
        resp = _scan(admin_client, code)
        assert resp.status_code == 302, code
        assert resp.headers["Location"].endswith("/doc/%d" % doc), code


def test_unknown_code_still_reports_not_found(admin_client, db_module):
    resp = _scan(admin_client, "NOPE-1")
    assert resp.status_code == 302
    body = admin_client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "Няма документ с баркод" in body


def test_scan_placeholder_matches_the_real_barcode_format(admin_client):
    body = admin_client.get("/").get_data(as_text=True)
    placeholder = re.search(r'id="scan-input"[^>]*placeholder="([^"]+)"', body).group(1)
    today = date.today()
    assert "CMR-%02d%02d%d-0001" % (today.day, today.month, today.year) in placeholder
    assert re.search(r"[A-Z]+-\d{8}-\d{4}", placeholder)
