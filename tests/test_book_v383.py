# -*- coding: utf-8 -*-
"""Заявка (06.11.2026, група BOOK, v3.83.0): единен адресник (връзка запис за
фактури → клиент, db._m017_address_book, address_book.py) и глобално търсене
(global_search.py, templates/search.html, static/book.js)."""
import json
import re
import sqlite3

import pytest

from conftest import post_with_csrf, read_source

_SEQ = {"n": 0}


def _add_doc(db_module, doc_type, data, number=None):
    _SEQ["n"] += 1
    n = _SEQ["n"]
    con = db_module.get_db()
    cur = con.execute(
        "INSERT INTO documents (doc_type, number, year, seq, barcode, public_token, data, created_by)"
        " VALUES (?, ?, 2026, ?, ?, ?, ?, 1)",
        (doc_type, number or "%04d/2026" % (900 + n), 900 + n, "BK-%06d" % n, "bk-%d" % n,
         json.dumps(data, ensure_ascii=False)))
    con.commit()
    doc_id = cur.lastrowid
    con.close()
    return doc_id


def _client(db_module, name, **fields):
    con = db_module.get_db()
    cols = ["name"] + list(fields)
    cur = con.execute("INSERT INTO clients (%s) VALUES (%s)" % (", ".join(cols), ",".join("?" * len(cols))),
                      [name] + list(fields.values()))
    con.commit()
    cid = cur.lastrowid
    con.close()
    return cid


def _entry(db_module, name, client_id=None, **fields):
    con = db_module.get_db()
    cols = ["name", "client_id"] + list(fields)
    cur = con.execute("INSERT INTO invoice_clients (%s) VALUES (%s)" % (", ".join(cols), ",".join("?" * len(cols))),
                      [name, client_id] + list(fields.values()))
    con.commit()
    eid = cur.lastrowid
    con.close()
    return eid


def _entry_row(db_module, entry_id):
    con = db_module.get_db()
    row = con.execute("SELECT * FROM invoice_clients WHERE id = ?", (entry_id,)).fetchone()
    con.close()
    return row


def _last_audit(db_module):
    import applog
    applog.flush_audit()
    con = db_module.get_db()
    row = con.execute("SELECT action, detail FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    con.close()
    return row


# ================================================================ миграция

def test_migration_adds_optional_client_link_without_touching_data(db_module):
    con = db_module.get_db()
    cols = {r[1]: r for r in con.execute("PRAGMA table_info(invoice_clients)")}
    assert "client_id" in cols and cols["client_id"][3] == 0  # NOT NULL = 0 → незадължителна
    idx = [r[1] for r in con.execute("PRAGMA index_list(invoice_clients)")]
    assert "idx_invoice_clients_client_id" in idx
    con.close()


def test_migration_on_old_database_keeps_entries(tmp_path):
    """База отпреди v3.83: таблицата без колоната и с данни — колоната се
    добавя, записите остават непокътнати и несвързани."""
    import db as dbm
    path = str(tmp_path / "old.db")
    raw = sqlite3.connect(path)
    raw.row_factory = sqlite3.Row
    raw.execute("CREATE TABLE invoice_clients (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,"
                " delivery_name TEXT NOT NULL DEFAULT '', billing_name TEXT NOT NULL DEFAULT '')")
    raw.execute("INSERT INTO invoice_clients (name, delivery_name) VALUES ('ABB BR', 'ABB Ltda')")
    dbm._m017_address_book(raw)
    dbm._m017_address_book(raw)  # идемпотентна
    row = raw.execute("SELECT * FROM invoice_clients").fetchone()
    assert dict(row) == {"id": 1, "name": "ABB BR", "delivery_name": "ABB Ltda", "billing_name": "",
                         "client_id": None}
    raw.close()


# ================================================================ връзка между книгите

def test_link_from_entry_page_and_both_pages_show_it(employee_client, db_module):
    cid = _client(db_module, "Müller & Söhne GmbH", city="München", country="DE")
    eid = _entry(db_module, "MULLER DE", billing_name="Müller & Söhne GmbH")
    page = employee_client.get("/invoices/clients/%d/edit" % eid).get_data(as_text=True)
    assert 'id="book-links"' in page and "Предложения (същото име)" in page
    assert "Müller &amp; Söhne GmbH" in page  # предложен по Bill To, без регистър/диакритика
    resp = post_with_csrf(employee_client, "/invoices/clients/%d/link" % eid, {"client_id": cid},
                          csrf_source_url="/invoices/clients/%d/edit" % eid)
    assert resp.status_code == 302 and resp.headers["Location"].endswith("#book-links")
    assert _entry_row(db_module, eid)["client_id"] == cid
    assert _last_audit(db_module)[0] == "свързан запис за фактури с клиент"
    entry_page = employee_client.get("/invoices/clients/%d/edit" % eid).get_data(as_text=True)
    assert "Свързан с клиента:" in entry_page and "/clients/%d/edit" % cid in entry_page
    client_page = employee_client.get("/clients/%d/edit" % cid).get_data(as_text=True)
    assert "Записи за фактури" in client_page and "/invoices/clients/%d/edit" % eid in client_page
    listing = employee_client.get("/invoices/clients").get_data(as_text=True)
    assert 'class="book-badge"' in listing and "↔ Müller &amp; Söhne GmbH" in listing


def test_unlink_keeps_both_records_unchanged(admin_client, db_module):
    cid = _client(db_module, "ABB Brasil Ltda", city="Sorocaba")
    eid = _entry(db_module, "ABB BR", cid, delivery_address="Rua 1\nSorocaba")
    before_entry = dict(_entry_row(db_module, eid))
    resp = post_with_csrf(admin_client, "/invoices/clients/%d/unlink" % eid, {"back": "client"},
                          csrf_source_url="/clients/%d/edit" % cid)
    assert resp.status_code == 302 and "/clients/%d/edit" % cid in resp.headers["Location"]
    after = dict(_entry_row(db_module, eid))
    assert after.pop("client_id") is None
    before_entry.pop("client_id")
    assert after == before_entry
    con = db_module.get_db()
    assert con.execute("SELECT name, city FROM clients WHERE id = ?", (cid,)).fetchone()[:] == (
        "ABB Brasil Ltda", "Sorocaba")
    con.close()
    assert _last_audit(db_module)[0] == "премахната връзка запис за фактури — клиент"


def test_link_from_client_page_searches_only_unlinked_entries(admin_client, db_module):
    cid = _client(db_module, "WEG Equipamentos")
    other = _client(db_module, "Друг клиент")
    free = _entry(db_module, "WEG BR", billing_name="WEG Equipamentos")
    taken = _entry(db_module, "WEG BR 2", other, billing_name="WEG Equipamentos")
    page = admin_client.get("/clients/%d/edit" % cid).get_data(as_text=True)
    sug = page.split("Предложения (същото име)")[1]
    assert "WEG BR</b>" in sug and "WEG BR 2</b>" not in sug  # чужда връзка не се „открадва“
    found = admin_client.get("/clients/%d/edit?book_q=weg+br" % cid).get_data(as_text=True)
    assert "Несвързани записи по търсенето" in found
    resp = post_with_csrf(admin_client, "/invoices/clients/%d/link" % free,
                          {"client_id": cid, "back": "client"}, csrf_source_url="/clients/%d/edit" % cid)
    assert "/clients/%d/edit#book-links" % cid in resp.headers["Location"]
    assert _entry_row(db_module, free)["client_id"] == cid
    assert _entry_row(db_module, taken)["client_id"] == other


def test_entry_page_search_finds_client_by_vat(admin_client, db_module):
    _client(db_module, "Hitachi Energy Norway AS", vat="NO123456789")
    eid = _entry(db_module, "HE NO")
    page = admin_client.get("/invoices/clients/%d/edit?link_q=no1234" % eid).get_data(as_text=True)
    assert "Клиенти по търсенето" in page and "Hitachi Energy Norway AS" in page
    none = admin_client.get("/invoices/clients/%d/edit?link_q=zzzz" % eid).get_data(as_text=True)
    assert "Няма намерени клиенти по това търсене." in none


def test_create_invoice_entry_from_client(employee_client, db_module):
    cid = _client(db_module, "São Paulo Energia S.A.", alias="SPE", address="Av. Paulista 1000",
                  postcode="01310-100", city="São Paulo", country="Brazil", vat="BR123", phone="+55 11 1234")
    resp = post_with_csrf(employee_client, "/clients/%d/invoice-entry" % cid, {},
                          csrf_source_url="/clients/%d/edit" % cid)
    assert resp.status_code == 302
    eid = int(re.search(r"/invoices/clients/(\d+)/edit", resp.headers["Location"]).group(1))
    row = _entry_row(db_module, eid)
    assert row["client_id"] == cid and row["name"] == "SPE"
    assert row["delivery_name"] == row["billing_name"] == "São Paulo Energia S.A."
    assert row["delivery_address"] == "Av. Paulista 1000\n01310-100 São Paulo\nBrazil"
    assert row["billing_address"] == "Av. Paulista 1000\n01310-100 São Paulo\nBrazil\nVAT No: BR123"
    assert row["delivery_phone"] == row["billing_phone"] == "+55 11 1234"
    assert _last_audit(db_module)[0] == "създаден запис за фактури от клиент"
    # Клиентът не е променен; второто натискане прави втори запис (нищо не се слива).
    post_with_csrf(employee_client, "/clients/%d/invoice-entry" % cid, {},
                   csrf_source_url="/clients/%d/edit" % cid)
    con = db_module.get_db()
    assert con.execute("SELECT COUNT(*) FROM invoice_clients WHERE client_id = ?", (cid,)).fetchone()[0] == 2
    con.close()


def test_saving_the_invoice_entry_keeps_the_link(admin_client, db_module):
    cid = _client(db_module, "EVN")
    eid = _entry(db_module, "EVN BG", cid)
    resp = post_with_csrf(admin_client, "/invoices/clients/%d/edit" % eid,
                          {"name": "EVN BG (нов)", "billing_name": "EVN Bulgaria"},
                          csrf_source_url="/invoices/clients/%d/edit" % eid)
    assert resp.status_code == 302
    row = _entry_row(db_module, eid)
    assert row["name"] == "EVN BG (нов)" and row["client_id"] == cid


def test_deleted_client_means_unlinked(admin_client, db_module):
    cid = _client(db_module, "Изтрит")
    eid = _entry(db_module, "X", cid)
    post_with_csrf(admin_client, "/clients/%d/delete" % cid, {"confirm_documents": "1"})
    page = admin_client.get("/invoices/clients/%d/edit" % eid).get_data(as_text=True)
    assert "Свързан с клиента:" not in page and "Търси клиент" in page


def test_link_errors(admin_client, db_module):
    eid = _entry(db_module, "E")
    assert post_with_csrf(admin_client, "/invoices/clients/999999/link", {"client_id": 1}).status_code == 404
    assert post_with_csrf(admin_client, "/clients/999999/invoice-entry", {}).status_code == 404
    resp = post_with_csrf(admin_client, "/invoices/clients/%d/link" % eid, {"client_id": "abc"},
                          follow_redirects=True)
    assert "Клиентът не е намерен в адресната книга." in resp.get_data(as_text=True)
    assert _entry_row(db_module, eid)["client_id"] is None


def test_new_pages_need_login(client, db_module):
    eid = _entry(db_module, "E")
    for url in ("/search?q=abc", "/search/live?q=abc", "/address-book/check?entry_id=%d" % eid):
        assert client.get(url).status_code == 302
    assert client.post("/invoices/clients/%d/link" % eid).status_code in (302, 400, 403)


# ================================================================ фактура: друг клиент

def test_check_mismatch_by_client_key_and_alias(admin_client, db_module):
    cid = _client(db_module, "Müller & Söhne GmbH", alias="MULLER")
    eid = _entry(db_module, "M", cid)
    free = _entry(db_module, "Без връзка")

    def check(entry, *names):
        q = "&".join("name=%s" % n for n in names)
        return admin_client.get("/address-book/check?entry_id=%s&%s" % (entry, q)).get_json()

    same = check(eid, "  MÜLLER &amp; SÖHNE GMBH ".replace("&amp;", "%26"))
    assert same["ok"] and same["mismatch"] is False
    assert check(eid, "muller")["mismatch"] is False  # псевдонимът
    other = check(eid, "ABB Brasil Ltda", "muller")
    assert other["mismatch"] is True and other["linked_name"] == "Müller & Söhne GmbH"
    assert other["shipment_names"] == ["ABB Brasil Ltda"]
    assert check(free, "ABB Brasil Ltda")["mismatch"] is False  # без връзка — не знаем
    assert check(eid)["mismatch"] is False                       # без пратка
    assert check("abc", "x")["mismatch"] is False


def test_pull_pallet_reports_the_shipment_client(admin_client, db_module):
    _add_doc(db_module, "pallet", {"client_name": "ABB Brasil Ltda", "items": [
        {"order_no": "4700201619", "pos": "10", "reference": "1VL1", "reference_desc": "X", "qty": "2"}]},
        number="0777/2026")
    resp = post_with_csrf(admin_client, "/invoice/pull-pallet", {"code": "0777/2026"},
                          csrf_source_url="/invoice-br/new")
    data = resp.get_json()
    assert data["ok"] and data["shipment_clients"] == ["ABB Brasil Ltda"]


def test_invoice_forms_carry_the_warning_box(admin_client):
    for url in ("/invoice-br/new", "/invoice-no/new", "/invoice-dubai/new"):
        body = admin_client.get(url).get_data(as_text=True)
        assert 'class="book-mismatch" role="alert" hidden data-check-url="/address-book/check"' in body
        assert "Внимание: фактурата е за друг клиент от този на пратката." in body


def test_js_dispatches_shipment_clients_and_checks_on_select():
    app_js = read_source("static", "app.js")
    assert 'new CustomEvent("ph:shipment-clients"' in app_js and "data.shipment_clients" in app_js
    book_js = read_source("static", "book.js")
    assert '"ph:shipment-clients"' in book_js and 'select.addEventListener("change", check)' in book_js
    assert "fetchJsonSafe(" in book_js and "AbortController" in book_js


# ================================================================ глобално търсене

@pytest.fixture
def corpus(db_module):
    ids = {
        "cmr": _add_doc(db_module, "cmr", {"consignee_name": "Müller & Söhne GmbH",
                                            "consignee_address": "Hauptstraße 5, Köln",
                                            "items": [{"description": "Кабел"}]}, number="0101/2026"),
        "packing": _add_doc(db_module, "packing", {"receiver_name": "Енерго-Про Мрежи АД", "items": [
            {"description": "Табло"}, {"order_no": "4700201619", "description": "HV cabinets"}]}),
        "invoice": _add_doc(db_module, "invoice_br", {"consignee_name": "ABB Brasil Ltda", "items": [
            {"po_no": "4700201619", "material_code": "1VL100001", "qty": "1"}]}),
    }
    ids["client"] = _client(db_module, "Müller & Söhne GmbH", city="Köln", vat="DE999")
    ids["entry"] = _entry(db_module, "ABB BR", billing_address="Av. São Paulo 10")
    con = db_module.get_db()
    con.execute("INSERT INTO materials (code, description, net_weight) VALUES ('1VL100001', 'Прекъсвач', '12.5')")
    con.commit()
    con.close()
    return ids


def _groups(resp):
    data = resp.get_json()
    return {g["key"]: g for g in data["groups"]}


def test_live_search_groups_with_where_found(admin_client, corpus):
    groups = _groups(admin_client.get("/search/live?q=4700201619"))
    assert set(groups) == {"documents", "invoices"}
    doc = groups["documents"]["items"][0]
    assert doc["url"] == "/doc/%d" % corpus["packing"] and doc["title"].startswith("Опаковъчен лист №")
    assert doc["where"] == [{"field": "PO / поръчка", "row": 2, "value": "4700201619"}]
    inv = groups["invoices"]["items"][0]
    assert inv["url"] == "/doc/%d" % corpus["invoice"] and inv["subtitle"].startswith("ABB Brasil Ltda")
    assert inv["where"][0]["field"] == "PO / поръчка"
    assert groups["invoices"]["more_url"] == "/invoices?q=4700201619"


def test_live_search_folds_accents_and_covers_all_books(admin_client, corpus):
    groups = _groups(admin_client.get("/search/live?q=muller+koln"))
    assert groups["documents"]["items"][0]["url"] == "/doc/%d" % corpus["cmr"]
    fields = [w["field"] for w in groups["documents"]["items"][0]["where"]]
    assert "получател" in fields and "адрес" in fields
    assert groups["clients"]["items"][0]["url"] == "/clients/%d/edit" % corpus["client"]
    assert "invoices" not in groups
    groups = _groups(admin_client.get("/search/live?q=sao+paulo"))
    assert groups["invoice_clients"]["items"][0]["url"] == "/invoices/clients/%d/edit" % corpus["entry"]
    assert groups["invoice_clients"]["items"][0]["where"][0]["field"] == "адрес за фактуриране"
    groups = _groups(admin_client.get("/search/live?q=1vl100001"))
    assert groups["materials"]["items"][0]["title"] == "1VL100001"
    assert groups["materials"]["items"][0]["url"] == "/materials?q=1VL100001"
    assert groups["invoices"]["items"][0]["where"][0]["field"] == "материал"
    groups = _groups(admin_client.get("/search/live?q=" + "прекъсвач".upper()))
    assert list(groups) == ["materials"]


def test_search_number_short_query_and_minimum(admin_client, corpus):
    assert admin_client.get("/search/live?q=0101").get_json()["groups"][0]["items"][0]["where"][0] == {
        "field": "номер", "row": None, "value": "0101/2026"}
    assert admin_client.get("/search/live?q=a").get_json()["groups"] == []
    two = _groups(admin_client.get("/search/live?q=01"))
    assert "documents" in two and two["documents"]["items"][0]["where"][0]["field"] == "номер"
    nothing = admin_client.get("/search/live?q=zzqqxx").get_json()
    assert nothing["ok"] and nothing["groups"] == [] and nothing["page_url"] == "/search?q=zzqqxx"


def test_group_limit_and_more_link(admin_client, db_module):
    for i in range(8):
        _client(db_module, "Limit Co %d" % i)
    live = _groups(admin_client.get("/search/live?q=limit+co"))["clients"]
    assert len(live["items"]) == 5 and live["more"] and live["more_url"] == "/clients?q=limit+co"
    page = admin_client.get("/search?q=limit+co").get_data(as_text=True)
    assert page.count('class="gs-title">Limit Co ') == 8 and 'href="/clients?q=limit+co"' not in page


def test_employee_sees_the_same_results(admin_client, employee_client, corpus):
    a = admin_client.get("/search/live?q=4700201619").get_json()
    e = employee_client.get("/search/live?q=4700201619").get_json()
    a.pop("ms"), e.pop("ms")
    assert a == e


def test_full_search_page(employee_client, corpus):
    body = employee_client.get("/search?q=muller").get_data(as_text=True)
    assert "<h1>Търсене в цялата програма</h1>" in body
    assert 'href="/doc/%d"' % corpus["cmr"] in body and 'href="/clients/%d/edit"' % corpus["client"] in body
    assert "Намерено в:" in body and 'class="gs-chip"' in body
    assert 'value="muller"' in body  # и в полето на страничната лента
    empty = employee_client.get("/search?q=zzqqxx").get_data(as_text=True)
    assert "Няма намерени резултати." in empty
    short = employee_client.get("/search?q=a").get_data(as_text=True)
    assert "Въведете поне 2 знака." in short


def test_carriers_group_with_plates_and_drivers(admin_client, db_module):
    """Превозвачите (група PARTIES, db._m016) — и по рег. № и шофьор."""
    con = db_module.get_db()
    cid = con.execute("INSERT INTO carriers (name, city) VALUES ('Speedy Trans', 'Варна')").lastrowid
    con.execute("INSERT INTO carrier_vehicles (carrier_id, plate) VALUES (?, 'В 1234 ВВ')", (cid,))
    con.execute("INSERT INTO carrier_drivers (carrier_id, name) VALUES (?, 'Петър Шофьоров')", (cid,))
    con.commit()
    con.close()
    item = _groups(admin_client.get("/search/live?q=speedy"))["carriers"]["items"][0]
    assert item["title"] == "Speedy Trans" and item["subtitle"] == "Варна"
    assert item["url"] == "/carriers/%d/edit" % cid
    plate = _groups(admin_client.get("/search/live?q=в+1234"))["carriers"]["items"][0]
    assert plate["where"] == [{"field": "рег. №", "row": None, "value": "В 1234 ВВ"}]
    driver = _groups(admin_client.get("/search/live?q=шофьоров"))["carriers"]["items"][0]
    assert driver["where"][0]["field"] == "шофьор"
    assert "carriers" not in _groups(admin_client.get("/search/live?q=b+1234"))  # латинско „b“ ≠ „В“


def test_carriers_group_is_skipped_without_the_table():
    import global_search
    raw = sqlite3.connect(":memory:")
    assert global_search._carrier_fields(raw) is None
    raw.execute("CREATE TABLE carriers (id INTEGER PRIMARY KEY, name TEXT, city TEXT)")
    assert [f[1] for f in global_search._carrier_fields(raw)] == ["name", "city"]
    raw.close()


def test_sidebar_has_search_field_and_assets(employee_client):
    body = employee_client.get("/").get_data(as_text=True)
    assert 'id="gs-form"' in body and 'action="/search"' in body and 'data-live-url="/search/live"' in body
    assert 'id="gs-input"' in body and 'id="gs-mobile-btn"' in body and "Ctrl K" in body
    # type="text": търсачките във формите (`form input[type=search]`) остават първи.
    assert '<input type="text" enterkeyhint="search" name="q" id="gs-input"' in body
    assert re.search(r'<script src="/static/book\.js[^"]*"></script>', body)
    assert re.search(r'<link rel="stylesheet" href="/static/book\.css[^"]*">', body)


def test_login_page_does_not_load_book_js(client):
    assert "book.js" not in client.get("/login").get_data(as_text=True)


def test_search_uses_the_trigram_index_for_documents(admin_client, db_module, corpus, monkeypatch):
    """Документите се търсят през document_fts (не през обхождане на data)."""
    import search_index
    assert search_index.is_fts_ready()
    seen = []
    import appcore
    real = appcore.document_text_search

    def spy(query, cols):
        sql, params = real(query, cols)
        seen.append(sql)
        return sql, params
    import global_search
    monkeypatch.setattr(global_search, "document_text_search", spy)
    admin_client.get("/search/live?q=4700201619")
    assert seen and all("document_fts" in s for s in seen)
